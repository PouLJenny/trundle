#!/usr/bin/env bash
# trundle-discuss —— 扫描本机可用的 agent CLI
#
# 只做存在性检查,不调用任何 CLI(可用性验证是选中之后的事)。
# 输出三段,供 Claude 渲染成选项给用户勾选。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
AGENTS_YAML="$SCRIPT_DIR/../agents.yaml"

# 适配库里已登记的 agent。只收实测跑通的——没验证过的条目是负债:
# 用户选了它、它不工作,体验比"没有这个选项"更差。
#
# 名单从 invoke.py 的 AGENTS 派生,不在这里抄第二份。以前这里硬编码一份、
# verify.sh 里两份,新增 agent 要人手同步三处 —— 漏一处的症状是「装了却不被
# 调用」或「自检说没装」,而且不报错。现在真值只有 AGENTS 一处。
# 不用 mapfile:macOS 自带的 bash 3.2 没有它。
REGISTERED=()
while IFS= read -r a; do
  [ -n "$a" ] && REGISTERED+=("$a")
done < <(python3 "$SCRIPT_DIR/invoke.py" --list-agents 2>/dev/null)

if [ "${#REGISTERED[@]}" -eq 0 ]; then
  echo "取不到已适配名单:python3 缺失,或 $SCRIPT_DIR/invoke.py 有问题。" >&2
  echo "先跑 $SCRIPT_DIR/verify.sh 看依赖。" >&2
  exit 1
fi

# PATH 里可能出现的其他 agent CLI —— 只用来提示用户"发现了但没登记",
# 绝不调用。猜错非交互 flag 只是挂掉,猜错只读 flag 会给它写权限。
#
# 注意这**不是** agents.yaml 末尾那份「还没适配的」名单的副本,两者不该对齐:
# 这里是「可能装在机器上、值得提一句」的探测清单,那里是「真的探过、结论是
# 什么」的记录。这里长、那里短是正常的。
KNOWN_UNREGISTERED=(opencode cline kiro-cli aider amp droid goose crush q copilot grok kilo continue)

# 必须是真实文件,不能是 shell builtin/alias/function。
# 实测 `command -v continue` 会命中 builtin,产生误报。
is_real_binary() {
  [ "$(type -t "$1" 2>/dev/null)" = "file" ]
}

echo "=== 已登记且本机可用 ==="
found=0
for cli in "${REGISTERED[@]}"; do
  if is_real_binary "$cli"; then
    found=$((found + 1))
    printf '%-12s %s\n' "$cli" "$(command -v "$cli")"
  fi
done
[ "$found" -eq 0 ] && echo "(无 —— 至少需要装一个 agent CLI 才能讨论)"

echo
echo "=== 已登记但本机没装 ==="
for cli in "${REGISTERED[@]}"; do
  is_real_binary "$cli" || printf '%s\n' "$cli"
done

echo
echo "=== 发现但未登记(不会被调用) ==="
unreg=0
for cli in "${KNOWN_UNREGISTERED[@]}"; do
  if is_real_binary "$cli"; then
    unreg=$((unreg + 1))
    printf '%-12s %s\n' "$cli" "$(command -v "$cli")"
  fi
done
if [ "$unreg" -gt 0 ]; then
  echo
  echo "要接纳它们,需先确认非交互 flag 与只读 flag 并登记进 agents.yaml。"
  echo "步骤见 references/adapting-new-cli.md —— 只读 flag 必须实测,不能猜。"
else
  echo "(无)"
fi

echo
echo "适配库: $AGENTS_YAML"
