#!/usr/bin/env bash
# trundle-discuss —— 并行调用参与讨论的 agent
#
# 用法:
#   invoke.sh codex:/path/to/codex.md gemini:/path/to/gemini.md
#
# 实现全在 invoke.py。本文件只做运行时检测 + 转调,保留原文件名是为了
# SKILL.md / references/ / 用户已有习惯里的路径都不用改。
#
# 为什么是 Python:超时要按「吐字间隔」判定、要并发读多路事件流、要杀干净
# 整个进程组 —— 这三件事 bash 干得很勉强(旧版 kill -TERM 单个 pid 就杀不掉
# codex spawn 出来的孙进程)。json 在 Python 标准库里,所以这一换反而少了一个
# 依赖:不再需要 jq。详见 references/invocation.md。

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  cat >&2 <<'EOF'
trundle-discuss 需要 python3 (>= 3.8),当前 PATH 里找不到它。

  macOS : xcode-select --install   (系统不预装 python3,/usr/bin/python3 只是个跳板)
  Debian/Ubuntu : sudo apt install python3
  Arch  : sudo pacman -S python
EOF
  exit 127
fi

exec python3 "$DIR/invoke.py" "$@"
