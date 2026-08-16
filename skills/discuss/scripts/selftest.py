#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trundle-discuss —— 自检

    python3 selftest.py        (verify.sh 会自动带上这一步)

只测纯函数,**不调用任何 agent CLI**。这条约束有来历:排查「gemini 卡死」时,
在一个会自己变化的外部状态(API 配额窗口)上做 A/B 对照,五组实验因为窗口
在实验之间滚动而全部作废。自检要是也依赖外部状态,它就会时灵时不灵 ——
那比没有自检更糟,因为它会让人以为验证过了。

不引入 pytest:本项目的既有立场是不付白依赖(见 verify.sh 里关于不装 jq 的
那段注释),assert 够用。
"""

import io
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoke  # noqa: E402


# ── 迷你测试框架 ─────────────────────────────────────────────

_FAILED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        _FAILED.append(name)
        print("  ✗ %s" % name)
        print("      %s" % (exc or "断言失败"))
    except Exception as exc:                        # noqa: BLE001
        _FAILED.append(name)
        print("  ✗ %s(抛异常)" % name)
        print("      %r" % (exc,))
    else:
        print("  ✓ %s" % name)


def mkrun(name="gemini", **kw):
    """造一个跑完了的 Run。promptfile 不会被读,给 /dev/null 即可。"""
    r = invoke.Run(name, "/dev/null")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


# ── 超时诊断:两种情况必须给不同的旋钮 ────────────────────────
#
# 这组用例锁的是本次修复的核心。回归的样子是:两种超时又共用一段文案,
# 于是「从未开始」的人被建议去调 DISCUSSION_IDLE —— 而那条路径走的是
# GRACE,那个开关根本不通电。

def t_never_started_points_at_grace():
    r = mkrun(killed="idle", got_event=False, wall=222.6, progress_at_kill="启动中")
    invoke.classify(r)
    assert r.status == "timeout", r.status
    assert "DISCUSSION_FIRST_BYTE_GRACE" in r.note, r.note
    # STALLED_MSG 的原话不该出现 —— 它是另一种情况的建议
    assert "调大 DISCUSSION_IDLE 再试" not in r.note, r.note
    assert "无效" in r.note, "必须明说 DISCUSSION_IDLE 对这条路径无效"
    assert str(invoke.GRACE) in r.note, "要印出实际用的宽限值"


def t_never_started_shows_probe():
    r = mkrun("gemini", killed="idle", got_event=False, wall=181.6,
              progress_at_kill="启动中")
    invoke.classify(r)
    # 卡在启动阶段时,真实原因往往只在终端里看得到,所以必须给出手动诊断命令
    assert invoke.AGENTS["gemini"]["probe"] in r.note, r.note


def t_stalled_midway_points_at_idle():
    r = mkrun("codex", killed="idle", got_event=True, wall=305.0,
              progress_at_kill="发言中")
    invoke.classify(r)
    assert r.status == "timeout", r.status
    assert "调大 DISCUSSION_IDLE 再试" in r.note, r.note
    assert "DISCUSSION_FIRST_BYTE_GRACE" not in r.note, r.note
    # codex 是 item 级事件,空闲上限单独定成 300
    assert str(invoke.AGENTS["codex"]["idle"]) in r.note, r.note


def t_progress_at_kill_is_reported():
    r = mkrun(killed="idle", got_event=False, wall=100.0, progress_at_kill="启动中")
    invoke.classify(r)
    assert "启动中" in r.note, r.note


# ── 限流:被超时砍掉之前也要认出来 ────────────────────────────
#
# 限流的典型表现就是长时间没输出,所以它经常先被空闲超时砍掉。
# 修复前两个 kill 分支直接 return,stderr 里明写着 429 也只报「卡死」。

def t_ratelimit_beats_idle_timeout():
    r = mkrun(killed="idle", got_event=False, wall=200.0,
              stderr_lines=["Error: 429 Too Many Requests"])
    invoke.classify(r)
    assert r.status == "ratelimited", r.status
    assert "稍后重试" in r.note, r.note


def t_ratelimit_beats_maxwall():
    r = mkrun(killed="maxwall", wall=540.0,
              stderr_lines=["Quota exceeded for metric: generate_content_free_tier"])
    invoke.classify(r)
    assert r.status == "ratelimited", r.status


def t_gemini_actual_quota_wording():
    # 实测原话。注意它不含连续的 "quota exceeded",单靠那个短语接不住
    r = mkrun(killed="idle", got_event=False, wall=180.0, stderr_lines=[
        "You exceeded your current quota, please check your plan and billing details."])
    invoke.classify(r)
    assert r.status == "ratelimited", r.status


def t_bare_429_in_body_is_not_ratelimit():
    # 回归:裸子串 "429" 会把正文里的 4290ms 判成限流
    r = mkrun(rc=1, raw_sample=['{"text":"建议把超时从 4290ms 调到 5000ms"}'])
    assert invoke.is_rate_limited(r) is False, "4290ms 不该算限流"


def t_real_429_still_detected():
    r = mkrun(rc=1, raw_sample=['{"status":429,"msg":"slow down"}'])
    assert invoke.is_rate_limited(r) is True


# ── Run.body() 的三种正文来源 ────────────────────────────────
#
# 三家 CLI 的正文形状不同(codex 完整消息 / gemini delta / claude 末条 result),
# 优先级搞反会静默拿到错的正文 —— 不会报错,只会发言变成半截。

def t_body_source_priority():
    r = mkrun()
    r.deltas = ["前", "半", "段"]
    assert r.body() == "前半段", r.body()
    r.messages = ["先看看文件", "这才是正文"]
    assert r.body() == "这才是正文", "messages 优先于 deltas,且取最后一条"
    r.final = "权威结果"
    assert r.body() == "权威结果", "final 最高优先"


def t_empty_final_still_wins():
    # 边界:final 为空字符串也算"有",不该回退到 deltas。
    # 空正文由 classify 判成 error,不是在这里兜。
    r = mkrun(final="", deltas=["不该用这个"])
    assert r.body() == "", r.body()


def t_empty_body_classified_as_error():
    r = mkrun(rc=0, final="")
    invoke.classify(r)
    assert r.status == "error", r.status


def t_successful_run_is_ok():
    r = mkrun(rc=0, final="这是正文")
    invoke.classify(r)
    assert r.status == "ok", r.status
    assert r.note is None, "成功时不该有 note —— 正文走 body()"


def t_success_mentioning_429_still_ok():
    # 新加的「报 timeout 前先查限流」不能误伤正常发言:
    # 讨论里聊到 HTTP 429 是很自然的事,不该因此把这一轮判成被限流。
    r = mkrun(rc=0, final="建议对 429 响应做指数退避,首次等 1s")
    invoke.classify(r)
    assert r.status == "ok", r.status


# ── gemini 信任目录 ──────────────────────────────────────────

def t_trusted_folder_multiline_json():
    """跨行 JSON 也要认。

    verify.sh 曾经用 grep 管道判同一件事,跨行时得出相反结论(还有另外五处
    分歧:根目录、精确相等 vs 含子串、软链、$HOME、损坏 JSON)。那份实现已经
    删掉,verify.sh 现在转调 invoke.py --precheck —— 也就是这个函数。所以这条
    用例现在同时守着两边。
    """
    tmp = os.path.realpath(tempfile.mkdtemp())     # macOS 上 /var 是软链,必须解
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(tmp, ".gemini"))
    os.makedirs(proj)
    with open(os.path.join(tmp, ".gemini", "trustedFolders.json"), "w",
              encoding="utf-8") as fh:
        fh.write('{\n  "%s":\n    "TRUST_FOLDER"\n}\n' % proj)
    old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
    try:
        os.environ["HOME"] = tmp
        os.chdir(proj)
        assert invoke.gemini_is_trusted() is True
    finally:
        os.chdir(old_cwd)
        if old_home is not None:
            os.environ["HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


def t_untrusted_when_config_missing():
    # 安全侧失败:读不到配置就当不信任,宁可跳过 gemini 也不误放行
    tmp = os.path.realpath(tempfile.mkdtemp())
    old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
    try:
        os.environ["HOME"] = tmp
        os.chdir(tmp)
        assert invoke.gemini_is_trusted() is False
    finally:
        os.chdir(old_cwd)
        if old_home is not None:
            os.environ["HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


# ── 未登记的 agent 绝不调用 ──────────────────────────────────

def t_unknown_agent_has_no_spec():
    # 猜错非交互 flag 只是挂掉,猜错只读 flag 会给它写权限 —— 所以必须没有 spec
    assert invoke.AGENTS.get("aider") is None
    assert mkrun("aider").spec is None


def t_every_agent_spec_is_complete():
    for name, spec in invoke.AGENTS.items():
        for field in ("build", "probe", "stream"):
            assert spec.get(field), "%s 缺 %s" % (name, field)
        assert callable(spec["build"]), name
        assert spec["stream"] in ("token", "item", "none"), \
            "%s: stream=%r" % (name, spec["stream"])
        if spec["stream"] == "none":
            # 无事件流的 agent 不该有 parse:写了就说明有人误以为能从它的
            # stdout 里读出进度。idle 同理 —— 填了就是在暗示空闲超时对它生效。
            assert spec.get("parse") is None, "%s 无事件流却有 parse" % name
            assert spec.get("idle") is None, "%s 无事件流却填了 idle" % name
        else:
            assert callable(spec["parse"]), name


# ── 无事件流的 agent(dsh)────────────────────────────────────
#
# 这组锁的是:没有事件流的 agent 不能被拖进「活动驱动」那套判定里。
# 回归的样子是 —— 它跑超过 GRACE 就被判「压根没开始」,然后建议用户去拧
# DISCUSSION_FIRST_BYTE_GRACE。那个开关对它不通电,和上面那组修的是同一种病。

def t_no_stream_has_no_idle():
    assert invoke.has_stream(mkrun("dsh")) is False
    assert invoke.idle_for(mkrun("dsh")) is None
    assert invoke.has_stream(mkrun("codex")) is True


def t_idle_override_does_not_resurrect_idle():
    # DISCUSSION_IDLE 是调试旋钮,但它也不该把无流 agent 拽回空闲判定 ——
    # 那等于凭空造出第四个不通电的开关
    old = invoke._IDLE_OVERRIDE
    try:
        invoke._IDLE_OVERRIDE = 300
        assert invoke.idle_for(mkrun("dsh")) is None
        assert invoke.idle_for(mkrun("codex")) == 300
    finally:
        invoke._IDLE_OVERRIDE = old


def t_no_stream_has_shorter_maxwall():
    # 无流 = 卡死无征兆,墙钟是唯一护栏,所以要比有流的更保守
    assert invoke.maxwall_for(mkrun("dsh")) == 300
    assert invoke.maxwall_for(mkrun("codex")) == invoke.MAXWALL


def t_maxwall_override_applies_to_all():
    # 显式设了 DISCUSSION_MAX_WALL 就是全局的,per-agent 的收紧要让位
    old = invoke._MAXWALL_OVERRIDE
    try:
        invoke._MAXWALL_OVERRIDE = 20
        assert invoke.maxwall_for(mkrun("dsh")) == 20
        assert invoke.maxwall_for(mkrun("codex")) == 20
    finally:
        invoke._MAXWALL_OVERRIDE = old


def t_no_stream_timeout_points_at_maxwall():
    r = mkrun("dsh", killed="maxwall", wall=300.0,
              progress_at_kill="运行中(无进度事件)")
    invoke.classify(r)
    assert r.status == "timeout", r.status
    assert "DISCUSSION_MAX_WALL" in r.note, r.note
    assert "没有事件流" in r.note, r.note
    # 另外两套文案的哨兵句一个都不许出现
    assert "调大 DISCUSSION_IDLE 再试" not in r.note, r.note
    assert "压根没开始" not in r.note, r.note
    assert invoke.AGENTS["dsh"]["probe"] in r.note, "无流 agent 更依赖手动 probe"


def t_no_stream_idle_kill_still_points_at_maxwall():
    # 防御性:watch 现在不会给它 killed="idle",但万一将来改出这条路径,
    # 也不能把「拧 DISCUSSION_IDLE」发给它
    r = mkrun("dsh", killed="idle", got_event=False, wall=200.0)
    invoke.classify(r)
    assert "DISCUSSION_MAX_WALL" in r.note, r.note
    assert "压根没开始" not in r.note, r.note


def t_no_stream_body_comes_from_text():
    r = mkrun("dsh")
    r.text = ["第一段\n", "\n", "第二段\n"]
    assert r.body() == "第一段\n\n第二段\n", repr(r.body())
    # 空行是段落分隔,不能被吞掉 —— 这是纯文本路径和 JSONL 路径的关键差别
    assert "\n\n" in r.body()
    r.deltas = ["不该用这个"]
    assert r.body().startswith("第一段"), "text 优先于 deltas"


def t_no_stream_status_line_has_no_countdown():
    # 状态行不许出现「静默 N/M」:那读起来是倒计时,而它的静默是正常的
    r = mkrun("dsh", progress="运行中(无进度事件)")
    r.started = time.monotonic() - 100
    r.last_activity = r.started
    line = invoke.status_body([r], silence=True)
    assert "静默" not in line, line
    assert "已跑" in line, line
    assert "300" in line, "上限要可见,否则墙钟那一枪显得突然:%s" % line


def t_readonly_env_overrides_user_env():
    # 只读铁律不能交给用户环境决定:dsh 的 headless profile 默认是
    # workspace-write,用户环境里若已有这个变量,必须被覆盖而不是被尊重
    old = os.environ.get("DSH_PERMISSION_MODE")
    try:
        os.environ["DSH_PERMISSION_MODE"] = "danger-full-access"
        env = invoke.build_env(invoke.AGENTS["dsh"])
        assert env["DSH_PERMISSION_MODE"] == "read-only", \
            env.get("DSH_PERMISSION_MODE")
    finally:
        if old is None:
            os.environ.pop("DSH_PERMISSION_MODE", None)
        else:
            os.environ["DSH_PERMISSION_MODE"] = old


# ── 文档与代码一致(agents.yaml / README)────────────────────
#
# agents.yaml 没有任何代码读它 —— 真值是 invoke.py 的 AGENTS,因为那里的
# build/parse/precheck 是 Python 函数对象,YAML 表达不了。所以这个文件是
# **文档**,而文档会漂:本项目实测漂过两次(stance 两处对不上、README 的
# claude 延迟与另外两处对不上)。
#
# 这组用例是那份文档唯一的看守。它不追求覆盖全部字段 —— 只锁最容易漏且
# 漏了最要命的:名单本身,以及三个决定超时行为的标量。

_HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_YAML = os.path.join(_HERE, "..", "agents.yaml")
README = os.path.join(_HERE, "..", "..", "..", "README.md")


def _scalar(raw):
    if raw == "null":
        return None
    if re.match(r"^-?\d+$", raw):
        return int(raw)
    return raw.strip("\"'")


def _yaml_agents(path=AGENTS_YAML):
    """从 agents.yaml 的 agents: 块里抽出 {名字: {标量字段}}。

    刻意只认两级缩进的简单形状 —— 它不是 YAML 解析器,也不打算成为。要回答的
    问题只有一个:文档里登记的名单和几个标量,跟代码里的 AGENTS 对不对得上。

    值里带冒号/引号/中文的字段(cmd / extract / probe / latency_observed)
    一概不碰 —— 那些正是会绊倒正则的地方,而它们也不值得用正则去赌。
    同理,`|` 块标量、行内 flow map、trust 子块都在更深的缩进层,天然被滤掉。

    形状一旦变化就什么都抽不出来,而 t_yaml_extractor_found_something 会因此
    变红 —— 宁可红,也不要静默地什么都没校验完还报绿。
    """
    out = {}
    cur = None
    in_agents = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if re.match(r"^\S", line):              # 回到顶层 key
                in_agents = line.startswith("agents:")
                cur = None
                continue
            if not in_agents:
                continue
            m = re.match(r"^  ([A-Za-z][\w-]*):\s*$", line)      # 二级 = agent 名
            if m:
                cur = m.group(1)
                out[cur] = {}
                continue
            if cur is None:
                continue
            m = re.match(r"^    (stream|idle|max_wall):\s*([^\s#]+)", line)
            if m:
                out[cur][m.group(1)] = _scalar(m.group(2))
    return out


def _readme_agents(path=README):
    """README「支持的 agent CLI」表首列的名字集合。

    锚定到那一节再抓,不全文扫 —— README 里另有两张表(@ 语法、斜杠命令)
    的首列也是反引号包起来的,全文扫会把 `@codex <你的话>` 之类一起抓进来。
    """
    names = set()
    inside = False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("## "):
                inside = line.startswith("## 支持的 agent CLI")
                continue
            if not inside:
                continue
            m = re.match(r"^\|\s*`([a-z][\w-]*)`\s*\|", line)
            if m:
                names.add(m.group(1))
    return names


def t_yaml_extractor_found_something():
    # 这条是下面两条的地基:提取器静默返回 {} 的话,它们会全部空转还报绿
    got = _yaml_agents()
    assert len(got) >= 4, "只抽到 %d 个 agent,提取器多半跟 yaml 的形状脱节了" % len(got)
    for name, fields in got.items():
        assert "stream" in fields, "%s 没抽到 stream" % name


def t_yaml_agent_names_match_code():
    assert set(_yaml_agents()) == set(invoke.AGENTS), \
        "agents.yaml 与 AGENTS 名单不一致:yaml=%s code=%s" % (
            sorted(_yaml_agents()), sorted(invoke.AGENTS))


def t_yaml_scalars_match_code():
    got = _yaml_agents()
    for name, spec in invoke.AGENTS.items():
        doc = got.get(name, {})
        for field in ("stream", "idle", "max_wall"):
            assert doc.get(field) == spec.get(field), \
                "%s.%s: agents.yaml=%r 但代码是 %r" % (
                    name, field, doc.get(field), spec.get(field))


def t_readme_lists_every_agent():
    # README.md 不在 skill 目录里。skill 被单独拷出来用时它就不存在 —— 那是
    # 确定性的结构事实,不是本文件开头反对的那种"时灵时不灵"的外部状态。
    # CI 里 README 一定在,所以这条约束真正由 CI 兜住。
    if not os.path.isfile(README):
        print("      (README.md 不在,跳过 —— skill 被单独拷出来用了)")
        return
    assert _readme_agents() == set(invoke.AGENTS), \
        "README 支持表与 AGENTS 不一致:readme=%s code=%s" % (
            sorted(_readme_agents()), sorted(invoke.AGENTS))


# ── 内省接口(给 shell 脚本用)──────────────────────────────

class _Capture(object):
    """把 cmd_* 写的东西收起来,别混进自检输出。"""

    def __init__(self):
        self.out = io.StringIO()
        self.err = io.StringIO()

    def __enter__(self):
        self._o, self._e = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self._o, self._e
        return False


def t_list_agents_prints_every_registered_name():
    # discover.sh 和 verify.sh 的名单都从这里派生 —— 它少一行,那两处一起少
    with _Capture() as cap:
        rc = invoke.cmd_list_agents()
    assert rc == 0, rc
    assert cap.out.getvalue().split() == list(invoke.AGENTS), cap.out.getvalue()


def t_precheck_unknown_agent_is_distinct_exit():
    # 未登记要和「登记了但没通过」区分开:前者是用户打错名字,后者是环境问题,
    # 两种的下一步动作完全不同。verify.sh 只把后者显示成提醒。
    with _Capture() as cap:
        assert invoke.cmd_precheck("aider") == 2
        assert invoke.cmd_precheck("codex") == 0      # codex 没有 precheck
    assert "未知 agent" in cap.err.getvalue()
    assert cap.out.getvalue() == "", "未登记的提示不该走 stdout —— 那是给调用方解析的"


def t_dsh_prompt_is_last_positional():
    # 任务是位置参数且不读 stdin;在 prompt 后面再追加任何东西,都会把它
    # 变成某个 flag 的参数
    argv = invoke.build_dsh("你好")
    assert argv[-1] == "你好", argv
    assert "--profile" in argv and "headless" in argv, argv


# ── moderator round plan 校验 ─────────────────────────────────
#
# 罐头 fixture 全在 plan_fixtures.py(纯数据),校验器在 plan_check.py
# (纯函数)。零 CLI 调用、零外部状态 —— 与本文件的硬约束一致。

import plan_check      # noqa: E402
import plan_fixtures   # noqa: E402


def t_good_plans_have_no_failures():
    for name, plan in plan_fixtures.GOOD_PLANS:
        results = plan_check.validate_plan(plan, plan_fixtures.ROSTER_NAMES)
        bad = [c["check"] for c in results if not c["ok"]]
        assert not bad, "%s: %s" % (name, bad)


def t_bad_plans_each_hit_expected_check():
    # 口径:目标 check 必红;不强求恰好一条红(schema 违规常连锁)。
    for name, plan, want in plan_fixtures.BAD_PLANS:
        results = plan_check.validate_plan(plan, plan_fixtures.ROSTER_NAMES)
        hit = [c["check"] for c in results if not c["ok"]]
        assert want in hit, "%s: 期望 %s,实际 %s" % (name, want, hit)


def t_extract_json_tolerates_fence_and_chatter():
    assert plan_check.extract_json(plan_fixtures.FENCED_RAW) is not None
    assert plan_check.extract_json(plan_fixtures.CHATTER_RAW) is not None


def t_extract_json_rejects_real_corruption():
    # CORRUPTED_RAW 是真实样本(claude-moderator,spike f4 复跑):
    # 大括号不配平,三级容忍必须接不住 —— 接住了反而说明解析器在瞎猜。
    assert len(plan_fixtures.CORRUPTED_RAW) > 0, "损坏样本丢了"
    assert plan_check.extract_json(plan_fixtures.CORRUPTED_RAW) is None


def t_retry_feedback_names_failures_and_sentinel():
    results = plan_check.validate_plan(None, plan_fixtures.ROSTER_NAMES)
    hard = plan_check.hard_failures(results)
    fb = plan_check.build_retry_feedback(plan_fixtures.CORRUPTED_RAW, hard)
    assert plan_check.RETRY_SENTINEL in fb
    assert "json_parse" in fb
    assert "【上一次输出被拒绝】" in fb


def t_hard_soft_split_is_stable():
    # hard 集合就是「计划不可执行」的定义,动它等于改协议 —— 必须显式过这里。
    results = plan_check.validate_plan(
        dict(plan_fixtures.GOOD_PLANS[0][1]), plan_fixtures.ROSTER_NAMES)
    hard_names = set(c["check"] for c in results if c["hard"])
    assert hard_names == {"json_parse", "schema_shape", "host_say_present",
                          "targets_in_roster", "origin_enum", "active_budget"}, hard_names


# ── moderator 协议与代码一致(漂移守卫)────────────────────────
#
# 这就是「允许的那半个机械检查」:枚举字面量与字段名两边对齐。
# 行为层(moderator 真按协议产计划)只能靠 spike runner 活体抽样,CI 不碰。

_PROTOCOL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "protocol", "moderator.md")


def _protocol_text():
    with io.open(_PROTOCOL, encoding="utf-8") as fh:
        return fh.read()


def t_protocol_file_exists():
    assert os.path.isfile(_PROTOCOL), _PROTOCOL


def t_protocol_contains_every_enum_literal():
    text = _protocol_text()
    for group in (plan_check.ORIGINS, plan_check.REASONS,
                  plan_check.CLOSING_TYPES, plan_check.VERDICTS):
        for literal in group:
            assert literal in text, "协议正文缺枚举字面量: %s" % literal
    for field in ("host_say", "rationale", "blind", "prompt",
                  "transcript_delta", "direction_change"):
        assert field in text, "协议正文缺字段名: %s" % field


def t_moderate_default_and_roster_parse():
    import moderate
    assert moderate.DEFAULT_MODERATOR == "codex"
    tmp = tempfile.mkdtemp()
    try:
        missing = os.path.join(tmp, "nope.yaml")
        assert moderate.parse_moderator_field(missing) is None
        p1 = os.path.join(tmp, "r1.yaml")
        with io.open(p1, "w", encoding="utf-8") as fh:
            fh.write("participants:\n  - agent: codex\n")
        assert moderate.parse_moderator_field(p1) is None
        p2 = os.path.join(tmp, "r2.yaml")
        with io.open(p2, "w", encoding="utf-8") as fh:
            fh.write("# 注释\nmoderator: dsh\nparticipants:\n  - agent: codex\n")
        assert moderate.parse_moderator_field(p2) == "dsh"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_moderate_rejects_unregistered_moderator():
    # 只断言「响亮」:显式指了未登记的名字必须出警告。回退落到谁取决于
    # 本机装了什么(外部状态),这里不碰。
    import moderate
    _, warns = moderate.pick_moderator("aider")
    assert warns and "未在适配库登记" in warns[0], warns


def t_moderate_input_roster_filters_unregistered():
    import moderate
    text = ("【名册】\n- codex(站位:证据)\n- aider(未登记)\n- dsh\n"
            "【共识状态头】\n(无)\n")
    names, dropped = moderate.roster_names_from_input(text)
    assert names == ["codex", "dsh"], names
    assert dropped == ["aider"], dropped


def t_skill_version_matches_plugin_json():
    # 版本同步:SKILL.md frontmatter vs .claude-plugin/plugin.json。
    # ★ 条件跳过(与 README 深度检查同款,同款危险)★:plugin 安装是**拷贝**,
    # 装出去的目录里没有 ../../../.claude-plugin/ —— 此时这条静默变绿。
    # 真正的执法者是 CI 的打包冒烟步,这里只是本地开发时的早期预警。
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_json = os.path.join(here, "..", "..", "..", ".claude-plugin", "plugin.json")
    if not os.path.isfile(plugin_json):
        print("      (跳过:.claude-plugin/plugin.json 不在,由 CI 兜底)")
        return
    import json as _json
    with io.open(plugin_json, encoding="utf-8") as fh:
        plugin_version = _json.load(fh)["version"]
    skill_md = os.path.join(here, "..", "SKILL.md")
    skill_version = None
    with io.open(skill_md, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^version:\s*(\S+)", line)
            if m:
                skill_version = m.group(1)
                break
    assert skill_version == plugin_version, (
        "SKILL.md=%s plugin.json=%s" % (skill_version, plugin_version))


def main():
    print("── 超时诊断 ──")
    check("从未开始 → 指向 FIRST_BYTE_GRACE", t_never_started_points_at_grace)
    check("从未开始 → 给出手动诊断命令", t_never_started_shows_probe)
    check("中途卡住 → 指向 DISCUSSION_IDLE", t_stalled_midway_points_at_idle)
    check("印出被砍时的状态", t_progress_at_kill_is_reported)

    print("── 限流分类 ──")
    check("429 优先于 idle 超时", t_ratelimit_beats_idle_timeout)
    check("配额超限优先于 maxwall", t_ratelimit_beats_maxwall)
    check("认得 gemini 的配额原话", t_gemini_actual_quota_wording)
    check("正文里的 4290ms 不算限流", t_bare_429_in_body_is_not_ratelimit)
    check("真的 429 仍然认得", t_real_429_still_detected)

    print("── 正文提取 ──")
    check("三种来源的优先级", t_body_source_priority)
    check("空 final 不回退到 deltas", t_empty_final_still_wins)
    check("空正文判为 error", t_empty_body_classified_as_error)
    check("正常返回判为 ok", t_successful_run_is_ok)
    check("正文里聊到 429 不误伤", t_success_mentioning_429_still_ok)

    print("── 信任目录 ──")
    check("跨行 JSON 认得出", t_trusted_folder_multiline_json)
    check("配置缺失按不信任处理", t_untrusted_when_config_missing)

    print("── 适配表 ──")
    check("未登记的 agent 没有 spec", t_unknown_agent_has_no_spec)
    check("已登记的 spec 字段齐全", t_every_agent_spec_is_complete)

    print("── 无事件流的 agent ──")
    check("无流 agent 没有空闲上限", t_no_stream_has_no_idle)
    check("DISCUSSION_IDLE 也不把它拽回空闲判定", t_idle_override_does_not_resurrect_idle)
    check("无流 agent 的墙钟单独收紧", t_no_stream_has_shorter_maxwall)
    check("显式墙钟覆盖对所有 agent 生效", t_maxwall_override_applies_to_all)
    check("超时 → 指向 DISCUSSION_MAX_WALL", t_no_stream_timeout_points_at_maxwall)
    check("即便被判 idle 也不指错旋钮", t_no_stream_idle_kill_still_points_at_maxwall)
    check("正文取整个 stdout,空行不丢", t_no_stream_body_comes_from_text)
    check("状态行不显示成倒计时", t_no_stream_status_line_has_no_countdown)
    check("只读强制覆盖用户环境", t_readonly_env_overrides_user_env)
    check("prompt 是最后一个位置参数", t_dsh_prompt_is_last_positional)

    print("── 文档与代码一致 ──")
    check("提取器真的抽到东西了", t_yaml_extractor_found_something)
    check("agents.yaml 名单与代码一致", t_yaml_agent_names_match_code)
    check("agents.yaml 标量与代码一致", t_yaml_scalars_match_code)
    check("README 支持表列全了", t_readme_lists_every_agent)

    print("── 内省接口 ──")
    check("--list-agents 就是注册表", t_list_agents_prints_every_registered_name)
    check("未登记与未通过是不同退出码", t_precheck_unknown_agent_is_distinct_exit)

    print("── moderator round plan 校验 ──")
    check("合法计划零失败", t_good_plans_have_no_failures)
    check("每类违规都被点名", t_bad_plans_each_hit_expected_check)
    check("围栏与闲话都解得出", t_extract_json_tolerates_fence_and_chatter)
    check("真实损坏样本必须解析失败", t_extract_json_rejects_real_corruption)
    check("重试反馈点名失败项", t_retry_feedback_names_failures_and_sentinel)
    check("hard/soft 边界锁死", t_hard_soft_split_is_stable)

    print("── moderator 协议与代码一致 ──")
    check("协议文件在位", t_protocol_file_exists)
    check("枚举与字段名两边对齐", t_protocol_contains_every_enum_literal)
    check("默认 moderator 与名册解析", t_moderate_default_and_roster_parse)
    check("未登记的 moderator 响亮拒绝", t_moderate_rejects_unregistered_moderator)
    check("输入名册过滤未登记者", t_moderate_input_roster_filters_unregistered)
    check("SKILL 与 plugin 版本同步", t_skill_version_matches_plugin_json)

    print()
    if _FAILED:
        print("✗ %d 项失败:%s" % (len(_FAILED), "、".join(_FAILED)))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
