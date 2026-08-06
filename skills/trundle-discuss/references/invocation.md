# 调用参考

实测环境:codex-cli 0.144.1 / gemini 0.40.1,2026-08。

## invoke.sh 接口

```bash
${CLAUDE_SKILL_DIR}/scripts/invoke.sh codex:/tmp/codex.md gemini:/tmp/gemini.md
```

每个参数是 `<agent>:<提示文件>`。提示文件由 Claude 组装好(站位 + 共识状态头 + 署名 transcript,见 `prompt-kit.md`)。脚本负责:trust 前置检查、并行喷、超时、提取正文、如实汇报失败。

输出按 agent 分段:

```
===AGENT codex ok 8s===
<正文>

===AGENT gemini untrusted 0s===
<补救指引>
```

`status` 取值:

| status | 含义 | 渲染成 |
|---|---|---|
| `ok` | 正常返回 | 正常发言 |
| `timeout` | 超过 `DISCUSSION_TIMEOUT`(默认 120s) | `· 超时 · 本轮缺席` |
| `untrusted` | 目录未被 gemini 信任,**已跳过且未降级绕过** | `· 未信任目录 · 本轮缺席` + 补救指引 |
| `ratelimited` | stderr 含 429 / resource_exhausted / quota exceeded | `· 限流 · 本轮缺席`,可建议稍后重试 |
| `error` | 其他失败 | `· 调用失败 · 本轮缺席` + stderr 末尾几行 |

失败**不中止整轮**。唯一例外是显式对赌时一方缺席——对赌无效,要问用户。

## 各 agent 的精确命令

```bash
# codex —— 非 git 目录才需要 --skip-git-repo-check
codex exec --sandbox read-only [--skip-git-repo-check] "<prompt>" </dev/null

# gemini —— 必须在已 trust 的目录,见下
gemini --approval-mode plan -p "<prompt>" </dev/null

# claude —— 作为参与者的子进程,必须清掉 CLAUDECODE
env -u CLAUDECODE claude --allowedTools "Read,Glob,Grep" -p "<prompt>" </dev/null
```

## 零外部依赖

脚本刻意不用 `jq`,也不用 GNU coreutils 的 `timeout` —— 这两个在 macOS 上都得额外装,为一个讨论工具让人先 `brew install` 不值得。

- **不用 jq**:gemini 加 `--output-format json` 会把正文埋进 `.response`,得靠 jq 抠;不加则 `-p` 在非 TTY 下直接输出纯正文。代价是读不到 `.stats`(模型路由与失败率),那只在排查时有用,值得换
- **不用 timeout**:改用 bash 原生看门狗——后台跑命令 + 一个 `sleep N; kill` 的守护子进程。超时时 `wait` 返回 143(128+SIGTERM),脚本按这个判定

只依赖 bash 和 POSIX 标准命令(`sleep` / `mktemp` / `date` / `grep` / `dirname`),Linux 和 macOS 都自带。

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

## 超时

`DISCUSSION_TIMEOUT` 环境变量控制,默认 120s。正常延迟 codex 7–13s、gemini 6–14s、claude 约 13s,120s 足够覆盖长回答;调低到 30s 以下会在正常回答上误杀。

超时由 bash 看门狗实现(见上「零外部依赖」),不需要装任何东西。
