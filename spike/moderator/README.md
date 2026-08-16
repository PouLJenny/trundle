# moderator spike

验证「把 moderator 从 host 会话里拆成独立 agent」是否可行的一次性实验。
背景与结论见 [results.md](results.md);它裁决的讨论在
`.claude/trundle-discuss/2026-08-16-multi-host.md`。

| 文件 | 是什么 |
|---|---|
| `moderator-prompt.md` | 协议 v0:SKILL.md 裁量散文的 host 无关蒸馏 + round plan schema |
| `fixtures/cases.py` | 6 个真实回合 fixture,含参照答案 |
| `run_spike.py` | 跑 fixture → 并行调 moderator 模型 → 机械断言 |
| `results.md` | 结果与裁决建议 |
| `out/` | 运行产物(gitignore) |

这是 spike,不是产品代码:它回答「该不该做、缺什么」,不是最终实现。
v1 落地时协议正文应迁往正式位置,本目录整体可删。
