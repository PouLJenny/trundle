#!/usr/bin/env bash
# trundle-discuss —— 依赖自检
#
# 故意不用 set -e:要跑完所有检查再统一报结果。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
fail=0
warn=0

ok()   { printf '  ✓ %s\n' "$1"; }
bad()  { printf '  ✗ %s\n' "$1"; fail=$((fail + 1)); }
note() { printf '  ! %s\n' "$1"; warn=$((warn + 1)); }

echo "── 基础工具 ──"
# 本 skill 刻意不依赖 jq —— json 在 Python 标准库里,再装一个 jq 是白付依赖。
# 下面几个是 POSIX 标准命令,任何 Linux/macOS 都自带,检查只为兜底。
for b in sleep mktemp date grep; do
  command -v "$b" >/dev/null 2>&1 && ok "$b" || bad "$b 缺失(这不该发生)"
done

# invoke 的实现在 invoke.py,这是唯一的硬依赖
if ! command -v python3 >/dev/null 2>&1; then
  bad "python3 缺失 —— invoke.py 跑不起来"
  echo "      macOS : xcode-select --install(系统不预装 python3)"
  echo "      Debian/Ubuntu : sudo apt install python3"
  echo "      Arch  : sudo pacman -S python"
elif ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
  bad "python3 版本过低(需 >= 3.8):$(python3 -V 2>&1)"
else
  ok "python3 ($(python3 -V 2>&1 | cut -d' ' -f2))"
fi

command -v git >/dev/null 2>&1 && ok "git" || note "git 缺失 —— codex 的 git repo 检查会走 fallback"

echo
echo "── 参与者 CLI(至少需要一个)──"

agents_found=0
for cli in codex gemini claude; do
  if [ "$(type -t "$cli" 2>/dev/null)" = "file" ]; then
    agents_found=$((agents_found + 1))
    ok "$cli"
  fi
done

if [ "$agents_found" -eq 0 ]; then
  bad "一个 agent CLI 都没装 —— 目前已适配:codex / gemini / claude"
fi

echo
echo "── gemini 信任目录 ──"

if [ "$(type -t gemini 2>/dev/null)" = "file" ]; then
  cfg="$HOME/.gemini/trustedFolders.json"
  trusted=0
  if [ -f "$cfg" ]; then
    dir="$PWD"
    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
      if { grep -F "\"$dir\"" "$cfg" | grep -q 'TRUST_FOLDER'; } 2>/dev/null; then
        trusted=1; break
      fi
      dir="$(dirname "$dir")"
    done
  fi
  if [ "$trusted" -eq 1 ]; then
    ok "当前目录已被信任($dir)"
  else
    note "当前目录未被 gemini 信任 —— gemini 会跳过发言"
    echo "      补救:在该目录交互式跑一次 gemini 并选择信任,或在"
    echo "      $cfg 中加入  \"$PWD\": \"TRUST_FOLDER\""
    echo "      不要用环境变量绕过:那会把模型路由降级,延迟涨约 10 倍"
  fi
else
  ok "未安装 gemini,跳过"
fi

echo
echo "── 脚本自身 ──"

for s in invoke.sh discover.sh install.sh verify.sh; do
  p="$SCRIPT_DIR/$s"
  if [ ! -f "$p" ]; then
    bad "$s 不存在"
  elif ! bash -n "$p" 2>/dev/null; then
    bad "$s 语法错误"
  elif [ ! -x "$p" ]; then
    note "$s 不可执行(chmod 755 $p)"
  else
    ok "$s"
  fi
done

p="$SCRIPT_DIR/invoke.py"
if [ ! -f "$p" ]; then
  bad "invoke.py 不存在 —— invoke.sh 只是转调 wrapper,没有它什么都跑不了"
elif ! command -v python3 >/dev/null 2>&1; then
  note "invoke.py 语法未检查(没有 python3)"
elif ! python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$p" 2>/dev/null; then
  bad "invoke.py 语法错误"
else
  ok "invoke.py"
fi

echo
echo "── 自检(纯函数,不调用任何 agent CLI)──"

p="$SCRIPT_DIR/selftest.py"
if [ ! -f "$p" ]; then
  note "selftest.py 不存在,跳过"
elif ! command -v python3 >/dev/null 2>&1; then
  note "自检跳过(没有 python3)"
elif out="$(python3 "$p" 2>&1)"; then
  ok "$(printf '%s' "$out" | tail -1 | sed 's/^✓ //')"
else
  bad "自检失败"
  printf '%s\n' "$out" | sed 's/^/      /'
fi

echo
if [ "$fail" -gt 0 ]; then
  echo "✗ $fail 项必需依赖缺失,补齐后再用。"
  exit 1
elif [ "$warn" -gt 0 ]; then
  echo "✓ 可用($warn 项提醒,见上)"
else
  echo "✓ 全部通过"
fi
