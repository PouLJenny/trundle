#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trundle discuss —— moderator 引擎

每轮讨论,壳(host)把本轮输入写进一个文件,调本脚本;本脚本负责:
选 moderator 模型 → 组装协议+输入 → 经 invoke.py 调用 → 校验 round plan
(plan_check.py)→ hard 失败则带反馈重试一次 → 输出计划与可直接执行的
invoke.sh 命令行。壳照计划机械执行,不补任何判断。

用法:
    moderate.py --input <本轮输入文件> [--roster PATH] [--moderator NAME] [--outdir DIR]

输入文件由壳组装,五段(见 ../protocol/moderator.md 的「输入」):
【名册】【共识状态头】【讨论记录】【主持人立场】【用户本轮消息】

stdout(供壳解析,与 invoke.py 的 ===分段=== 约定同风格):
    ===PLAN ok moderator=<名> retry=<0|1> <wall>s===
    <校验通过的 plan JSON,pretty-print>
    ===EXEC===
    <invoke.sh 命令行,或「本轮无外部调用」>
    ===WARN===
    <soft 检查未过项,逐行;全过则省略此段>

退出码:0 计划可执行;1 moderator 缺席或两次都产不出合法计划(壳走降级
模式:自己读协议在脑内主持);2 基建错误(协议文件缺失、参数错)。

★ 全程不 chdir、subprocess 不传 cwd ★ —— gemini 的信任目录 precheck 判的
是**当前目录**(verify.sh 里那条星号警告约束的就是这类代码):在这里换目录,
自检会绿着灯而讨论时 gemini 凭空缺席,是这个仓库公认最难查的失败模式。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import invoke        # noqa: E402  (sibling,同 selftest 的引法)
import plan_check    # noqa: E402

DEFAULT_MODERATOR = "codex"
DEFAULT_ROSTER = os.path.expanduser("~/.claude/trundle-discuss/roster.yaml")
PROTOCOL = os.path.join(HERE, "..", "protocol", "moderator.md")

# 总预算:必须砍在 Bash 工具 600s 上限之前,与 invoke.py 的 540 同哲学。
# 重试共享同一预算——第二次调用的窗口 = 590 - 已耗,低于下限就不重试。
TOTAL_BUDGET = 590
RETRY_FLOOR = 60

AGENT_RE = re.compile(
    r"^===AGENT (\S+) (\S+) ([\d.]+)s===\n(.*?)(?=^===AGENT |\Z)", re.M | re.S)


