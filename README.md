# Guts — Claude Code Slack Controller

<img src="assets/guts.png" alt="Guts, the Black Swordsman" width="280">

Control Claude Code terminal sessions from your phone via Slack. Powered by Guts, the Black Swordsman from Berserk — who now swings code instead of the Dragonslayer.

## How It Works

```
Phone (Slack app)
    │
    ▼ (Socket Mode WebSocket)
Slack API
    │
    ▼ (Assistant thread events / Channel messages)
Python server (your Mac)
    │
    ▼ (async subprocess)
claude CLI (--print --output-format stream-json)
    │
    ▼ (parsed JSON events)
Python server → Slack thread replies
    │
    ▼
Phone sees responses in real time
```

1. You DM the Slack bot or mention `@guts` in a channel
2. The Python server receives the message via Socket Mode (WebSocket, no public URL)
3. It spawns the `claude` CLI as a subprocess with `stream-json` output
4. Claude's events (text, tool calls, results) stream back to Slack as thread replies
5. Each thread maps to a persistent Claude session — follow-ups continue the conversation

## What You See in Slack

| Claude Event | Slack Rendering |
|---|---|
| Message received | Skull reaction (ack) |
| Text response | Single message, updated as text streams in |
| Tool call (Read, Bash, etc.) | Status line: `*Bash* \`npm test\`... done` |
| Tool error | Error block with output |
| Completion | Final message with checkmark reaction |
| Skipped (not for Guts) | Bust-in-silhouette reaction |

## Features

