# 调用参考

实测环境:codex-cli 0.144.1 / gemini 0.55.1 / claude 2.1.233 / dsh 0.1.0-rc.6,2026-08。

## invoke.sh 接口

```bash
${CLAUDE_SKILL_DIR}/scripts/invoke.sh codex:/tmp/codex.md gemini:/tmp/gemini.md
```

`invoke.sh` 只是个 wrapper:检测 python3,然后转调同目录的 `invoke.py`。实现全在后者。保留 `.sh` 这个名字是为了调用路径不用改。

每个参数是 `<agent>:<提示文件>`。提示文件由 Claude 组装好(站位 + 共识状态头 + 署名 transcript,见 `prompt-kit.md`)。脚本负责:trust 前置检查、并行喷、活动驱动的超时、逐字回显、提取正文、如实汇报失败。

**stdout** 按 agent 分段,这是给 Claude 解析的唯一来源:

```
===AGENT codex ok 8.2s===
<正文>

===AGENT gemini untrusted 0.0s===
<补救指引>
```

**stderr** 是给人看的实时进度,每行以 `···` 开头,**解析时全部忽略**:

```
··· 15s │ codex ▸ 执行命令 rtk sed -n '1,240p' skills/… │ gemini ▸ 输出中 1.2K字
··· gemini │ 先别急着选方案,这里可能解错了问题。你们两个都假设……
```

`status` 取值:

| status | 含义 | 渲染成 |
|---|---|---|
| `ok` | 正常返回 | 正常发言 |
| `timeout` | 空闲超时或达到绝对上限,正文里写明是哪一种 | `· 超时 · 本轮缺席` |
| `untrusted` | 目录未被 gemini 信任,**已跳过且未降级绕过** | `· 未信任目录 · 本轮缺席` + 补救指引 |
| `ratelimited` | stderr 或事件流含 429 / resource_exhausted / quota exceeded | `· 限流 · 本轮缺席`,可建议稍后重试 |
| `error` | 其他失败(含 CLI 未安装) | `· 调用失败 · 本轮缺席` + stderr 末尾几行 |

失败**不中止整轮**。唯一例外是显式对赌时一方缺席——对赌无效,要问用户。

## ★ 调用时必须调大 Bash 工具的 timeout ★

**Claude Code 的 Bash 工具默认 timeout 是 120s。调用 `invoke.sh` 时必须显式设成 `600000`。**

这是"经常超时"最容易忽略的一半:即使脚本自己的看门狗放宽了,Bash 工具仍会在 120s 准时开枪,而且它一开枪,调用方**连分段输出和失败分类都拿不到**,整轮结果全丢——比脚本自己超时糟得多。

脚本的绝对上限定在 540s 而不是 600s,就是为了永远先于 Bash 工具开枪,留 60s 余量。改 `DISCUSSION_MAX_WALL` 时不要越过这条线。

## 各 agent 的精确命令

四家里三家走 JSONL 事件流。不是为了好看:**没有事件流就没有活动信号,空闲超时无从判起**。`dsh` 是例外——它压根没有,代价见下。

```bash
# codex —— 非 git 目录才需要 --skip-git-repo-check
codex exec --json --sandbox read-only [--skip-git-repo-check] "<prompt>" </dev/null

# gemini —— 必须在已 trust 的目录,见下
gemini --approval-mode plan -o stream-json -p "<prompt>" </dev/null

# claude —— 作为参与者的子进程,必须清掉 CLAUDECODE
env -u CLAUDECODE claude --allowedTools "Read,Glob,Grep" \
  --output-format stream-json --verbose --include-partial-messages \
  -p "<prompt>" </dev/null

# dsh —— 任务是位置参数,不读 stdin;只读靠环境变量,它没有只读 flag,
#         而且 headless profile 的默认值是**可写的**
DSH_PERMISSION_MODE=read-only dsh --profile headless "<prompt>" </dev/null
```

事件流形状与正文来源(实测):

