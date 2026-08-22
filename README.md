# trundle

[![CI](https://github.com/PouLJenny/trundle/actions/workflows/ci.yml/badge.svg)](https://github.com/PouLJenny/trundle/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

> **trundle** — turns your solo Claude Code conversation into a turn-based group chat (skill: `/trundle:discuss`).
> Bring codex, gemini, and other CLI agents into the technical discussion you're already having — **you stay in as a participant, not someone waiting for a report**.

**Claude Code is a party to the discussion; each round's "who speaks, what they're fed, how to wrap up" comes from a plan produced by an independent moderator agent; the other CLI agents you have installed are the participants pulled in.**

---

## What is this

Rather than describe it, here's a real round:

```
You: So should billing cycles align to calendar months or subscription dates?

Claude: (answers directly — pulls in no one)

You: Let's just do pro-rated refunds then, simple

Claude: I lean toward agreeing, but that judgment rests on "refund volume
        is small", which I haven't verified. Let me have codex poke at it
        from the implementation-cost angle.

  codex · 8.2s
  The cost of pro-rated refunds isn't in the arithmetic, it's in
  reconciliation. Every refund produces a channel-fee record...

    Position: confirm the refund volume first, then decide whether to go
              with vouchers instead
    Disagree: Claude says "simple to implement" — only if you never have
              to reconcile with the payment channel
    If I'm wrong: if channel fees don't need to be refunded, then yes,
              it really is simple

Claude: codex disagrees with my proposal. The disagreement isn't about
        implementation — it's about **whether to do this at all**.

        I assumed refunds are low-frequency, so pro-rating is good enough;
        codex assumes you have multiple payment channels and every refund
        gets reconciled, in which case the fees make "pro-rated" not add up.

        **Our actual disagreement is: do your refunds need to reconcile
        against the channel?** If yes, my proposal doesn't hold.

─── Discussion mode · codex + gemini · round 3 · open: 2 ───
```

Note the last block: **Claude does not produce an "all things considered, I recommend A" — it reduces the disagreement to a question only you can answer.** That is the core of this project.

## Core design

These four points are the entire value of this project:

- **By default, nobody speaks.** In a 10-round discussion, proactively pulling someone in should happen once or twice. Summoning everyone every round degrades into three paragraphs of mutual nodding — plus a wasted 15-second wait.
- **Don't synthesize — put the disagreement on the table.** Claude is a party, not a referee. Saying "all things considered, I recommend A" while a disagreement is unresolved is a protocol violation — it means playing both player and referee, and you lose the judgment call that was yours to make.
- **Let agents hear each other.** The context fed to each agent includes the other agents' previous-round statements, **with attribution**. That is the single switch that turns "three independent reviews" into a discussion — attribution produces targeted rebuttals; without it everyone just talks past each other.
- **Agreement ≠ correctness.** Fixed stances manufacture false consensus: give gemini the stance "watch for whether we're solving the wrong problem" and it will almost inevitably say "you're solving the wrong problem". When everyone agrees, first suspect that the way the question was asked fed them the answer.

## How it works

Three roles, three sets of responsibility:

- **host (Claude Code)** — a **party** to the discussion: states its own position, argues with you, gets rebutted by others. Also the executor: makes the calls per plan, renders output, keeps the transcript.
- **moderator (independent CLI agent, defaults to codex, configurable in the roster)** — each round's **discretion**: who speaks, how prompts are composed, how to wrap up, emitted as a mechanically-checkable round plan (protocol: [`skills/discuss/protocol/moderator.md`](skills/discuss/protocol/moderator.md), in Chinese). Measured 13–47s/round; a broken plan is retried once with feedback attached; if the moderator is absent entirely, the host presides itself under the same protocol (degraded mode, announced explicitly).
- **participants (codex / gemini / claude / dsh subprocesses)** — **read-only throughout**, stateless on every call.

## Installation

**Option 1: plugin (recommended)** — inside Claude Code:

```
/plugin marketplace add PouLJenny/trundle
/plugin install trundle@trundle
```

Restart the session afterwards; the skill name is `/trundle:discuss`.

**Option 2: symlink (developer path)** — the skill name is `/discuss`:

```bash
git clone https://github.com/PouLJenny/trundle.git
cd trundle
./skills/discuss/scripts/install.sh    # symlinks into ~/.claude/skills/
./skills/discuss/scripts/verify.sh     # self-checks dependencies
```

**Pick one of the two** — installing both gives you two identical skills. Note for existing users: if you previously symlink-installed the old version (directory name trundle-discuss), run `rm ~/.claude/skills/trundle-discuss` first, or old and new will coexist; your roster and discussion transcripts haven't moved and keep working in place.

**Codex CLI as host** (≥ 0.144) — codex natively recognizes this repo's plugin layout; the same content installs with zero changes:

```bash
codex plugin marketplace add PouLJenny/trundle
codex plugin add trundle@trundle
```

The support status, stated plainly: **packaging, installation, and skill discovery are all verified on codex; discussion behavior quality is not certified** (the superpowers model: behavior is tested on the primary host, only packaging on secondary hosts). The roster and discussion transcripts live at host-agnostic paths — start a discussion in Claude Code, switch to codex mid-way, and you're reading and writing the same files. Details and mechanism mapping: [`references/hosts.md`](skills/discuss/references/hosts.md) (in Chinese).

**Prerequisites** (only three)

- **One host**: Claude Code (primary host, behavior verified) or Codex CLI ≥ 0.144 (packaging verified, behavior not certified)
- **At least one** participant CLI: `codex` / `gemini` / `claude` / `dsh`
- **python3 >= 3.8** — standard library only, nothing to pip-install. Usually preinstalled on Linux; macOS 12.3+ doesn't ship it, `xcode-select --install` does

No `jq` required — `json` is in the Python standard library; installing jq on top is a dependency paid for nothing.

> ### ⚠️ Required reading for gemini users
>
> **gemini must run in a trusted directory**, or its model routing degrades to an unstable preview branch — measured 7 failures out of 8 requests, with wall-clock time worsening from 14 seconds to 108–199 seconds.
>
> Run `gemini` interactively in that directory once and choose to trust it, or add to `~/.gemini/trustedFolders.json`:
>
> ```json
> { "/path/to/your/project": "TRUST_FOLDER" }
> ```
>
> **Do not bypass the trust check with environment variables.** The way it makes the error disappear is by making latency ten times worse. This project's scripts skip gemini outright when the directory is untrusted and tell you how to fix it, instead of sneaking around it.

**Restart your Claude Code session after installing** — skills don't hot-reload.

## Usage

### Entering discussion mode

Three ways in, **all of them end with your nod** (Claude will not drag you into discussion mode on its own):

```
/trundle:discuss how should we change subscription billing    explicit command
"bring codex in on this"                                      spoken request
```

Or, when you say "I'm not sure about this part", Claude proposes "want me to pull codex and gemini in to discuss?" — you enter only if you nod.

**Why explicit on both ends?** Because this mode changes Claude's behavior — it calls external agents, refuses to hand you a direct conclusion, and puts disagreements back on your table to judge. With implicit entry and exit, you couldn't tell "can't think of an answer" from "withholding an answer per protocol".

### First run: pick your participants

Scans the CLIs available on your machine → you check the boxes → each participant gets a **stance fixed for the whole discussion** → written to the roster at `~/.config/trundle/roster.yaml`. It won't ask again. Older rosters live at `~/.claude/trundle-discuss/roster.yaml` — anything still there is picked up via automatic fallback, zero migration; you can also point at one explicitly with the `TRUNDLE_ROSTER` environment variable. The roster path is host-agnostic: run this skill under a different host later and it reads the same file.

The top of the roster may also carry a `moderator: <name>` line to pick the model that produces each round's plan; the default is `codex` (measured most stable and fastest). If the named one isn't installed or fails preflight, the fallback is **loud** — no silent substitution.

Two participants recommended, three max: parallel-call wait time is set by the slowest one, and the more participants, the more mutual nodding. **Two agents must not share a stance** — a duplicated perspective is fuel for false consensus and a latency bill paid twice.

### Control syntax during discussion

| You say | Effect |
|---|---|
| `@codex <your words>` | Only codex responds; **your words are relayed verbatim** |
| `@codex @gemini <your words>` | Address several at once |
| `@all <your words>` | Everyone responds |
| "have codex and gemini argue A and B separately" | A bet: each gets a different assignment |
| "add cline to the discussion" | Add mid-discussion |
| "drop gemini" | Remove from the roster |
| "skip gemini this round" | Skip for this round only |
| `/trundle:discuss agents` | Re-pick participants |
| "exit discussion" / "let's start writing" | End |

**Words after `@` are relayed verbatim.** You can push back on an agent directly:

```
@codex your reconciliation concern doesn't hold — we only have one channel
```

codex receives exactly that sentence — **not Claude's paraphrase, "the user has concerns about the reconciliation part"**. This matters: your own words are the most pointed input in the whole discussion; processed, they turn into cotton wool, and cotton wool is what comes back.

### Adding / removing participants mid-discussion

**Adding** usually happens when the discussion has spiraled inward and you want a fresh perspective not anchored by the preceding conversation. The newcomer gets a **consensus-state summary plus the last two rounds**, not the full history — the whole value of joining late is being unanchored, and feeding too much history destroys exactly that. Their briefing says explicitly: if you think an established premise is wrong, say so.

**Removing** comes in two kinds: "skip X this round" is temporary; "drop X" is written to the roster. **What a removed participant said stays in the transcript, not deleted, not struck through** — their arguments don't expire because they left.

No adding or removing while a bet is in progress — swapping people destroys the comparison.

### Status line

One line at the end of every round:

```
─── Discussion mode · codex + gemini · round 4 · open: 2 ───
```

Who's participating, which round, and how many questions remain open.

### Exiting

You say so explicitly ("let's start writing"), or Claude asks once — on any of three signals: the same open question has gone two rounds with no new arguments / you start speaking in execution terms / the open-question list is empty.

On exit, a two-or-three-line wrap-up:

```
─── Exited discussion mode ───
Settled in this discussion: billing goes through Stripe, refunds pro-rated
Still unresolved: who absorbs channel fees in multi-channel reconciliation
— you said you'd ask finance
```

**No spec document is generated.** Unless you explicitly ask, the transcript is not "written up into a document" — the moment that happens, this turns back into a report pipeline.

After exit Claude returns to normal: no more agent calls, back to writing code.

## Supported agent CLIs

| CLI | Read-only mode | Measured latency | Notes |
|---|---|---|---|
| `codex` | `--sandbox read-only` | 7–13s | Needs `--skip-git-repo-check` outside a git directory |
| `gemini` | `--approval-mode plan` | 6–14s | **Must run in a trusted directory**, or it degrades to 108–199s |
| `claude` | tool allowlist | 13–30s | Needs `CLAUDECODE` cleared to avoid nested sessions |
| `dsh` | `DSH_PERMISSION_MODE=read-only` (**environment variable, not a flag**) | 4–35s | **No event stream**: silent throughout, full text delivered at once on completion; bounded only by the absolute cap (540s, same as the global default — it was briefly tightened to 300s, but real discussion prompts outgrew it) |

**Only CLIs that have actually been run end-to-end are listed.** Each one is verified: non-interactive invocation works, read-only mode genuinely blocks writes, and output extracts cleanly.

Unadapted CLIs are never called — if you have `opencode`, `cline`, `aider`, or similar installed, the scan reports them as "found but not registered", but will not guess how to invoke them. [PRs to add them are welcome](CONTRIBUTING.md) (in Chinese).

## Adding a new agent CLI

The adapter layer is thin — nine fields cover it: command template / non-interactive flag / read-only constraint / output extraction / event-stream granularity / timeouts / auth / diagnostic command / trust gating.

Note there is **no "stance"** in there — a stance is a position in a discussion, not a property of a CLI; it lives only in your own roster, and it's optional.

**Event-stream granularity must be measured, not guessed.** Timeouts are judged by inter-output gaps — as long as the agent keeps emitting events we keep waiting, and only 90 consecutive silent seconds counts as stuck. So whether a CLI emits token-by-token, stage-by-stage, or holds everything until the end directly determines how timeouts are judged. Guess wrong and you'll reliably kill it.

`dsh` is the third kind: it emits not a single byte from start to finish — at 8.36s, first byte and last byte arrive together. This kind **cannot be saved by raising timeouts**; the idle check must be skipped entirely — otherwise, the moment it outlives the first-byte grace period it gets classified as "never started", and you get sent off to turn a knob that isn't wired to it.

Full steps: [`references/adapting-new-cli.md`](skills/discuss/references/adapting-new-cli.md) (in Chinese).

**The read-only constraint must be verified by running it — never guessed.** Guessing the non-interactive flag wrong just crashes; **guessing the read-only constraint wrong hands the agent write access to your codebase**. That's why, when the scan finds an unregistered CLI, this project only lists it and tells you — it never invokes on its own.

Read-only isn't always a flag either: `dsh` only has an environment variable, and its default is **writable**. That case carries one extra requirement — the script must **override** your environment, not inherit it.

## Privacy & cost

- Transcripts are written to `<your project>/.trundle/` (projects that already have `.claude/trundle-discuss/` keep writing there — records aren't split in two), containing the full discussion content. Take care not to commit it into your own repository
- **Your discussion content is sent to the service provider behind every CLI you selected**
- Every pull-in is a real API call incurring real cost. That's one of the practical reasons for "nobody speaks by default"

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| An agent sits at "starting" until it times out and is marked absent | It **never started working**: quota exhausted / auth expired / network down. In non-interactive mode these errors may emit nothing at all | Run the exact command from the failure notice **manually in a terminal** to see the real error. To wait longer, raise `DISCUSSION_FIRST_BYTE_GRACE` (not `DISCUSSION_IDLE`, which governs silence *after* output has started) |
| gemini goes permanently absent after a few rounds | The free tier is tiny (measured `limit: 5` requests/window); once exhausted it silently backs off and retries without reporting an error | Wait for the quota window to roll over, or switch to paid quota. Let the others speak this round |
| gemini is very slow (100s+), occasionally fails outright | Untrusted directory; model routing degraded to an unstable branch | Add the directory to `trustedFolders.json`. **Don't bypass with environment variables** |
| dsh shows no progress at all, looks stuck | It has **no event stream**: zero stdout for the whole round, full text delivered at once on completion | **This is normal.** The status line shows "running Ns (cap 540s)" instead of a countdown. The only knob that extends the wait is `DISCUSSION_MAX_WALL` — `DISCUSSION_IDLE` and `DISCUSSION_FIRST_BYTE_GRACE` have no effect on it |
| codex reports `Failed to read prompt from stdin` / os error 11 | Under parallel invocation stdin is an unreadable pipe; codex assumes piped input | Append `</dev/null` to the call (the bundled scripts already do) |
| Installed but the skill doesn't trigger | Skills don't hot-reload | Restart the Claude Code session |
| "moderator absent · I'll preside this round myself" | The chosen moderator CLI isn't installed / failed preflight / over budget | Run `verify.sh` to see which CLIs are available; to switch models, put `moderator: <name>` at the top of the roster. Degradation doesn't stop the discussion — discretion just moves back into the host's head |
| Status line mentions a round-plan retry (retry=1) | The moderator occasionally emits corrupted JSON (measured 1 in 14) | Automatic, ignore it; if it retries **every** round, open an issue with the moderate.py output |
| A crowd of agents fights to speak every round | The protocol isn't in effect | By default only Claude should speak — please open an issue |
| Claude produced an "all things considered, I recommend A" | The protocol isn't in effect | With a disagreement unresolved this is a violation — please open an issue |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) (in Chinese). The most valuable contribution is **a new agent CLI adapter entry**.

## Acknowledgements

Inspired by [kitchenloop](https://github.com/0xagentkitchen/kitchenloop).

## License

Apache-2.0
