#!/usr/bin/env bash
# trundle-discuss —— 并行调用参与讨论的 agent
#
# 用法:
#   invoke.sh codex:/path/to/codex.md gemini:/path/to/gemini.md
#
# 每个参数是 <agent>:<提示文件>。提示文件由调用方(Claude)组装好,
# 内含该 agent 的固定站位、共识状态头、署名 transcript(见 references/prompt-kit.md)。
#
# 本脚本只负责:trust 前置检查、并行喷、超时、提取正文、如实汇报失败。
#
# 输出(供 Claude 解析,每个 agent 一段):
#   ===AGENT <name> <status> <wall>s===
#   <正文,或失败说明>
#
# status: ok | timeout | untrusted | ratelimited | error
#
# 新增一个 agent:除了在 agents.yaml 登记七字段,还要在下面 run_agent 的
# case 里加一个分支。详见 references/adapting-new-cli.md。

set -uo pipefail

TIMEOUT="${DISCUSSION_TIMEOUT:-120}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# 超时用 bash 原生看门狗实现,不依赖 GNU coreutils 的 timeout
# (macOS 自带的是 BSD 工具集,没有 timeout;为此让用户装 coreutils 不值得)。
# 调用处的重定向会作用于整个函数,所以子进程的 stdin/stdout 照常生效。
run_with_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  # 看门狗开枪时留个标记。不能只看退出码:实测 codex 会捕获 SIGTERM
  # 并优雅退出(exit 0),那样超时会被误判成"返回了空结果的失败"。
  local flag="$WORKDIR/.timedout.$pid"
  # TERM 之后再等 5s 补一发 KILL:实测有的 CLI 收到 TERM 后要磨蹭几秒,
  # 完全无视 TERM 的会把整轮拖死。
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null && : >"$flag"
    sleep 5; kill -KILL "$pid" 2>/dev/null ) &
  local watcher=$!
  local rc=0
  wait "$pid" 2>/dev/null || rc=$?
  kill -TERM "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null || true
  if [ -f "$flag" ]; then rm -f "$flag"; return 143; fi
  return "$rc"
}

# ── 失败分类 ─────────────────────────────────────────────────

is_rate_limited() {
  local msg; msg="$(tr '[:upper:]' '[:lower:]' <"$1" 2>/dev/null)"
  case "$msg" in
    *429*|*resource_exhausted*|*"quota exceeded"*|*"rate limit"*|\
    *"too many requests"*|*rate_limit_exceeded*) return 0 ;;
    *) return 1 ;;
  esac
}

# ── trust 前置检查 ───────────────────────────────────────────
# 不是可选的礼节。gemini 在未 trust 目录下会被降级到不稳定的模型分支
# (实测 8 请求 7 失败,墙钟 10x 恶化),所以宁可不喷也不绕过。

gemini_is_trusted() {
  local cfg="$HOME/.gemini/trustedFolders.json" dir="$PWD"
  [ -f "$cfg" ] || return 1
  # 用 grep -F 按字面量找,避免把路径当正则(也就不需要 jq)。
  # 该文件每行形如   "/some/path": "TRUST_FOLDER"
  # 整段吞掉 stderr:万一环境残缺(grep 都没有)也只是判定为「不信任」,
  # 安全侧失败 —— 宁可跳过 gemini,也不会误放行。
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if { grep -F "\"$dir\"" "$cfg" | grep -q 'TRUST_FOLDER'; } 2>/dev/null; then
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

in_git_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1
}

# ── 单个 agent 的调用 ────────────────────────────────────────
#
# 所有调用都必须 </dev/null:并行运行时 stdin 是不可读管道,
# 某些 CLI(实测 codex)会认为存在 piped 输入而去读它,然后失败
# (Failed to read prompt from stdin / os error 11)。

