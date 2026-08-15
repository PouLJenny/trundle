#!/usr/bin/env bash
# trundle-discuss —— 安装
#
# 只做一件事:把本 skill 软链进 ~/.claude/skills/。
# 幂等,可重复运行。

set -euo pipefail

# -P 解软链,拿到仓库里的真实路径
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SKILL_NAME="$(basename "$SKILL_DIR")"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
TARGET="$CLAUDE_DIR/skills/$SKILL_NAME"

mkdir -p "$CLAUDE_DIR/skills"

if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  echo "✗ $TARGET 已存在且不是软链,请先自行处理" >&2
  exit 1
fi

ln -sfn "$SKILL_DIR" "$TARGET"
chmod 755 "$SKILL_DIR"/scripts/*.sh "$SKILL_DIR"/scripts/*.py

echo "✓ 已链接 $TARGET → $SKILL_DIR"
echo
echo "下一步:"
echo "  1. 跑 $SKILL_DIR/scripts/verify.sh 自检依赖"
echo "  2. 重启 Claude Code 会话(skill 不热加载)"
echo "  3. 用 /trundle-discuss <话题> 开始一场讨论"
