#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trundle-discuss —— 并行调用参与讨论的 agent

用法:
    invoke.py codex:/path/to/codex.md gemini:/path/to/gemini.md

每个参数是 <agent>:<提示文件>。提示文件由调用方(Claude)组装好,
内含该 agent 的固定站位、共识状态头、署名 transcript(见 references/prompt-kit.md)。

本脚本只负责:trust 前置检查、并行喷、活动驱动的超时、逐字回显、
提取正文、如实汇报失败。

输出到 stdout(供 Claude 解析,每个 agent 一段):
    ===AGENT <name> <status> <wall>s===
    <正文,或失败说明>

status: ok | timeout | untrusted | ratelimited | error

实时进度到 stderr(或 /dev/tty),每行以 "··· " 开头 —— 那是给人看的,
解析时必须全部忽略,只认 ===AGENT 分段。

为什么是 Python 而不是 bash+jq:难的从来不是解析 JSON,是进程管理
(空闲计时、多路并发读、杀干净进程组)。json/subprocess/threading 都在
标准库里,所以换 Python 反而**少**一个依赖(不再需要 jq)。

新增一个 agent:除了在 agents.yaml 登记字段,还要在下面 AGENTS 表里
加一条 Spec。详见 references/adapting-new-cli.md。
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

if sys.version_info < (3, 8):
    sys.stderr.write(
        "trundle-discuss 需要 python3 >= 3.8,当前是 %s\n"
        % ".".join(str(x) for x in sys.version_info[:3])
    )
    sys.exit(127)


# ── 超时参数 ─────────────────────────────────────────────────
#
# 超时不再是墙钟,而是「吐字间隔」:只要 agent 还在出事件(读文件、思考、
# 吐字),就一直等;连续 IDLE 秒没有任何事件才判定卡死。
#
# 为什么还要 MAXWALL:空闲超时防不住工具循环 —— agent 可以一直很"活跃"地
# 反复读同一批文件,永远不结束。MAXWALL 是那种情况唯一的出口。


def envint(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(float(raw)))
    except ValueError:
        sys.stderr.write("··· 环境变量 %s=%r 不是数字,按默认 %s 处理\n" % (name, raw, default))
        return default


# 空闲上限按 agent 的**事件粒度**定,不是一个全局常数 —— 这是实测教训:
#
#   token 级(gemini/claude):delta 持续到达,90s 没动静确实等于卡死。
#   item  级(codex)  :只有工具调用和整条消息两种事件,**生成最终回答的
#                      全过程一个事件都不发**。实测生成速率约 25 字符/秒,
#                      1400 字的回答就静默 58s;讨论场景里上下文大、回答长,
#                      破 90s 是常态。用 90s 卡它,恰好砍在它要说出正文那一刻。
#
# DISCUSSION_IDLE 一旦显式设置就覆盖所有 agent(调试用)。
IDLE_DEFAULT = 90
_IDLE_OVERRIDE = envint("DISCUSSION_IDLE", 0) or None


def has_stream(run):
    """这个 agent 的 stdout 是不是事件流。

    不是 = 整个运行期间一个字节都不吐,跑完才一次性给出全文。实测 dsh 就是:
    一个 1044 字节的回答,首字节和末字节都落在同一个 8.36s 时刻。

    这不只是"解析方式不同"。活动驱动的超时**整套**建立在有事件流的假设上——
    没有事件流时 last_activity 永远停在启动时刻、got_event 恒为 False,
    idle 和 GRACE 都退化成"按墙钟误杀",必须整体跳过而不是调大。
    """
    return (run.spec or {}).get("stream", "token") != "none"


def idle_for(run):
    # 无事件流的 agent 没有「空闲」这个概念,再大的 idle 也只是把误杀推迟。
    # 注意它排在 _IDLE_OVERRIDE **之前**:DISCUSSION_IDLE 也不该把它拽回
    # 空闲判定 —— 否则又多一个「看起来能拧、其实不通电」的开关。
    if not has_stream(run):
        return None
    if _IDLE_OVERRIDE:
        return _IDLE_OVERRIDE
    return (run.spec or {}).get("idle", IDLE_DEFAULT)
# 首个事件到达前的宽限。实测 gemini 要 42s 才出首字节,用 IDLE 卡首字节会误杀。
GRACE = envint("DISCUSSION_FIRST_BYTE_GRACE", 180)
# 540 不是 600,是刻意的:Claude Code 的 Bash 工具最大 timeout 就是 600s。
# 本脚本必须先于它开枪,否则被 Bash 工具杀掉时,调用方拿不到任何分段输出和
# 失败分类,整轮结果全丢。留 60s 余量。
# DISCUSSION_TIMEOUT 是旧变量名,保留为兼容别名而不是静默失效。
_MAXWALL_OVERRIDE = envint("DISCUSSION_MAX_WALL", envint("DISCUSSION_TIMEOUT", 0)) or None
MAXWALL = _MAXWALL_OVERRIDE or 540


def maxwall_for(run):
    # 墙钟上限也可以按 agent 单独收紧。理由和 idle 恰好相反:有事件流的 agent
    # 有空闲超时兜底,跑飞了会先被 idle 砍;**无事件流的 agent 中途没有任何信号
    # 能区分「在想」和「死了」**,墙钟是它唯一的护栏,所以要更保守。
    # 显式设了 DISCUSSION_MAX_WALL 就全局覆盖(调试用),与 _IDLE_OVERRIDE 同构。
    if _MAXWALL_OVERRIDE:
        return _MAXWALL_OVERRIDE
    return (run.spec or {}).get("max_wall") or MAXWALL
# 回显限额。/dev/tty 在 Claude Code 的 Bash 工具下不可用(实测 OSError errno 6),
# 回显只能走 stderr,而 stderr 最终会连同正文一起交给模型 —— 全量回显等于
# 把正文重复一遍。限额让用户看得到"它真的在写",又不为此付两份 token。
ECHO_CAP = envint("DISCUSSION_ECHO_CAP", 600)

TERM_GRACE = 5      # TERM 之后等多久补 KILL
POLL = 0.5          # 看门狗轮询间隔
TICK = 5            # 状态行最短间隔
TICK_FORCE = 15     # 状态行强制心跳间隔
SILENCE_HINT = 20   # 静默超过这么久就在状态行里显示计时,让超时可预见
# 回显攒够多少字符就吐一行(没有换行时)。按中文标定:60 太大,一整句
# 短回答都凑不满一次,结果全程不回显、结束时才一把吐出。
ECHO_CHUNK = 24

# 这些进度短语只表示"进程起来了/开始想了",不算实质进展。
# 收到它们**不**结束首字节宽限期 —— 实测 gemini 会在 init 事件之后、
# 首个正文之前静默 90s 以上,拿 init 当首字节会把宽限期白白作废,
# 然后被 IDLE 误杀。
SOFT_PROGRESS = ("启动中", "思考中")


# ── 回显通道 ─────────────────────────────────────────────────
# 优先 /dev/tty:用户在真实终端里手动跑本脚本时,回显只给终端,零 token 浪费。
# Claude Code 的 Bash 工具下没有 tty,降级到 stderr。

def _open_sink():
    try:
        return open("/dev/tty", "w", buffering=1), True
    except OSError:
        return sys.stderr, False


SINK, SINK_IS_TTY = _open_sink()
SINK_LOCK = threading.Lock()


def emit(text):
    with SINK_LOCK:
        try:
            SINK.write("··· " + text + "\n")
            SINK.flush()
        except Exception:
            pass


def human(n):
    if n >= 1000:
        return "%.1fK字" % (n / 1000.0)
    return "%d字" % n


