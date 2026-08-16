# trundle

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

> **trundle-discuss** —— 把 Claude Code 的单人对话变成回合制群聊。
> 让 codex、gemini 等 CLI agent 加入你正在进行的技术讨论,**而你是参与者,不是等报告的人**。

**Claude Code 是主持人,你装的其他 CLI agent 是被拉进来的参与者。**

> **English**: A Claude Code skill that turns your solo conversation into a turn-based group discussion with codex / gemini / other CLI agents. You stay in the discussion as a participant — it does not run an unattended debate and hand you a report. Docs are in Chinese.

---

## 这是什么

与其描述,不如看一个真实回合:

```
你:那计费周期用自然月还是订阅日?

Claude:(直接回答,没有拉任何人)

你:那就按比例退款吧,简单

Claude:我倾向同意,但这个判断压在「退款笔数不大」上,我没验证过。
       让 codex 从实现代价的角度挑一下。

  codex · 8.2s
  按比例退款的实现代价不在计算,在对账。每笔退款都会产生一条渠道手续费记录……

    立场:先确认退款笔数量级,再决定要不要走代金券
    不同意:Claude 说「实现简单」—— 前提是不用跟支付渠道对账
    如果我错了:如果渠道手续费可以不退,那确实简单

Claude:codex 不同意我的方案。分歧不在实现,在**要不要做这件事**。

       我假设退款是低频的,所以按比例算够用;codex 假设你们接了多个
       支付渠道、每笔退款都要对账,那手续费会让「按比例」算不平。

       **我们的分歧其实是:你们的退款要不要跟渠道对账?**
       如果要,我这个方案不成立。

─── 讨论模式 · codex + gemini · 第 3 轮 · 未决 2 ───
```

注意最后一段:**Claude 没有给「综合考虑建议 A」,而是把分歧还原成一个只有你能回答的问题。** 这是本项目的核心。

## 核心设计

这四条是这个项目全部的价值所在:

- **默认没人说话。** 一场 10 轮讨论里主动拉人只该发生 1–2 次。每轮都喊人来发言会退化成三段互相点头的废话,还白等 15 秒。
- **不综合,把分歧端出来。** Claude 是当事方不是裁判。分歧未解决时说"综合考虑建议 A"是违规行为 —— 那等于既当选手又当裁判,而你丢掉了本该由你做的那次判断。
- **让 agent 互相听见。** 喂给每个 agent 的上下文带其他 agent 上一轮的发言**并且署名**。这是"三份独立 review"变成"讨论"的唯一开关 —— 有署名才会产生指向性反驳,没署名就只会各说各的。
- **一致 ≠ 正确。** 固定站位会制造伪共识:给 gemini 设"盯是不是解错了问题"的站位,它几乎必然说"你们解错了问题"。所有人一致时,先怀疑是提问方式把答案喂了进去。

## 安装

```bash
git clone https://github.com/PouLJenny/trundle.git
cd trundle
./skills/trundle-discuss/scripts/install.sh    # 软链进 ~/.claude/skills/
./skills/trundle-discuss/scripts/verify.sh     # 自检依赖
```

**前置依赖**(只有三条)

- **Claude Code** —— 这是 host,必需。本 skill 只在 Claude Code 里运行,不支持其他 agent 环境
- **至少一个**参与者 CLI:`codex` / `gemini` / `claude` / `dsh`
- **python3 >= 3.8** —— 只用标准库,不需要 pip 装任何东西。Linux 一般自带;macOS 12.3+ 不预装,`xcode-select --install` 即可

不需要装 `jq` —— `json` 在 Python 标准库里,再装一个 jq 是白付依赖。

> ### ⚠️ gemini 用户必读
>
> **gemini 必须在已信任的目录里运行**,否则模型路由会降级到一个不稳定的 preview 分支 —— 实测 8 次请求失败 7 次,墙钟从 14 秒恶化到 108–199 秒。
>
> 在该目录交互式跑一次 `gemini` 并选择信任,或在 `~/.gemini/trustedFolders.json` 中加入:
>
> ```json
> { "/path/to/your/project": "TRUST_FOLDER" }
> ```
>
> **不要用环境变量绕过信任检查。** 它让报错消失的方式是把延迟涨十倍。本项目的脚本在未信任时会直接跳过 gemini 并告诉你怎么修,而不是偷偷绕过。

**装完要重启 Claude Code 会话** —— skill 不热加载。

## 使用

