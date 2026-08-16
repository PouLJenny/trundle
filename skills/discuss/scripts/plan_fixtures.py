# -*- coding: utf-8 -*-
"""trundle discuss —— round plan 罐头 fixture(纯数据,零 I/O)

给 selftest.py 的 plan 校验组用:GOOD_PLANS 必须零 hard 失败,
BAD_PLANS 每条必须命中指定的 check。全部是罐头 JSON,不调任何 CLI——
这是 selftest 的硬约束(见其 docstring)。

CORRUPTED_RAW 是**真实样本**,不是合成的:spike 第一轮里 claude-moderator
在 f4 复跑时产出的损坏 JSON(closing 对象少一个 `}`,consensus 与
transcript_delta 被嵌进 closing,末尾大括号不配平)。14 次调用出 1 次。
spike/moderator/out/ 被 gitignore,这里是它唯一的持久化落点——
它证明 extract_json 的三级容忍接不住结构性损坏,重试路径必须存在。
"""


def _prompt(who, ask):
    """构造一份满足契约的最小 prompt:骨架 + footer 四字段,长度 >= 200。"""
    return (
        "你是 %s,正在参加一场技术讨论。讨论对象是 trundle 项目的多 host 改造。\n\n"
        "【已确立的前提】\n- 参与者侧可扩展已做到(第 1 轮,无人反对)\n\n"
        "【未决问题】\n1. 要不要把 moderator 从会话里拆出来\n\n"
        "【讨论记录】\n[用户]   如果我想把这个 skill 做成适配所有 agent 的呢\n"
        "[Claude] 我倾向三层拆分,核心主张:打包不是拦路虎,合规性才是。\n\n"
        "【这一轮请你回答】\n%s\n\n"
        "回答完在最后附上:\n立场:<一句话>\n同意:<你接受对方哪几点,没有就写\"暂无\">\n"
        "不同意:<具体哪一点 + 为什么,指名道姓说是谁的哪句话>\n"
        "如果我错了:<一句可验证的条件>\n" % (who, ask)
    )


ROSTER_NAMES = ["codex", "gemini", "dsh"]

_ASK = "我主张用固定窗口,理由是写入均匀。这个理由站得住吗?如果写入不均匀,窗口方案会在哪里先崩?"

GOOD_PLANS = [
    ("no-pull", {
        "calls": [],
        "host_say": "直接回应用户:先点破上一轮遗留的分歧在前提层,再把决定权还给用户。不下综合结论。",
        "closing": {"type": "user_question", "text": "你们的退款要不要跟渠道对账?"},
        "consensus": {"verdict": "na", "boundary": "本轮无多方发言可判。"},
        "transcript_delta": {"append": "[用户] 那就按比例退款吧,简单", "direction_change": None},
    }),
    ("user-named", {
        "calls": [{
            "target": "codex", "origin": "user", "reason": None,
            "rationale": None, "blind": False,
            "prompt": _prompt("codex", "[用户] @codex 你说的对账问题不成立\n先直接回应用户这句话(接受还是不接受、为什么),再补别的。"),
        }],
        "host_say": "转达已按原话进行;我在自己这段指出用户忽略了手续费记录,但不改转达内容。",
        "closing": {"type": "user_question", "text": "如果对账确实免不了,你能接受退款走代金券吗?"},
        "consensus": {"verdict": "disagree", "boundary": "分歧在要不要做,不在实现;双方前提差在退款频率。"},
        "transcript_delta": {"append": "[用户] @codex 你说的对账问题不成立", "direction_change": None},
    }),
    ("active-pull", {
        "calls": [{
            "target": "codex", "origin": "active", "reason": "我是当事方",
            "rationale": "窗口方案是 host 自己提的,写入均匀这个假设没验证过,答案会决定选不选它。",
            "blind": False,
            "prompt": _prompt("codex", _ASK),
        }],
        "host_say": "亮明我是方案提出方,请 codex 攻击写入均匀假设;codex 回来后先复述它的反对再表态。",
        "closing": {"type": "user_question", "text": "你们的写入是均匀的还是按时间聚集的?"},
        "consensus": {"verdict": "na", "boundary": "等 codex 发言后再判。"},
        "transcript_delta": {"append": "[用户] 这块我不太确定,你看着办", "direction_change": None},
    }),
    ("bet", {
        # 盲开对赌:两份 prompt 各自成文,不共享讨论记录(blind 的题中之义),
        # 也顺便保证 bet_prompts_differ 的相似度断言有真实区分度。
        "calls": [
            {"target": "codex", "origin": "bet", "reason": None, "rationale": None,
             "blind": True,
             "prompt": (
                 "你是 codex,正在参加一场技术讨论的分头论证环节。\n\n"
                 "【你的任务】请为「固定窗口」这条路立最强的案子:窗口大小按什么定、"
                 "写入不均匀时兜底机制是什么、迁移成本落在哪里、监控指标怎么选。"
                 "把它当成你要辩护到底的方案,不做中立评估,不讨论对立路线,"
                 "论证必须落到可操作的实现层,不接受泛泛的原则陈述。\n\n"
                 "回答完在最后附上:\n立场:<一句话>\n同意:<暂无即写暂无>\n"
                 "不同意:<具体哪一点 + 为什么,指名道姓>\n如果我错了:<一句可验证的条件>\n")},
            {"target": "dsh", "origin": "bet", "reason": None, "rationale": None,
             "blind": True,
             "prompt": (
                 "你是 dsh,这一轮是分头论证。\n\n"
                 "【你的任务】为「全量重算」建立最强论证:重算的成本上限如何收束、"
                 "什么规模之后必须增量化、正确性验证怎么做、失败重跑的幂等性怎么保证。"
                 "辩护到底,禁止中立骑墙,禁止提及其他候选方案,"
                 "每一条论据都要给出可以被反驳的具体断言而不是口号。\n\n"
                 "回答完在最后附上:\n立场:<一句话>\n同意:<暂无即写暂无>\n"
                 "不同意:<具体哪一点 + 为什么,指名道姓>\n如果我错了:<一句可验证的条件>\n")},
        ],
        "host_say": "对赌进行中:只渲染双方发言,不加倾向性评论,禁止中途加人踢人。",
        "closing": {"type": "none", "text": ""},
        "consensus": {"verdict": "na", "boundary": "对赌首轮,互为盲开。"},
        "transcript_delta": {"append": "[用户] 让 codex 和 dsh 分头论证窗口和全量", "direction_change": None},
    }),
    ("fact-verdict", {
        "calls": [],
        "host_say": "这是事实分歧,不端给用户:我当轮实测,把结果以事实裁定行回填 transcript。",
        "closing": {"type": "fact_verdict",
                    "text": "查证 invoke.py 的 MAXWALL 默认值:读 scripts/invoke.py 顶部常量并核对 agents.yaml 的 max_wall 字段是否一致。"},
        "consensus": {"verdict": "disagree", "boundary": "分歧是 540 还是 600,可实测,不交用户。"},
        "transcript_delta": {"append": "[用户] 我记得上限是 600 吧?", "direction_change": None},
    }),
]

