# 调用参考

实测环境:codex-cli 0.144.1 / gemini 0.55.1 / claude 2.1.233,2026-08。

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

三家都走 JSONL 事件流。不是为了好看:**没有事件流就没有活动信号,空闲超时无从判起**。

```bash
# codex —— 非 git 目录才需要 --skip-git-repo-check
codex exec --json --sandbox read-only [--skip-git-repo-check] "<prompt>" </dev/null

# gemini —— 必须在已 trust 的目录,见下
gemini --approval-mode plan -o stream-json -p "<prompt>" </dev/null

# claude —— 作为参与者的子进程,必须清掉 CLAUDECODE
env -u CLAUDECODE claude --allowedTools "Read,Glob,Grep" \
  --output-format stream-json --verbose --include-partial-messages \
  -p "<prompt>" </dev/null
```

事件流形状与正文来源(实测):

| CLI | 粒度 | 正文取自 |
|---|---|---|
| codex | **item 级**(`item.started`/`item.completed`,类型有 `command_execution` / `agent_message` / `reasoning` / `web_search`) | 最后一条 `item.completed` 且 `item.type=="agent_message"` 的 `.item.text` |
| gemini | **token 级** | 拼接所有 `type=="message" && role=="assistant" && delta==true` 的 `.content` |
| claude | **token 级** + 工具调用事件 | 末条 `type=="result"` 的 `.result` |

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
| `DISCUSSION_IDLE` | 90s | 连续这么久没有**任何事件**才算卡死 |
| `DISCUSSION_FIRST_BYTE_GRACE` | 180s | 首个事件到达前的宽限 |
| `DISCUSSION_MAX_WALL` | 540s | 绝对上限(旧名 `DISCUSSION_TIMEOUT` 仍可用) |

- **为什么要首字节宽限**:实测 gemini 要 42s 才吐出第一个字节。用 IDLE 卡首字节会稳定误杀。
- **为什么空闲超时之外还要绝对上限**:空闲超时防不住工具循环——agent 可以一直很"活跃"地反复读同一批文件,永远不结束。MAXWALL 是那种情况唯一的出口。
- **为什么 540 不是 600**:见上「必须调大 Bash 工具的 timeout」。

`timeout` 状态因此有两种含义,脚本会在正文里写明是哪一种:

```
连续 90s 没有任何输出,判定卡死并中止(已等 213s)。
达到 540s 绝对上限并中止,可能陷入了工具循环。
```

正常延迟参考:codex 7–13s(要读文件时 40–150s)、gemini 6–14s、claude 约 13–30s。

## 逐字回显

gemini 和 claude 有 token 级 delta,直接回显到终端;codex 只有 item 级事件,显示 `▸ 执行命令 …` / `▸ 思考中` / `▸ 发言中`。另有每 5s 一次的状态行汇总。

回显优先写 `/dev/tty`——用户在真实终端里手动跑脚本时,这条路径零 token 浪费。**但 Claude Code 的 Bash 工具下没有 tty**(实测 `open("/dev/tty")` 报 `OSError errno 6`),只能降级到 stderr,而 stderr 最终会连同正文一起交给模型,等于正文重复一遍。

所以有 `DISCUSSION_ECHO_CAP`(默认 600 字/agent):回显前 600 字让用户看到"它真的在写",超出后该 agent 转为状态行报体量。想完全关掉回显就设成 1。