- **Guts persona** — Blunt, direct, dark-humored coding assistant from Berserk
- **Persistent sessions** — each thread = one Claude session with full context
- **Join external sessions** — resume Claude sessions running in IntelliJ/terminal from Slack
- **All your MCP servers, plugins, and skills** work (uses your local Claude CLI config)
- **Model selection** — defaults to Sonnet, use `!opus` for Opus
- **Tiered access** — admin (full access) and guest (read-only + PR ops)
- **Auto-intent detection** — just describe what you want, no commands needed
- **PII redaction** — credentials and sensitive data automatically scrubbed from responses
- **Background loops** — schedule a recurring prompt, or let Claude iterate on a task until done, independent of your Slack session
- **Architecture crawl** — point it at any local repo and it builds durable architecture docs (`!crawl`), so future questions get answered from a map instead of a fresh re-read
- **On-call auto-ack** — optionally acks your Zenduty alerts for a window and reports back when it ends
- **Self-modifying** (`!evolve`, admin only) — Guts can edit, validate, commit, and restart its own source in response to a plain-English request. Powerful and genuinely risky — see [Security & Privacy](#security--privacy)
- **Per-person psych profiles** — Guts keeps a private rolling read on everyone it talks to (including guests) to tailor tone; see [Security & Privacy](#security--privacy) before whitelisting anyone
- **Local usage & trace observability** — every run's cost, tokens, duration, and every tool/skill it invoked is logged locally (`!usage`, or the dashboard at `localhost:8767`) — nothing leaves the machine, and it never slows a response down; see [Observability](#observability)
- **No public URL** — Socket Mode uses outbound WebSocket only

## Natural Language Workflows

Just talk naturally — Guts auto-detects what you want:

| What you say | What Guts does |
|---|---|
| "review PR 1234" / "check this PR" | Reads diff, provides structured review |
| "approve PR 1234" / "LGTM" / "ship it" | Reviews and approves the PR |
| "fix the PR comments on 1234" | Uses resolve-pr-comments skill to fix, commit, push |
| "what's our deploy process?" | Searches the work knowledgebase |
| "add retry logic to payments-service" | Checks your repos directory for it, implements the feature |
| "explain src/auth.py" | Reads and explains the file |

## Explicit Commands

These also work if you prefer being explicit:

### Workflows

| Command | Access | Action |
|---|---|---|
| `!review <pr>` | Everyone | Review PR diff, provide feedback |
| `!approve <pr>` | Everyone | Quick review + approve PR |
| `!fix-pr <pr>` | Admin | Resolve PR comments, push fixes |
| `!kb <question>` | Everyone | Search work knowledgebase |
| `!debug <issue>` | Everyone | Investigate an issue — logs/metrics/code → root cause (read-only, invokes a `/troubleshooter` skill if you have one) |
| `!qa <feature> [scenario]` | Admin | Run E2E QA scenarios (invokes a `/qa` skill if you have one) |
| `!evolve <change>` | Admin | ⚠️ Guts edits its own source code, validates, commits, and restarts itself. See [Security & Privacy](#security--privacy) |
| `!help` | Everyone | Show all available commands |

### Session Management (Admin only)

| Command | Action |
|---|---|
| `!sessions` | List all active Claude sessions (IntelliJ, terminal, etc.) |
| `!join <id_or_name>` | Connect this thread to an external Claude session |
| `!opus <prompt>` | Use Opus model for this prompt |
| `!fresh <prompt>` | Start a new session (ignore thread history) |
| `!cd ~/path <prompt>` | Set working directory |
| `!status` | Show Guts-managed sessions |
| `!kill` | Terminate current session (and the session mapping) |
| `!stop` | Kill the Claude subprocess currently running in this thread, keep the session |
| `!leave` | Disconnect this thread from a joined external session |

### External Session Resume

You can interact with Claude sessions running anywhere on your Mac:

1. `!sessions` — lists all active Claude sessions with name, repo, and ID
2. `!join payments-service` — connects the current Slack thread to that session (fuzzy matches by name, repo, or session ID prefix)
3. Send messages — they resume the external session with full conversation history

**Note:** This uses `claude --resume` which reads the shared conversation history. The external session (IntelliJ/terminal) won't see Guts' messages in real-time, but will pick them up on its next API call. Both sessions share the same conversation log.

### Background Loops (Admin only)

Run a prompt on a schedule, or let Claude iterate on a task until it's done, independent of anything else happening in Slack:

| Command | Action |
|---|---|
| `!loop add <name> scheduled <interval> <prompt>` | Re-run `<prompt>` every `<interval>` (min 5m) and post the result each time |
| `!loop add <name> iterate <max_iters> <prompt>` | Keep re-invoking Claude (Opus) with its own prior output until it emits `[LOOP_DONE]` or `<max_iters>` is hit |
| `!loop list` | Show all loops and their status |
| `!loop status <name>` | Detail + last result for one loop |
| `!loop stop <name>` | Stop a loop |

Capped at 5 concurrent loops, 50 iterations, and a 5-minute minimum scheduled interval — a runaway loop can't silently drain your usage.

### Architecture Crawl (Admin only)

Point Guts at a local repo checkout and it builds durable architecture docs — entry points, call flows, downstream dependencies — via a detached worker→supervisor `claude` pipeline that survives a restart:

| Command | Action |
|---|---|
| `!crawl <repo>` | Crawl one repo (a name under `REPOS_BASE_DIR`, or an absolute/`~` path) into `data/system-map/` |
| `!crawl-all <repo1> <repo2> ...` | Crawl several repos at once |
| `!crawl-status` | Show status/pid/elapsed for every crawl started so far |
| `!crawl stitch <repo>` | Re-run just the reconcile step for a repo whose worker already finished |

Once a repo is crawled, ask Guts "how does `<service>` work" and it reads `data/system-map/` instead of re-exploring the whole repo. There's currently no cap on how many crawls `!crawl-all` can kick off at once — each one is a real Opus subprocess, so keep the repo list reasonable.

### On-Call Auto-Ack (Admin only)

Optional — requires `ZENDUTY_TOKEN`, `ZENDUTY_USER_ID`, `ZENDUTY_TEAM_ID` in `.env`:

| Command | Action |
|---|---|
| `!oncall <hours>` | Start a window — Guts polls Zenduty and auto-acks incidents assigned to you for `<hours>` |
| `!oncall status` | Check whether a window is active, and how many alerts it's acked so far |
| `!oncall off` | End the window early and get the summary report now |

### User Management (Admin only)

| Command | Action |
|---|---|
| `!whitelist @user` | Grant guest access, sends welcome DM |
| `!unwhitelist @user` | Revoke guest access |
| `!inbox` | Show recent DMs from guest users |

### Other (Admin only)

| Command | Action |
|---|---|
| `!delete <slack_message_url>` | Delete bot's own message |
| `!huddle` | Play slack ringtone on Mac speakers |
| `!say @user <message>` | DM someone AS THE BOT (not as you) |
| `!read-dm @user` | Read the bot's own DM history with someone |
| `<anything> !raw` (or "send it unredacted") | Lifts credential redaction for that one reply only — the only way to get a real secret value back verbatim. See [Security & Privacy](#security--privacy) |
| `!usage [today\|week\|all]` | Cost/tokens/tool &amp; skill usage summary — see [Observability](#observability) |

Commands can be combined: `!opus !fresh explain this codebase`

## Access Control

| | Admin | Guest | Others |
|---|---|---|---|
| Claude prompts | All local tools/MCP servers, minus nothing | Read, Glob, Grep, WebSearch, WebFetch, LSP, `gh` CLI — plus any other local MCP tool not explicitly blocked (see [Security & Privacy](#security--privacy)) | Ignored |
| PR review/approve, `!debug` | Yes | Yes (via `gh` commands) | — |
| Feature implementation | Yes | No | — |
| External session resume, `!loop`, `!crawl*`, `!oncall`, `!evolve`, `!qa` | Yes | No | — |
| `!opus` model override | Yes | No (Sonnet only) | — |
| Kill, stop, status, cd | Yes | No | — |
| `!raw` (unredacted output) | Yes | Never, no matter what they ask | — |
| Working directory | Configurable | Fixed (`GUEST_CWD`) | — |

## Setup

### Prerequisites

- Python 3.11+
- Claude Code CLI installed and authenticated (`claude` command works)
- A Slack app with Socket Mode enabled

### 1. Slack App Configuration

**Fast path:** go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest** → pick your workspace → paste the contents of [`slack-app-manifest.json`](slack-app-manifest.json). That sets every scope, event subscription, and App Home setting below in one shot. Rename `display_information.name` / `features.bot_user.display_name` first if you don't want it called "Guts".

Guts is an **AI app / agent** in Slack's terms (it uses the Assistant thread experience for DMs), so the manifest declares `features.agent_view` — that's the only messaging-experience option Slack now offers new apps (the older `assistant_view` is legacy and being phased out). No separate toggle needed beyond what's in the manifest.

Either way, one step the manifest can't do for you: **Socket Mode still needs a manually generated App-Level Token** — go to *Basic Information → App-Level Tokens → Generate Token and Scopes*, add the `connections:write` scope, and save the resulting `xapp-...` token for `SLACK_APP_TOKEN`.

If you'd rather configure by hand instead of using the manifest:

**OAuth & Permissions — Bot Token Scopes:**
- `chat:write`, `channels:history`, `groups:history`, `im:history`, `im:read`, `im:write`, `mpim:history`, `reactions:write`, `assistant:write` — core messaging/session functionality
- `files:read` — needed to resolve Slack file/permalink links in a message into images the bot can attach to a prompt
- `users:read` — optional but recommended: without it, profiles and DM viewers fall back to raw Slack IDs instead of display names

**Event Subscriptions — Subscribe to bot events:**
- `assistant_thread_started`
- `assistant_thread_context_changed`
- `message.im`
- `message.groups` (for private channels)
- `message.channels` (for public channels)

**App Home:**
- Messages Tab: ON
- "Allow users to send Slash commands and messages from the messages tab": checked

**Socket Mode:**
- Enabled
- Create an App-Level Token with `connections:write` scope

### 2. Install Dependencies

```bash
cd guts
pip3 install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
ALLOWED_USER_ID=your-slack-user-id
BOT_USER_ID=the-bots-own-slack-user-id
ADMIN_NAME=your name
WHITELISTED_USER_IDS=U111111,U222222
DEFAULT_CWD=/path/to/default/workspace
GUEST_CWD=/path/to/guest/workspace
DEFAULT_MODEL=sonnet
```

- **Bot Token**: OAuth & Permissions page
- **App Token**: Basic Information > App-Level Tokens
- **User ID**: Click your Slack profile > "..." > Copy member ID
- **Bot User ID**: Click the bot's profile in Slack (after installing the app) > "..." > Copy member ID
- **Admin Name**: whatever Guts should call you in its persona text — purely cosmetic
- **Whitelisted Users**: Comma-separated Slack user IDs for guest access
- **Repos directory** (optional): set `REPOS_BASE_DIR` if you want `!crawl` and the "implement a feature" workflow to look somewhere other than `~/repos` for local checkouts

### 4. Run

```bash
python3 main.py
```

You should see:
```
Claude Slack Controller starting...
⚡️ Bolt app is running!
```

### 5. Test

- **DMs**: Open Slack on your phone, DM the bot, send a message in a new thread
- **Channels**: Invite the bot to a channel, mention `@guts`

## Architecture

```
main.py              — Slack Bolt app, Socket Mode, assistant + channel handlers, all ! commands
claude_runner.py     — Async subprocess management, stream-json parsing
slack_formatter.py   — Claude events → Slack messages, reactions, PII redaction
session_manager.py   — Thread-to-session mapping, external session discovery
workflows.py         — Predefined workflow commands (!review, !approve, etc.)
config.py            — Environment variables, system prompts, Guts persona, permissions
loop_manager.py       — Background AI loop tasks (!loop): scheduled and iterate-until-done
crawl_manager.py      — Architecture crawl (!crawl): detached worker→supervisor claude pipeline
oncall.py             — On-call auto-ack window manager (!oncall)
zenduty.py            — Minimal Zenduty API client used by oncall.py
profile_manager.py    — Builds/reads the per-person psych profile injected into every reply
backfill_personas.py  — One-off script: seed profiles from existing DM history
evolve.py             — Self-modification support: schedules the deferred restart for !evolve
transcribe.py         — Local Whisper transcription for voice-message DMs
send_as_guts.py       — CLI helper: post a Slack message as the bot, not as your account
dm_viewer.py          — Local read-only web viewer (localhost:8765) for the bot's own DM history
persona_viewer.py     — Local read-only web dashboard (localhost:8766) for the psych profiles
usage_tracker.py      — Local observability: per-run cost/token/tool/skill tracing, background-thread writer
usage_viewer.py       — Local read-only usage/cost dashboard (localhost:8767), with trace drill-down
run.sh                — Watchdog: restarts main.py if it dies, also launches persona_viewer.py + usage_viewer.py
slack-app-manifest.json — Paste into api.slack.com/apps → From a manifest to configure the Slack app in one shot
sounds/               — Audio files for !huddle
```

### Key Design Decisions

- **CLI subprocess over SDK** — Uses `claude -p --output-format stream-json` directly. More stable than the young Python SDK, and all local plugins/MCP/skills work automatically.
- **Socket Mode over webhooks** — No public URL needed, works behind firewalls and corporate networks.
- **Slack Assistant API for DMs** — Uses Slack's built-in assistant thread model, clean per-thread conversation UX.
- **Channel support** — In channels/groups the bot only responds when `@guts` is explicitly tagged; in DMs no tag is needed.
- **External session resume** — Reads `~/.claude/sessions/*.json` to discover sessions, uses `claude --resume` from the correct cwd to share conversation history.
- **Tiered access** — Admin gets full control, guests get read-only + PR operations.
- **PII redaction** — Regex-based scrubbing of tokens, keys, passwords, and connection strings before sending to Slack.
- **Session persistence** — Thread-to-session mapping saved to `sessions.json`, survives server restarts.

## Security & Privacy

Read this before whitelisting anyone as a guest, or running this on a machine with sensitive local tools configured.

- **Guests inherit your local MCP servers.** `GUEST_ALLOWED_TOOLS` in `config.py` only names the *built-in* Claude tools guests get (Read/Glob/Grep/WebSearch/WebFetch/LSP/`gh`). It does **not** restrict MCP tools — a guest's prompt runs through the same local `claude` CLI config as yours, so any MCP server you have configured (Jira, databases, internal APIs, other Slack workspaces, etc.) is reachable by a guest unless it's explicitly added to `GUEST_DISALLOWED_TOOLS`. Audit your own `~/.claude` MCP config before whitelisting anyone, and add anything sensitive to `GUEST_DISALLOWED_TOOLS`.
- **Guts keeps a private profile on everyone it talks to**, including guests — `profile_manager.py` rewrites `profiles/<user_id>.md` after every reply with a psychological read (temperament, expertise, what irritates them) used to tailor tone. The persona is explicitly instructed to never reveal or reference this to the person being profiled. If you whitelist someone, consider telling them this happens — there's no opt-out mechanism.
- **`!raw` lifts credential redaction, deliberately.** Everything Guts sends to Slack is redacted by default (tokens, keys, passwords, connection strings — see `slack_formatter.py`). The admin — and only the admin — can ask for the unredacted value (`!raw`, or plain language like "send the exact key"). Guests and the sibling-bot path can never trigger this, but treat the admin token itself as sensitive: whoever controls `ALLOWED_USER_ID`'s Slack account can extract any secret Guts can read.
- **`!evolve` lets the bot rewrite its own source and restart itself**, admin-only, no confirmation step beyond the request itself. It does validate (`import`-checks every module before committing) and reverts on failure, but it's still arbitrary code execution against your running deployment triggered from a chat message. Only ever grant admin to yourself.
- **`dm_viewer.py`, `persona_viewer.py`, and `usage_viewer.py` serve DM history, psych profiles, and usage/trace data over plain HTTP with no authentication.** Safe as long as they stay bound to `127.0.0.1` (the default) — never port-forward, tunnel (ngrok, `ssh -R`, etc.), or otherwise expose 8765/8766/8767 beyond localhost.
- **`bypassPermissions` mode.** The `claude` subprocess always runs with `--permission-mode bypassPermissions` — Claude won't stop to ask before running a tool. The tool *allow/deny* lists are the only gate; there's no per-action confirmation like you'd get in an interactive terminal session.

## Observability

Every `claude` subprocess Guts itself spawns — interactive chat, `!loop` ticks, `!crawl` workers — is traced locally. Nothing leaves the machine, no vendor, no OpenTelemetry collector required, and it's built specifically to never slow a response down.

- **What's captured, per run:** cost (`total_cost_usd`), input/output/cache token counts, duration, every tool call in order (including which `Skill`s were invoked and with what args), and whether it errored — the same data Claude's own `stream-json` output already carries, which was previously parsed once for the live Slack status line and then thrown away.
- **Two local files, both gitignored:** `data/usage.jsonl` — one rollup row per completed run, cheap to aggregate; `data/traces/<date>.jsonl` — one line per span (tool call / error / result), so any run can be replayed as its full span tree. Trace files older than 30 days are pruned automatically on startup (`TRACE_RETENTION_DAYS` in `.env`); the usage rollup is kept forever.
- **Off the hot path, on purpose.** `usage_tracker.RunTracer.observe()` never touches disk — it's pure in-memory bookkeeping that hands the row to a background daemon thread over a queue. That thread owns all file I/O, so a slow or full disk can never stall the event loop that's also juggling Slack API calls. Trade-off: a handful of rows still in the queue at the moment of a hard `SIGKILL` (e.g. an `!evolve` restart) are lost — acceptable for observability data.
- **Redacted before it's written**, not after — Bash command lines and Skill arguments go through the same credential-redaction regex (`slack_formatter._redact`) used for Slack output, before they're queued for disk. Same discipline, same regressions, same known gaps.
- **Scoped to what Guts spawns**, nothing else. There's no daemon polling other `claude` processes on your machine — `usage_tracker` only ever sees what's explicitly fed to it from `run_claude_prompt`, `run_loop_tick`, and `crawl_manager`'s log-tailing poller.
- **`!usage [today|week|all]`** — quick cost/token/tool/skill summary without leaving Slack.
- **`python3 usage_viewer.py`** (`localhost:8767`) — the full dashboard: spend over time, spend by command/user, top tools and skills invoked, a table of recent runs, and click any run to see its full trace (every tool call it made, in order).

## Limitations

- Server must be running on your Mac for the bot to respond
- MCP servers requiring interactive auth won't work (already-authenticated ones are fine)
- Slack message limit is ~4000 chars — long outputs are split across multiple messages
- One prompt at a time per thread (queued, not concurrent)
- External session resume shares conversation history but doesn't inject into running sessions in real-time
- `gh auth status` may warn about missing `read:org` scope — this is a false alarm, all PR operations work
- `!crawl-all` has no concurrency cap (unlike `!loop`, which caps at 5) — each repo you give it spawns a real Opus subprocess, so a long repo list means that many running at once
- `sessions.json` is only pruned of stale entries at startup, not periodically — on a long-running deployment it grows until you restart
- Restarting (including via `!evolve`) sends the previous process `SIGKILL` immediately, not a graceful `SIGTERM` — an in-flight Slack edit or subprocess can be cut off mid-write
