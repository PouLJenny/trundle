# tests/live —— 行为级活体测试

本项目的测试分两层:

| 层 | 在哪 | 测什么 | 谁跑 |
|---|---|---|---|
| 罐头层 | `skills/discuss/scripts/selftest.py` | 校验器认不认得违规、协议与代码有没有漂移(零 CLI 调用) | CI,每次 PR |
| **活体层** | 本目录 `run_live.py` | 真实模型按当前协议产不产得出与参照一致的 round plan | **手动**,改协议/校验器时(CONTRIBUTING 有此要求) |

活体层不进 CI 是刻意的:它要调真 CLI、花真钱、依赖外部状态(配额窗口),
进 CI 会时灵时不灵——那比没有测试更糟(selftest 的 docstring 里有这条教训的来历)。

```bash
python3 tests/live/run_live.py --models codex        # 默认 moderator,最快
python3 tests/live/run_live.py                       # claude+codex 双模型对照
python3 tests/live/run_live.py --cases f2,f4 --tag r2   # 微妙 case 稳定性复跑
```

| 文件 | 是什么 |
|---|---|
| `run_live.py` | 跑 fixture → 并行调 moderator 模型 → 契约(plan_check)+ 参照断言 |
| `fixtures/cases.py` | 6 个真实回合 fixture,含「host Claude 当时实际怎么做」参照 |
| `spike-results.md` | 前身 moderator spike 的裁决记录(历史档案,数字对应协议 v0) |
| `out/` | 运行产物(gitignore) |

协议正文在 `skills/discuss/protocol/moderator.md`(`--protocol` 可指别的);
契约校验在 `skills/discuss/scripts/plan_check.py`——都不在本目录,
这里没有任何会漂移的副本。给新 host 升级「行为已验证」资格,也是在这里
加该 host 的驱动(见 `skills/discuss/references/hosts.md`)。
