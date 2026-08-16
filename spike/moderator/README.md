# moderator 活体一致性工具

始于「要不要拆 moderator」的一次性实验(spike),现在是**活体一致性工具**:
CI 只能跑罐头 fixture(selftest),这里跑真 CLI——验证「将要发布的协议 +
校验器」在真实模型上仍产出与参照一致的 round plan。改协议或 plan_check
后手动跑一遍,结果附在 PR 里(CONTRIBUTING 有此要求)。

背景与裁决见 [results.md](results.md);它裁决的讨论在
`.claude/trundle-discuss/2026-08-16-multi-host.md`。协议 v0 已删,
在本分支首个 commit 的 git 历史里。

```bash
python3 spike/moderator/run_spike.py                  # 全部 6 case,claude+codex
python3 spike/moderator/run_spike.py --models codex   # 只跑默认 moderator
python3 spike/moderator/run_spike.py --cases f2,f4 --tag r2   # 稳定性复跑
```

| 文件 | 是什么 |
|---|---|
| `run_spike.py` | 跑 fixture → 并行调 moderator 模型 → 契约(plan_check)+ 参照断言 |
| `fixtures/cases.py` | 6 个真实回合 fixture,含「host Claude 当时实际怎么做」参照 |
| `results.md` | spike 阶段的结果与裁决建议(历史记录) |
| `out/` | 运行产物(gitignore) |

协议正文在 `skills/discuss/protocol/moderator.md`(`--protocol` 可指别的);
内在契约校验在 `skills/discuss/scripts/plan_check.py`——两者都不在本目录,
这里没有任何会漂移的副本。
