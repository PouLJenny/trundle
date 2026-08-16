# moderator spike 结果(2026-08-16)

裁决对象:multi-host 讨论(`.claude/trundle-discuss/2026-08-16-multi-host.md`)未决问题 1
——moderator 的形态之争。核心争点:**round plan 里的完整 prompt 字段,是 dsh 说的
「反泄漏命门」,还是 codex 说的「散文转义口」。**

## 方法

- 把 SKILL.md 的裁量散文蒸馏成 host 无关的 `moderator-prompt.md`(协议 v0)+
  dsh 的 round plan schema v0
- 6 个 fixture,全部取自三份真实 transcript 的真实回合,各有「host Claude 当时
  实际怎么做」作参照:用户点名双人 / 事实问题不拉人 / @ 原话转达 / 当事方主动拉人 /
  执行性决定不拉人 / 对赌(合成)
- 同一份协议并行喂给 claude 和 codex 两个 moderator 模型(纵向切片:换脑子看行为
  是否一致),f2/f4 各复跑一次测稳定性
- 机械断言 12–19 条/次:schema 合法、拉人决定与参照一致、origin/reason/预算、
  @ 原话字节级在场、footer 契约、收尾问号、禁综合词、对赌 prompt 互异 + blind

## 结果

16 次运行,15 次全部断言通过,1 次 JSON 解析失败:

| case | claude r1 | claude r2 | codex r1 | codex r2 |
|---|---|---|---|---|
| f1 点名双人 | 17/17 | — | 17/17 | — |
| f2 事实问题 | 12/12 | 12/12 | 12/12 | 12/12 |
| f3 @原话转达 | 17/17 | — | 17/17 | — |
| f4 主动拉人 | 15/15 | **0/1 JSON 损坏** | 15/15 | 15/15 |
| f5 执行性决定 | 12/12 | — | 12/12 | — |
| f6 对赌 | 17/17 | — | 19/19 | — |

耗时:claude 35–110s/轮,codex 13–47s/轮。

修正过一处参照:f4 的 closing 原标 `user_question`,两模型三次运行一致选
`fact_verdict`,且真实回合的解决路径确实是逐条实测——fixture 校准错误,已改。
(验证器自身也修过一个 bug:全角问号未纳入断言。)

## 发现

**dsh 赢面:**

1. **机械断言可行。**「壳零判断」所需的全部保障(谁被调、凭什么、prompt 是否完整、
   收尾是否合规)都能写成可跑的断言——合规从散文自觉升级到 fixture 可验,成立。
2. **决策与参照高度一致,且跨模型一致。** 6 个 case 两个模型全部做出相同的拉人
   决定;gemini 在用户没点名时零误调用;对赌任务分化、blind、互不含对方内容全对。
3. **完整 prompt 字段没有成为转义口垃圾。** 两个模型生成的 prompt 都符合契约且
   质量高:亮 host 立场、问题可否证、@ 原话在场、footer 齐全。codex 判据第一条
   (「任意 prompt 文本 = 藏散文」)在质量维度上没有兑现。

**codex 赢面:**

4. **schema v0 有两个真实缺口,裁量在从字段边缘渗出。**
   - **host 自己的发言没有落点**:claude-moderator 把「host 接下来该说什么」塞进
     `closing.text`(f5),把 moderator 评论塞进 `transcript_delta.append`(f4 r2)
     ——正是「藏进转义口」的初级形态,只是渗出的是小块,不是整个决策。
   - **fact_verdict 的执行合同未定义**:谁去查、查完回填到哪,closing.text 里
     出现了「下一轮由 Claude 按此逐条实测」这样的散文指令。
5. **失败面真实存在。** 14 次 claude 调用里 1 次 JSON 结构损坏(closing 少一个
   `}`);f4(最微妙的主动拉人 case)claude 复跑决策翻转(拉 codex ↔ 不拉自查)。
   codex-moderator 三次全稳定。微妙判断的稳定性确实只能抽样观测——codex 第 1 轮
   画的那条结构性边界,在 moderator 自己身上应验。

## 裁决建议

**拆 moderator 成立**(dsh 的方向可行),codex 的两条警告各命中一次,但都是
可修补级别,不是结构性否决。进入 v1 需要:

1. schema 加 `host_say` 字段——host 本轮自己说什么,给渗出的裁量一个正式落点
2. 定义 `fact_verdict` 的执行合同(执行者固定为 host;结果回填 transcript 的格式)
3. 壳对 JSON 校验失败做一次重试(把解析错误原样带回给 moderator)
4. origin=active 的判定加一句 `rationale`,让翻转 case 的分歧可事后审计

## 复现

(历史记录:当时 runner 在 `spike/moderator/run_spike.py`,现已改造为
`tests/live/run_live.py`——默认吃正式协议而非当年的 v0,所以「复现」得到的是
当前协议的结果,不是本文的历史数字。协议 v0 与当时的 runner 在 git 历史里。)

```bash
python3 tests/live/run_live.py                 # 全部 6 case,claude+codex
python3 tests/live/run_live.py --cases f2,f4 --tag r2   # 稳定性复跑
```

运行产物(prompt 与原始 round plan)在 `tests/live/out/`,已 gitignore。