### 进入讨论模式

三条路,**都以你点头收尾**(Claude 不会自作主张把你拖进讨论模式):

```
/trundle-discuss 订阅计费怎么改      显式命令
「叫上 codex 一起聊聊这个」          口头声明
```

或者你说「这块我不太确定」时,Claude 提议一句「要不要拉 codex 和 gemini 进来讨论?」,你点头才进。

**为什么两端都要显式?** 因为这个模式会改变 Claude 的行为 —— 它会调用外部 agent、会拒绝直接给你结论、会把分歧端回来让你判断。如果隐式进出,你就分不清它是"想不出答案"还是"按协议不给答案"。

### 第一次会让你挑参与者

扫描本机可用的 CLI → 你勾选 → 给每人分一个**整场不变的站位** → 写进 `~/.claude/trundle-discuss/roster.yaml`。之后不再问。

建议 2 人,上限 3 人:并行调用的等待时间取最慢那个,而且人越多越容易互相点头。**两个 agent 不能分到同一站位** —— 重复视角既是伪共识的燃料,又白付一份延迟。

### 讨论中的控制语法

| 你说 | 效果 |
|---|---|
| `@codex <你的话>` | 只让 codex 回应,**你的原话原样转达** |
| `@codex @gemini <你的话>` | 同时点名多个 |
| `@all <你的话>` | 全员回应 |
| 「让 codex 和 gemini 分头论证 A 和 B」 | 对赌,各给不同任务 |
| 「把 cline 加进来」 | 中途加人 |
| 「让 gemini 退出」 | 踢出名册 |
| 「这轮别带 gemini」 | 仅本轮跳过 |
| `/trundle-discuss agents` | 重新挑参与者 |
| 「退出讨论」「开始写吧」 | 结束 |

**`@` 的原话是原样转达的。** 你可以直接怼某个 agent:

```
@codex 你说的对账问题不成立,我们只有一个渠道
```

codex 收到的就是这句原话,**不会被 Claude 转述成「用户对对账部分存在疑虑」**。这条很重要 —— 你的原话是整场讨论里指向性最强的输入,加工过就变成一团棉花,回来的也是一团棉花。

### 中途加人 / 踢人

**加人**常见于讨论绕进去了,想要一个没被前面对话锚定的新鲜视角。新人拿到的是**共识状态摘要 + 最近两轮**,不是全量历史 —— 中途加人的价值恰恰是"没被锚定",喂太多历史反而毁掉它。简报里会明确告诉它:如果觉得已确立的前提有问题,直接说。

**踢人**分两种:「这轮别带 X」是临时的,「让 X 退出」会写进名册。**被踢的人说过的话留在讨论记录里,不删不划掉** —— 他的论据不会因为他走了就失效。

对赌进行中不能加/踢人,换人会毁掉对照。

### 状态行

每个回合末尾一行:

```
─── 讨论模式 · codex + gemini · 第 4 轮 · 未决 2 ───
```

参与者是谁、第几轮、还有几个没解决的问题。

### 退出

你明说(「开始写吧」),或者 Claude 在三种信号下主动问一次:同一个未决问题连着两轮没有新论据 / 你开始说执行性的话 / 未决问题清空。

退出时给两三行收尾:

```
─── 已退出讨论模式 ───
这场讨论定下来的:计费走 Stripe,退款按比例
仍然没解决的:多渠道对账的手续费谁承担 —— 你说要问一下财务
```

**不会生成规格文档。** 除非你明确要,否则讨论记录不会被"整理成文档" —— 一有这个动作,它就变回报告流水线了。

退出后 Claude 恢复常态:不再拉 agent,正常写代码。

## 支持的 agent CLI

| CLI | 只读模式 | 实测延迟 | 备注 |
|---|---|---|---|
| `codex` | `--sandbox read-only` | 7–13s | 非 git 目录需 `--skip-git-repo-check` |
| `gemini` | `--approval-mode plan` | 6–14s | **必须在信任目录**,否则降级到 108–199s |
| `claude` | 工具白名单 | 13s | 需清 `CLAUDECODE` 避免嵌套 session |
| `dsh` | `DSH_PERMISSION_MODE=read-only`(**环境变量,不是 flag**) | 4–35s | **没有事件流**:全程静默,跑完一次性给全文;只受绝对上限约束,且上限单独收紧到 300s |

**列表里只有实测跑通的。** 每一个都验证过:非交互调用、只读模式真的拦得住写入、输出能干净提取。