def parse_moderator_field(path):
    """从用户名册抽顶层 `moderator: <名>`。窄正则,不当 YAML 解析器
    (selftest 的 _yaml_agents 同款哲学)。文件/字段缺失 → None。
    绝不改写用户文件。"""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^moderator:\s*(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _installed(name):
    return shutil.which(name) is not None


def _precheck_ok(name):
    spec = invoke.AGENTS.get(name)
    if spec is None:
        return False
    precheck = spec.get("precheck")
    return (precheck() if precheck else None) is None


def pick_moderator(explicit):
    """(名字, 警告行列表) 或 (None, 警告)。

    显式指定(roster 或 --moderator)必须已登记且已安装,否则**响亮**降级
    进回退链——静默换人会让用户以为在用自己选的模型。
    回退链:codex → AGENTS 登记顺序里第一个已安装且 precheck 通过的。
    precheck 不过的候选跳过(gemini 未信任目录时绝不能被回退选中)。
    """
    warns = []
    if explicit:
        if explicit not in invoke.AGENTS:
            warns.append("moderator=%s 未在适配库登记,忽略并走回退链"
                         "(登记名单见 invoke.py --list-agents)" % explicit)
        elif not _installed(explicit):
            warns.append("moderator=%s 未安装(PATH 里找不到),走回退链" % explicit)
        elif not _precheck_ok(explicit):
            warns.append("moderator=%s precheck 未通过(如 gemini 未信任目录),走回退链"
                         % explicit)
        else:
            return explicit, warns

    chain = [DEFAULT_MODERATOR] + [n for n in invoke.AGENTS
                                   if n != DEFAULT_MODERATOR]
    for name in chain:
        if _installed(name) and _precheck_ok(name):
            if explicit or name != DEFAULT_MODERATOR:
                warns.append("moderator 回退为 %s" % name)
            return name, warns
    warns.append("没有任何已登记 CLI 可当 moderator,进入降级模式")
    return None, warns


def roster_names_from_input(text):
    """从输入的【名册】块抽参与者名。只认 `- 名字` 行,读到下一个【段】为止。

    与 invoke.AGENTS 求交集:未登记的名字进了名册也绝不会被执行——
    猜错只读约束会给它写权限,这是铁律的最后一道闸。"""
    names, in_roster = [], False
    for line in text.splitlines():
        if line.startswith("【名册】"):
            in_roster = True
            continue
        if in_roster and line.startswith("【"):
            break
        if in_roster:
            m = re.match(r"^\s*-\s*([A-Za-z][\w-]*)", line)
            if m:
                names.append(m.group(1))
    registered = [n for n in names if n in invoke.AGENTS]
    dropped = [n for n in names if n not in invoke.AGENTS]
    return registered, dropped


def call_moderator(name, prompt_path, timeout):
    """经 invoke.py 调一次。stderr 不捕获(··· 进度行直接流到用户终端)。"""
    argv = [sys.executable, os.path.join(HERE, "invoke.py"),
            "%s:%s" % (name, prompt_path)]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "调用超出本轮总预算(%ds)" % timeout
    for m in AGENT_RE.finditer(proc.stdout or ""):
        if m.group(1) == name:
            return {"status": m.group(2), "wall": float(m.group(3)),
                    "body": m.group(4).strip()}, None
    return None, "invoke.py 输出里没有 %s 的分段(exit=%s)" % (name, proc.returncode)


def emit_fail(reason, code):
    sys.stdout.write("===PLAN fail===\n%s\n" % reason)
    sys.stdout.flush()
    return code


def run_round(opts):
    if not os.path.isfile(PROTOCOL):
        return emit_fail("协议文件缺失: %s" % PROTOCOL, 2)
    try:
        with open(opts.input, encoding="utf-8") as fh:
            round_input = fh.read()
    except OSError as exc:
        return emit_fail("读不了输入文件: %s" % exc, 2)

    with open(PROTOCOL, encoding="utf-8") as fh:
        protocol = fh.read()

    roster_names, dropped = roster_names_from_input(round_input)
    if dropped:
        sys.stderr.write("··· 名册里有未登记的名字,已忽略(绝不调用): %s\n"
                         % ", ".join(dropped))
    if not roster_names:
        return emit_fail("输入的【名册】块里没有任何已登记的参与者", 2)

    explicit = opts.moderator or parse_moderator_field(opts.roster)
    moderator, warns = pick_moderator(explicit)
    for w in warns:
        sys.stderr.write("··· %s\n" % w)
    if moderator is None:
        return emit_fail("\n".join(warns) or "无可用 moderator", 1)

    outdir = opts.outdir or tempfile.mkdtemp(prefix="trundle-round-")
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    started = time.monotonic()
    prompt = protocol + "\n\n---\n\n# 本轮输入\n\n" + round_input
    plan, raw, wall, notes = None, "", 0.0, []

    for attempt in (0, 1):
        remaining = TOTAL_BUDGET - (time.monotonic() - started)
        if attempt and remaining < RETRY_FLOOR:
            notes.append("预算不足 %ds,放弃重试" % RETRY_FLOOR)
            break
        pf = os.path.join(outdir, "moderator-round%s.md"
                          % (".retry" if attempt else ""))
        with open(pf, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        res, err = call_moderator(moderator, pf, max(int(remaining), 1))
        if err or res["status"] != "ok":
            notes.append(err or "moderator 调用 %s:\n%s"
                         % (res["status"], res["body"]))
            break                     # 调用层失败(超时/限流/未装)重试无益
        raw, wall = res["body"], res["wall"]
        plan = plan_check.extract_json(raw)
        results = plan_check.validate_plan(plan, roster_names)
        hard = plan_check.hard_failures(results)
        if not hard:
            soft = plan_check.soft_failures(results)
            return emit_plan(plan, moderator, attempt, wall, soft, outdir)
        notes.append("第 %d 次输出未过校验: %s"
                     % (attempt + 1, ", ".join(c["check"] for c in hard)))
        if attempt == 0:
            prompt = (protocol + "\n\n---\n\n# 本轮输入\n\n" + round_input
                      + "\n\n" + plan_check.build_retry_feedback(raw, hard))

    return emit_fail("moderator(%s)没能产出合法计划:\n%s"
                     % (moderator, "\n".join(notes)), 1)


def emit_plan(plan, moderator, retry, wall, soft, outdir):
    out = sys.stdout
    out.write("===PLAN ok moderator=%s retry=%d %.1fs===\n"
              % (moderator, retry, wall))
    out.write(json.dumps(plan, ensure_ascii=False, indent=2))
    out.write("\n===EXEC===\n")
    calls = plan.get("calls") or []
    if calls:
        args = []
        for c in calls:
            path = os.path.join(outdir, "%s.md" % c["target"])
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(c["prompt"])
            args.append("%s:%s" % (c["target"], path))
        out.write("%s %s\n" % (os.path.join(HERE, "invoke.sh"), " ".join(args)))
    else:
        out.write("本轮无外部调用\n")
    if soft:
        out.write("===WARN===\n")
        for c in soft:
            out.write("%s%s\n" % (c["check"],
                                  ((" · %s" % c["note"]) if c.get("note") else "")))
    out.flush()
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="本轮输入文件(五段)")
    ap.add_argument("--roster", default=DEFAULT_ROSTER)
    ap.add_argument("--moderator", default=None,
                    help="覆盖名册里的 moderator 字段(调试用)")
    ap.add_argument("--outdir", default=None,
                    help="prompt 与计划落盘目录,默认临时目录")
    return run_round(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
