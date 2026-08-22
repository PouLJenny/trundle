# 接纳一个新的 API 端点

`adapting-new-cli.md` 管的是 CLI 参与者。这一份管**直连 API 的参与者**——
两者的验证清单几乎没有重叠,所以不合并。

## 先看清楚差别

| | CLI 参与者 | API 参与者 |
|---|---|---|
| 能不能读代码 | **能**(只读沙箱 / 工具白名单) | **不能**,只看得到 prompt 里的字 |
| 只读铁律 | 最难的部分,每个都要实测 flag 或环境变量 | **天然满足**,它够不到文件系统 |
| 认证 | 复用 CLI 已有登录态,项目零密钥管理 | 引入凭证,四条泄漏面要堵 |
| 失败诊断 | exit code + stderr + probe 命令 | HTTP 状态码 + `finish_reason` + 事件数/字数 |
| 谁登记 | 适配库 `agents.yaml` 的 `agents:` 块 | **用户自己的名册**,适配库只记 wire format |

**所以「只读验证」那一步在这里不存在**,而多出三步 CLI 没有的:流完整性、
静默截断、思考字段。

## 字段(用户在名册里填)

```yaml
- agent: <参与者名>         # 必须匹配 [A-Za-z][\w-]*,且不得与已适配 CLI 重名
  api:
    base_url: <必填>        # http/https 都行;明文、内网、localhost 都照发
    model: <必填>
    api_key_env: <可选>     # ★ 只写变量名,永远不写值 ★ 本地端点可以不填
    headers: {}             # 可选。★ 别把 token 写在这里 ★ 见下
    max_tokens: <可选>      # 不填就不发这个参数,走各家默认
```

**未在这个集合里的键会被警告并忽略**,不会被发送。静默忽略是不行的——你填了
`temperature: 0.7` 期待它更保守,而它根本没出去,这和下面「静默截断」是同一种病:
看起来生效了的没生效。

**凭证只走 `api_key_env`。** 写进 `headers` 也能跑通,但那个 token 会明文留在你的
名册文件里,而名册常常在 dotfiles 仓库中。检测到会警告一次(只印键名,不印值)。

## 验证步骤(缺一不可)

### ① 非流式冒烟

```bash
curl -sS <base_url>/chat/completions \
  -H 'Content-Type: application/json' \
  ${KEY:+-H "Authorization: Bearer $KEY"} \
  -d '{"model":"<model>","messages":[{"role":"user","content":"只说三个字:你好呀"}]}'
```

拿到 HTTP 200 和一段正文才继续。**无凭证的本地端点要确认不带 `Authorization`
头也能跑**(实测 ollama 可以)。

### ② 抓真实 SSE 原始字节 ★ 不能跳过 ★

```bash
curl -sS -N <上面那条> -d '{...,"stream":true,"stream_options":{"include_usage":true}}' \
  -o raw.sse -w 'HTTP=%{http_code}\n'
head -3 raw.sse; echo '---'; tail -3 raw.sse
```

四件事必须逐个对着原始字节确认,**不能靠文档、不能靠猜**:

| 要确认 | 怎么看 | 不对会怎样 |
|---|---|---|
| 正文在 `.choices[0].delta.content` | 逐条解析看拼出来是不是正文 | 提取不到正文,一律判「正文为空」缺席 |
| 末尾发 `data: [DONE]` | `tail -3` | **不发的厂商每次调用都判缺席,等于完全不可用** |
| 末条 chunk 给 `finish_reason` | 逐条找 | 不给就降级为只判 `[DONE]`,完整性判定变弱 |
| 支持 `stream_options.include_usage` | 找带 `usage` 的那条 | 拿不到就没有静默截断检测 |

实测记录:ollama 与 Google 的 OpenAI 兼容端点都发 `[DONE]`;
**Google 的末条 delta 根本没有 `content` 这个键**(不是 `null`,是缺失)——
所以解析一律用 `.get()`,不能用下标。

### ③ 思考字段(推理模型必做)

```bash
python3 - <<'EOF'
import json
lines=[l.strip() for l in open("raw.sse",encoding="utf-8") if l.strip().startswith("data:")]
n=c=0; other={}
for l in lines:
    if l=="data: [DONE]": continue
    d=json.loads(l[6:])["choices"][0]["delta"]
    n+=1; c+=len(d.get("content") or "")
    for k,v in d.items():
        if k not in ("role","content") and isinstance(v,str): other[k]=other.get(k,0)+len(v)
print("事件 %d 条 · 正文 %d 字 · 其他字段 %s" % (n,c,other))
EOF
```

**如果正文字数远小于事件数,这就是个推理模型。** 实测 `qwen3:0.6b`:
81 条事件、正文 3 字、`reasoning` 133 字——也就是 96% 的事件里 `content` 是空串。

这不需要你改代码(非 `content` 的字符串字段一律当作「思考内容」,产出「思考中」
进度短语、不进正文、不置实质事件),但**你要知道它长这样**:回显会长时间空白,
而那是正常的。

### ④ 上下文超限的表现 ★ 最重要的一步 ★

