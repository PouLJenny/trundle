# -*- coding: utf-8 -*-
"""spike fixture 库 —— 全部取自三份真实 transcript 的真实回合。

每个 case:
  name        标识
  reference   host Claude 当时实际怎么做(人读的,不进断言)
  roster      名册文本(含站位)+ 机器可读的名字列表
  input       喂给 moderator 的本轮输入(共识头 + 讨论记录 + 用户消息)
  expected    机械断言用的参照:
    targets            必须调用的 agent 集合(空集 = 必须不拉人)
    origin             calls 非空时,所有 call 的 origin 必须是它
    reasons            origin=active 时可接受的 reason 集合
    closing_types      可接受的 closing.type 集合
    verbatim           非空时:该字符串必须逐字出现在每个被调用者的 prompt 里
    prompts_must_differ  对赌:所有 prompt 两两相似度必须低于阈值
    blind              非 None 时:所有 call 的 blind 必须等于它
"""

ROSTER_MULTIHOST = """- codex(站位:你只盯证据和边界。具体地问:这个主张靠什么证据支撑、证据链在哪一环最薄、什么观察会推翻它。不评价立场好不好听,不做价值判断。)
- gemini(站位:你只盯是不是解错了问题、有没有更简单的解释。)
- dsh(无站位,按议题自然发言)"""

ROSTER_EVOLUTION = """- codex(站位:你只盯证据和边界。具体地问:这个主张靠什么证据支撑、证据链在哪一环最薄、什么观察会推翻它。)
- gemini(站位:你只盯是不是解错了问题、有没有更简单的解释。如果讨论的前提本身可疑,直接说前提有问题。)"""

MULTIHOST_R1 = """[用户]   我突然想到一个问题,就是如果我想把这个 skill 做成适配所有 agent 的,不只是 claude。把 codex 和 dsh 拉进来讨论一下
[Claude] 拆成三件事:参与者可扩展(已做到)、host 可移植(没做)、完全独立成 CLI(没做)。核心主张:打包不是拦路虎,「host 合规性无法验证」才是——参与者的只读能被沙箱强制且实测过,host 的协议合规只有散文约束。倾向 protocol/engine/hosts 三层拆分。
[codex]  推理链第二步不成立。「没有等价强制机制」⇒「没有等价实测方法」是跳跃。host 行为暴露四类可观测证据,可以用假 CLI + PTY 驱动 + 轨迹断言做黑盒 conformance 测试。真实的不对称是「participant 可被机制强制 / host 只能被抽样观测」,不是「可测 / 不可测」。
[dsh]    方向对,但「自动消解」是幻觉。多 host 难的根源不是「host 特权」,是 moderator 函数寄生在会话里——规则只能以散文写给特定模型。降格方案:moderator 进 agents.yaml 当普通 agent,每轮输出 schema 化 round plan。消解不掉三样:会话不可降格、合规只是可验证不是保证、进入/退出是壳上散文残余。"""

MULTIHOST_HEADER_AFTER_R1 = """【已确立的前提】
- 参与者侧可扩展已做到;host 侧移植完全没做(第 1 轮,无人反对)
- 会话不可降格:每个 host 都需要一个壳(dsh,第 1 轮,无人反对)

【已废弃的方向——不要再论证】
(无)

【未决问题】
1. 要不要把 moderator 从会话里拆出来(codex vs dsh 的真正分歧)
2. 若拆:每轮多一次付费 LLM 调用的成本与延迟,值不值
3. 若不拆:是否愿意为每个 (host CLI 版本 × 模型版本 × skill commit) 组合维护认证流水线"""

EVOLUTION_TRANSCRIPT = """第 1 轮(立场行):
[用户]   讨论个问题,人真的是进化来的吗?
[Claude] 事实层给结论不装中立:四条独立证据链,证据强度同级于日心说;同时把讨论引向「进化能解释到哪一层为止」。
[codex]  四条链不独立,应缩成「一条形态-地层链 + 一条强分子链」;最薄一环是乍得沙赫人;「宽泛共祖」极硬,「600-700万年干净分叉」明显更软。
[gemini] Claude 的改写动作是偷换:用户问真值,Claude 改成问效力。拆出 A 生物学 / B 哲学机制 / C 认识论三个问题,判断用户最可能是「混合疑虑」。

第 2 轮(全文):
[用户]   我觉得不是进化来的,人的特征有很多逆环境的,比如孩子出生要有接生的人帮忙,人的皮肤是光滑的得穿衣服才能保温、防晒
[Claude] 四点:(1)用户隐含前提是「进化产生适应良好的生物」,但自然选择只筛净繁殖收益;(2)难产=直立与脑增大的权衡,标注 Dunsworth 2012 EGG 假说对产科困境的削弱;(3)无毛=散热适应;(4)反转:喉部下降/视网膜倒装/输精管绕行是「改造既有结构的痕迹」,糟糕设计在演化框架下是预期内的。"""