| CLI | 粒度 | 正文取自 |
|---|---|---|
| codex | **item 级**(`item.started`/`item.completed`,类型有 `command_execution` / `agent_message` / `reasoning` / `web_search`) | 最后一条 `item.completed` 且 `item.type=="agent_message"` 的 `.item.text` |
| gemini | **token 级** | 拼接所有 `type=="message" && role=="assistant" && delta==true` 的 `.content` |
| claude | **token 级** + 工具调用事件 | 末条 `type=="result"` 的 `.result` |
| dsh | **none**——全程零输出,跑完一次性写出全文(实测 1044 字节的回答,首字节和末字节都落在同一个 8.36s 时刻) | 整个 stdout |

## 依赖:python3,不要 jq

唯一硬依赖是 **python3 >= 3.8**,只用标准库,不需要 pip 装任何东西。

为什么不是"bash + jq":因为难的从来不是解析 JSON。

| 真正要解决的 | jq 能帮忙吗 | Python 标准库 |
|---|---|---|
| 按吐字间隔判超时 | 不能 | `time.monotonic` |
| 并发读多路事件流 | 不能 | `threading` |
| 杀干净整个进程组 | 不能 | `Popen(start_new_session=True)` + `os.killpg` |
| 解析 JSONL / trustedFolders.json | 能 | `json` |

jq 只覆盖最后一行,却要在 macOS/Windows 上额外装。换成 Python 之后 `json` 在标准库里,**依赖是净减少一个**。

进程组这条不是理论问题:旧 bash 版 `kill -TERM $pid` 杀不掉 codex spawn 出来的孙进程,超时后会留一地残骸。

代价写在明处:**macOS 12.3+ 不预装 python3**,`/usr/bin/python3` 只是个跳板,首次调用会弹 Xcode CLT 安装框。`invoke.sh` 和 `verify.sh` 都会明说这件事,而不是让它神秘失败。

## 坑 1:并行调用必须 `</dev/null`

后台 `&` 运行时 stdin 变成不可读管道,codex 检测到"stdin is piped"就去读它,然后失败:

```
Reading additional input from stdin...
Failed to read prompt from stdin: Resource temporarily unavailable (os error 11)
```

复现:去掉 `</dev/null` 后台跑 codex。修复:每条调用都显式重定向。`invoke.sh` 里每处调用都有,改动时不要漏。

## 坑 2:gemini 的信任目录(最重要)

**症状**:gemini 特别慢(100s+),偶尔整个失败。

**成因**:未 trust 的目录下,即使用环境变量强行放行,模型路由会降级到一个不稳定的 preview 分支。实测对比:

| 环境 | 路由到 | 请求/失败 | API 延迟 | 墙钟 |
|---|---|---|---|---|
| 已 trust 目录 | 稳定 flash 分支 | 1 / 0 | 6.4s | **14s** |
| 环境变量绕过 | preview 分支 | 8 / **7** | 62s | **108–199s** |

那 100+ 秒几乎全是失败重试堆出来的,不是真实推理时间。

**所以:绝不 bypass。** 前置检查 `~/.gemini/trustedFolders.json` 是否覆盖当前目录(或任一祖先),未覆盖就返回 `untrusted` 并引导用户:

```json
{
  "/path/to/your/project": "TRUST_FOLDER"
}
```

或在该目录交互式跑一次 `gemini` 并选择 trust。

> 那个能"解决问题"的环境变量名故意没写进本仓库的代码里。看到它请不要加——它让症状消失的方式是把延迟涨 10 倍。

## 坑 3:不要硬编码模型名

`gemini-2.5-flash` 已对新用户下线。适配库不 pin 模型,走各 CLI 的默认路由;想换模型是用户在各 CLI 自己的配置里的事。

## 坑 4:codex 的 `--full-auto` 已废弃

用 `--sandbox read-only`。`--full-auto` 是保留兼容的旧 flag,不要用。

## 超时:按吐字间隔,不按墙钟

固定墙钟是错的。codex 一旦决定读几个文件再回答,轻松破 120s——而它整个过程都在正常干活。**判据应该是"有没有动静",不是"跑了多久"。**

三个参数:

