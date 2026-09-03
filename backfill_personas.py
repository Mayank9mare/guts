#!/usr/bin/env python3
"""Backfill Guts persona profiles from real DM conversation history.

The profiler (profile_manager.py) normally updates a person's profile from the single
latest exchange, after each reply. This script SEEDS profiles from everything already in
history: it walks every DM the bot (@guts) has, pulls each conversation — including thread
replies (the assistant-thread replies hold the real substance) — builds a transcript, and
runs the same profiler to write profiles/<user_id>.md.

Reuses dm_viewer.py's Slack fetch helpers and profile_manager.update_profile (so the write
path, redaction, atomic write, and format are identical to the live profiler).

Usage:
    python3 backfill_personas.py                # every DM
    python3 backfill_personas.py --user U0123ABCD   # one person
    python3 backfill_personas.py --model sonnet --min-msgs 2

Read-only against Slack (history only); the only writes are the local profiles/*.md.
"""
import argparse
import asyncio

import dm_viewer
import profile_manager


def _transcript_for(channel: str, label: str) -> tuple[str, int]:
    """Build a chronological transcript string for a DM channel. Returns (text, real_msg_count).

    real_msg_count excludes empty / 'New Assistant Thread' auto-markers so we can skip DMs
    with no actual conversation."""
    msgs = dm_viewer.messages_for(channel)
    lines = []
    real = 0
    for m in msgs:
        text = (m.get("text") or "").strip()
        if not text or m.get("sender") == "error":
            continue
        # assistant-thread auto-markers aren't real content
        if text == "New Assistant Thread":
            continue
        who = "GUTS" if m.get("is_bot") else label
        lines.append(f"[{m.get('iso_time','')}] {who}: {text}")
        real += 1
    return "\n".join(lines), real


async def backfill(only_user: str | None, model: str, min_msgs: int):
    convos = dm_viewer.list_conversations()
    if only_user:
        convos = [c for c in convos if c["user_id"] == only_user]
        if not convos:
            print(f"No DM found for user {only_user}. Has Guts ever DM'd them?")
            return

    done = skipped = failed = 0
    for c in convos:
        uid = c["user_id"]
        label = c["label"]
        if not uid or uid == "USLACKBOT":
            continue
        # Fetching history can transiently fail (Slack IncompleteRead / rate limit on a heavy
        # thread). Don't let one bad fetch abort the whole backfill — retry once, then skip.
        transcript, n = "", 0
        for attempt in (1, 2):
            try:
                transcript, n = _transcript_for(c["channel_id"], label)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"  ✗ {uid:<12} {label:<18} history fetch FAILED: {e}")
                    failed += 1
                    transcript, n = "", -1  # sentinel: don't also count as skipped
                else:
                    await asyncio.sleep(2)
        if n == -1:
            continue
        # ease Slack rate limits between conversations
        await asyncio.sleep(0.5)
        if n < min_msgs:
            print(f"  ↷ {uid:<12} {label:<18} skipped ({n} real msg(s))")
            skipped += 1
            continue
        try:
            # display_name from the profiler's own resolver (KNOWN_LABELS-backed), so files
            # get the friendly name where we know it.
            name = dm_viewer.label_for(uid)
            await profile_manager.update_profile(uid, name, transcript, model)
            print(f"  ✓ {uid:<12} {name:<18} profiled from {n} msg(s)")
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {uid:<12} {label:<18} FAILED: {e}")
            failed += 1

    print(f"\nBackfill complete: {done} profiled, {skipped} skipped (too little history), {failed} failed.")
    print(f"View them: python3 persona_viewer.py  →  http://localhost:8766")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="Backfill only this Slack user id (e.g. U0123ABCD)")
    ap.add_argument("--model", default="sonnet[1m]", help="Model for the profiler (default: sonnet[1m])")
    ap.add_argument("--min-msgs", type=int, default=1,
                    help="Skip DMs with fewer than this many real messages (default: 1)")
    args = ap.parse_args()
    asyncio.run(backfill(args.user, args.model, args.min_msgs))


if __name__ == "__main__":
    main()