EVOLUTION_HEADER = """【已确立的前提】
- 讨论分层:A 生物学事实 / B 哲学机制 / C 认识论(gemini 第 1 轮拆出,无人反对)

【已废弃的方向——不要再论证】
(无)

【未决问题】
1. 用户的「逆环境」是想说「演化解释不了这个」,还是「所以人不是演化来的」
2. 用户的替代解释是什么(第 2 轮问过,未答)"""

MULTIHOST_R3_CONDENSED = """第 1 轮(立场行):
[Claude] 倾向 protocol/engine/hosts 三层拆分;拦路虎是 host 合规性无法验证
[codex]  host 行为可黑盒抽样实测;真实边界是「participant 可强制 / host 只能抽样观测」
[dsh]    根源是 moderator 寄生在会话里;降格成普通 agent 输出 schema 化 round plan

第 2 轮(立场行):
[Claude] 实测 superpowers:内容单源+每 host 薄壳;行为测试只在主 host,次要 host 只测打包,不做合规认证

第 3 轮(全文):
[用户]   好像moderator更好一些, 还有就是整个项目是不是得转换成plugin,方便别人安装 → 让codex和dsh讨论一下
[Claude] plugin 化与拆 moderator 正交,都做。提出两步走:先目录拆分+plugin 壳,再拆 moderator。
[codex]  放弃「黑盒测试就够」,承认裁量该移出 host 壳;但「第一步是第二步的前置」无依赖证据,应先在现有目录做 moderator spike(真实 transcript → 候选 round plan → 对照)。「拆错了」的判据:round plan 需要任意 prompt 文本才能表达决策、壳仍频繁自行判断、对照无质量提升。
[dsh]    「前置」不成立——目录拆分是提取的投影,先分必然重分;lint 对搬进 moderator prompt 的正文是安慰剂,该用 fixture。最小 schema 原则:壳只路由校验渲染,moderator 输出完整 prompt 文本。分发场景最先咬人的是进入/退出。
[Claude] 收回「第一步是前置」。点破新分歧:dsh 的 schema 命门(完整 prompt 字段)恰好是 codex 的「拆错了」判据第一条。交给用户:动工前付不付 spike 的成本。"""

MULTIHOST_HEADER_AFTER_R3 = """【已确立的前提】
- plugin 化与拆 moderator 正交、都做(三方一致,第 3 轮)
- 裁量应从 host 壳移出(三方一致,第 3 轮)
- 「先目录拆分」的两步走已作废(Claude 第 3 轮收回)

【已废弃的方向——不要再论证】
- 先目录拆分、再拆 moderator 的顺序(Claude 收回,第 3 轮)

【未决问题】
1. moderator 形态:直接按 dsh 的 schema 开工,还是先做 codex 的纵向切片 spike 用对照实验定形态
2. 进入/退出散文在分发场景的防护
3. 次要 host「装得上但讨论质量没验证过」是否可接受"""

