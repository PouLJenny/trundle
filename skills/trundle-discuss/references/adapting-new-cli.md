# 接纳一个未登记的 CLI

`discover.sh` 发现 PATH 里有个 agent CLI 但适配库没登记时,会列出来告知,**但不会调用它**。

## 为什么不自动猜

猜错**非交互 flag** → 它挂掉,损失是一次失败调用。
猜错**只读 flag** → 它拿到写权限,可能改用户的代码库。

第二种是不可接受的。所以未登记的 CLI 必须走一次人工确认才能入库。

## 八个字段

```yaml
  <cli-name>:
    verified: true                    # 通过下面全部验证才登记;没跑通的不进库
    cmd: <命令模板,{flags} 和 {prompt} 会被替换>
    noninteractive_flags: []          # 让它一次性输出、不进 TUI
    readonly_flags: []                # ★ 关键:禁止写入/执行的 flag
    extract: jsonl:<哪条事件的哪个字段>  # 正文怎么拿
    progress: token | item | none     # ★ 事件流粒度,必须实测
    auth_env: []                      # 需要的认证环境变量
    trust:
      check: none                     # none | git_repo | <自定义检查>
    latency_observed: "8-12s"         # 实测耗时
    stance: |
      <整场不变的站位,要和已有的都不重复>
```

**优先选 JSONL 事件流输出,不要纯文本。** 这条和早期版本相反,原因是实测:

- 纯文本模式下 **claude 是一次性吐出的**——54s 内 stdout 一个字节都没有,然后一把吐 3926 字节。整轮**零活动信号**,而超时是按吐字间隔判的,没有信号就只能退化成墙钟。
- gemini 纯文本虽然增量吐字,但首字节要等 42s,且拿不到工具调用之类的进度。

"正文埋在 JSON 字段里要靠 jq 抠"这个顾虑已经不成立——实现是 Python,`json` 在标准库里。

除了 `progress` 字段,还要在 `scripts/invoke.py` 的 `AGENTS` 表里加一条 Spec,并写一个 `parse_<cli>` 函数:吃一条事件,吐 `(进度短语, 要回显的文本)`,正文写进 run 的累加器。照着已有的三个抄。

**没有事件流的 CLI 怎么办**:`progress: none`,登记时写清楚它只受 `max_wall` 约束,并把这一点写进注释——不要让后来的人以为空闲超时对它生效。

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

**文件一旦被创建,这个 readonly_flags 就是错的,不许入库。** 找不到只读模式的 CLI 可以登记条目但 `readonly_flags` 留空并标注待确认,**且不得进默认阵容**。

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

**⑤ 站位不重复**

新 agent 的 `stance` 必须和名册里已有的都不同。两个 agent 分到同一视角,既是伪共识的燃料,又白付一份并行延迟。

## 贡献回上游

`agents.yaml` 是这个项目的核心公共资产。提 PR 时请附上:

- 填好的八字段 + `invoke.py` 里的 `parse_<cli>`
- **②只读验证的实际输出**(证明文件没被创建)
- **④事件流粒度的实际输出**(带时间戳,证明 `progress` 不是猜的)
- 一次真实调用的耗时
- CLI 版本号

只读 flag 靠猜的 PR 不会被合并——这不是不信任贡献者,是这条错了代价太大。
