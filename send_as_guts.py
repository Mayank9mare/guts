#!/usr/bin/env python3
"""Send a Slack message AS THE GUTS BOT (not as the admin's personal account).

This is the ONLY correct way for Guts to proactively message someone — it uses
the bot's own SLACK_BOT_TOKEN, so the message shows up as @guts, never as the
admin's personal account. The Slack MCP (mcp__claude_ai_Slack__*) posts as the
admin and must NOT be used for "send as Guts" requests.

Usage:
    python3 send_as_guts.py <channel_or_user_id> [--thread <ts>] <message text...>

- <channel_or_user_id>: a channel id (C…/D…) or a user id (U…). If a user id is
  given, a DM channel is opened with that user first.
- --thread <ts>: OPTIONAL. Post as a reply in the thread with this parent ts (e.g.
  1784646046.149689). It's a named flag, so it can't get swallowed into the message
  text. Omit it to post a normal top-level message.
- The message is every remaining arg joined with spaces (quote it to be safe).

Exits 0 on success (prints the ts), non-zero on failure (prints Slack error).
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _bot_token() -> str:
    env_path = os.path.join(HERE, ".env")
    for line in open(env_path):
        if line.startswith("SLACK_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("SLACK_BOT_TOKEN not found in .env")


def _api(method: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: send_as_guts.py <channel_or_user_id> [--thread <ts>] <message...>", file=sys.stderr)
        return 2
    target = sys.argv[1]

    # Pull an optional --thread <ts> flag out of the args before joining the rest as text,
    # so a thread ts can never accidentally end up inside the message body.
    rest = sys.argv[2:]
    thread_ts = None
    if "--thread" in rest:
        i = rest.index("--thread")
        if i + 1 >= len(rest):
            print("--thread needs a ts value", file=sys.stderr)
            return 2
        thread_ts = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    text = " ".join(rest)
    if not text.strip():
        print("no message text given", file=sys.stderr)
        return 2
    token = _bot_token()

    channel = target
    if target.startswith("U"):  # user id → open a DM channel as the bot
        opened = _api("conversations.open", token, {"users": target})
        if not opened.get("ok"):
            print(f"conversations.open failed: {opened.get('error')}", file=sys.stderr)
            return 1
        channel = opened["channel"]["id"]

    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    res = _api("chat.postMessage", token, payload)
    if not res.get("ok"):
        print(f"chat.postMessage failed: {res.get('error')}", file=sys.stderr)
        return 1
    where = f"thread={thread_ts}" if thread_ts else "top-level"
    print(f"sent as @guts | channel={channel} ts={res.get('ts')} {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
