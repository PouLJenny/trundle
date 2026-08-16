#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""moderator spike runner —— 裁决「拆 moderator」的形态之争。

对每个 fixture(取自真实 transcript 的真实回合):
  1. moderator-prompt.md + fixture 输入 → 完整 prompt
  2. 经 invoke.py 并行喂给多个 moderator 模型(默认 claude + codex —— 纵向切片:
     同一份协议换脑子)
  3. 输出按 schema 机械断言,并与「host Claude 当时实际怎么做」的参照比对

用法:
  python3 run_spike.py [--models claude,codex] [--cases f1,f3] [--outdir DIR] [--tag N]

退出码:0 = 全部断言通过;1 = 有失败;2 = 基建错误。
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
INVOKE = os.path.join(REPO, "skills", "trundle-discuss", "scripts", "invoke.py")

sys.path.insert(0, os.path.join(HERE, "fixtures"))
from cases import FIXTURES  # noqa: E402

FORBIDDEN_CLOSING = ("综合来看", "总的来说", "建议采用", "取长补短")
FOOTER_MARKERS = ("立场", "不同意", "如果我错了")
ORIGINS = ("user", "active", "bet")
REASONS = ("没把握", "杠住了", "我是当事方")
CLOSING_TYPES = ("user_question", "fact_verdict", "none")
VERDICTS = ("agree", "disagree", "echo_suspect", "na")


def extract_json(text):
    """agent 输出 → JSON 对象。容忍 markdown 围栏与前后闲话。"""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return None


def check(results, name, ok, note=""):
    results.append({"check": name, "ok": bool(ok), "note": note})


def validate(plan, fx):
    """plan(dict|None)→ [{check, ok, note}]。断言即「壳零判断」所需的全部保障。"""
    r = []
    exp = fx["expected"]
    check(r, "json_parse", plan is not None)
    if plan is None:
        return r

    calls = plan.get("calls")
    closing = plan.get("closing") or {}
    consensus = plan.get("consensus") or {}
    delta = plan.get("transcript_delta") or {}

    check(r, "schema_shape",
          isinstance(calls, list) and isinstance(closing, dict)
          and isinstance(consensus, dict) and isinstance(delta, dict))
    if not isinstance(calls, list):
        return r

    # ── calls 通用断言 ──
    bad_target = [c.get("target") for c in calls
                  if c.get("target") not in fx["roster_names"]]
    check(r, "targets_in_roster", not bad_target, str(bad_target))

    bad_origin = [c.get("origin") for c in calls if c.get("origin") not in ORIGINS]
    check(r, "origin_enum", not bad_origin, str(bad_origin))

    actives = [c for c in calls if c.get("origin") == "active"]
    check(r, "active_budget", len(actives) <= 1, "active=%d" % len(actives))
    check(r, "active_has_reason",
          all(c.get("reason") in REASONS for c in actives))

    empty_prompt = [c.get("target") for c in calls
                    if not (isinstance(c.get("prompt"), str) and len(c["prompt"]) >= 200)]
    check(r, "prompt_substantial", not empty_prompt,
          "过短/缺失: %s" % empty_prompt)

    for c in calls:
        p = c.get("prompt") or ""
        check(r, "footer_contract:%s" % c.get("target"),
              all(m in p for m in FOOTER_MARKERS))

    # ── 与参照比对 ──
    got = sorted(c.get("target") or "" for c in calls)
    want = sorted(exp["targets"])
    check(r, "targets_match_reference", got == want,
          "got=%s want=%s" % (got, want))

    if exp["origin"] and calls:
        check(r, "origin_match_reference",
              all(c.get("origin") == exp["origin"] for c in calls),
              str([c.get("origin") for c in calls]))
    if exp["reasons"] and actives:
        check(r, "reason_match_reference",
              all(c.get("reason") in exp["reasons"] for c in actives),
              str([c.get("reason") for c in actives]))
    if exp["verbatim"] and calls:
        check(r, "verbatim_in_prompt",
              all(exp["verbatim"] in (c.get("prompt") or "") for c in calls))
    if exp["blind"] is not None and calls:
        check(r, "blind_flag",
              all(bool(c.get("blind")) == exp["blind"] for c in calls))
    if exp["prompts_must_differ"] and len(calls) >= 2:
        prompts = [c.get("prompt") or "" for c in calls]
        ratio = difflib.SequenceMatcher(None, prompts[0], prompts[1]).ratio()
        check(r, "bet_prompts_differ", ratio < 0.9, "similarity=%.2f" % ratio)

    # ── closing ──
    ctype, ctext = closing.get("type"), closing.get("text") or ""
    check(r, "closing_enum", ctype in CLOSING_TYPES, str(ctype))
    check(r, "closing_match_reference", ctype in exp["closing_types"],
          "got=%s want=%s" % (ctype, exp["closing_types"]))
    if ctype == "user_question":
        check(r, "closing_ends_question",
              ctext.rstrip().endswith(("?", "？")), ctext[-30:])
        hit = [w for w in FORBIDDEN_CLOSING if w in ctext]
        check(r, "closing_no_synthesis_words", not hit, str(hit))

    # ── consensus / delta ──
    check(r, "consensus_enum", consensus.get("verdict") in VERDICTS,
          str(consensus.get("verdict")))
    check(r, "delta_append_nonempty",
          isinstance(delta.get("append"), str) and delta["append"].strip() != "")
    return r