| 环境变量 | 默认 | 判什么 |
|---|---|---|
| `DISCUSSION_IDLE` | **按 agent 定**(见下) | 连续这么久没有**任何事件**才算卡死 |
| `DISCUSSION_FIRST_BYTE_GRACE` | 180s | 首个**实质**事件到达前的宽限 |
| `DISCUSSION_MAX_WALL` | 540s | 绝对上限(旧名 `DISCUSSION_TIMEOUT` 仍可用) |

- **为什么要首字节宽限**:实测 gemini 要 42s 才吐出第一个字节。用 IDLE 卡首字节会稳定误杀。
  注意「实质」两个字:gemini 启动时立刻发一个 `init` 事件,然后可能静默 90s 以上才出正文。把 `init` 当首字节会让宽限期白白作废,然后被 IDLE 误杀。所以握手类事件(`init` / `turn.started`)不结束宽限期。
- **为什么空闲超时之外还要绝对上限**:空闲超时防不住工具循环——agent 可以一直很"活跃"地反复读同一批文件,永远不结束。MAXWALL 是那种情况唯一的出口。
- **为什么 540 不是 600**:见上「必须调大 Bash 工具的 timeout」。

### 超时了该拧哪个旋钮:看「最后的状态」

`IDLE` 和 `FIRST_BYTE_GRACE` 管的是**两条不同的路径**,拧错了那个开关根本不通电。失败说明里印的「最后的状态是「X」」就是判据:

| 最后的状态 | 说明它 | 拧这个 |
|---|---|---|
| 「启动中」 | 一个**实质**事件都没产出,压根没开始 | `DISCUSSION_FIRST_BYTE_GRACE` |
| 「发言中」「执行命令 …」「思考中」 | 开始干活了,中途静默超限 | `DISCUSSION_IDLE` |
| 「运行中(无进度事件)」 | 这个 CLI 从头到尾不吐字,静默不携带任何信息 | `DISCUSSION_MAX_WALL`(另两个对它**完全无效**) |

这个区分不是文档洁癖:本项目自己踩过,两次,一共等了 400 秒。当时两种情况共用一段文案、一律建议「调大 `DISCUSSION_IDLE`」,而实际卡在启动阶段——那个参数对这条路径完全无效。

**卡在「启动中」通常不是超时不够长,是它根本起不来**:额度耗尽、认证失效、网络不通。这些错误在非交互模式下可能一个字都不吐(实测过一次 gemini 配额耗尽,无 TTY 时 100s 内 stdout 只有 `init` 和 prompt 回显、stderr 只有一条颜色警告,限流关键词零命中)。所以失败说明里会直接给出一条**在终端手动跑**的诊断命令,来自 `AGENTS[name]["probe"]`——新增 agent 时这个字段要一起填。

限流也走这条路:它的典型表现恰恰就是长时间没有输出,所以经常先被空闲超时砍掉。`classify()` 因此在报 `timeout` 之前先查一遍 `is_rate_limited()`,命中就归类成 `ratelimited`——否则 stderr 里明写着 429,报出来也只是「卡死」,把人引向"调大超时"而不是"等一会儿再来"。

### ★ IDLE 必须按事件粒度分别定 ★

一个全局 IDLE 是错的,这是实测踩出来的:

| agent | stream | idle | 理由 |
|---|---|---|---|
| codex | `item` | **300s** | 只有工具调用和整条消息两种事件,**生成最终回答的全过程一个事件都不发** |
| gemini | `token` | 90s | delta 持续到达,没动静就是真没动静 |
| claude | `token` | 90s | 思考阶段也有 `thinking_delta` |
| dsh | `none` | —— | **没有事件流**,空闲超时对它无意义,整体跳过;只受 `max_wall` 约束 |

实测证据:codex 一次 8.0K 字的回答,生成期间静默约 70s;讨论场景里上下文更大、回答更长时会破 90s。用 90s 卡它,**恰好砍在它要说出正文那一刻**——而日志上看起来像"它卡死了",极易误判成脚本 bug。

codex 没有开启 token 级事件的开关(`codex features list` 里 `apply_patch_streaming_events` 和 `concurrent_reasoning_summaries` 都是 under development,而且都覆盖不到最终回答的生成阶段),所以只能在超时侧让步。上限仍受 `max_wall` 约束。

