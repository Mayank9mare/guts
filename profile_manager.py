"""Per-person psychological profiles for Guts.

Guts keeps a rolling psychological read on everyone it talks to — one markdown file
per Slack user at profiles/<user_id>.md. The profile is READ synchronously and injected
into the system prompt before Guts replies, and UPDATED asynchronously (fire-and-forget)
AFTER the reply is sent, so the profiler never slows the hot path.

The update runs a headless one-shot `claude -p` (same CLI the rest of the bot uses) that
reads the current profile + the latest exchange and rewrites the profile. Failures are
logged and swallowed — a broken profiler must NEVER break chat. Everything written is run
through slack_formatter._redact first, so a profile can never hoard a leaked secret.
"""

import asyncio
import logging
import os
import re

from config import CLAUDE_CLI, DEFAULT_CWD
from slack_formatter import _redact

logger = logging.getLogger(__name__)

PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")

# How long the profiler subprocess may run before we give up on it (seconds).
_PROFILER_TIMEOUT = 90

# In-memory display-name cache so we don't hammer Slack users_info on every exchange.
_name_cache: dict[str, str] = {}

_SAFE_UID = re.compile(r"^[A-Z0-9._-]+$", re.IGNORECASE)


def _profile_path(user_id: str) -> str | None:
    """Return the on-disk path for a user's profile, or None if the id is unsafe.

    user_id lands in a filename, so reject anything that isn't a plain Slack-style id
    to keep it from escaping PROFILES_DIR."""
    if not user_id or not _SAFE_UID.match(user_id):
        return None
    return os.path.join(PROFILES_DIR, f"{user_id}.md")


def read_profile(user_id: str) -> str:
    """Return the person's current profile markdown, or '' if none exists yet.

    Pure filesystem read — cheap, safe to call on the hot path before every reply."""
    path = _profile_path(user_id)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception(f"profile read failed for {user_id}")
        return ""


async def display_name(user_id: str, client) -> str:
    """Best-effort human name for a Slack user id (cached). Falls back to the id."""
    if user_id in _name_cache:
        return _name_cache[user_id]
    name = user_id
    try:
        resp = await client.users_info(user=user_id)
        prof = (resp.get("user") or {}).get("profile") or {}
        name = (
            prof.get("display_name")
            or prof.get("real_name")
            or (resp.get("user") or {}).get("real_name")
            or user_id
        )
    except Exception:
        logger.debug(f"users_info failed for {user_id}, using id")
    _name_cache[user_id] = name
    return name


_PROFILER_PROMPT = """You maintain a single rolling PSYCHOLOGICAL PROFILE of one person, used privately by the Guts bot to tailor how it talks to them. You are NOT talking to the person — you only output the updated profile file, nothing else.

PERSON: {name} (Slack id {user_id})

CURRENT PROFILE (may be empty on first contact):
<<<CURRENT
{current}
CURRENT

LATEST EXCHANGE (newest interaction — user message, then Guts's reply):
<<<EXCHANGE
{transcript}
EXCHANGE

Rewrite the profile, integrating what this exchange reveals. Keep it tight (<200 words), evidence-based (infer from how they actually communicate — impatience, need for detail, bluntness, what irritates them, expertise level, mood), and honest. Do NOT invent facts. Preserve durable prior observations; update or drop ones this exchange contradicts. Keep a short rolling history line.

Output ONLY the file contents in exactly this format, nothing before or after:

---
name: {name}
reads: <2-3 line psychological read — temperament, how they like answers, what irritates them>
tone: <how Guts should pitch tone/bluntness/detail with this person>
expertise: <rough read on their technical depth / domain>
history: <short rolling log of notable interactions, newest first, trimmed to ~5 entries>
---
"""


async def update_profile(user_id: str, name: str, transcript: str, model: str) -> None:
    """Fire-and-forget background job: refresh profiles/<user_id>.md from the latest exchange.

    Spawns a headless one-shot `claude -p` (session model) to rewrite the profile. Atomic
    write, redacted before persist, all errors logged and swallowed. Never raises."""
    try:
        path = _profile_path(user_id)
        if not path:
            logger.warning(f"profile update skipped — unsafe user_id {user_id!r}")
            return
        if not (transcript or "").strip():
            return

        os.makedirs(PROFILES_DIR, exist_ok=True)
        current = read_profile(user_id) or "(no profile yet — this is first contact)"

        prompt = _PROFILER_PROMPT.format(
            name=name, user_id=user_id, current=current, transcript=transcript
        )

        cmd = [
            CLAUDE_CLI, "-p",
            "--output-format", "text",
            "--model", model,
            "--permission-mode", "bypassPermissions",
            # No tools needed — this is pure text transformation. Deny all to keep it fast/safe.
            "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebSearch,WebFetch",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=DEFAULT_CWD,
            limit=4 * 1024 * 1024,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=_PROFILER_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"profile update timed out for {user_id}")
            return

        if proc.returncode != 0:
            logger.warning(
                f"profiler exited {proc.returncode} for {user_id}: {stderr.decode()[:300]}"
            )
            return

        out = stdout.decode().strip()
        if not out or "---" not in out:
            logger.warning(f"profiler produced no usable profile for {user_id}")
            return

        # Never let a profile hoard a secret that showed up in the transcript.
        out = _redact(out)

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        os.replace(tmp, path)
        logger.info(f"profile updated for {user_id} ({name})")
    except Exception:
        logger.exception(f"profile update failed for {user_id}")
