# host 差异与支持状态

本 skill 的裁量在 moderator 协议里、执行在 scripts/ 里,两者都 host 无关;
壳(SKILL.md)也尽量中立。但 host 之间仍有机制差异,这里是映射表——
superpowers 的 Platform Adaptation 模式:**host 特定内容是加法,不是分叉**,
这个文件只补差异,绝不复述协议。

## 支持状态

| host | 打包/安装 | skill 发现 | 讨论行为质量 |
|---|---|---|---|
| Claude Code | ✅ plugin + 软链 | ✅ | ✅ **主 host,行为已验证**(fixture + 活体一致性) |
| Codex CLI ≥ 0.144 | ✅ 实测(见下) | ✅ 实测(能读到 SKILL.md 并理解用途) | ⚠️ **未认证**——只测打包不测行为,superpowers 同款风险姿态 |

「未认证」的确切含义:没有人验证过 codex 作为 host 时会不会忠实执行进入/退出、
降级模式、渲染约定这些壳职责。裁量层风险相对小(moderator 是子进程,与 host
无关);壳职责里最可能出问题的是**进入/退出判定**——它是散文,在每条用户消息
上跑(multi-host 讨论未决问题 #1)。遇到质量问题请开 issue 附对话片段。

## Codex CLI

**安装**(codex 原生识别 `.claude-plugin/` 布局,无需 `.codex-plugin`,
本仓库因此保持单一 plugin 清单——零副本、零 sync 脚本):

```bash
codex plugin marketplace add PouLJenny/trundle   # 或本地路径
codex plugin add trundle@trundle
```

装到 `~/.codex/plugins/cache/trundle/trundle/<版本>/`,**按版本目录整仓拷贝**。
升级:仓库 bump 版本后 `codex plugin marketplace upgrade` 再重新 `plugin add`。

**机制映射**:

| 事项 | Claude Code | Codex |
|---|---|---|
| `<SKILL>` 根 | `${CLAUDE_SKILL_DIR}`(软链)/ `${CLAUDE_PLUGIN_ROOT}/skills/discuss`(plugin) | 无环境变量,就是你读到本 SKILL.md 的所在目录(cache 路径) |
| 长命令超时 | Bash 工具默认 120s,**必须显式 timeout=600000** | 无对应参数;执行 `moderate.py` / `invoke.sh` 时**不要自行提前中断**,它们内置看门狗(590s/540s),会自己收尾并给出失败分类 |
| 名册里与 host 同名的条目 | `claude` 指参与者子进程,不是主持人 | `codex` 指参与者子进程,同理;同一 CLI 兼任 moderator 也没问题——每次调用都是无状态子进程 |
| 数据路径 | 同一套,host 无关 | 同上:名册走 `resolve_roster()` 链,transcript 在 `<项目>/.trundle/`(旧路径回退)——**换 host 接着聊,读写的是同一份** |

**其余一切照 SKILL.md 与 protocol/moderator.md 执行,没有 codex 特例。**
stderr 的 `···` 行照样忽略、`===AGENT` / `===PLAN` 分段照样解析、
参与者只读约束由 invoke.py 强制(与谁当 host 无关)。

## 加新 host 时

1. 先实测打包与 skill 发现,能装能读再写进上表——没验证的行写进来是负债
2. 机制差异**只写差异**,一旦发现自己在复述协议或壳的正文,停下——那是漂移的开端
3. 行为质量列如实标注;想升级到「已验证」,给 tests/live/run_live.py
   加该 host 的驱动跑活体一致性,别靠感觉