没适配的不会被调用 —— 装了 `opencode`、`cline`、`aider` 之类,扫描时会告诉你"发现但未登记",但不会去猜它怎么调。[欢迎提 PR 加进来](CONTRIBUTING.md)。

## 新增一个 agent CLI

适配层很薄,九个字段就能覆盖:命令模板 / 非交互 flag / 只读约束 / 输出提取 / 事件流粒度 / 超时 / 认证 / 诊断命令 / 信任门禁。

注意里面**没有「站位」**——站位是讨论里的位置,不是 CLI 的属性,它只存在于你自己的名册里,而且是可选的。

**事件流粒度必须实测。** 超时是按"吐字间隔"判的——只要 agent 还在出事件就一直等,连续 90 秒没动静才算卡死。所以一个 CLI 到底是逐 token 吐、逐阶段吐,还是憋到最后一次性吐,直接决定超时怎么判。猜错会稳定误杀它。

`dsh` 就是第三档:它从头到尾一个字都不吐,8.36s 时首字节和末字节同时到达。这一档**不能靠调大超时救**,必须整体跳过空闲判定——否则它一旦跑超过首字节宽限就会被判成"压根没开始",然后请你去拧一个对它不通电的开关。

完整步骤见 [`references/adapting-new-cli.md`](skills/trundle-discuss/references/adapting-new-cli.md)。

**只读约束必须实测确认,不能猜。** 猜错非交互 flag 只是挂掉;**猜错只读约束会让它拿到写你代码库的权限**。所以扫描到未登记的 CLI 时,本项目只会列出来告诉你,绝不擅自调用。

只读也不一定是 flag:`dsh` 就只有环境变量,而且它的默认值是**可写的**。这种情况多一条要求——脚本必须**覆盖**你的环境而不是继承它。

## 隐私与成本

- 讨论记录会落盘到 `<你的项目>/.claude/trundle-discuss/`,含完整讨论内容。仓库自带的 `.gitignore` 已包含它,注意别提交进你自己的仓库
- **你的讨论内容会被发送给你选中的每个 CLI 对应的服务商**
- 每次拉人都是真实的 API 调用,产生真实费用。这也是"默认不拉人"的现实理由之一

## 故障排查

| 症状 | 原因 | 解法 |
|---|---|---|
| 某个 agent 一直卡在「启动中」,直到超时缺席 | 它**压根没开始干活**:额度耗尽 / 认证失效 / 网络不通。这些错误在非交互模式下可能一个字都不吐 | 照失败说明里给的那条命令,**在终端手动跑一次**看真实报错。要放宽等待请调 `DISCUSSION_FIRST_BYTE_GRACE`(不是 `DISCUSSION_IDLE`,那个管的是开始输出之后的静默) |
| gemini 跑几轮之后就一直缺席 | 免费层额度很小(实测 `limit: 5` 请求/窗口),耗尽后它会静默退避重试而不报错 | 等额度窗口滚过去,或换成付费额度。这轮先让别人说 |
| gemini 特别慢(100s+),偶尔整个失败 | 目录未信任,模型路由降级到不稳定分支 | 把目录加进 `trustedFolders.json`。**别用环境变量绕过** |
| dsh 全程没有任何进度,看起来像卡住了 | 它**没有事件流**:整轮 stdout 零输出,跑完才一次性给出全文 | **这是正常的**。状态行会显示「已跑 Ns(上限 300s)」而不是倒计时。要放宽等待只能调 `DISCUSSION_MAX_WALL`——`DISCUSSION_IDLE` 和 `DISCUSSION_FIRST_BYTE_GRACE` 对它完全无效 |
| codex 报 `Failed to read prompt from stdin` / os error 11 | 并行调用时 stdin 是不可读管道,codex 以为有 piped 输入 | 调用加 `</dev/null`(内置脚本已处理) |
| 装了但 skill 不触发 | skill 不热加载 | 重启 Claude Code 会话 |
| 每轮都有一堆 agent 抢着发言 | 协议没生效 | 默认应该只有 Claude 说话 —— 请开 issue |
| Claude 给了"综合考虑建议 A" | 协议没生效 | 分歧未解决时这是违规行为 —— 请开 issue |

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。最有价值的贡献是**新增 agent CLI 适配条目**。

## Acknowledgements

Inspired by [kitchenloop](https://github.com/0xagentkitchen/kitchenloop).

## License

Apache-2.0
