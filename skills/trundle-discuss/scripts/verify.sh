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
# 本 skill 刻意不依赖 jq / GNU timeout —— 这些在 macOS 上都要额外装。
# 下面几个是 POSIX 标准命令,任何 Linux/macOS 都自带,检查只为兜底。
for b in sleep mktemp date grep; do
  command -v "$b" >/dev/null 2>&1 && ok "$b" || bad "$b 缺失(这不该发生)"
done

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

echo
if [ "$fail" -gt 0 ]; then
  echo "✗ $fail 项必需依赖缺失,补齐后再用。"
  exit 1
elif [ "$warn" -gt 0 ]; then
  echo "✓ 可用($warn 项提醒,见上)"
else
  echo "✓ 全部通过"
fi
