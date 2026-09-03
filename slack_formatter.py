import re
import time

from slack_sdk.web.async_client import AsyncWebClient

from claude_runner import ClaudeEvent
from config import MESSAGE_UPDATE_INTERVAL

MAX_MESSAGE_LENGTH = 3900

# Patterns to redact from messages before sending to Slack
_SENSITIVE_PATTERNS = [
    (re.compile(r"xoxb-[A-Za-z0-9\-]+"), "[REDACTED_SLACK_BOT_TOKEN]"),
    (re.compile(r"xoxp-[A-Za-z0-9\-]+"), "[REDACTED_SLACK_USER_TOKEN]"),
    (re.compile(r"xapp-[A-Za-z0-9\-]+"), "[REDACTED_SLACK_APP_TOKEN]"),
    (re.compile(r"xoxs-[A-Za-z0-9\-]+"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"gho_[A-Za-z0-9]{36,}"), "[REDACTED_GITHUB_OAUTH]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)password\s*[=:]\s*['\"]?[^\s'\"]{4,}"), "[REDACTED_PASSWORD]"),
    (re.compile(r"(?i)secret\s*[=:]\s*['\"]?[^\s'\"]{8,}"), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*['\"]?[^\s'\"]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)token\s*[=:]\s*['\"]?[A-Za-z0-9\-_.]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(mongodb(\+srv)?://)[^\s]+"), r"\1[REDACTED_URI]"),
    (re.compile(r"(?i)(postgres(ql)?://)[^\s]+"), r"\1[REDACTED_URI]"),
    (re.compile(r"(?i)(mysql://)[^\s]+"), r"\1[REDACTED_URI]"),
    (re.compile(r"(?i)(redis://)[^\s]+"), r"\1[REDACTED_URI]"),
]