# (名字, plan, 必须命中的 check 名)
# 断言口径:目标 check 必红 + GOOD_PLANS 全绿;不强求 BAD 恰好一条红,
# schema 违规常连锁(比如 calls 是 dict 时后续检查全跳)。
_UQ = {"type": "user_question", "text": "窗口够用吗?"}
_OK_CONS = {"verdict": "na", "boundary": ""}
_OK_DELTA = {"append": "[用户] x", "direction_change": None}
_SAY = "本轮由 host 直接回应。"

BAD_PLANS = [
    ("target 不在名册", {
        "calls": [{"target": "aider", "origin": "user", "reason": None,
                   "rationale": None, "blind": False, "prompt": _prompt("aider", _ASK)}],
        "host_say": _SAY, "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "targets_in_roster"),
    ("origin 非法", {
        "calls": [{"target": "codex", "origin": "auto", "reason": None,
                   "rationale": None, "blind": False, "prompt": _prompt("codex", _ASK)}],
        "host_say": _SAY, "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "origin_enum"),
    ("active 超预算", {
        "calls": [
            {"target": "codex", "origin": "active", "reason": "没把握",
             "rationale": "假设未验证。", "blind": False, "prompt": _prompt("codex", _ASK)},
            {"target": "dsh", "origin": "active", "reason": "没把握",
             "rationale": "假设未验证。", "blind": False, "prompt": _prompt("dsh", _ASK)},
        ],
        "host_say": _SAY, "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "active_budget"),
    ("active 缺 rationale", {
        "calls": [{"target": "codex", "origin": "active", "reason": "没把握",
                   "rationale": None, "blind": False, "prompt": _prompt("codex", _ASK)}],
        "host_say": _SAY, "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "active_has_rationale"),
    ("prompt 缺 footer", {
        "calls": [{"target": "codex", "origin": "user", "reason": None,
                   "rationale": None, "blind": False,
                   "prompt": "你是 codex。" + "上下文补齐。" * 40 + "请回答问题。"}],
        "host_say": _SAY, "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "footer_contract:codex"),
    ("prompt 过短", {
        "calls": [{"target": "codex", "origin": "user", "reason": None,
                   "rationale": None, "blind": False, "prompt": "回答一下。立场 不同意 如果我错了"}],
        "host_say": _SAY, "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "prompt_substantial"),
    ("closing 无问号", {
        "calls": [], "host_say": _SAY,
        "closing": {"type": "user_question", "text": "你自己看着定吧。"},
        "consensus": _OK_CONS, "transcript_delta": _OK_DELTA,
    }, "closing_ends_question"),
    ("closing 含综合词", {
        "calls": [], "host_say": _SAY,
        "closing": {"type": "user_question", "text": "综合来看你觉得选 A 好吗?"},
        "consensus": _OK_CONS, "transcript_delta": _OK_DELTA,
    }, "closing_no_synthesis_words"),
    ("缺 host_say", {
        "calls": [], "closing": _UQ, "consensus": _OK_CONS,
        "transcript_delta": _OK_DELTA,
    }, "host_say_present"),
    ("fact_verdict 空文本", {
        "calls": [], "host_say": _SAY,
        "closing": {"type": "fact_verdict", "text": "  "},
        "consensus": _OK_CONS, "transcript_delta": _OK_DELTA,
    }, "fact_verdict_text_nonempty"),
    ("对赌 prompt 雷同", {
        "calls": [
            {"target": "codex", "origin": "bet", "reason": None, "rationale": None,
             "blind": True, "prompt": _prompt("codex", _ASK)},
            {"target": "dsh", "origin": "bet", "reason": None, "rationale": None,
             "blind": True, "prompt": _prompt("dsh", _ASK)},
        ],
        "host_say": _SAY, "closing": {"type": "none", "text": ""},
        "consensus": _OK_CONS, "transcript_delta": _OK_DELTA,
    }, "bet_prompts_differ"),
    ("对赌 blind 缺失", {
        "calls": [
            {"target": "codex", "origin": "bet", "reason": None, "rationale": None,
             "blind": None, "prompt": _prompt("codex", "论证 A 路线:窗口方案的最强案子。")},
            {"target": "dsh", "origin": "bet", "reason": None, "rationale": None,
             "blind": True, "prompt": _prompt("dsh", "论证 B 路线:全量重算的最强案子。")},
        ],
        "host_say": _SAY, "closing": {"type": "none", "text": ""},
        "consensus": _OK_CONS, "transcript_delta": _OK_DELTA,
    }, "bet_blind_flag"),
    ("consensus 枚举非法", {
        "calls": [], "host_say": _SAY, "closing": _UQ,
        "consensus": {"verdict": "maybe", "boundary": ""},
        "transcript_delta": _OK_DELTA,
    }, "consensus_enum"),
    ("delta.append 为空", {
        "calls": [], "host_say": _SAY, "closing": _UQ,
        "consensus": _OK_CONS,
        "transcript_delta": {"append": "", "direction_change": None},
    }, "delta_append_nonempty"),
    ("schema 形状错", {
        "calls": {"target": "codex"}, "host_say": _SAY, "closing": _UQ,
        "consensus": _OK_CONS, "transcript_delta": _OK_DELTA,
    }, "schema_shape"),
]

# 真实损坏样本(claude-moderator,spike f4 复跑,2026-08-16):closing 少一个 },
# consensus/transcript_delta 被嵌进 closing,末尾大括号不配平。原样保存,勿修。
CORRUPTED_RAW = '{"calls": [], "closing": {"type": "fact_verdict", "text": "「剩余结论可不可信」不该由『外部推导 vs 仓库实测』这个只有两个样本、未经验证的归因模式来连带裁决——它本身可以被绕过:直接把剩下三条按已塌两条同样的实测标准逐条查证。要查证什么、怎么查:① 「加 CI」——ls .github/workflows/ 并读取现有 workflow 的触发条件与步骤,确认该结论声称的缺口(无 CI 或 CI 未覆盖关键检查)是否真实存在;注意 git log 里已有『加 CI 守住它』的提交,先确认这条结论是否已经过时。② 「转 plugin」——把该结论依赖的前提逐条落地实测:它假设的当前分发方式是什么(读 README/安装说明),plugin 机制对本仓库的目录结构是否兼容(对照 plugin 规范查),能查的先查,查完剩下的才是真正的判断题。③ 「E 组小 bug」——逐条在代码中定位并尝试复现,能复现的保留,不能复现的撤回。判定标准与已塌两条一致:实测不过即撤回;实测通过则不因『来源是外部推导』被连带撤回。查证结果直接更新共识状态头的未决问题 1。", "consensus": {"verdict": "na", "boundary": "本轮无多方发言可判。既有共识仅覆盖已实测的两条撤回结论,未覆盖剩余三条;host 提出的『外部推导必塌』归因只有两个样本且未经任何一方检验,不构成共识。"}, "transcript_delta": {"append": "[用户] 那你觉得剩下的结论还可信吗?\\n[moderator] 判定为事实查证轮:剩余三条结论(转 plugin、加 CI、E 组小 bug)按与已塌两条相同的实测标准逐条查证,不采用 host 提出的按来源连带撤回;host 的两样本归因模式记录在案但未被采信。", "direction_change": null}}'

FENCED_RAW = """好的,这是本轮的计划:

```json
{"calls": [], "host_say": "直接回应。", "closing": {"type": "none", "text": ""},
 "consensus": {"verdict": "na", "boundary": ""},
 "transcript_delta": {"append": "[用户] ok", "direction_change": null}}
```

以上就是我的输出。"""

CHATTER_RAW = """本轮不需要拉人。{"calls": [], "host_say": "直接回应。",
"closing": {"type": "none", "text": ""},
"consensus": {"verdict": "na", "boundary": ""},
"transcript_delta": {"append": "[用户] ok", "direction_change": null}} 完毕。"""