```bash
python3 -c "
import json
p='填充文本。'*8000 + '\n以上有多少个句号?只回答数字。'
print(json.dumps({'model':'<model>','stream':True,
                  'stream_options':{'include_usage':True},
                  'messages':[{'role':'user','content':p}]},ensure_ascii=False))" > big.json
curl -sS -N <endpoint> -H 'Content-Type: application/json' --data-binary @big.json | tail -5
```

**先看清楚这一步真正在问什么:** 实测下来云端和本地 runtime 的行为是**相反**的,
而这条路径上的全部危险都集中在后者。

| | 超限时的行为 | 走哪条规则 | 用户看到什么 |
|---|---|---|---|
| **云端**(Gemini 实测) | HTTP 400 + `The input token count exceeds the maximum number of tokens allowed 131072` | R-027 带服务端原文 | 一句精确可行动的报错,含上限数字 |
| **本地 runtime**(ollama 实测) | HTTP 200 + `finish_reason: stop` + `[DONE]` + 通顺正文 | 没有任何错误路径 | **什么都看不到** |

云端窗口还普遍很大——DeepSeek 三个模型全是 **1M tokens**,而一轮讨论的 prompt
是几千到几万字符,低三个数量级。**所以云端基本碰不到这个场景。**

反过来,ollama 的默认 `num_ctx` 常常只有 **4096**,远小于模型本身的能力
(实测 llama3.1:8b 的 `llama.context_length` 是 131072,是运行时默认值在掐它)。
**下面那套检测就是为这种情况准备的。**

两种结果,处理方式完全不同:

- **报 400 / 明确错误** → 好办,`classify` 会把服务端原文带给用户
- **HTTP 200 正常出流、`finish_reason: stop`、有 `[DONE]`、正文通顺** →
  **这是静默截断,是这条路径上最危险的形态**

静默截断为什么危险:所有判据都说一切正常,而模型只看到了 prompt 的一部分。
更糟的是截断方向——实测 ollama 从前往后丢,被丢掉的恰好是 prompt 开头的
**【已确立的前提】和【已废弃的方向】**。于是这个参与者会理直气壮地重新论证
一个已废弃的方案,而在 transcript 里它看起来只是「提出了不同意见」。
transcript 只增不改,这个污染不可逆。

唯一的带内信号是 `usage.prompt_tokens`。实测数据:

| 字符数 | `prompt_tokens` | 比值 | 真实情况 |
|---|---|---|---|
| 311 | 154 | 0.50 | 未截断 |
| 2,511 | 1,143 | 0.46 | 未截断 |
| 6,011 | 2,598 | 0.43 | 未截断 |
| 1,542(中文) | 1,270 | 0.82 | 未截断 |
| 11,011 | **4,096** | 0.37 | **已截断** |
| 14,096 | **4,096** | 0.29 | **已截断** |
| 20,011 | **4,096** | 0.20 | **已截断** |
| 37,317 | **4,096** | 0.11 | **已截断** |

**纯比值判据抓不住** ——自然 token/字符比取决于内容语言(英文代码约 0.25,
中文约 0.8),要抓住 0.29 就得把阈值提到 0.33 以上,而未截断的英文代码
自然就在 0.25。这是原理性的,不是阈值没调好。

所以用两条判据:比值 `< 1/8` 抓「窗口不是 2 的幂但丢弃极多」,
`prompt_tokens 恰为 ≥2048 的 2 的整数次幂` 抓「恰好撞上窗口」。
后者在上表 4/4 命中、5 个未截断样本上零误报——因为截断时 `prompt_tokens`
被钉死在 `num_ctx` 上,而 `num_ctx` 几乎总是 2 的幂。

**如果你的端点 `num_ctx` 不是 2 的幂**(`llama.cpp` 允许设 3000 这类值),
指纹失效,只剩比值兜底。请在 PR 里写明。

### ⑤ 错误体形状

至少故意制造三种失败,记下服务端给的原文:

```bash
# 模型名写错
# 凭证写错
# 超出配额(如果能构造)
```

实测两条,说明**原文必须原样带给用户**:
- ollama cloud 的 403 真因是 `this model requires a subscription, upgrade for access`,
  不是「认证失败」
- Google 的 404 真因是 `no longer available to new users. Please update your code
  to use models/gemini-3.6-flash` —— 它直接告诉了你该换哪个模型

## 提 PR 时请附上

- **②的原始 SSE 字节**(首 3 行 + 末 3 行),证明 `[DONE]` 与字段路径不是猜的
- **③的统计输出**,说明是不是推理模型
- **④的实测结果**:报错还是静默截断;若是静默截断,附 `prompt_tokens` 与字符数
- **⑤的三段服务端错误原文**
- 一次真实调用的耗时

登记进 `agents.yaml` 的 `api_endpoints_verified` 块,**不要填 model 名**——
与 `pin_model: false` 同一条理由:实测 `gemini-2.5-flash` 已对新用户下线,
硬编码模型名等于埋定时炸弹。model 由用户在自己的名册里填。

**没跑完这五步的端点只进 `api_endpoints_unverified`,只列出、不背书。**
与「未登记的 CLI 只列出、绝不调用」是同一条纪律。
