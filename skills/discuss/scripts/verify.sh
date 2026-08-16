#!/usr/bin/env bash
# trundle discuss —— 依赖自检
#
# 故意不用 set -e:要跑完所有检查再统一报结果。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
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

# 名单从 invoke.py 的 AGENTS 派生,不在这里再抄一份。以前这个文件里有两份
# (循环一份、下面的提示文案一份)、discover.sh 里还有第三份 —— 新增 agent
# 要人手同步三处,漏一处的症状是「装了却不被调用」或「自检说没装」,不报错。
# 不用 mapfile:macOS 自带的 bash 3.2 没有它。
REGISTERED=()
while IFS= read -r a; do
  [ -n "$a" ] && REGISTERED+=("$a")
done < <(python3 "$SCRIPT_DIR/invoke.py" --list-agents 2>/dev/null)

if [ "${#REGISTERED[@]}" -eq 0 ]; then
  bad "取不到已适配名单(invoke.py --list-agents 没有输出)"
fi

agents_found=0
for cli in "${REGISTERED[@]}"; do
  if [ "$(type -t "$cli" 2>/dev/null)" = "file" ]; then
    agents_found=$((agents_found + 1))
    ok "$cli"
  fi
done

if [ "$agents_found" -eq 0 ] && [ "${#REGISTERED[@]}" -gt 0 ]; then
  bad "一个 agent CLI 都没装 —— 目前已适配:$(printf '%s / ' "${REGISTERED[@]}" | sed 's, / $,,')"
fi

echo
echo "── agent 前置检查(信任目录等)──"

# 这一段以前是 gemini 专属的,自己用 grep 管道判信任目录。那份实现与
# invoke.py 的 gemini_is_trusted() 实测有六处行为分歧:跨行 JSON、根目录 /
# 是否被检查、值是精确相等还是含子串、$PWD vs 解过软链的 getcwd、$HOME vs
# expanduser、损坏 JSON 怎么算。最糟的一个方向是自检报「已信任」而讨论时
# gemini 被跳过 —— 绿灯自检加凭空缺席,最难查。
#
# 现在转调 invoke.py --precheck,只有 Python 那一套实现,而且对所有 agent
# 通用(precheck 本来就是 AGENTS 里的通用钩子),不必为每个有门禁的 agent
# 在这里再加一段。
#
# ★ 不要先 cd ★ precheck 判的是「当前目录」,cd 到脚本目录再调就判错对象了。
for cli in ${REGISTERED[@]+"${REGISTERED[@]}"}; do
  [ "$(type -t "$cli" 2>/dev/null)" = "file" ] || continue
  if out="$(python3 "$SCRIPT_DIR/invoke.py" --precheck "$cli" 2>/dev/null)"; then
    ok "$cli"
  else
    note "$cli 前置检查未通过 —— 讨论时它会被跳过"
    printf '%s\n' "$out" | sed 's/^/      /'
  fi
done

echo
echo "── dsh 只读模式 ──"

if [ "$(type -t dsh 2>/dev/null)" = "file" ]; then
  # dsh 没有只读 flag,只读靠 DSH_PERMISSION_MODE,而它的 headless profile
  # 默认值是 workspace-write(**默认可写**)。invoke.py 每次调用都强制覆盖成
  # read-only,所以这里不会失败 —— 这一段只是把"为什么你环境里的设置不生效"
  # 提前说清楚,免得有人以为是 bug。
  if [ -n "${DSH_PERMISSION_MODE:-}" ] && [ "$DSH_PERMISSION_MODE" != "read-only" ]; then
    note "你的环境里 DSH_PERMISSION_MODE=$DSH_PERMISSION_MODE,讨论时会被强制改为 read-only"
    echo "      辅助 agent 全程只读是铁律,不随环境变量放宽(你手动跑 dsh 不受影响)"
  else
    ok "dsh(只读由 DSH_PERMISSION_MODE=read-only 强制)"
  fi
else
  ok "未安装 dsh,跳过"
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