def _redact(text: str) -> str:
    """Redact sensitive data from text before sending to Slack."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _fix_slack_markdown(text: str) -> str:
    """Convert standard markdown to Slack markdown."""
    # **bold** → *bold* (but don't touch already-single *bold*)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # ### heading → *heading* (Slack has no headings)
    text = re.sub(r'^#{1,3}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    return text


# Flavor emoji buckets, chosen by the tone of Guts's own response. Selection order
# is intentional: refusal/anger is checked before failure (a refusal often mentions
# "can't"), and serious-incident text suppresses the emoji entirely.
_EMOJI_SUCCESS = [
    ":lgtm:", ":shipit:", ":rocket2:", ":100_marks:", ":fire-bright:",
]
_EMOJI_ANGRY = [  # someone asked something inappropriate / out-of-scope; Guts pushes back
    ":sadak-par-bhagta-firega:", ":angry-max:", ":angry-bird:", ":pepeangry:",
    ":thalaiva_angry:", ":ok-boomer:", ":facepalm:", ":sus:", ":clown:", ":gawar:",
]
_EMOJI_FAIL = [  # something genuinely broke (but not a serious live incident)
    ":pain_hurts:", ":crying_inside:", ":this-is-fine-fire:", ":elmofire:",
    ":iamdead:", ":kill_me_now:", ":deadpepe:",
]
_EMOJI_NEUTRAL = [  # default Berserk-cool flavor
    ":pika-kill:", ":killbill:", ":the-dark-lord:", ":moonknight:", ":knight:",
    ":wardenofthenorth:", ":msword:", ":crossed_swords:", ":dagger_knife:",
    ":skull:", ":monster_mithun:", ":fire:", ":zap:", ":garage:",
    ":surprised-pikachu:", ":friday-aa:", ":l-friday-aa:",
]

# Serious-incident signals: if present, append NO emoji (don't make light of it).
_SERIOUS_RE = re.compile(
    r"(?i)\b(incident|outage|prod(uction)? (is )?down|sev[-\s]?[012]|"
    r"data loss|breach|leaked|on[-\s]?call|paging|p0\b|p1\b|critical alert)\b"
)
# Guts is refusing / pushing back on an inappropriate or out-of-scope ask.
_REFUSAL_RE = re.compile(
    r"(?i)(\bi (won't|will not|can't|cannot|refuse)\b|not (going to|appropriate|"
    r"something i)\b|that's not (ok|appropriate|happening)|\bno\.?\s*$|"
    r"i'm not (doing|going)|absolutely not|hard no\b|tch\b)"
)
# Something failed / broke.
_FAIL_RE = re.compile(
    r"(?i)(\berror\b|failed|failure|broke|broken|exception|traceback|"
    r"couldn't|could not|didn't work|not working|stuck|blocked\b|exit code [1-9])"
)
# Success / done.
_SUCCESS_RE = re.compile(
    r"(?i)(\bdone\b|completed|approved|deployed|shipped|merged|fixed|passed|"
    r"all green|success|✅|works now|up and running|:white_check_mark:)"
)


def _pick_flavor_emoji(text: str) -> str | None:
    """Pick a flavor emoji whose tone matches Guts's response. Returns None to
    append nothing (e.g. serious incidents)."""
    import random
    if _SERIOUS_RE.search(text):
        return None  # never clown on a real incident
    if _REFUSAL_RE.search(text):
        return random.choice(_EMOJI_ANGRY)
    if _FAIL_RE.search(text):
        return random.choice(_EMOJI_FAIL)
    if _SUCCESS_RE.search(text):
        return random.choice(_EMOJI_SUCCESS)
    return random.choice(_EMOJI_NEUTRAL)


class SlackFormatter:
    def __init__(self, client: AsyncWebClient, channel: str, thread_ts: str, original_msg_ts: str | None = None, allow_raw: bool = False):
        self._client = client
        self._channel = channel
        self._original_msg_ts = original_msg_ts
        self._thread_ts = thread_ts
        # When True, skip credential redaction on outbound text. Only ever set by the
        # ADMIN + explicit `!raw` path in main.py; never for guests or loop ticks.
        self._allow_raw = allow_raw
        self._current_message_ts: str | None = None
        self._accumulated_text = ""
        self._status_message_ts: str | None = None
        self._status_lines: list[str] = []
        self._last_update_time = 0.0

    async def handle_event(self, event: ClaudeEvent):
        if event.raw_type == "system" and event.subtype == "init":
            # Don't post session started immediately — wait to see if Claude skips
            self._session_id_short = event.session_id[:8] if event.session_id else "?"
            return

        elif event.raw_type == "assistant" and event.content_type == "tool_use":
            self._tool_count = getattr(self, "_tool_count", 0) + 1
            summary = self._tool_summary(event.tool_name, event.tool_input)
            sid = getattr(self, "_session_id_short", "?")
            status = f"_Working... ({self._tool_count}) {summary}_ | `{sid}`"
            await self._update_status(status)

        elif event.raw_type == "user" and event.content_type == "tool_result":
            if event.is_error:
                error_text = (event.text or "")[:200]
                await self._post(f"*Error:*\n```\n{error_text}\n```")

        elif event.raw_type == "assistant" and event.content_type == "text":
            text = event.text or ""
            import logging
            logging.getLogger(__name__).info(f"Text event: {text[:100]}")
            if "[SKIP]" in text.strip():
                logging.getLogger(__name__).info("SKIPPED — Claude chose not to respond")
                self._skipped = True
                if self._original_msg_ts:
                    # Remove ack emoji, add bust_in_silhouette
                    for emoji in ("skull", "kya-bak-rhe-ho"):
                        try:
                            await self._client.reactions_remove(
                                channel=self._channel,
                                timestamp=self._original_msg_ts,
                                name=emoji,
                            )
                        except Exception:
                            pass
                    try:
                        await self._client.reactions_add(
                            channel=self._channel,
                            timestamp=self._original_msg_ts,
                            name="bust_in_silhouette",
                        )
                    except Exception:
                        pass
                return
            self._accumulated_text = text
            await self._update_response()

        elif event.raw_type == "result":
            if getattr(self, "_skipped", False):
                return
            result = event.result or ""
            if "[SKIP]" in result.strip():
                return
            if result and result != self._accumulated_text:
                self._accumulated_text = result
            # Self-modification restart trigger — schedule restart, strip marker from display
            restart_requested = "[GUTS_RESTART]" in self._accumulated_text
            if restart_requested:
                self._accumulated_text = self._accumulated_text.replace("[GUTS_RESTART]", "").rstrip()
            # Context-aware flavor emoji (~40%) — pick a bucket matching the response tone
            import random
            if self._accumulated_text and random.random() < 0.40:
                emoji = _pick_flavor_emoji(self._accumulated_text)
                if emoji:
                    self._accumulated_text = self._accumulated_text.rstrip() + f"\n\n{emoji}"
            await self._update_response(force=True)
            if restart_requested:
                try:
                    from evolve import schedule_restart
                    schedule_restart(5)
                except Exception:
                    pass
            # Delete the status message — only final response remains
            if self._status_message_ts:
                try:
                    await self._client.chat_delete(
                        channel=self._channel,
                        ts=self._status_message_ts,
                    )
                except Exception:
                    pass
            final_text = (result or self._accumulated_text or "").strip()
            if self._current_message_ts:
                asked_question = final_text.endswith("?")
                reaction = "kya-bak-rhe-ho" if asked_question else "white_check_mark"
                try:
                    await self._client.reactions_add(
                        channel=self._channel,
                        timestamp=self._current_message_ts,
                        name=reaction,
                    )
                except Exception:
                    pass
            # If PR was approved
            result_lower = (result or self._accumulated_text or "").lower()
            if "approved" in result_lower or "approve" in result_lower:
                if self._original_msg_ts:
                    # Channel — react on original message
                    try:
                        await self._client.reactions_add(
                            channel=self._channel,
                            timestamp=self._original_msg_ts,
                            name="modi-approves",
                        )
                    except Exception:
                        pass
                else:
                    # DM — send emoji as a message
                    await self._post(":modi-approves:")

        elif event.raw_type == "error":
            await self._post(f"*Error:* {event.text}")

    def _tool_summary(self, tool_name: str | None, tool_input: dict | None) -> str:
        if not tool_name:
            return "Working..."
        name = tool_name
        detail = ""
        if tool_input:
            if name == "Bash":
                cmd = tool_input.get("command", "")
                # Show just the command name, not full args
                short = cmd.split()[0] if cmd else ""
                if short.startswith("gh"):
                    detail = f" `{cmd[:60]}`"
                else:
                    detail = f" `{short}`" if short else ""
            elif name in ("Read", "Edit", "Write"):
                path = tool_input.get("file_path", "")
                # Show just filename, not full path
                filename = path.split("/")[-1] if "/" in path else path
                detail = f" `{filename}`"
            elif name in ("Glob", "Grep"):
                pattern = tool_input.get("pattern", "")
                detail = f" `{pattern[:40]}`"
            elif name == "WebSearch":
                query = tool_input.get("query", "")
                detail = f" `{query[:40]}`"
            elif name == "WebFetch":
                url = tool_input.get("url", "")
                # Show just domain
                domain = url.split("/")[2] if url.count("/") >= 2 else url[:30]
                detail = f" `{domain}`"
        return f"{name}{detail}"

    async def _update_status(self, text: str):
        """Single status message that gets edited in place."""
        now = time.time()
        if now - self._last_update_time < MESSAGE_UPDATE_INTERVAL:
            return
        self._last_update_time = now

        if self._status_message_ts:
            try:
                await self._client.chat_update(
                    channel=self._channel,
                    ts=self._status_message_ts,
                    text=text,
                )
            except Exception:
                self._status_message_ts = None
        if not self._status_message_ts:
            resp = await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=text,
            )
            self._status_message_ts = resp["ts"]

    async def _update_response(self, force: bool = False):
        now = time.time()
        if not force and (now - self._last_update_time) < MESSAGE_UPDATE_INTERVAL:
            return
        self._last_update_time = now

        text = _fix_slack_markdown(self._accumulated_text if self._allow_raw else _redact(self._accumulated_text))
        if not text:
            return

        if len(text) <= MAX_MESSAGE_LENGTH:
            if self._current_message_ts:
                try:
                    await self._client.chat_update(
                        channel=self._channel,
                        ts=self._current_message_ts,
                        text=text,
                    )
                except Exception:
                    self._current_message_ts = None
                    await self._update_response(force=True)
            else:
                resp = await self._client.chat_postMessage(
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    text=text,
                )
                self._current_message_ts = resp["ts"]
        else:
            # Split long messages
            chunks = []
            remaining = text
            while remaining:
                chunks.append(remaining[:MAX_MESSAGE_LENGTH])
                remaining = remaining[MAX_MESSAGE_LENGTH:]
            for i, chunk in enumerate(chunks):
                if i == 0 and self._current_message_ts:
                    await self._client.chat_update(
                        channel=self._channel,
                        ts=self._current_message_ts,
                        text=chunk,
                    )
                else:
                    resp = await self._client.chat_postMessage(
                        channel=self._channel,
                        thread_ts=self._thread_ts,
                        text=chunk,
                    )
                    if i == len(chunks) - 1:
                        self._current_message_ts = resp["ts"]

    async def _post(self, text: str):
        await self._client.chat_postMessage(
            channel=self._channel,
            thread_ts=self._thread_ts,
            text=_fix_slack_markdown(text if self._allow_raw else _redact(text)),
        )