def run_case(fx, models, outdir, protocol, tag):
    prompt = protocol + "\n\n---\n\n# 本轮输入\n\n【名册】\n" + fx["roster"] + "\n\n" + fx["input"]
    pf = os.path.join(outdir, "%s.prompt.md" % fx["name"])
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write(prompt)

    args = [sys.executable, INVOKE] + ["%s:%s" % (m, pf) for m in models]
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=590)
    sections = {}
    for m in re.finditer(
            r"^===AGENT (\S+) (\S+) ([\d.]+)s===\n(.*?)(?=^===AGENT |\Z)",
            proc.stdout, re.M | re.S):
        sections[m.group(1)] = {
            "status": m.group(2), "wall": float(m.group(3)),
            "body": m.group(4).strip(),
        }

    out = {}
    for model in models:
        sec = sections.get(model)
        if sec is None or sec["status"] != "ok":
            out[model] = {
                "status": (sec or {}).get("status", "missing"),
                "wall": (sec or {}).get("wall", 0),
                "checks": [{"check": "agent_ok", "ok": False,
                            "note": (sec or {}).get("body", "")[:200]}],
                "plan": None,
            }
            continue
        plan = extract_json(sec["body"])
        raw_path = os.path.join(outdir, "%s.%s%s.json" % (fx["name"], model, tag))
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump({"raw": sec["body"], "plan": plan}, fh,
                      ensure_ascii=False, indent=2)
        out[model] = {"status": "ok", "wall": sec["wall"],
                      "checks": validate(plan, fx), "plan": plan}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude,codex")
    ap.add_argument("--cases", default="")
    ap.add_argument("--outdir", default=os.path.join(HERE, "out"))
    ap.add_argument("--tag", default="", help="附加在结果文件名上的标记(重复跑用)")
    opts = ap.parse_args()

    if not os.path.isfile(INVOKE):
        sys.stderr.write("找不到 invoke.py: %s\n" % INVOKE)
        return 2
    models = [m.strip() for m in opts.models.split(",") if m.strip()]
    wanted = {c.strip() for c in opts.cases.split(",") if c.strip()}
    fixtures = [f for f in FIXTURES
                if not wanted or f["name"] in wanted
                or f["name"].split("-")[0] in wanted]
    if not fixtures:
        sys.stderr.write("没有匹配的 case: %s\n" % opts.cases)
        return 2
    os.makedirs(opts.outdir, exist_ok=True)
    tag = (".%s" % opts.tag) if opts.tag else ""

    failed = 0
    summary = []
    for fx in fixtures:
        sys.stderr.write("── %s ──\n" % fx["name"])
        results = run_case(fx, models, opts.outdir, PROTOCOL, tag)
        for model, res in results.items():
            bad = [c for c in res["checks"] if not c["ok"]]
            failed += len(bad)
            n_ok = len(res["checks"]) - len(bad)
            line = "%-28s %-7s %5.1fs  %2d/%2d" % (
                fx["name"], model, res["wall"], n_ok, len(res["checks"]))
            if bad:
                line += "  FAIL: " + "; ".join(
                    "%s(%s)" % (c["check"], c["note"]) if c["note"] else c["check"]
                    for c in bad)
            summary.append(line)
            sys.stderr.write(line + "\n")

    report = os.path.join(opts.outdir, "summary%s.txt" % tag)
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(summary) + "\n")
    print("\n".join(summary))
    print("\n结果文件: %s" % opts.outdir)
    return 1 if failed else 0


with open(os.path.join(HERE, "moderator-prompt.md"), encoding="utf-8") as fh:
    PROTOCOL = fh.read()

if __name__ == "__main__":
    sys.exit(main())
