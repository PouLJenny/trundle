# 接纳一个未登记的 CLI

`discover.sh` 发现 PATH 里有个 agent CLI 但适配库没登记时,会列出来告知,**但不会调用它**。

## 为什么不自动猜

猜错**非交互 flag** → 它挂掉,损失是一次失败调用。
猜错**只读 flag** → 它拿到写权限,可能改用户的代码库。

第二种是不可接受的。所以未登记的 CLI 必须走一次人工确认才能入库。

## 九个字段

```yaml
  <cli-name>:
    verified: true                    # 通过下面全部验证才登记;没跑通的不进库
    cmd: <命令模板,{flags} 和 {prompt} 会被替换>
    noninteractive_flags: []          # 让它一次性输出、不进 TUI
    readonly_flags: []                # ★ 关键:禁止写入/执行的 flag
    readonly_env: {}                  # ★ 只读也可能靠环境变量,见下
    extract: jsonl:<哪条事件的哪个字段>  # 正文怎么拿
    progress: token | item | none     # ★ 事件流粒度,必须实测
    idle: 90                          # ★ 由 progress 决定,见下
    auth_env: []                      # 需要的认证环境变量
    probe: <cli> -p "说一句话"         # ★ 给人在终端手动跑的最小调用,见下
    trust:
      check: none                     # none | git_repo | <自定义检查>
    latency_observed: "8-12s"         # 实测耗时
```

**这里没有 `stance`。** 站位是「讨论里的位置」,不是 CLI 的属性 —— 适配库既不知道
用户装了哪些 agent,也不该替所有场次预定立场。它还有第二个坏处:适配库随 skill
更新被覆盖,而站位是用户偏好,两者生命周期不同,写在一起必然漂(本项目实测漂过)。
站位只存在于名册 `~/.claude/trundle-discuss/roster.yaml`,而且是**可选的**。

**只读不一定是 flag。** `dsh` 就没有任何 flag,只读靠 `DSH_PERMISSION_MODE=read-only`,
而它的默认值恰恰是可写的。无论哪种形式,验证方法不变(见下面的 ②),但环境变量
这种额外有一条铁律:脚本必须**覆盖**用户环境,而不是继承 —— 用户环境里碰巧有个
`workspace-write` 就把只读放宽掉,是不能接受的。`invoke.py` 里对应的是 spec 的
`set_env` 字段和 `build_env()`。

**`probe` 不是可有可无的礼节字段。** agent 卡在启动阶段被判超时时,失败说明会把它原样印给用户。存在的理由是实测教训:额度耗尽、认证失效这类错误,在非交互模式下**可能一个字都不吐**——实测过一次 gemini 配额耗尽,无 TTY 时 100s 内 stdout 只有 `init` 和 prompt 回显、stderr 只有一条颜色警告,限流关键词零命中;而同一时刻同样的调用在终端里会打出 `You exceeded your current quota`。用户唯一的出路就是去终端手动跑一次,所以不要让他自己猜命令怎么写。

`scripts/selftest.py` 会检查每个 spec 都有这个字段,漏填直接红。

**优先选 JSONL 事件流输出,不要纯文本。** 这条和早期版本相反,原因是实测:

- 纯文本模式下 **claude 是一次性吐出的**——54s 内 stdout 一个字节都没有,然后一把吐 3926 字节。整轮**零活动信号**,而超时是按吐字间隔判的,没有信号就只能退化成墙钟。
- gemini 纯文本虽然增量吐字,但首字节要等 42s,且拿不到工具调用之类的进度。

"正文埋在 JSON 字段里要靠 jq 抠"这个顾虑已经不成立——实现是 Python,`json` 在标准库里。

除了 `progress` 字段,还要在 `scripts/invoke.py` 的 `AGENTS` 表里加一条 Spec,并写一个 `parse_<cli>` 函数:吃一条事件,吐 `(进度短语, 要回显的文本)`,正文写进 run 的累加器。照着已有的三个抄。

> `AGENTS` 里表达事件流粒度的 key 叫 **`stream`**(取值与本文的 `progress` 完全一致:
> `token`/`item`/`none`)。之所以不同名,是因为 `run.progress` 在代码里已经是
> 「状态短语」的意思(`"发言中"`、`"执行命令 …"`),两个 `progress` 会天天打架。

### 没有事件流的 CLI 怎么办(`progress: none`)

`dsh` 是库里第一个这样的例子。把它接进来时改的东西比预想的多,因为**活动驱动的
超时整套建立在「有事件流」的假设上**,不是换个解析函数就完事:

| 机制 | 有事件流时 | `progress: none` 时 |
|---|---|---|
| stdout 读取 | `drain_stdout`:逐行 `json.loads` → `parse_<cli>` | `drain_stdout_text`:整块进 `run.text`。**不要写 parse 函数**(spec 里显式填 `None`) |
| 空闲超时 / 首字节宽限 | `idle_for(run)` / `GRACE` | **整体跳过**。`idle_for()` 返回 `None`,`watch()` 直接 `continue` |
| 绝对上限 | 全局 `max_wall` | 可按 agent 单独收紧(spec 的 `max_wall`)——它是唯一的护栏,该更保守 |
| 状态行 | `静默 45s/300s` | `已跑 45s(上限 300s)`——**绝不能显示成倒计时** |
| 超时文案 | `NEVER_STARTED_MSG` / `STALLED_MSG` | `NO_STREAM_MSG`,唯一能拧的是 `DISCUSSION_MAX_WALL` |
| 逐字回显 / `ECHO_CAP` | 逐 token 回显 | 不适用。正文跑完才到手,回显它既失去「它真的在写」的意义,又要为同一段文字付两份 token。改成结束时一行体量回执 |

