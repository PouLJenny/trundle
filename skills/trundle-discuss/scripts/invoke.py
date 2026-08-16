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
import subprocess
import sys
import threading
import time

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

它没有登记在 agents.yaml 里,因此不会被调用 —— 猜测调用方式是危险的:
猜错只读 flag 会让它拿到写权限。接纳步骤见 references/adapting-new-cli.md。"""


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
        # 无流 = 卡死无征兆,墙钟是唯一护栏,所以比全局 540 收紧。
        # 实测最重的讨论级 prompt 只用 34.6s,300 有约 9 倍余量。
        "max_wall": 300,
        # 带上环境变量前缀是刻意的:这条同时是诊断命令和「只读怎么开」的
        # 唯一一份可复制文档。
        "probe": 'DSH_PERMISSION_MODE=read-only dsh --profile headless "说一句话"',
    },
}


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
        self.spec = AGENTS.get(name)
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


def classify(run):
    if run.status:                      # untrusted / 启动失败,已经定了
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
            if r.proc is None:
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


def main(argv):
    if not argv:
        sys.stderr.write("用法: invoke.py <agent>:<提示文件> [...]\n")
        return 1

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