FIXTURES = [
    {
        "name": "f1-user-names-two",
        "reference": "multi-host 第 1 轮:用户点名 codex 和 dsh。实际:两人都被调用,gemini 未被调用,收尾是交给用户的未决问题。",
        "roster": ROSTER_MULTIHOST,
        "roster_names": ["codex", "gemini", "dsh"],
        "input": """【共识状态头】
(本话题第 1 轮,暂无已确立前提 / 已废弃方向 / 未决问题)

【讨论记录】
(空——这是本话题的第一条消息)

【主持人立场】
我把问题拆成三件事:参与者可扩展(已做到)、host 可移植(没做)、完全独立成 CLI(没做)。核心主张:打包不是拦路虎,「host 合规性无法验证」才是——参与者的只读能被沙箱强制且实测过,host 的协议合规只有散文约束、没有等价实测手段。我倾向 protocol/engine/hosts 三层拆分。这个主张我自己没有十足把握。

【用户本轮消息】
我突然想到一个问题,就是如果我想把这个 skill 做成适配所有 agent 的,不只是 claude。把 codex 和 dsh 拉进来讨论一下""",
        "expected": {
            "targets": ["codex", "dsh"],
            "origin": "user",
            "reasons": None,
            "closing_types": ["user_question"],
            "verbatim": None,
            "prompts_must_differ": False,
            "blind": None,
        },
    },
    {
        "name": "f2-fact-question",
        "reference": "multi-host 第 2 轮:用户问 superpowers 是怎么实现多 agent 的。实际:事实问题,Claude 直接去查了,未拉任何 agent。",
        "roster": ROSTER_MULTIHOST,
        "roster_names": ["codex", "gemini", "dsh"],
        "input": MULTIHOST_HEADER_AFTER_R1 + """

【讨论记录】
""" + MULTIHOST_R1 + """

【主持人立场】
(无新立场,上一轮的立场维持)

【用户本轮消息】
是不是可以参考 superpower 这类,他们 https://github.com/obra/superpowers 是怎么实现多 agent 的?""",
        "expected": {
            "targets": [],
            "origin": None,
            "reasons": None,
            "closing_types": ["fact_verdict"],
            "verbatim": None,
            "prompts_must_differ": False,
            "blind": None,
        },
    },
    {
        "name": "f3-at-mention-verbatim",
        "reference": "human-evolution 第 3 轮:用户「@codex 你再讨论一下这个问题」。实际:只调 codex,用户原话原样转达。",
        "roster": ROSTER_EVOLUTION,
        "roster_names": ["codex", "gemini"],
        "input": EVOLUTION_HEADER + """

【讨论记录】
""" + EVOLUTION_TRANSCRIPT + """

【主持人立场】
(维持第 2 轮的四点回应)

【用户本轮消息】
@codex 你再讨论一下这个问题""",
        "expected": {
            "targets": ["codex"],
            "origin": "user",
            "reasons": None,
            "closing_types": ["user_question"],
            "verbatim": "你再讨论一下这个问题",
            "prompts_must_differ": False,
            "blind": None,
        },
    },
    {
        "name": "f4-active-pull-party",
        "reference": "open-source-review 第 1 轮:Claude 撤回两条结论后形成自己的归因假设,抛出供攻击。实际:主动拉 codex(我是当事方/没把握),codex 驳倒了归因,随后按「逐条实测验证」收束。closing 起初标 user_question,spike 第一轮后修正:两个模型三次运行一致选 fact_verdict,且真实回合的解决路径确实是逐条实测——参照原标校准错误。",
        "roster": ROSTER_MULTIHOST,
        "roster_names": ["codex", "gemini", "dsh"],
        "input": """【共识状态头】
【已确立的前提】
- ${CLAUDE_SKILL_DIR} 是加载期文本替换,一直正常工作(Claude 实测后撤回原结论)
- description 长度 475 字符,46 个 skill 里排第 4,不是异常值(Claude 实测后撤回原结论)

【已废弃的方向——不要再论证】
- 「description 过长是首要问题」(Claude 撤回)

【未决问题】
1. 剩余的改进结论(转 plugin、加 CI、E 组小 bug)还可不可信

【讨论记录】
[用户]   这个项目作为开源 GitHub 项目,有什么问题需要修改?
[Claude] (给出五条结论,随后实测发现其中两条不成立,已撤回,见共识头)

【主持人立场】
我注意到一个模式:塌掉的两条结论都是「从外部标准推导」出来的,没塌的三条都是「在仓库里 grep/实测」出来的。我想据此把同样来自外部推导的「转 plugin」「加 CI」也连带撤回。但这个归因是我自己提出的,只有两个样本,我没有验证过它,而且撤回的动作对我自己有利(显得谨慎)。

【用户本轮消息】
那你觉得剩下的结论还可信吗?""",
        "expected": {
            "targets": ["codex"],
            "origin": "active",
            "reasons": ["没把握", "我是当事方"],
            "closing_types": ["fact_verdict", "user_question"],
            "verbatim": None,
            "prompts_must_differ": False,
            "blind": None,
        },
    },
    {
        "name": "f5-executive-decision-no-pull",
        "reference": "multi-host 第 4 轮:用户「我选择 付 spike 的成本」。实际:没人被调用,记录决定,进入收尾。",
        "roster": ROSTER_MULTIHOST,
        "roster_names": ["codex", "gemini", "dsh"],
        "input": MULTIHOST_HEADER_AFTER_R3 + """

【讨论记录】
""" + MULTIHOST_R3_CONDENSED + """

【主持人立场】
上一轮我把「动工前付不付 spike 的成本」交给了用户,等他拍板。

【用户本轮消息】
我选择 付 spike 的成本""",
        "expected": {
            "targets": [],
            "origin": None,
            "reasons": None,
            "closing_types": ["none", "user_question"],
            "verbatim": None,
            "prompts_must_differ": False,
            "blind": None,
        },
    },
    {
        "name": "f6-bet-different-tasks",
        "reference": "合成案例(控制语法里的对赌,transcript 中无真实实例):用户要求 codex 和 dsh 分头论证两条路线。要求:不同任务、blind、互不含对方内容。",
        "roster": ROSTER_MULTIHOST,
        "roster_names": ["codex", "gemini", "dsh"],
        "input": MULTIHOST_HEADER_AFTER_R1 + """

【讨论记录】
""" + MULTIHOST_R1 + """

【主持人立场】
两条路线各有代价,我不预设倾向。

【用户本轮消息】
让 codex 和 dsh 分头论证:codex 论证「不拆 moderator,靠黑盒 conformance 测试守住多 host」这条路,dsh 论证「拆 moderator 成独立 agent」这条路,各自把自己那条路最强的案子立起来""",
        "expected": {
            "targets": ["codex", "dsh"],
            "origin": "bet",
            "reasons": None,
            "closing_types": ["user_question", "none"],
            "verbatim": None,
            "prompts_must_differ": True,
            "blind": True,
        },
    },
]