**最容易做错的地方**:什么都不改,只把 `idle` 调到很大。那样它一旦跑超过
`first_byte_grace` 就会被判成「压根没开始」,失败说明会建议用户去拧
`DISCUSSION_FIRST_BYTE_GRACE`——而它其实正常在跑,只是不吐字。用户于是对着一个
不通电的开关反复试。这个项目为同一种病已经付过 400 秒的学费。

**`probe` 对这一档更重要**:有事件流的 agent 还能从「最后的状态是什么」里读出
线索,无流的连这个都没有——唯一的诊断手段就是去终端手动跑一次。

## 验证步骤(缺一不可)

**① 非交互调用**

```bash
<cli> <noninteractive_flags> "说一句话" </dev/null
```

必须返回正文、退出码 0、不进入交互界面。注意 `</dev/null`——并行调用时缺它会让某些 CLI 尝试读 stdin 而失败。

**② 只读验证(安全测试,不能跳过)**

```bash
cd "$(mktemp -d)" && git init -q .
<cli> <readonly_flags> "在当前目录创建一个名为 SHOULD_NOT_EXIST.txt 的文件" </dev/null
ls SHOULD_NOT_EXIST.txt    # 必须报 No such file
```

只读靠环境变量的(如 `dsh`),把 `<readonly_flags>` 换成 `readonly_env` 的前缀,
并**额外**验一次覆盖语义——故意把用户环境设成最宽,确认仍然写不进去:

```bash
DSH_PERMISSION_MODE=danger-full-access <repo>/scripts/invoke.sh dsh:/tmp/write-test.md
ls        # 必须零文件生成
```

顺手确认它能不能从 shell 逃逸(`echo hello > f.txt`)——文件沙箱和 bash 沙箱常常
是两套东西,只验前者会漏。

**文件一旦被创建,这个只读约束就是错的,不许入库。** 找不到只读模式的 CLI 可以登记条目但 `readonly_flags` 留空并标注待确认,**且不得进默认阵容**。

**③ 输出提取**

确认 `extract` 拿到的是纯正文——不含进度日志、不含统计信息、不含 ANSI 转义、不含任何 JSON 残留。

**④ 事件流粒度(决定 `progress`)**

```bash
<cli> <flags> "分五段详细讲解 bash 的信号处理,每段至少200字" </dev/null \
  | while IFS= read -r l; do printf '%s  %s\n' "$(date +%T)" "${l:0:80}"; done
```

看时间戳的疏密:
- 几十毫秒一条、每条只有几个字 → `token`
- 几秒一条、每条是一次工具调用或一整段消息 → `item`
- **全部时间戳挤在最后一秒** → `none`,它没有真正的流式输出

第三种必须当场发现。猜成 `token` 而实际是 `none`,空闲超时会稳定误杀它。
实测 `dsh` 就是这一档:一个 1044 字节的回答,首字节和末字节都落在同一个 8.36s
时刻,整个时间戳序列只有一行。

**`progress` 直接决定 `idle` 该填多少**,别照抄 90:

| progress | idle | 为什么 |
|---|---|---|
| `token` | 90 | delta 持续到达,没动静就是真没动静 |
| `item` | **300** | 生成回答期间通常完全静默,静默时长随回答长度增长 |
| `none` | —— | 空闲超时对它无意义,只受 `max_wall` 约束。`invoke.py` 里要填 `stream: "none"`、`idle: None`,并按上一节改四处。墙钟建议单独收紧 |

`item` 那一档的 300 是实测来的:codex 一次 8.0K 字的回答静默了约 70s,而讨论场景上下文更大时会破 90s——用 90s 卡它,恰好砍在它要说出正文那一刻。**判这一档时,请专门跑一次"让它写一篇很长的回答",测最后一个事件到进程结束之间的间隔。**

**⑤ 站位不重复(这一步在名册里做,不在适配库里)**

适配库不带 `stance`,所以这一步不是「新 agent 该填什么站位」,而是:**名册里若给
多个参与者设了 `stance`,彼此不得重复**。两个 agent 分到同一视角,既是伪共识的
燃料,又白付一份并行延迟。留空的参与者不占视角,不参与这个约束。

## 贡献回上游

`agents.yaml` 是这个项目的核心公共资产。提 PR 时请附上:

- 填好的九字段 + `invoke.py` 里的 `parse_<cli>`(无事件流的 CLI 不写 parse,
  改填 `stream: "none"`,并按「没有事件流的 CLI 怎么办」那一节改四处)
- **②只读验证的实际输出**(证明文件没被创建;只读靠环境变量的还要附覆盖语义那一次)
- **④事件流粒度的实际输出**(带时间戳,证明 `progress` 不是猜的)
- 一次真实调用的耗时
- CLI 版本号

只读约束靠猜的 PR 不会被合并——这不是不信任贡献者,是这条错了代价太大。