def short(s, limit=48):
    s = " ".join((s or "").split())
    # codex 的 command 一律包着 /bin/bash -lc "...",剥掉才看得见真正在跑什么
    for prefix in ('/bin/bash -lc "', "/bin/bash -lc '", "/bin/bash -lc "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s[:limit] + "…" if len(s) > limit else s


# ── 各 CLI 的事件解析 ────────────────────────────────────────
#
# 每个 parse 函数签名: (run, event) -> (progress, echo_text)
#   progress  : 状态行里显示的短语,None 表示不改
#   echo_text : 要逐字回显的文本,"" 表示这条事件没有正文增量
# 正文由 parse 直接写进 run 的累加器 —— 三家的正文来源形状不同
# (codex 是完整消息、gemini 是 delta、claude 是末条 result),
# 与其抽象成统一的 body_op,不如让各自的 parse 自己管。

def parse_codex(run, ev):
    t = ev.get("type")
    if t == "thread.started":
        return "启动中", ""
    if t == "turn.started":
        return "思考中", ""
    if t == "turn.completed":
        return "已完成", ""
    if t in ("item.started", "item.completed"):
        item = ev.get("item") or {}
        it = item.get("type")
        if it == "agent_message":
            if t == "item.completed":
                # codex 一轮里可能有多条 agent_message(先说"我先看看文件",
                # 最后才是正文)。取最后一条 —— 与官方 -o/--output-last-message
                # 的语义一致。
                run.messages.append(item.get("text") or "")
            return "发言中", ""
        if it == "command_execution":
            return "执行命令 " + short(item.get("command") or ""), ""
        if it == "reasoning":
            return "思考中", ""
        if it == "web_search":
            return "联网搜索", ""
        if it == "error":
            # 无害警告也走 error item(实测那条 "Skill descriptions were
            # shortened" 就是),不能据此判失败,也不该盖掉进度。
            return None, ""
        if it == "todo_list":
            return "整理计划", ""
        return None, ""
    return None, ""


def parse_gemini(run, ev):
    t = ev.get("type")
    if t == "init":
        return "启动中", ""
    if t == "message":
        if ev.get("role") != "assistant":
            return None, ""      # role=user 是它把我们的 prompt 回显了一遍
        text = ev.get("content") or ""
        if ev.get("delta"):
            run.deltas.append(text)
        else:
            # 非 delta 的整条 assistant message。只有在一个 delta 都没收到时
            # 才会被当成正文(见 Run.body),避免与 delta 拼接出双份正文。
            run.messages.append(text)
        return "输出中", text
    if t == "tool_call":
        return "调用工具 " + short(str(ev.get("name") or "")), ""
    if t == "result":
        return "已完成", ""
    return None, ""


def parse_claude(run, ev):
    t = ev.get("type")
    if t == "stream_event":
        e = ev.get("event") or {}
        et = e.get("type")
        if et == "content_block_start":
            cb = e.get("content_block") or {}
            if cb.get("type") == "tool_use":
                return "调用工具 " + str(cb.get("name") or ""), ""
            if cb.get("type") == "thinking":
                return "思考中", ""
            return None, ""
        if et == "content_block_delta":
            d = e.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                # 只回显,不入正文 —— 正文以末条 result 为准(权威且已去重)
                return "输出中", d.get("text") or ""
            if dt == "thinking_delta":
                return "思考中", ""
            if dt == "input_json_delta":
                return None, ""
        return None, ""
    if t == "user":
        return "读取结果", ""
    if t == "result":
        run.final = ev.get("result") or ""
        return "已完成", ""
    if t == "system":
        if ev.get("subtype") == "init":
            return "启动中", ""
        return None, ""
    return None, ""


# ── trust 前置检查 ───────────────────────────────────────────
# 不是可选的礼节。gemini 在未 trust 目录下会被降级到不稳定的模型分支
# (实测 8 请求 7 失败,墙钟 10x 恶化),所以宁可不喷也不绕过。

def in_git_repo():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def gemini_is_trusted():
    cfg = os.path.expanduser("~/.gemini/trustedFolders.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        # 文件缺失/损坏/无权读 —— 一律按「不信任」处理。安全侧失败:
        # 宁可跳过 gemini,也不会误放行到那个 10x 延迟的降级分支。
        return False
    if not isinstance(data, dict):
        return False
    cur = os.path.abspath(os.getcwd())
    while True:
        if data.get(cur) == "TRUST_FOLDER":
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


UNTRUSTED_MSG = """当前目录未被 gemini 信任,已跳过本次调用。

没有绕过是有意的:绕过会把模型路由降到不稳定的 preview 分支,
实测失败率 7/8、墙钟从 14s 恶化到 108-199s。

补救(任选其一):
  1. 在该目录交互式运行一次 gemini,选择信任该目录
  2. 在 ~/.gemini/trustedFolders.json 中加入:
       "%s": "TRUST_FOLDER\""""

UNKNOWN_MSG = """未知 agent: %s

它既不在适配库(agents.yaml)里,也不在本轮的 sidecar 里,因此不会被调用。

- 想加一个 CLI:猜测调用方式是危险的(猜错只读 flag 会让它拿到写权限),
  接纳步骤见 references/adapting-new-cli.md
- 想加一个 API 参与者:在名册里写好 api 块,并确认调用时带了
  --api-config <sidecar.json>"""


# ── agent 适配表 ─────────────────────────────────────────────
#
# 所有调用都必须 stdin=DEVNULL:并行运行时 stdin 是不可读管道,
# 某些 CLI(实测 codex)会认为存在 piped 输入而去读它,然后失败
# (Failed to read prompt from stdin / os error 11)。

def build_codex(prompt):
    argv = ["codex", "exec", "--json", "--sandbox", "read-only"]
    if not in_git_repo():
        # codex 要求 cwd 在 git repo 内。这个 flag 放行,无副作用(与 gemini 不同)
        argv.append("--skip-git-repo-check")
    argv.append(prompt)
    return argv


def build_gemini(prompt):
    # --approval-mode plan:可读可搜,不可写不可执行
    return ["gemini", "--approval-mode", "plan", "-o", "stream-json", "-p", prompt]


def build_claude(prompt):
    # 作为参与者的 claude 子进程,与运行本 skill 的主持人 Claude 不是一回事。
    # --verbose 是 stream-json 在 -p 模式下的硬要求;
    # --include-partial-messages 才有 token 级 delta(否则整轮零活动信号)。
    return [
        "claude", "--allowedTools", "Read,Glob,Grep",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "-p", prompt,
    ]


def build_dsh(prompt):
    # 一次性问答入口只有 --profile headless 这一个,任务是**位置参数**
    # (help 里除 -h 外没有任何 flag),不读 stdin。
    # 只读约束不在这里 —— 它没有只读 flag,靠环境变量,见 AGENTS["dsh"]["set_env"]。
    return ["dsh", "--profile", "headless", prompt]


def build_env(spec):
    """子进程环境。unset 在前、set 在后。

    set 是**覆盖**,不是 setdefault —— 用户环境里若已有
    DSH_PERMISSION_MODE=workspace-write,"尊重它"等于把「辅助 agent 全程只读」
    这条铁律交给一个环境变量决定。这条不容协商。
    """
    env = os.environ.copy()
    for key in spec.get("unset_env") or []:
        env.pop(key, None)
    env.update(spec.get("set_env") or {})
    return env


# probe:一条**给人在终端里手动跑**的最小调用。agent 卡在启动阶段被判超时时,
# 会把它印在失败说明里。存在的理由是实测教训:额度耗尽、认证失效这类错误,
# 在非交互模式下可能一个字都不吐(实测过一次 gemini 配额耗尽,无 TTY 时 100s 内
# stdout 只有 init 和 prompt 回显、stderr 只有一条颜色警告),而同样的调用在终端
# 里会打出真正的原因。所以不要让用户自己去猜命令怎么写。
AGENTS = {
    "codex": {
        "build": build_codex,
        "parse": parse_codex,
        "precheck": None,
        "unset_env": [],
        "stream": "item",           # 工具调用 / 整条消息,无 token delta
        # item 级事件 —— 生成回答期间完全静默,见 idle_for 上方的注释
        "idle": 300,
        "probe": 'codex exec --sandbox read-only "说一句话"',
    },
    "gemini": {
        "build": build_gemini,
        "parse": parse_gemini,
        "stream": "token",
        "idle": 90,                 # token 级 delta,没动静就是真没动静
        # 未 trust 就不喷,并给补救指引 —— 绝不用环境变量绕过
        "precheck": lambda: None if gemini_is_trusted()
        else ("untrusted", UNTRUSTED_MSG % os.getcwd()),
        "unset_env": [],
        "probe": 'gemini -p "说一句话"',
    },
    "claude": {
        "build": build_claude,
        "parse": parse_claude,
        "precheck": None,
        # 必须清掉 CLAUDECODE,否则子进程认为自己在嵌套 session 里而报错
        "unset_env": ["CLAUDECODE"],
        "stream": "token",
        "idle": 90,                 # token 级 delta(思考阶段也有 thinking_delta)
        "probe": 'claude -p "说一句话"',
    },
    "dsh": {
        "build": build_dsh,
        # 没有 parse —— 它没有事件流可解析,走 drain_stdout_text。
        # 显式写 None 而不是省略:selftest 会断言 stream=="none" 的 spec 必须
        # 没有 parse,防止后来的人以为能从它的 stdout 里读出进度。
        "parse": None,
        "precheck": None,
        "unset_env": [],
        # ★ 只读靠环境变量,不是 flag ★
        # headless profile 的 sandbox 默认值是
        #   mode: process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
        # —— **默认可写**,不显式改就等于给它写权限。
        # 实测这个约束比 gemini 的 --approval-mode plan 更硬:read-only 下让它
        # 建文件被沙箱拒绝(目录里零文件生成);走 bash 逃逸 `echo hello > f.txt`
        # 得到 "bash: Read-only file system";它尝试自行升级权限时得到
        # 'sandbox escalation to "workspace-write" requires approval, but no
        # approval channel is available' —— headless 下没有审批通道,绕不过去。
        "set_env": {"DSH_PERMISSION_MODE": "read-only"},
        # ★ 没有事件流 ★ 实测:1044 字节的回答,首字节和末字节都在同一个 8.36s
        # 时刻到达;讨论级 prompt(读两个文件 + 写 600 字判断)静默 34.6s 后一次性
        # 吐 2004 字节。全程 stdout 零输出 —— 没有事件、没有 JSON、没有进度信号。
        # 源码印证(@deepseek-ai/dsh-headless):await agent.whenIdle() 跑到静止,
        # 然后 io.stdout.write(outcome.text + "\n") 一把写出。
        "stream": "none",
        "idle": None,               # 空闲超时对它无意义,见 idle_for
        # 无流 = 卡死无征兆,墙钟是唯一护栏。曾收紧到 300,但实际使用中
        # dsh 的重 prompt 会超过它,所以放宽到全局同款 540 —— 这已是上限:
        # 不能给 600,Bash 工具自己就在 600s 开枪,必须留余量让本脚本先收尾,
        # 否则整轮分段输出和失败分类全丢(见 MAXWALL 上方的注释)。
        "max_wall": 540,
        # 带上环境变量前缀是刻意的:这条同时是诊断命令和「只读怎么开」的
        # 唯一一份可复制文档。
        "probe": 'DSH_PERMISSION_MODE=read-only dsh --profile headless "说一句话"',
    },
}


# ═════════════════════════════════════════════════════════════
# API 参与者
# ═════════════════════════════════════════════════════════════
#
# 规范见 plan §4.1(42 条 EARS)。这里只标每处对应的 R-ID,不复述理由。
#
# ★ API 参与者与 CLI 参与者是两类,不是同一件事的两种接法 ★
#   CLI :有工具(能读代码)、有子进程、有 rc 和 stderr、只读靠 flag 或环境变量
#   API :无工具(只看得到 prompt 里的字)、无进程、只有 HTTP 状态码、只读天然成立
#
# ★ 两张注册表,刻意不合并 ★  AGENTS 是随 skill 发布的 CLI 适配库;API 实例是
# 用户私有配置,每轮从 sidecar 加载。合并会让 selftest 的三条漂移守卫
# (t_yaml_agent_names_match_code / t_readme_lists_every_agent /
#  t_list_agents_prints_every_registered_name)在别人机器上变红 —— 它们断言的是
# set(AGENTS) 相等。见 R-004。

API_AGENTS = {}                  # 运行时从 sidecar 加载,进程内有效

# R-004 / R-039:两个名单是**不同**的集合,混为一谈会让全 API 阵容彻底不工作
#   适配库登记名单 = AGENTS          (--list-agents 的输出,discover/verify 从它派生)
#   本轮可调用名单 = AGENTS ∪ API_AGENTS (判定名册里的名字是否合法,用这个)


def resolve_spec(name):
    """本轮可调用名单里的查找。API 实例优先 —— 但撞名在加载时已被 R-007 拦住,
    所以这个顺序只是保险,不会真的发生覆盖。"""
    return API_AGENTS.get(name) or AGENTS.get(name)


def callable_names():
    """R-039:本轮可调用名单。moderate.py 判定名册合法性用这个,不要用 AGENTS。"""
    return list(AGENTS) + [n for n in API_AGENTS if n not in AGENTS]


# ── wire format:openai_compat ────────────────────────────────
#
# v1 只做这一种。它覆盖 DeepSeek / Kimi / GLM / Qwen / MiniMax / ollama /
# vLLM / OpenRouter。Anthropic Messages 与 Gemini 原生格式留到 v2。

API_FIELDS = ("base_url", "model", "api_key_env", "headers", "max_tokens")
API_NAME_RE = re.compile(r"^[A-Za-z][\w-]*$")
# R-042:与 moderate.py 的名册解析正则同款。名字含空格会让 host 侧的
# AGENT_RE(^===AGENT (\S+) (\S+) ([\d.]+)s===)整段丢弃输出,而且不报错。

# R-015 凭证形状:封闭枚举。新增取值须同步 plan §3 的「凭证形状」条目。
CRED_HEADER_NAMES = ("authorization", "x-api-key", "api-key")
CRED_VALUE_RE = re.compile(r"^\s*Bearer\s+\S", re.I)

# R-023 静默截断的两条判据。
#   比值   —— 抓「窗口不是 2 的幂,但丢弃极多」
#   2 的幂 —— 抓「恰好撞上窗口」
# ★ 两条缺一不可 ★ 敌手扫描实测:只用比值时漏报 3/4(14096/4096、11011/4096、
# 20011/4096 全部被放行),因为自然 token/字符比取决于内容语言,而 R-033 恰好
# **强制**给无工具参与者内联英文代码 —— 两条规则会互相打架。九个实测样本见
# plan §2.1 的 G-22..G-30。
TRUNC_RATIO_DIVISOR = 8
TRUNC_POW2_FLOOR = 2048


def parse_sse_line(line):
    """一行 SSE -> ("done", None) | ("event", dict) | None(跳过)。

    要处理:`data: {...}` / `data: [DONE]` / 空行心跳 / `: comment` 行。
    """
    line = (line or "").strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return ("done", None)
    try:
        ev = json.loads(payload)
    except ValueError:
        return None
    return ("event", ev) if isinstance(ev, dict) else None


def sse_delta(ev):
    """一条事件 -> (正文增量, 思考增量, finish_reason, usage)。

    R-018:content 缺失或为 null 一律按空串 —— 实测 gemini-flash-latest 的末条
    delta **根本没有 content 这个键**(不是 null,是缺失),所以只能 .get()。
    R-019:content 之外的字符串字段算「思考内容」。实测 ollama 叫 reasoning、
    Gemini 叫 extra_content;不枚举字段名,免得下一家换个叫法就漏。
    """
    usage = ev.get("usage")
    usage = usage if isinstance(usage, dict) else None
    choices = ev.get("choices") or []
    if not choices:
        return "", "", None, usage
    ch = choices[0] if isinstance(choices[0], dict) else {}
    d = ch.get("delta") or {}
    content = d.get("content") or ""
    think = "".join(v for k, v in d.items()
                    if k not in ("role", "content") and isinstance(v, str))
    if not isinstance(d.get("content"), str) and d.get("content") is not None:
        content = ""
    return content, think, ch.get("finish_reason"), usage


def is_credential_shaped(name, value):
    """R-015 的判定对象。**启发式,不追求无漏** —— 它触发的是警告不是阻断。"""
    if (name or "").strip().lower() in CRED_HEADER_NAMES:
        return True
    return bool(CRED_VALUE_RE.match(value if isinstance(value, str) else ""))


def detect_truncation(prompt_chars, prompt_tokens):
    """R-023。返回 None(没问题)或一句人可读的原因。

    R-024:拿不到 prompt_tokens 就不判 —— 宁可漏,不可对着 None 瞎猜。
    """
    if prompt_tokens is None or prompt_chars <= 0:
        return None
    if prompt_tokens < prompt_chars // TRUNC_RATIO_DIVISOR:
        return ("prompt_tokens=%d 不足所发字符数 %d 的 1/%d"
                % (prompt_tokens, prompt_chars, TRUNC_RATIO_DIVISOR))
    if prompt_tokens >= TRUNC_POW2_FLOOR and not (prompt_tokens & (prompt_tokens - 1)):
        return ("prompt_tokens=%d 恰为 2 的整数次幂,是撞上上下文窗口的典型指纹"
                % prompt_tokens)
    return None


def build_openai_request(spec, prompt):
    """R-002/R-010/R-014/R-016/R-017 -> (url, headers, body_bytes)。

    ★ 请求体里永远没有 tools / functions / tool_choice ★ 那是黄卡反面的
    可观测形式:API 参与者无工具,不是「暂时没给」,是**不给**。
    ★ 凭证不在这里注入 ★ 见 api_auth_header(),那样这个函数保持纯函数可测。
    """
    url = (spec.get("base_url") or "").rstrip("/") + "/chat/completions"
    body = {
        "model": spec.get("model"),
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        # R-017:一律携带。usage.prompt_tokens 是静默截断唯一的带内信号,
        # 而静默截断在实测里既不报错也不缺字节,没有它就完全看不见。
        "stream_options": {"include_usage": True},
    }
    mt = spec.get("max_tokens")
    if mt is not None:                       # R-016:填 0 算填写,不算未填
        body["max_tokens"] = mt
    headers = {"Content-Type": "application/json"}
    for k, v in (spec.get("headers") or {}).items():
        headers[str(k)] = str(v)
    return url, headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


# ── sidecar:加载与校验 ──────────────────────────────────────
#
# ★ 校验失败一律拒绝**整个** sidecar,不做部分加载 ★
# 与 R-008/R-009 的取向一致(加载期严格),更重要的是避免「一半参与者在、一半
# 不在」这种中间态 —— 那是这个仓库公认最难查的失败形状(见 verify.sh:68-72
# 关于「绿灯自检 + 凭空缺席」的那段)。

BAD_NAME_MSG = """sidecar 里的名字不合法: %s

名字必须匹配 [A-Za-z][\\w-]* —— 与名册解析用的是同一套约束。
含空格的名字会让调用方的分段正则(===AGENT <名> <状态> <墙钟>s===)整段丢弃
本轮输出,而且不报错。"""

COLLIDE_MSG = """sidecar 里的名字与适配库已登记的 CLI 重名: %s

没有静默覆盖是有意的:覆盖之后你以为在用有工具的 %s CLI,实际跑的是无工具的
API,而 discover.sh 仍会把它列在「已登记且本机可用」里 —— 自检与实际行为分岔。

改个名字即可,例如 %s-api。"""

MISSING_FIELD_MSG = """sidecar 条目 %s 缺少必填字段: %s

API 实例配置的字段集合:base_url(必填)/ model(必填)/ api_key_env(可选)/
headers(可选)/ max_tokens(可选)。"""

BAD_URL_MSG = """sidecar 条目 %s 的 base_url 不是合法的 http/https URL: %r

注意 base_url 本身**不做** scheme 或主机白名单校验 —— 明文 http、内网主机名、
localhost 都照发。这里拒绝的只是「压根不是一个 URL」。"""

NO_KEY_MSG = """环境变量 %s 未设置或为空,已跳过本次调用。

API 实例配置里的 api_key_env 只存**变量名**,不存值 —— 凭证永远不进 sidecar、
不进命令行、不进日志。请在 shell 里 export 它,或换用不需要凭证的本地端点。"""


def _valid_base_url(u):
    return bool(isinstance(u, str) and re.match(r"^https?://[^/\s]+", u.strip()))


def api_precheck(spec):
    """R-011 / R-013 / R-012。

    ★ 永不发网络请求 ★ 前置检查有两个容易被忽略的调用点:verify.sh 会对每个
    参与者跑一遍,pick_moderator 选主持人时也会遍历回退链。在这里发请求 =
    每次自检、每轮讨论都对所有配置的端点打一发,白吃各家的限流配额。
    未填 api_key_env(本地模型)直接通过 —— 它没有凭证可查。
    """
    env = spec.get("api_key_env")
    if not env:
        return None
    if not (os.environ.get(env) or "").strip():
        return ("error", NO_KEY_MSG % env)
    return None


def api_key_value(spec):
    """调用那一刻才读环境变量。返回 "" 表示无凭证(本地模型的正常形态)。"""
    env = spec.get("api_key_env")
    return (os.environ.get(env) or "").strip() if env else ""


def build_api_spec(name, cfg, warns):
    """一条 API 实例配置 -> spec。未知字段按 R-034 记进 warns 并忽略。"""
    unknown = sorted(k for k in cfg if k not in API_FIELDS)
    if unknown:
        # R-034:警告一次、列出全部未知键、忽略它们、照常加载。
        # 静默忽略是不行的 —— 你填了 temperature 期待它更保守,而它根本没发出去,
        # 这与 R-023 防的「静默截断」是同一种病:看起来生效了的没生效。
        warns.append("%s: 忽略未在字段集合内的键 %s(不会被发送)"
                     % (name, ", ".join(unknown)))
    spec = {
        "kind": "api",
        "base_url": (cfg.get("base_url") or "").strip(),
        "model": cfg.get("model"),
        "api_key_env": cfg.get("api_key_env") or None,
        "headers": cfg.get("headers") or {},
        "max_tokens": cfg.get("max_tokens"),
        # R-036:API 参与者的事件流粒度恒为 token(SSE 是真 delta)。
        # 注意推理模型实测**接近 none** —— 81 条事件里只有 3 条带正文。
        # 不新增第四档,改由「思考内容」产出进度短语来补活动信号(R-019)。
        "stream": "token",
        "idle": IDLE_DEFAULT,
        "parse": None,               # 不走 JSONL 事件解析那条路
        "build": None,               # 没有 argv
        "unset_env": [],
    }
    # precheck 捕获 spec 自身:AGENTS 里的 precheck 签名是零参可调用,这里对齐
    spec["precheck"] = lambda: api_precheck(spec)
    return spec


def load_api_config(path):
    """-> (ok: bool, 错误说明: str|None, 警告: list[str])"""
    warns = []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return False, "读不了 sidecar (%s): %s" % (path, exc), warns
    if not isinstance(data, dict):
        return False, "sidecar 顶层必须是一个对象(名字 -> API 实例配置)", warns

    # 先全量校验再落地 —— 见本节开头「不做部分加载」
    for name, cfg in data.items():
        if not API_NAME_RE.match(name or ""):
            return False, BAD_NAME_MSG % repr(name), warns
        if name in AGENTS:
            return False, COLLIDE_MSG % (name, name, name), warns
        if not isinstance(cfg, dict):
            return False, "sidecar 条目 %s 必须是一个对象" % name, warns
        missing = [f for f in ("base_url", "model")
                   if not str(cfg.get(f) or "").strip()]
        if missing:
            return False, MISSING_FIELD_MSG % (name, ", ".join(missing)), warns
        if not _valid_base_url(cfg.get("base_url")):
            return False, BAD_URL_MSG % (name, cfg.get("base_url")), warns

    for name, cfg in data.items():
        API_AGENTS[name] = build_api_spec(name, cfg, warns)
    return True, None, warns


def scrub(text, spec):
    """R-001 的最后一道闸。

    凭证有四条泄漏面:argv(ps 可见)、sidecar 落盘、进度输出、失败文案。
    前两条靠「只存环境变量名」解决;后两条靠这个函数 —— 而**失败文案那条最隐蔽**,
    因为 stderr 与正文最终会一起交给模型。
    """
    if not text:
        return text
    val = api_key_value(spec or {})
    if val and len(val) >= 8:
        text = text.replace(val, "***")
    return text


# ── 一次调用 ─────────────────────────────────────────────────

# 短语类标记 —— 足够长,不会误命中正文。
# "exceeded your current quota" 是实测 gemini 配额耗尽时的原话,它不含
# 连续的 "quota exceeded",单靠后者接不住。
RATE_MARKERS = (
    "resource_exhausted", "quota exceeded", "quota_exceeded",
    "exceeded your current quota",
    "rate limit", "rate_limit_exceeded", "too many requests",
)
# 纯数字状态码必须带词边界:裸子串 "429" 会把正文里的 "4290ms"、"重试 4295 次"
# 判成限流(本项目实测踩过)。\b 让 "4290" 不命中,而 '"status":429' 仍然命中。
RATE_CODE_RE = re.compile(r"\b429\b")

# codex 每次启动都会喷这条,与失败无关。报错时展示它只会误导排查方向。
STDERR_NOISE = ("failed to load models cache",)


class Run(object):
    def __init__(self, name, promptfile):
        self.name = name
        self.promptfile = promptfile
        # R-039:查的是**本轮可调用名单**(AGENTS ∪ API_AGENTS),不是适配库登记名单
        self.spec = resolve_spec(name)
        self.kind = (self.spec or {}).get("kind", "cli")
        self.status = None          # 提前判定的状态(untrusted / error)
        self.note = None            # 提前判定时要展示的正文
        self.progress = "等待中"
        self.deltas = []            # gemini: token 级增量
        self.messages = []          # codex: 完整 agent_message,取最后一条
        self.final = None           # claude: 末条 result,权威正文
        self.text = []              # dsh: 无事件流,整个 stdout 就是正文
        self.stderr_lines = []
        self.raw_sample = []        # 事件流采样,只用于限流关键词检测
        self.proc = None
        self.rc = None
        self.killed = None          # "idle" | "maxwall"
        self.progress_at_kill = None
        self.started = None
        self.wall = 0.0
        self.last_activity = None
        self.echoed = 0
        self.echo_buf = ""
        self.echo_capped = False
        self.got_event = False      # 首个**实质**事件是否到达,决定用 GRACE 还是 IDLE
        self.done = threading.Event()
        # ── 以下只有 kind=="api" 的 run 会用到 ──
        self.cancel = threading.Event()   # 代替 SIGTERM:没有进程可杀,只能让 worker 自己收
        self.resp = None                  # 打开着的 HTTP 响应,kill 时从别的线程 close 它
        self.http_status = None
        self.error_body = ""              # 非 200 时服务端给的原文(403 说"需要订阅"、404 说"换哪个模型")
        self.conn_error = None            # 建连失败(DNS/拒绝连接/SSL)
        self.first_byte_timeout = False   # 连上了但首字节没在宽限内到达
        self.stream_error = None          # 流读到一半炸了
        self.saw_done = False             # 收到 data: [DONE] 没有
        self.finish_reason = None
        self.usage = None
        self.event_count = 0
        self.think_chars = 0              # 思考内容累计字数,只用于超时文案(R-037)
        self.prompt_chars = 0             # 静默截断检测的分母(R-023)
        self.raw_body = []                # 非事件行,用于认出「HTTP 200 但不是 SSE」(R-038)

    def body(self):
        # 四种来源按 agent 互斥(text 只有无事件流的 agent 会填),这个顺序是道
        # 保险而不是真的会撞车。text 放最前:对它来说 stdout 就是正文本身,
        # 没有"更权威的来源"这一说。
        if self.text:
            return "".join(self.text)
        if self.final is not None:
            return self.final
        if self.messages:
            return self.messages[-1]
        return "".join(self.deltas)

    def size(self):
        if self.text:
            return sum(len(t) for t in self.text)
        if self.final is not None:
            return len(self.final)
        return sum(len(m) for m in self.messages) + sum(len(d) for d in self.deltas)

    def touch(self):
        self.last_activity = time.monotonic()

    # 回显:攒够一行或一段就吐,超过限额后转为状态行显示体量
    def echo(self, text):
        if not text or self.echo_capped:
            return
        self.echo_buf += text
        while True:
            nl = self.echo_buf.find("\n")
            if nl >= 0:
                line, self.echo_buf = self.echo_buf[:nl], self.echo_buf[nl + 1:]
            elif len(self.echo_buf) >= ECHO_CHUNK:
                line, self.echo_buf = self.echo_buf[:ECHO_CHUNK], self.echo_buf[ECHO_CHUNK:]
            else:
                return
            line = line.strip()
            if not line:
                continue
            emit("%s │ %s" % (self.name, line))
            self.echoed += len(line)
            if self.echoed >= ECHO_CAP:
                self.echo_capped = True
                self.echo_buf = ""
                emit("%s │ …(回显已达 %d 字上限,继续输出中,正文完整保留)" % (self.name, ECHO_CAP))
                return

    def kill(self, why):
        self.killed = why
        # 开枪那一刻它在干什么 —— 排查误杀的第一手证据。
        # 不能等到 classify 再读 progress,那时已被 waiter 覆盖成「已中止」。
        self.progress_at_kill = self.progress
        if self.kind == "api":
            # 没有进程可杀。置旗标让 worker 的读循环自己退出,并从这个线程把
            # 响应关掉 —— 阻塞在 read 上的 worker 会因此抛异常醒过来。
            self.cancel.set()
            try:
                if self.resp is not None:
                    self.resp.close()
            except Exception:
                pass
            return
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        # 杀进程组而不是单个 pid:codex/gemini 都会 spawn 子进程,
        # 只 TERM 父进程会留下一地孙进程。
        for sig, wait in ((signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, 0)):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except OSError:
                return
            if not wait:
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.2)


def drain_stderr(run):
    try:
        for line in run.proc.stderr:
            line = line.rstrip("\n")
            if line:
                run.stderr_lines.append(line)
                if len(run.stderr_lines) > 200:
                    del run.stderr_lines[:100]
    except Exception:
        pass


def drain_stdout(run):
    parse = run.spec["parse"]
    try:
        for line in run.proc.stdout:
            run.touch()
            line = line.strip()
            if not line:
                continue
            if len(run.raw_sample) < 400:
                run.raw_sample.append(line[:400])
            try:
                ev = json.loads(line)
            except ValueError:
                # 非 JSON 行(启动横幅、警告之类)。不当正文,但算活动。
                continue
            if not isinstance(ev, dict):
                continue
            try:
                progress, echo_text = parse(run, ev)
            except Exception as exc:
                progress, echo_text = ("解析事件出错: %s" % exc), ""
            if progress:
                run.progress = progress
                if progress not in SOFT_PROGRESS:
                    run.got_event = True
            if echo_text:
                run.got_event = True
                run.echo(echo_text)
    except Exception:
        pass


def drain_stdout_text(run):
    """纯文本 agent 的 stdout:整块就是正文,不解析、不回显、不置 got_event。

    为什么单独一个函数而不是在 drain_stdout 里加分支:JSONL 那条路径要 strip
    空行、要逐行 json.loads;而纯文本里**空行是段落分隔**,strip 掉正文就变形了。
    两条路径对"一行"的含义根本不同,混在一起迟早改坏其中一条。

    不往 raw_sample 里塞:那个采样只喂给限流关键词检测,而这里的每一行都是
    agent 的发言 —— 讨论里聊到 "429" 是很自然的事。dsh 的错误一律走 stderr
    (`dsh: <code>: <message>`,exit 1),stderr 那一路已经够用。
    """
    try:
        for line in run.proc.stdout:
            # 对超时判定没有影响(空闲判定已整体跳过),留着是为了不撒谎
            run.touch()
            run.text.append(line)
    except Exception:
        pass


def launch(run):
    """按参与者种类分发。两条路径除了 Run 与看门狗,不共用任何东西。"""
    if run.kind == "api":
        return launch_api(run)
    return launch_cli(run)


def launch_cli(run):
    prompt = open(run.promptfile, encoding="utf-8", errors="replace").read()
    argv = run.spec["build"](prompt)
    env = build_env(run.spec)
    run.started = time.monotonic()
    run.touch()
    # 无事件流的 agent 停在「启动中」会撒谎:那个短语在超时诊断里的确切含义是
    # 「压根没开始」(见 NEVER_STARTED_MSG),而它其实正常在跑,只是一个字都不吐。
    run.progress = "启动中" if has_stream(run) else "运行中(无进度事件)"
    try:
        run.proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            # 自成进程组,超时时能连孙进程一起收掉
            start_new_session=True,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        run.status = "error"
        run.note = "无法启动 %s: %s\n\n它是否已安装并在 PATH 里?" % (argv[0], exc)
        run.wall = 0.0
        run.done.set()
        return

    reader = drain_stdout if has_stream(run) else drain_stdout_text
    threads = [
        threading.Thread(target=reader, args=(run,), daemon=True),
        threading.Thread(target=drain_stderr, args=(run,), daemon=True),
    ]
    for t in threads:
        t.start()

    def waiter():
        try:
            run.rc = run.proc.wait()
        except Exception:
            run.rc = -1
        for t in threads:
            t.join(timeout=5)
        run.wall = time.monotonic() - run.started
        # 被杀的不能报「完成」—— 那一行紧接着就是 timeout,自相矛盾且误导排查
        run.progress = ("已中止 %.1fs" if run.killed else "完成 %.1fs") % run.wall
        run.done.set()

    threading.Thread(target=waiter, daemon=True).start()


# ── API 调用:执行路径 ───────────────────────────────────────
#
# ★ 这里没有子进程 ★ 没有 pid、没有进程组、没有 rc、没有 stderr。
# 与 CLI 那条路径共用的只有:Run 这个状态机、watch() 的看门狗、以及
# ===AGENT 分段输出。其余全部另起一套。

# ★ 不要在这里引入第二个超时旋钮 ★
# urlopen(timeout=) 是**整个 socket** 的超时,不是"建连超时" —— 一个重 prompt
# 让服务端想 40 秒才吐首字节是很正常的事(实测 ollama 处理 14K 字符的 prompt
# 就要几十秒),给个小值会把正常调用砍掉,而且报出来是"连不上",完全误诊。
#
# 所以它直接取首字节宽限 GRACE:语义正好对上(「首个事件到达前能等多久」),
# 用户能拧的还是 DISCUSSION_FIRST_BYTE_GRACE 那一个开关,不多不少。
# 这也是 urlopen 唯一无法被看门狗打断的窗口 —— resp 还没拿到,kill() 没东西可关。


def api_worker(run):
    """在自己的线程里跑完一次 API 调用。异常一律落到 run 的字段上,不抛出。"""
    spec = run.spec
    try:
        prompt = open(run.promptfile, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        run.conn_error = "读不了提示文件: %s" % exc
        return
    run.prompt_chars = len(prompt)

    url, headers, body = build_openai_request(spec, prompt)

    # R-015:凭证形状只警告、不阻断。★ 只印键名,绝不印值 ★(R-001)
    for k, v in (spec.get("headers") or {}).items():
        if is_credential_shaped(k, v):
            emit("%s │ 提醒:headers 里的 %s 看起来是凭证。建议改用 api_key_env,"
                 "否则它会明文留在你的名册文件里(名册常常在 dotfiles 仓库中)"
                 % (run.name, k))
            break                      # 一次就够,不逐条刷屏

    key = api_key_value(spec)
    if key:
        headers.setdefault("Authorization", "Bearer " + key)

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    run.progress = "连接中"
    try:
        resp = urllib.request.urlopen(req, timeout=GRACE)
    except urllib.error.HTTPError as exc:
        run.http_status = exc.code
        try:
            run.error_body = exc.read().decode("utf-8", "replace")[:2000]
        except Exception:
            run.error_body = ""
        run.raw_sample.append(run.error_body)      # 供限流关键词检测
        return
    except (socket.timeout, TimeoutError) as exc:
        # 连上了但迟迟不吐首字节。这**不是**"连不上" —— 报成连接失败会把用户
        # 引去查网络,而真正该拧的是 DISCUSSION_FIRST_BYTE_GRACE。
        run.first_byte_timeout = True
        return
    except Exception as exc:                        # URLError / DNS / SSL …
        run.conn_error = "%s: %s" % (type(exc).__name__, exc)
        return

    run.resp = resp
    run.http_status = getattr(resp, "status", None) or resp.getcode()
    run.progress = "等待首字节"
    try:
        for raw in resp:
            if run.cancel.is_set():
                break
            run.touch()
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            # 非事件行照样进 raw_body —— R-038 靠它认出「HTTP 200 但压根不是 SSE」
            if len(run.raw_body) < 8000:
                run.raw_body.append(line)
            parsed = parse_sse_line(line)
            if parsed is None:
                continue
            kind, ev = parsed
            if kind == "done":
                run.saw_done = True
                run.progress = "已完成"
                continue
            run.event_count += 1
            content, think, finish, usage = sse_delta(ev)
            if usage:
                run.usage = usage
            if finish:
                run.finish_reason = finish
            if content:
                # R-018:正文只由 content 拼成
                run.deltas.append(content)
                run.progress = "输出中"
                run.got_event = True
                run.echo(content)
            elif think:
                # R-019:思考内容 -> 进度短语,不回显、不进正文、不置实质事件。
                # 「思考中」在 SOFT_PROGRESS 里,所以下面这行**不会**让 got_event 变真
                # —— 与 parse_claude 处理 thinking_delta 的既有惯例一致。
                run.think_chars += len(think)
                run.progress = "思考中"
    except Exception as exc:
        # 流被对端掐断/被我们 close 掉,都落这里。是不是「完整」由 saw_done 与
        # finish_reason 判(R-021),不看这个异常。
        run.stream_error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            resp.close()
        except Exception:
            pass


def launch_api(run):
    """起一个 worker 线程 + 一个 waiter,形状与 launch_cli 对齐。"""
    run.started = time.monotonic()
    run.touch()
    run.progress = "启动中"
    worker = threading.Thread(target=api_worker, args=(run,), daemon=True)
    worker.start()

    def waiter():
        worker.join()
        run.wall = time.monotonic() - run.started
        run.progress = ("已中止 %.1fs" if run.killed else "完成 %.1fs") % run.wall
        run.done.set()

    threading.Thread(target=waiter, daemon=True).start()


# ── 失败分类 ─────────────────────────────────────────────────

def is_rate_limited(run):
    blob = ("\n".join(run.stderr_lines) + "\n" + "\n".join(run.raw_sample)).lower()
    if any(m in blob for m in RATE_MARKERS):
        return True
    return bool(RATE_CODE_RE.search(blob))


def stderr_tail(run, n=5):
    lines = [l for l in run.stderr_lines
             if not any(noise in l.lower() for noise in STDERR_NOISE)]
    return "\n".join(lines[-n:]) if lines else "(stderr 为空)"


def probe_cmd(run):
    return (run.spec or {}).get("probe") or "%s --help" % run.name


# 空闲超时有两种,区别不在措辞,在**该拧哪个旋钮**:
#   从未开始 → 用的是 GRACE,拧 DISCUSSION_IDLE 完全不起作用
#   中途卡住 → 用的是 idle_for(run),这时 DISCUSSION_IDLE 才是对的那个
# 这两种情况一度共用一段文案、一律建议"调大 DISCUSSION_IDLE",于是撞上前一种的人
# 会对着一个不通电的开关反复试 —— 本项目自己踩过,两次,一共等了 400 秒。

NEVER_STARTED_MSG = """\
%(wall).0fs 内一个实质事件都没产出,判定卡死并中止。本轮缺少 %(name)s 的发言。
最后的状态是「%(progress)s」—— 它不是写到一半被砍,是压根没开始。

首字节宽限 %(limit)ds,对应的环境变量是 DISCUSSION_FIRST_BYTE_GRACE。
**调大 DISCUSSION_IDLE 对这种情况无效** —— 那个参数管的是"开始输出之后的静默"。

卡在启动阶段,常见原因是额度耗尽、认证失效、网络不通。这些错误在非交互模式下
可能一个字都不吐,想看真实原因就在终端里手动跑一次:

    %(probe)s"""

STALLED_MSG = """\
连续 %(limit)ds 没有任何事件,判定卡死并中止(已等 %(wall).0fs)。本轮缺少 %(name)s 的发言。
最后的状态是「%(progress)s」。

注意:静默不一定等于卡死。事件粒度粗的 agent(codex 只有 item 级事件)在生成回答的
整个过程中不发任何事件,回答越长静默越久。
如果最后状态是「发言中」或刚做完一次工具调用,它很可能是被砍在正要说话那一刻——
调大 DISCUSSION_IDLE 再试。"""

MAXWALL_MSG = """\
达到 %(limit)ds 绝对上限并中止,可能陷入了工具循环。本轮缺少 %(name)s 的发言。
调大用 DISCUSSION_MAX_WALL(注意 Bash 工具上限 600s)。"""

# 第三种超时:这个 agent 压根没有事件流。
# 它不会被空闲超时砍(watch 跳过了),只可能撞墙钟 —— 所以这段文案里唯一能拧的
# 旋钮必须是 DISCUSSION_MAX_WALL,并且要明说另外两个不通电。
# 前两种文案的错误方式是「指错旋钮」;这一种的错误方式是「让人以为它卡死了」——
# 它可能正在正常干活,只是这个 CLI 从头到尾一个字都不吐。
NO_STREAM_MSG = """\
达到 %(limit)ds 绝对上限并中止(已等 %(wall).0fs)。本轮缺少 %(name)s 的发言。

%(name)s 没有事件流:整个运行期间 stdout 一个字节都不吐,跑完才一次性给出全文
(实测一个讨论级 prompt 静默 34.6s 后一把吐 2004 字节)。所以「一直没输出」既不
代表它卡住了,也不代表它在干活 —— 中途没有任何信号可以区分这两者。

因此它不走空闲判定:**DISCUSSION_IDLE 和 DISCUSSION_FIRST_BYTE_GRACE 对它完全
无效**,唯一起作用的是绝对上限 DISCUSSION_MAX_WALL(注意 Bash 工具上限 600s,
本脚本必须先于它开枪)。

如果反复撞上限,更可能是任务太重(让它读了太多文件),而不是上限太短 ——
把喂给它的 prompt 收窄一般比调大上限有效。想确认它本身是否健康,在终端里
手动跑一次:

    %(probe)s"""


# ── API 调用的失败分类 ──────────────────────────────────────
#
# CLI 那套(rc + stderr + 三段超时文案)对 API 一条都不适用:没有 exit code、
# 没有 stderr、没有 probe 命令可给。R-030 明确要求 API 文案用**手上真有的**
# 东西:HTTP 状态码 / finish_reason / 已收事件数 / 已收字数。

# R-037:三档超时文案。与 CLI 侧的 NEVER_STARTED / STALLED / MAXWALL 同构,
# 新增的是中间那档「只思考过」。
# ★ 为什么要中间这档 ★ 推理模型的 content 在整个思考阶段为空(实测 ollama
# qwen3:0.6b:81 条事件里只有 3 条带正文),而思考事件按 R-019 不算实质事件。
# 于是一个思考了 100 秒才卡住的模型,got_event 仍为 False,会被旧文案判成
# 「压根没开始」并指向 DISCUSSION_FIRST_BYTE_GRACE —— 一个对它不通电的开关。
# 这个项目为同一种病(dsh)已经付过 400 秒的学费,不能再犯。
API_TIMEOUT_NEVER = """\
%(wall).0fs 内一个事件都没收到,判定卡死并中止。本轮缺少 %(name)s 的发言。
最后的状态是「%(progress)s」—— 它不是写到一半被砍,是压根没开始。

首字节宽限 %(limit)ds,对应环境变量 DISCUSSION_FIRST_BYTE_GRACE。
**调大 DISCUSSION_IDLE 对这种情况无效。**

常见原因:端点不可达、凭证无效、模型名写错。这些在流式接口下可能一个字都不吐。"""

API_TIMEOUT_THINKING = """\
连续 %(limit)ds 没有新事件,判定卡死并中止(已等 %(wall).0fs)。本轮缺少 %(name)s 的发言。

**它不是没开始 —— 它收到过 %(events)d 条事件、产出了 %(think)d 字思考内容,但一个字正文都没有。**
推理模型在思考阶段 content 恒为空,这段时间不计入「实质事件」。

能拧的是 DISCUSSION_IDLE(现 %(limit)ds)与 DISCUSSION_MAX_WALL。
**DISCUSSION_FIRST_BYTE_GRACE 对这种情况无效** —— 首字节早就到了,只是它一直在想。
若反复撞上,把喂给它的 prompt 收窄通常比调大超时有效。"""

API_TIMEOUT_STALLED = """\
连续 %(limit)ds 没有新事件,判定卡死并中止(已等 %(wall).0fs)。本轮缺少 %(name)s 的发言。
已收到 %(events)d 条事件、%(chars)d 字正文,被砍在写到一半。

调大 DISCUSSION_IDLE 再试。"""

API_TIMEOUT_MAXWALL = """\
达到 %(limit)ds 绝对上限并中止。本轮缺少 %(name)s 的发言。
已收到 %(events)d 条事件、%(chars)d 字正文、%(think)d 字思考内容。

能拧的只有 DISCUSSION_MAX_WALL(注意调用方通常有 600s 上限,本脚本必须先开枪)。"""

# R-038:HTTP 200 但整个响应体不是 SSE。实测 ollama 在 stream=false 时就是这样,
# 而某些网关会直接忽略 stream 参数。状态仍是缺席(我们确实没拿到能用的流),
# 但文案必须说清真因 —— 否则用户看到「流中断,已收 0 条事件」会去查网络。
API_NOT_STREAMING_MSG = """\
端点返回了 HTTP 200,但整个响应**不是流式**的(没有任何 SSE 事件)。本轮缺席。

响应体看起来是一个完整的非流式回答。常见原因:该端点或网关忽略了 stream 参数。
trundle 只支持流式 —— 超时是按吐字间隔判的,没有事件流就只能退化成墙钟。

服务端返回的开头:
%(head)s"""

API_INCOMPLETE_MSG = """\
调用失败(生成未完成)。本轮缺席。
HTTP %(http)s · finish_reason=%(finish)s · 已收 %(events)d 条事件、%(chars)d 字正文。

%(why)s"""

API_TRUNCATED_MSG = """\
调用失败(疑似上下文被截断)。本轮缺席。
HTTP %(http)s · %(reason)s

模型只看到了 prompt 的一部分,而被丢弃的通常是**开头** —— 也就是
【已确立的前提】和【已废弃的方向】。它照样会给出一个听起来合理的回答,
所以这一轮宁可缺席也不能采信。

两条出路:
  1. 调大该模型的上下文窗口(ollama 是 num_ctx,云端一般在控制台或请求参数里)
  2. 换一个上下文更大的模型"""

API_EMPTY_MSG = """\
调用失败(流正常结束但正文为空)。本轮缺席。
HTTP %(http)s · finish_reason=%(finish)s · 已收 %(events)d 条事件、%(think)d 字思考内容。

finish_reason=content_filter 通常表示内容被服务端过滤;
若思考内容非空而正文为空,可能是模型一直在想却没给出结论。"""

API_HTTP_MSG = """\
调用失败(HTTP %(http)s)。本轮缺席。

服务端原文:
%(body)s"""

API_CONN_MSG = """\
调用失败(连不上)。本轮缺席。
base_url: %(url)s

%(err)s

注意 base_url 不做任何 scheme 或主机白名单校验,填什么就发什么 —— 这条报错
说的是网络层面真的没连上。"""


def _is_nonstream_json(run):
    """R-038:零 SSE 事件,但整块响应体是一个完整的非流式回答。"""
    if run.event_count or run.saw_done:
        return None
    blob = "".join(run.raw_body).strip()
    if not blob:
        return None
    try:
        d = json.loads(blob)
    except ValueError:
        return None
    try:
        return d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def classify_api(run):
    """R-022/R-024..R-030/R-035/R-037/R-038。

    ★ 永不返回 untrusted ★(R-035)—— 那个取值的语义是「目录未被信任」,
    只对 CLI 参与者有意义。API 参与者的失败一律落在 error/ratelimited/timeout。
    """
    spec = run.spec or {}
    ev, chars, think = run.event_count, len(run.body()), run.think_chars

    if run.killed:
        # 限流的典型表现恰恰**就是**长时间没有输出,先查一遍再报超时
        if is_rate_limited(run):
            run.status = "ratelimited"
            run.note = "被限流,本轮缺席(等了 %.0fs 后中止)。可稍后重试。" % run.wall
            return
        run.status = "timeout"
        base = {"name": run.name, "wall": run.wall, "events": ev,
                "chars": chars, "think": think,
                "progress": run.progress_at_kill or "未知"}
        if run.killed == "maxwall":
            base["limit"] = maxwall_for(run)
            run.note = API_TIMEOUT_MAXWALL % base
        elif run.got_event:
            base["limit"] = idle_for(run)
            run.note = API_TIMEOUT_STALLED % base
        elif ev:
            # ★ 中间那档 ★ 收到过事件、但全是思考内容
            base["limit"] = GRACE
            run.note = API_TIMEOUT_THINKING % base
        else:
            base["limit"] = GRACE
            run.note = API_TIMEOUT_NEVER % base
        return

    if run.first_byte_timeout:                                # R-037 第一档
        run.status = "timeout"
        run.note = API_TIMEOUT_NEVER % {
            "name": run.name, "wall": run.wall, "limit": GRACE,
            "progress": "等待首字节(连接已建立,服务端未开始返回)"}
        return

    if run.conn_error:                                        # R-028
        run.status = "error"
        run.note = API_CONN_MSG % {"url": spec.get("base_url"), "err": run.conn_error}
        return

    if run.http_status == 429 or (run.http_status != 200 and is_rate_limited(run)):
        run.status = "ratelimited"                            # R-026
        run.note = "被限流,本轮缺席。可稍后重试。\n\n%s" % (run.error_body or "")[:400]
        return

    if run.http_status != 200:                                # R-027
        run.status = "error"
        run.note = API_HTTP_MSG % {"http": run.http_status,
                                   "body": (run.error_body or "(响应体为空)")[:800]}
        return

    nonstream = _is_nonstream_json(run)
    if nonstream is not None:                                 # R-038
        run.status = "error"
        run.note = API_NOT_STREAMING_MSG % {"head": "".join(run.raw_body)[:400]}
        return

    if not ev and is_rate_limited(run):
        # HTTP 200 + 非 SSE 的限流体。只在**零事件**时查 —— 有事件时 raw_body 里
        # 全是参与者的发言,而讨论里聊到 "429" 或 "rate limit" 是很自然的事。
        run.status = "ratelimited"
        run.note = "被限流,本轮缺席。可稍后重试。"
        return

    # R-021:完整 = 收到 [DONE] **且** finish_reason 为 stop。
    # [DONE] 只保证传输完整,finish_reason 才保证生成完整 —— 实测
    # max_tokens 砍断的响应同样会发 [DONE](llama3.1:8b, finish_reason='length')。
    # 降级:端点压根不给 finish_reason 时,退回只判 [DONE]。
    # 实测三家(ollama / Gemini / DeepSeek)都发 [DONE]、都给 finish_reason;
    # 降级分支保留给将来可能出现的不给 finish_reason 的端点。
    complete = run.saw_done and run.finish_reason in (None, "stop")
    if not complete:                                          # R-022
        why = ("未收到 data: [DONE] 终止符,流被中途掐断。"
               if not run.saw_done else
               "finish_reason=%s 表示生成没有正常结束(length 通常是撞上了 max_tokens)。"
               % run.finish_reason)
        run.status = "error"
        run.note = API_INCOMPLETE_MSG % {
            "http": run.http_status, "finish": run.finish_reason,
            "events": ev, "chars": chars, "why": why}
        return

    # R-023 / R-024:静默截断。拿不到 usage 就不判(降级)。
    # ★ 这条确证只服务本地 runtime ★ 实测:Gemini 在超限时明确报
    # HTTP 400 "The input token count exceeds the maximum number of tokens
    # allowed 131072"(走 R-027,文案带服务端原文);而 ollama 是 HTTP 200 +
    # finish_reason=stop + 静默丢弃 prompt 开头。也就是说云端会告诉你,
    # 本地不会 —— 下面这个检测是为后者准备的。
    reason = detect_truncation(run.prompt_chars,
                               (run.usage or {}).get("prompt_tokens"))
    if reason:
        run.status = "error"
        run.note = API_TRUNCATED_MSG % {"http": run.http_status, "reason": reason}
        return

    if not run.body().strip():                                # R-025
        run.status = "error"
        run.note = API_EMPTY_MSG % {"http": run.http_status,
                                    "finish": run.finish_reason,
                                    "events": ev, "think": think}
        return

    run.status = "ok"


def classify(run):
    if run.status:                      # untrusted / 启动失败,已经定了
        run.note = scrub(run.note, run.spec)
        return
    if run.kind == "api":
        classify_api(run)
        # R-001 的最后一道闸:失败文案会连同正文一起交给模型,凭证绝不能混进去
        run.note = scrub(run.note, run.spec)
        return
    # 被超时砍掉的也要先查一遍限流。限流的典型表现恰恰**就是**长时间没有输出,
    # 而这两个 kill 分支原本直接 return —— 于是 stderr 里明写着 429,报出来
    # 也只是"卡死",把人引向"调大超时"而不是"等一会儿再来"。
    if run.killed and is_rate_limited(run):
        run.status = "ratelimited"
        run.note = ("被限流,本轮缺席(等了 %.0fs 后中止)。可稍后重试。\n\n%s"
                    % (run.wall, stderr_tail(run, 3)))
        return
    if run.killed:
        run.status = "timeout"
        # 无事件流的 agent 只可能撞墙钟(watch 跳过了空闲判定)。这里仍然先判它:
        # 哪怕将来有人在 watch 里改出一条 idle 路径,也不会把「调大
        # DISCUSSION_IDLE」这种对它不通电的建议发出去。
        if not has_stream(run):
            run.note = NO_STREAM_MSG % {
                "limit": maxwall_for(run),
                "wall": run.wall,
                "name": run.name,
                "probe": probe_cmd(run),
            }
        elif run.killed == "idle":
            run.note = (STALLED_MSG if run.got_event else NEVER_STARTED_MSG) % {
                "limit": idle_for(run) if run.got_event else GRACE,
                "wall": run.wall,
                "name": run.name,
                "progress": run.progress_at_kill or "未知",
                "probe": probe_cmd(run),
            }
        else:
            run.note = MAXWALL_MSG % {
                "limit": maxwall_for(run), "name": run.name}
        return
    if run.rc != 0 and is_rate_limited(run):
        run.status = "ratelimited"
        run.note = "被限流,本轮缺席。可稍后重试。\n\n" + stderr_tail(run, 3)
        return
    if run.rc != 0 or not run.body().strip():
        run.status = "error"
        run.note = "调用失败(exit=%s)。stderr 末尾:\n%s" % (run.rc, stderr_tail(run))
        return
    run.status = "ok"


# ── 状态行 ───────────────────────────────────────────────────

def status_body(runs, silence=False):
    """状态串。

    silence=False 的版本用来去重 —— 秒数每次都变,拿它比较等于不去重。
    silence=True 的版本用来显示,额外带上「静默 Ns」。
    静默时长必须可见:否则 agent 被空闲超时砍掉时,日志上看不出任何征兆,
    读起来就像误杀(而它可能真的是误杀,也可能只是它在闷头写正文)。
    """
    now = time.monotonic()
    parts = []
    for r in runs:
        if r.done.is_set():
            parts.append("%s ▸ %s" % (r.name, r.status or r.progress))
            continue
        note = r.progress
        size = r.size()
        if size and (r.echo_capped or not r.echoed):
            note = "%s %s" % (note, human(size))
        if silence and not has_stream(r):
            # 不显示「静默 N/M」:那个格式读起来是一句倒计时(「快超时了」),而它的
            # 静默既不携带信息、也不会因此被砍。这里给的是「还在跑」,不是
            # 「还剩多久」。上限仍然印出来,免得墙钟那一枪显得突然。
            note = "%s · 已跑 %ds(上限 %ds)" % (
                note, int(now - r.started), maxwall_for(r))
        elif silence and r.last_activity is not None:
            quiet = int(now - r.last_activity)
            if quiet >= SILENCE_HINT:
                note = "%s · 静默 %ds/%ds" % (
                    note, quiet, idle_for(r) if r.got_event else GRACE)
        parts.append("%s ▸ %s" % (r.name, note))
    return " │ ".join(parts)


# ── 主流程 ───────────────────────────────────────────────────

def watch(runs):
    last_tick = 0.0
    last_text = None
    start = time.monotonic()
    live = [r for r in runs if r.proc is not None or not r.done.is_set()]
    while True:
        now = time.monotonic()
        pending = [r for r in live if not r.done.is_set()]
        if not pending:
            break
        for r in pending:
            # ★ 这行曾经是 `if r.proc is None: continue`,语义是「启动失败的跳过」。
            # API run **永远**没有 proc —— 照原样写会让所有 API 调用完全不受
            # 空闲上限与绝对上限约束,一直挂到调用方的 600s 超时把整轮输出丢光。
            # 这是个静默失效:跑一次成功调用完全抓不到。见 plan R-003 / TC-003-1。
            if r.kind != "api" and r.proc is None:
                continue
            if now - r.started >= maxwall_for(r):
                r.kill("maxwall")
                continue
            # 无事件流的 agent 整轮零输出,「静默」不携带任何信息 —— 对它做空闲
            # 判定等于纯按墙钟误杀。实测 dsh 一个讨论级 prompt 静默 34.6s,上下文
            # 更大时破 GRACE(180s) 只是时间问题,而破了之后 got_event 恒为 False,
            # 会被判成「压根没开始」并建议去拧 FIRST_BYTE_GRACE —— 一个对它完全
            # 不通电的开关。它只受墙钟约束。
            if not has_stream(r):
                continue
            limit = idle_for(r) if r.got_event else GRACE
            if now - r.last_activity >= limit:
                r.kill("idle")
        elapsed = now - start
        if elapsed - last_tick >= TICK:
            body = status_body(runs)
            # 状态没变就不刷屏 —— 一条跑一分钟的命令不该刷出十几行同样的字。
            # 但也不能完全沉默,所以每 TICK_FORCE 秒强制心跳一次。
            # 去重用不带静默计时的版本,显示用带的。
            if body != last_text or elapsed - last_tick >= TICK_FORCE:
                emit("%ds │ %s" % (int(elapsed), status_body(runs, silence=True)))
                last_text = body
                last_tick = elapsed
        time.sleep(POLL)


# ── 内省接口 ─────────────────────────────────────────────────
#
# 给 shell 脚本用的两个只读查询。存在的理由是「同一件事只有一处实现」:
#
#   --list-agents  已登记名单。以前 discover.sh 和 verify.sh 各自硬编码一份
#                  (加上 verify.sh 的提示文案是三份),新增 agent 要人手同步 ——
#                  漏一处的症状是「装了却不被调用」或「自检说没装」,而且不报错。
#   --precheck     跑某个 agent 的 precheck。以前 verify.sh 用 grep 管道自己判
#                  gemini 的信任目录,与 gemini_is_trusted() 实测有六处行为分歧
#                  (跨行 JSON、根目录、精确相等 vs 含子串、软链、$HOME、损坏
#                  JSON)。最糟的一个方向是自检报「已信任」而讨论时被跳过 ——
#                  绿灯自检 + 凭空缺席,最难查。现在只有 Python 那一套实现。
#
# 注意:precheck 的结果依赖 os.getcwd()(见 gemini_is_trusted),所以调用方
# **不能**先 cd 到脚本目录再调,否则判的是脚本目录而不是用户目录。

def cmd_list_agents():
    for name in AGENTS:
        sys.stdout.write(name + "\n")
    return 0


def cmd_precheck(name):
    spec = resolve_spec(name)
    if spec is None:
        sys.stderr.write(UNKNOWN_MSG % name + "\n")
        return 2
    precheck = spec["precheck"]
    verdict = precheck() if precheck else None
    if not verdict:
        return 0
    # 补救指引原样交给调用方 —— 与讨论中缺席时给用户看的是同一段话,
    # 不再各写各的
    sys.stdout.write(verdict[1] + "\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("用法: invoke.py <agent>:<提示文件> [...]\n")
        return 1

    # --api-config:本轮的 API 实例配置(sidecar)。由调用方从名册转写而来,
    # 只含环境变量名、不含凭证值。放在最前面解析 —— 后面的 <agent>:<文件>
    # 参数要靠它才认得出 API 参与者的名字。
    if argv[0] == "--api-config":
        if len(argv) < 2:
            sys.stderr.write("用法: invoke.py --api-config <sidecar.json> <agent>:<提示文件> ...\n")
            return 2
        ok, err, warns = load_api_config(argv[1])
        for w in warns:
            emit(w)
        if not ok:
            sys.stderr.write(err + "\n")
            return 2
        argv = argv[2:]
        if not argv:
            sys.stderr.write("用法: invoke.py --api-config <sidecar.json> <agent>:<提示文件> ...\n")
            return 1

    if argv[0] == "--list-agents":
        return cmd_list_agents()
    if argv[0] == "--list-callable":
        # R-039:本轮可调用名单 = 适配库登记名单 ∪ 已加载的 API 参与者。
        # 与 --list-agents 是**两个**不同的集合,不要混用(见 R-004 的注释)。
        for name in callable_names():
            sys.stdout.write(name + "\n")
        return 0
    if argv[0] == "--precheck":
        if len(argv) != 2:
            sys.stderr.write("用法: invoke.py --precheck <agent>\n")
            return 2
        return cmd_precheck(argv[1])

    runs = []
    for arg in argv:
        name, sep, promptfile = arg.partition(":")
        if not sep or not name or not promptfile:
            sys.stderr.write("参数格式应为 <agent>:<提示文件>,收到: %s\n" % arg)
            return 1
        if not os.path.isfile(promptfile):
            sys.stderr.write("提示文件不存在: %s\n" % promptfile)
            return 1
        runs.append(Run(name, promptfile))

    for r in runs:
        if r.spec is None:
            r.status, r.note = "error", UNKNOWN_MSG % r.name
            r.done.set()
            continue
        precheck = r.spec["precheck"]
        verdict = precheck() if precheck else None
        if verdict:
            r.status, r.note = verdict
            r.done.set()
            continue
        launch(r)

    try:
        watch(runs)
    except KeyboardInterrupt:
        for r in runs:
            if not r.done.is_set():
                r.kill("maxwall")
        emit("已中断,子进程组已清理")
        return 130
    finally:
        # 无论怎么退出,都不留孙进程
        for r in runs:
            if r.proc is not None and r.proc.poll() is None:
                r.kill(r.killed or "maxwall")

    for r in runs:
        r.done.wait(timeout=TERM_GRACE + 5)
        # 回显缓冲里可能还剩不足一行的尾巴
        if r.echo_buf.strip() and not r.echo_capped:
            emit("%s │ %s" % (r.name, r.echo_buf.strip()))
            r.echo_buf = ""
        if not has_stream(r) and r.size():
            # 无事件流的 agent 全程零回显,这里给一行回执,让「它到底有没有干活」
            # 有个落点 —— 否则用户看到的是十几行「已跑 Ns」然后突然出结果。
            # 只报体量不报正文:正文这时才到手,回显它既失去「它真的在写」的意义,
            # 又要为同一段文字付两份 token(stderr 会连同 stdout 一起交给模型)。
            emit("%s │ 一次性返回 %s(全程无进度事件)" % (r.name, human(r.size())))
        classify(r)

    out = sys.stdout
    for r in runs:
        out.write("===AGENT %s %s %.1fs===\n" % (r.name, r.status, r.wall))
        out.write((r.note if r.note is not None else r.body()).rstrip("\n"))
        out.write("\n\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