显式设 `DISCUSSION_IDLE` 会覆盖所有**有事件流的** agent(调试用)。它拽不回 `dsh`——那会凭空造出第四个不通电的开关。

### 墙钟也可以按 agent 收紧

理由和 IDLE 恰好相反:有事件流的 agent 跑飞了会先被空闲超时砍,墙钟只是兜底;**无事件流的 agent 中途没有任何信号能区分「在想」和「死了」**,墙钟是它唯一的护栏,所以要更保守。

| agent | max_wall | 理由 |
|---|---|---|
| codex / gemini / claude | 540s | 有空闲超时兜底 |
| dsh | **300s** | 无流 = 卡死无征兆。实测最重的讨论级 prompt 只用 34.6s,300 有约 9 倍余量 |

显式设 `DISCUSSION_MAX_WALL` 会覆盖所有 agent(per-agent 的收紧让位)。

### 静默是可见的

状态行在静默超过 20s 后会带上计时,这样超时是可预见的,而不是突然发生:

```
··· 154s │ codex ▸ 执行命令 rtk npm view @openai/codex version… 477字 · 静默 45s/300s
··· 169s │ codex ▸ 执行命令 rtk npm view @openai/codex version… 477字 · 静默 60s/300s
··· 179s │ codex ▸ 已完成 8.0K字
```

没有这个计时,上面这段看起来就是"codex 卡了 60 秒然后莫名其妙好了"。

**无事件流的 agent 长得不一样**,它显示的是「还在跑」而不是「还剩多久」:

```
··· 5s  │ dsh ▸ 运行中(无进度事件) · 已跑 5s(上限 300s)  │ codex ▸ 思考中
··· 16s │ dsh ▸ 运行中(无进度事件) · 已跑 16s(上限 300s) │ codex ▸ 发言中 413字
··· dsh │ 一次性返回 546字(全程无进度事件)
```

刻意不用「静默 N/M」那个格式:它读起来是一句倒计时(「快超时了」),而 `dsh` 的静默是正常的,也不会因此被砍。上限仍然印出来,免得墙钟那一枪显得突然。

`timeout` 状态有两种含义,脚本会在正文里写明是哪一种,并附上**开枪那一刻它在干什么**:

```
连续 300s 没有任何事件,判定卡死并中止(已等 380s)。
最后的状态是「发言中」。
→ 最后状态是「发言中」或刚做完工具调用 = 大概率被砍在正要说话那一刻,调大 IDLE

达到 540s 绝对上限并中止,可能陷入了工具循环。
```

第三种是无事件流的 agent 撞上墙钟,文案里唯一能拧的是 `DISCUSSION_MAX_WALL`,并会明说另外两个不通电——因为对它来说「一直没输出」既不代表卡住,也不代表在干活。

正常延迟参考:codex 7–13s(要读文件时 40–150s)、gemini 6–14s、claude 约 13–30s、dsh 4–35s。

## 逐字回显

gemini 和 claude 有 token 级 delta,直接回显到终端;codex 只有 item 级事件,显示 `▸ 执行命令 …` / `▸ 思考中` / `▸ 发言中`。另有每 5s 一次的状态行汇总。

回显优先写 `/dev/tty`——用户在真实终端里手动跑脚本时,这条路径零 token 浪费。**但 Claude Code 的 Bash 工具下没有 tty**(实测 `open("/dev/tty")` 报 `OSError errno 6`),只能降级到 stderr,而 stderr 最终会连同正文一起交给模型,等于正文重复一遍。

所以有 `DISCUSSION_ECHO_CAP`(默认 600 字/agent):回显前 600 字让用户看到"它真的在写",超出后该 agent 转为状态行报体量。想完全关掉回显就设成 1。

**`dsh` 这类无事件流的 agent 不参与回显,`DISCUSSION_ECHO_CAP` 对它不起作用。** 它的正文跑完才到手,那一刻"让用户看到它真的在写"已经没有观众了;而回显走 stderr,等于为同一段文字付两份 token。取而代之是结束时一行回执:`dsh │ 一次性返回 546字(全程无进度事件)`。
