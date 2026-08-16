#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""moderator 活体一致性 runner —— 项目的行为级测试层

两层测试各管一段:CI 里的 selftest 是罐头 fixture,验「校验器认不认得违规」;
本文件调**真实 CLI**,验「真实模型按当前协议产不产得出与参照一致的计划」。
对每个 fixture(取自真实 transcript 的真实回合):
  1. 协议(默认:skills/discuss/protocol/moderator.md,即**将要发布的**那份)
     + fixture 输入 → 完整 prompt
  2. 经 invoke.py 并行喂给多个 moderator 模型
  3. 内在契约用 scripts/plan_check.py(与运行时同一份实现);
     参照比对(「host Claude 当时实际怎么做」)是测试专属断言,留在本文件

CI 不跑它(要调真 CLI);改协议或校验器后手动跑,附在 PR 里(CONTRIBUTING
有此要求)。前身是 moderator spike 的 runner,裁决记录见 spike-results.md;
协议 v0 与当时的 runner 在 git 历史里。

用法:
  python3 run_live.py [--models claude,codex] [--cases f1,f3] [--protocol PATH]
                      [--outdir DIR] [--tag N]

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
SCRIPTS = os.path.join(REPO, "skills", "discuss", "scripts")
INVOKE = os.path.join(SCRIPTS, "invoke.py")
DEFAULT_PROTOCOL = os.path.join(REPO, "skills", "discuss", "protocol", "moderator.md")

sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(HERE, "fixtures"))
import plan_check   # noqa: E402
from cases import FIXTURES  # noqa: E402


def check(results, name, ok, note=""):
    results.append({"check": name, "ok": bool(ok), "note": note})


def validate_reference(plan, fx):
    """实验专属:与「host Claude 当时实际怎么做」比对。内在契约不在这里
    (plan_check.validate_plan 管),两边不重复。"""
    r = []
    exp = fx["expected"]
    calls = plan.get("calls") or []

    got = sorted(c.get("target") or "" for c in calls)
    want = sorted(exp["targets"])
    check(r, "targets_match_reference", got == want,
          "got=%s want=%s" % (got, want))

    if exp["origin"] and calls:
        check(r, "origin_match_reference",
              all(c.get("origin") == exp["origin"] for c in calls),
              str([c.get("origin") for c in calls]))
    actives = [c for c in calls if c.get("origin") == "active"]
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
        check(r, "bet_prompts_differ_reference", ratio < 0.9,
              "similarity=%.2f" % ratio)

    closing = plan.get("closing") or {}
    check(r, "closing_match_reference",
          closing.get("type") in exp["closing_types"],
          "got=%s want=%s" % (closing.get("type"), exp["closing_types"]))
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
        plan = plan_check.extract_json(sec["body"])
        raw_path = os.path.join(outdir, "%s.%s%s.json" % (fx["name"], model, tag))
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump({"raw": sec["body"], "plan": plan}, fh,
                      ensure_ascii=False, indent=2)
        checks = [{"check": c["check"], "ok": c["ok"], "note": c["note"]}
                  for c in plan_check.validate_plan(plan, fx["roster_names"])]
        if plan is not None:
            checks += validate_reference(plan, fx)
        out[model] = {"status": "ok", "wall": sec["wall"],
                      "checks": checks, "plan": plan}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude,codex")
    ap.add_argument("--cases", default="")
    ap.add_argument("--protocol", default=DEFAULT_PROTOCOL)
    ap.add_argument("--outdir", default=os.path.join(HERE, "out"))
    ap.add_argument("--tag", default="", help="附加在结果文件名上的标记(重复跑用)")
    opts = ap.parse_args()

    if not os.path.isfile(INVOKE):
        sys.stderr.write("找不到 invoke.py: %s\n" % INVOKE)
        return 2
    if not os.path.isfile(opts.protocol):
        sys.stderr.write("找不到协议文件: %s\n" % opts.protocol)
        return 2
    with open(opts.protocol, encoding="utf-8") as fh:
        protocol = fh.read()

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
        results = run_case(fx, models, opts.outdir, protocol, tag)
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


if __name__ == "__main__":
    sys.exit(main())
