# Guts — Claude Code Slack Controller

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
| `!kill` | Terminate current session |

### External Session Resume

You can interact with Claude sessions running anywhere on your Mac:

1. `!sessions` — lists all active Claude sessions with name, repo, and ID
2. `!join payments-service` — connects the current Slack thread to that session (fuzzy matches by name, repo, or session ID prefix)
3. Send messages — they resume the external session with full conversation history

**Note:** This uses `claude --resume` which reads the shared conversation history. The external session (IntelliJ/terminal) won't see Guts' messages in real-time, but will pick them up on its next API call. Both sessions share the same conversation log.

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

Commands can be combined: `!opus !fresh explain this codebase`

## Access Control

| | Admin | Guest | Others |
|---|---|---|---|
| Claude prompts | All tools | Read, Glob, Grep, WebSearch, LSP, `gh` CLI | Ignored |
| PR review/approve | Yes | Yes (via `gh` commands) | — |
| Feature implementation | Yes | No | — |
| External session resume | Yes | No | — |
| `!opus` model override | Yes | No (Sonnet only) | — |
| Kill, status, cd | Yes | No | — |
| Working directory | Configurable | Fixed (`GUEST_CWD`) | — |

## Setup

### Prerequisites

- Python 3.11+
- Claude Code CLI installed and authenticated (`claude` command works)
- A Slack app with Socket Mode enabled

### 1. Slack App Configuration

Go to [api.slack.com/apps](https://api.slack.com/apps) and create/configure your app:

**OAuth & Permissions — Bot Token Scopes:**
- `chat:write`, `channels:history`, `groups:history`, `im:history`, `im:read`, `im:write`, `mpim:history`, `reactions:write`, `assistant:write`

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
cd claude-slack-controller
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
sounds/              — Audio files for !huddle
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

## Limitations

- Server must be running on your Mac for the bot to respond
- MCP servers requiring interactive auth won't work (already-authenticated ones are fine)
- Slack message limit is ~4000 chars — long outputs are split across multiple messages
- One prompt at a time per thread (queued, not concurrent)
- External session resume shares conversation history but doesn't inject into running sessions in real-time
- `gh auth status` may warn about missing `read:org` scope — this is a false alarm, all PR operations work
