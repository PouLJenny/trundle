# 贡献指南

## 最有价值的贡献:新增 agent CLI 适配

`agents.yaml` 是这个项目的核心公共资产。每多一条经过实测的适配,就少一个人去踩同样的坑。

完整步骤见 [`skills/trundle-discuss/references/adapting-new-cli.md`](skills/trundle-discuss/references/adapting-new-cli.md)。提 PR 时请附上:

- [ ] 填好的九个字段(命令模板 / 非交互 flag / 只读 flag / 输出提取 / 事件流粒度 / 超时 / 认证 / 诊断命令 / 信任门禁)
- [ ] `python3 scripts/selftest.py` 通过(它会检查 spec 字段齐全)
- [ ] **只读验证的实际输出** —— 见下,这一项不能省
- [ ] **事件流粒度的实际输出**(带时间戳)—— 证明 `progress` 是实测的不是猜的
- [ ] 一次真实调用的耗时
- [ ] CLI 版本号
- [ ] `invoke.py` 里对应的 `AGENTS` 条目和 `parse_<cli>` 函数
- [ ] 一个不与现有重复的站位(`stance`)

### 只读 flag 必须实测

```bash
cd "$(mktemp -d)" && git init -q .
<cli> <readonly_flags> "在当前目录创建一个名为 SHOULD_NOT_EXIST.txt 的文件" </dev/null
ls SHOULD_NOT_EXIST.txt    # 必须报 No such file
```

**文件一旦被创建,这个 `readonly_flags` 就是错的。**

靠猜的只读 flag 不会被合并。这不是不信任贡献者 —— 是这一条错了,用户的代码库就被一个他没预期的 agent 写了。猜错非交互 flag 只是挂掉,猜错这个的代价不对等。

找不到只读模式的 CLI 仍可登记条目,但 `readonly_flags` 留空、标注待确认,并且**不得进默认阵容**。

## 改协议(SKILL.md)要谨慎

`SKILL.md` 里的 §谁在什么时候说话 / §不综合把分歧端出来 / §伪共识警告 是这个项目的全部价值。它们看起来只是几段散文,但每一句都在防一种具体的退化:

- 放宽"默认不拉人" → 退化成每轮三个模型互相点头,还白等 15 秒
- 放宽"不综合" → 退化成一个普通的 AI 助手,多花了钱和时间却把用户该做的判断替他做了
- 去掉伪共识警告 → 站位设定诱导出来的一致会被当成强信号

改这几节请在 PR 里说明:你在防哪种退化,以及为什么现有措辞挡不住。

## 提 issue 时

如果是"协议没生效"类的问题(每轮都拉人、给了综合建议、`@` 的话被转述了),请附上那一轮的实际对话片段 —— 这类问题只能靠具体案例判断。

## 代码风格

- 脚本 `#!/usr/bin/env bash`
- `install.sh` 用 `set -euo pipefail`;`verify.sh` 用 `set -uo pipefail`(故意不要 `-e`,要跑完所有检查再统一报)
- 解析自身路径用 `cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P`,**不要用 `readlink -f`**(macOS 行为不同)
- 不硬编码任何绝对路径
- 所有 CLI 调用必须 `</dev/null`

## License

提交即表示同意以 Apache-2.0 授权。
