# 接纳一个未登记的 CLI

`discover.sh` 发现 PATH 里有个 agent CLI 但适配库没登记时,会列出来告知,**但不会调用它**。

## 为什么不自动猜

猜错**非交互 flag** → 它挂掉,损失是一次失败调用。
猜错**只读 flag** → 它拿到写权限,可能改用户的代码库。

第二种是不可接受的。所以未登记的 CLI 必须走一次人工确认才能入库。

## 七个字段

```yaml
  <cli-name>:
    verified: true                    # 通过下面全部验证才登记;没跑通的不进库
    cmd: <命令模板,{flags} 和 {prompt} 会被替换>
    noninteractive_flags: []          # 让它一次性输出、不进 TUI
    readonly_flags: []                # ★ 关键:禁止写入/执行的 flag
    extract: stdout                   # 正文怎么拿
    timeout: 120
    auth_env: []                      # 需要的认证环境变量
    trust:
      check: none                     # none | git_repo | <自定义检查>
    latency_observed: "8-12s"         # 实测耗时
    stance: |
      <整场不变的站位,要和已有的都不重复>
```

**提取正文优先选纯文本输出。** 很多 CLI 有 `--output-format json`,但那样正文埋在某个字段里,得靠 `jq` 抠——而本项目刻意不依赖 jq(macOS 上要额外装)。能直接吐纯文本就用纯文本。

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

确认 `extract` 拿到的是纯正文——不含进度日志、不含统计信息、不含 ANSI 转义。

**④ 站位不重复**

新 agent 的 `stance` 必须和名册里已有的都不同。两个 agent 分到同一视角,既是伪共识的燃料,又白付一份并行延迟。

## 贡献回上游

`agents.yaml` 是这个项目的核心公共资产。提 PR 时请附上:

- 填好的七字段
- **②只读验证的实际输出**(证明文件没被创建)
- 一次真实调用的耗时
- CLI 版本号

只读 flag 靠猜的 PR 不会被合并——这不是不信任贡献者,是这条错了代价太大。
