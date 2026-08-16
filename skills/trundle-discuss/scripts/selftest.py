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

import os
import shutil
import sys
import tempfile

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

    这条同时是个基线:verify.sh 用 grep 管道判同一件事,跨行时会得出相反结论。
    统一两处实现之后,这条用例应该扩到 verify.sh 上。
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
        for field in ("build", "parse", "probe"):
            assert spec.get(field), "%s 缺 %s" % (name, field)
        assert callable(spec["build"]), name
        assert callable(spec["parse"]), name


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

    print()
    if _FAILED:
        print("✗ %d 项失败:%s" % (len(_FAILED), "、".join(_FAILED)))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
