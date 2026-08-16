#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trundle discuss —— round plan 校验器(纯函数,无 I/O)

moderator(见 ../protocol/moderator.md)每轮输出一个 JSON round plan,
壳照计划机械执行。本模块回答一个问题:这份计划能不能被零判断地执行。

检查分两级:
  hard —— 违反则计划不可执行,触发「带反馈重试一次」(moderate.py)
  soft —— 违反则如实警告,host 看着办;不拦执行

三方共用:moderate.py(运行时)、selftest.py(罐头 fixture 断言)、
tests/live/run_live.py(活体一致性)。「同一件事只有一处实现」——
枚举字面量与协议正文的一致性由 selftest 的漂移守卫盯着。

刻意不 import invoke:校验不需要知道 CLI 怎么调;反向依赖会把
「纯函数可测」这个属性弄丢。
"""

import difflib
import json
import re

# 这些枚举必须与 protocol/moderator.md 的输出格式块逐字一致,
# selftest 会断言每个字面量都出现在协议正文里。
ORIGINS = ("user", "active", "bet")
REASONS = ("没把握", "杠住了", "我是当事方")
CLOSING_TYPES = ("user_question", "fact_verdict", "none")
VERDICTS = ("agree", "disagree", "echo_suspect", "na")

FOOTER_MARKERS = ("立场", "不同意", "如果我错了")
FORBIDDEN_CLOSING = ("综合来看", "总的来说", "建议采用", "取长补短")
PROMPT_MIN_LEN = 200
BET_SIMILARITY_MAX = 0.9


def extract_json(text):
    """agent 输出 → dict 或 None。三级容忍:裸 JSON → markdown 围栏 → 首尾大括号。"""
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass
    return None


def _check(results, name, ok, hard, note=""):
    results.append({"check": name, "ok": bool(ok), "hard": hard, "note": note})


def validate_plan(plan, roster_names):
    """plan(dict|None)→ [{check, ok, hard, note}]。

    只查内在契约,不做参照比对(那是 spike runner 的实验断言)。
    """
    r = []
    _check(r, "json_parse", plan is not None, True)
    if plan is None:
        return r

    calls = plan.get("calls")
    closing = plan.get("closing") or {}
    consensus = plan.get("consensus") or {}
    delta = plan.get("transcript_delta") or {}
    host_say = plan.get("host_say")

    _check(r, "schema_shape",
           isinstance(calls, list) and isinstance(closing, dict)
           and isinstance(consensus, dict) and isinstance(delta, dict),
           True)
    if not isinstance(calls, list):
        return r

    # ── host_say:host 发言的正式落点(v1 新增,hard)──
    # 没有它,moderator 会把 host 指令渗进 closing.text / transcript_delta
    # (spike 实测:f5 与 f4-r2 两例),裁量从字段边缘漏出去。
    _check(r, "host_say_present",
           isinstance(host_say, str) and host_say.strip() != "", True)

    # ── calls:hard 部分 ──
    bad_target = [c.get("target") for c in calls
                  if c.get("target") not in roster_names]
    _check(r, "targets_in_roster", not bad_target, True, str(bad_target))

    bad_origin = [c.get("origin") for c in calls if c.get("origin") not in ORIGINS]
    _check(r, "origin_enum", not bad_origin, True, str(bad_origin))

    actives = [c for c in calls if c.get("origin") == "active"]
    _check(r, "active_budget", len(actives) <= 1, True, "active=%d" % len(actives))

    # ── calls:soft 部分 ──
    _check(r, "active_has_reason",
           all(c.get("reason") in REASONS for c in actives), False,
           str([c.get("reason") for c in actives]))
    # rationale(v1 新增):为什么此刻拉、压在哪个未验证假设上。
    # 微妙判断的稳定性只能抽样观测(spike 里 f4 复跑翻转过一次)——
    # 翻转无法禁止,但必须留下可事后审计的说理。
    _check(r, "active_has_rationale",
           all(isinstance(c.get("rationale"), str) and c["rationale"].strip()
               for c in actives), False)

    thin = [c.get("target") for c in calls
            if not (isinstance(c.get("prompt"), str)
                    and len(c["prompt"]) >= PROMPT_MIN_LEN)]
    _check(r, "prompt_substantial", not thin, False, "过短/缺失: %s" % thin)

    for c in calls:
        p = c.get("prompt") or ""
        _check(r, "footer_contract:%s" % c.get("target"),
               all(m in p for m in FOOTER_MARKERS), False)

    bets = [c for c in calls if c.get("origin") == "bet"]
    if bets:
        _check(r, "bet_blind_flag",
               all(isinstance(c.get("blind"), bool) for c in bets), False)
        if len(bets) >= 2:
            prompts = [c.get("prompt") or "" for c in bets]
            worst = max(
                difflib.SequenceMatcher(None, prompts[i], prompts[j]).ratio()
                for i in range(len(prompts)) for j in range(i + 1, len(prompts)))
            _check(r, "bet_prompts_differ", worst < BET_SIMILARITY_MAX, False,
                   "similarity=%.2f" % worst)

    # ── closing ──
    ctype, ctext = closing.get("type"), closing.get("text") or ""
    _check(r, "closing_enum", ctype in CLOSING_TYPES, False, str(ctype))
    if ctype == "user_question":
        _check(r, "closing_ends_question",
               ctext.rstrip().endswith(("?", "？")), False, ctext[-30:])
        hit = [w for w in FORBIDDEN_CLOSING if w in ctext]
        _check(r, "closing_no_synthesis_words", not hit, False, str(hit))
    if ctype == "fact_verdict":
        # 执行合同:text 必须写清查什么怎么查,host 当轮执行并回填 transcript。
        _check(r, "fact_verdict_text_nonempty", ctext.strip() != "", False)

    # ── consensus / delta ──
    _check(r, "consensus_enum", consensus.get("verdict") in VERDICTS, False,
           str(consensus.get("verdict")))
    _check(r, "delta_append_nonempty",
           isinstance(delta.get("append"), str) and delta["append"].strip() != "",
           False)
    return r


def hard_failures(results):
    return [c for c in results if c["hard"] and not c["ok"]]


def soft_failures(results):
    return [c for c in results if not c["hard"] and not c["ok"]]


RETRY_SENTINEL = "只输出一个 JSON 对象"

_RETRY_TMPL = """【上一次输出被拒绝】
你上一次的输出没有通过校验,失败项:
%s

你上一次输出的末尾部分(供对照):
%s

请针对上述错误重新输出**完整**的 round plan。%s,不要 markdown 围栏,
不要任何解释文字,第一个字符必须是 {。"""


def build_retry_feedback(raw_text, failures):
    """生成重试段,拼在原 prompt 之后再喂一次 moderator。"""
    lines = "\n".join(
        "- %s%s" % (c["check"], ((":%s" % c["note"]) if c.get("note") else ""))
        for c in failures)
    tail = (raw_text or "").strip()[-500:] or "(空输出)"
    return _RETRY_TMPL % (lines, tail, RETRY_SENTINEL)