run_agent() {
  local name="$1" promptfile="$2"
  local out="$WORKDIR/$name.out" err="$WORKDIR/$name.err"
  local meta="$WORKDIR/$name.meta"
  local prompt; prompt="$(cat "$promptfile")"
  local start; start=$(date +%s)
  local rc=0

  case "$name" in
    codex)
      local flags=(--sandbox read-only)
      in_git_repo || flags+=(--skip-git-repo-check)
      run_with_timeout "$TIMEOUT" codex exec "${flags[@]}" "$prompt" \
        </dev/null >"$out" 2>"$err"
      rc=$?
      ;;

    gemini)
      if ! gemini_is_trusted; then
        # 明说,不静默降级,更不用环境变量绕过
        printf 'untrusted 0\n' >"$meta"
        cat >"$out" <<EOF
当前目录未被 gemini 信任,已跳过本次调用。

没有绕过是有意的:绕过会把模型路由降到不稳定的 preview 分支,
实测失败率 7/8、墙钟从 14s 恶化到 108-199s。

补救(任选其一):
  1. 在该目录交互式运行一次 gemini,选择信任该目录
  2. 在 ~/.gemini/trustedFolders.json 中加入:
       "$PWD": "TRUST_FOLDER"
EOF
        return
      fi
      # 不加 --output-format json:headless 下 -p 直接输出纯正文,
      # 走 json 还得靠 jq 把 .response 抠出来,白添一个依赖。
      run_with_timeout "$TIMEOUT" gemini --approval-mode plan \
        -p "$prompt" </dev/null >"$out" 2>"$err"
      rc=$?
      ;;

    claude)
      # 作为参与者的 claude 子进程。必须清掉 CLAUDECODE,
      # 否则它认为自己在嵌套 session 里而报错。
      run_with_timeout "$TIMEOUT" env -u CLAUDECODE claude \
        --allowedTools "Read,Glob,Grep" -p "$prompt" \
        </dev/null >"$out" 2>"$err"
      rc=$?
      ;;

    *)
      printf 'error 0\n' >"$meta"
      cat >"$out" <<EOF
未知 agent: $name

它没有登记在 agents.yaml 里,因此不会被调用 —— 猜测调用方式是危险的:
猜错只读 flag 会让它拿到写权限。接纳步骤见 references/adapting-new-cli.md。
EOF
      return
      ;;
  esac

  local wall=$(( $(date +%s) - start ))

  # 143 = 128+SIGTERM,看门狗超时杀掉;124/137 是外部 timeout 工具的约定,一并认
  if [ $rc -eq 143 ] || [ $rc -eq 124 ] || [ $rc -eq 137 ]; then
    printf 'timeout %s\n' "$wall" >"$meta"
    echo "超过 ${TIMEOUT}s 未返回,已中止。本轮缺少 $name 的发言。" >"$out"
  elif [ $rc -ne 0 ] && is_rate_limited "$err"; then
    printf 'ratelimited %s\n' "$wall" >"$meta"
    { echo "被限流,本轮缺席。可稍后重试。"; echo; tail -3 "$err"; } >"$out"
  elif [ $rc -ne 0 ] || [ ! -s "$out" ]; then
    printf 'error %s\n' "$wall" >"$meta"
    { echo "调用失败(exit=$rc)。stderr 末尾:"; tail -5 "$err"; } >"$out"
  else
    printf 'ok %s\n' "$wall" >"$meta"
  fi
}

# ── 并行喷 ───────────────────────────────────────────────────

[ $# -gt 0 ] || { echo "用法: invoke.sh <agent>:<提示文件> [...]" >&2; exit 1; }

names=()
for arg in "$@"; do
  name="${arg%%:*}"; promptfile="${arg#*:}"
  if [ "$name" = "$arg" ] || [ -z "$promptfile" ]; then
    echo "参数格式应为 <agent>:<提示文件>,收到: $arg" >&2; exit 1
  fi
  if [ ! -f "$promptfile" ]; then
    echo "提示文件不存在: $promptfile" >&2; exit 1
  fi
  names+=("$name")
  run_agent "$name" "$promptfile" &
done
wait

# ── 汇总 ─────────────────────────────────────────────────────

for name in "${names[@]}"; do
  meta="$WORKDIR/$name.meta"
  status=error; wall=0
  [ -f "$meta" ] && read -r status wall <"$meta"
  printf '===AGENT %s %s %ss===\n' "$name" "$status" "$wall"
  cat "$WORKDIR/$name.out" 2>/dev/null
  printf '\n'
done
