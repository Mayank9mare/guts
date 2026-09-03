"""Local observability — span tracing + usage rollups for every claude subprocess Guts itself
spawns (interactive chat, !loop ticks, !crawl workers). Nothing outside what Guts spawns is
ever touched — RunTracer only sees the ClaudeEvents explicitly fed to it.

Two local JSONL files, nothing leaves the machine:
  TRACES_DIR/<YYYY-MM-DD>.jsonl — one line per SPAN (a tool call, a tool error, or the final
                                   result), tagged with a trace_id so any run can be replayed
                                   as its full span tree.
  USAGE_FILE                    — one line per COMPLETED run: cost, tokens, duration, tool
                                   count, which skills/tools were used, error/skip flags. This
                                   is the METRICS layer — cheap to aggregate (usage_viewer.py,
                                   the !usage command) without re-reading every trace file.

RunTracer is constructed once per run and fed every ClaudeEvent from ClaudeRunner.run(). Off
the hot path by design: .observe() never touches disk — it does in-memory bookkeeping only and
hands the row to a background daemon thread via a thread-safe queue. That thread owns all file
I/O, so a slow disk (or a full one) can never stall the asyncio event loop that's juggling
Slack API calls and other concurrent runs. Tracing must also never break the actual chat
response, so every step is wrapped and logged, never raised — same philosophy as
profile_manager.py's profiler. Trade-off: a handful of rows queued at the moment of a hard
kill (SIGKILL, e.g. on !evolve restart) are lost — acceptable for observability data.
"""
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import Counter, defaultdict

from config import TRACES_DIR, USAGE_FILE, TRACE_RETENTION_DAYS
from slack_formatter import _redact

logger = logging.getLogger(__name__)

# --- background writer thread: the ONLY thing that touches disk -----------------------------

_write_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
_writer_thread: threading.Thread | None = None
_writer_lock = threading.Lock()


def _write_loop():
    while True:
        path, row = _write_queue.get()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("usage_tracker: background write failed")


def _ensure_writer_started():
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return
    with _writer_lock:
        if _writer_thread is None or not _writer_thread.is_alive():
            _writer_thread = threading.Thread(target=_write_loop, daemon=True, name="usage-tracker-writer")
            _writer_thread.start()


def _enqueue(path: str, row: dict):
    """Non-blocking: hands off to the background thread and returns immediately."""
    try:
        _ensure_writer_started()
        _write_queue.put_nowait((path, row))
    except Exception:
        logger.exception("usage_tracker: enqueue failed")


def _trace_path_for_today() -> str:
    return os.path.join(TRACES_DIR, time.strftime("%Y-%m-%d") + ".jsonl")


def prune_old_traces(now: float | None = None):
    """Delete trace files older than TRACE_RETENTION_DAYS. Call once at startup.
    USAGE_FILE (the rollup) is never pruned — it's small (one line/run) and cheap to keep."""
    if not os.path.isdir(TRACES_DIR):
        return
    cutoff = (now or time.time()) - TRACE_RETENTION_DAYS * 86400
    for fn in os.listdir(TRACES_DIR):
        if not fn.endswith(".jsonl"):
            continue
        path = os.path.join(TRACES_DIR, fn)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


class RunTracer:
    """Tracks one Claude subprocess run. Construct per run, call .observe() for every event."""

    def __init__(self, *, thread_ts: str, channel: str, user_id: str | None, role: str,
                 model: str, command: str):
        self.trace_id = str(uuid.uuid4())
        self.thread_ts = thread_ts
        self.channel = channel
        self.user_id = user_id
        self.role = role
        self.model = model
        self.command = command  # best-effort label: "!review", "chat", "loop:watch-5xx", "crawl:foo"
        self.started_at = time.time()
        self._tool_calls: list[dict] = []
        self._skills: list[str] = []
        self._is_error = False
        self._skipped = False
        self._finished = False

    def _span(self, span_type: str, **fields):
        row = {"ts": time.time(), "trace_id": self.trace_id, "thread_ts": self.thread_ts,
               "command": self.command, "span_type": span_type, **fields}
        _enqueue(_trace_path_for_today(), row)  # non-blocking — handed to the writer thread

    def observe(self, event) -> None:
        """Feed one ClaudeEvent from ClaudeRunner.run(). Never raises."""
        try:
            self._observe(event)
        except Exception:
            logger.exception("usage_tracker.observe failed")

    def _observe(self, event) -> None:
        if event.raw_type == "assistant" and event.content_type == "tool_use":
            name = event.tool_name or "?"
            tool_input = event.tool_input or {}
            entry = {"name": name}
            if name == "Skill":
                skill = tool_input.get("skill", "")
                if skill:
                    self._skills.append(skill)
                entry["skill"] = skill
                entry["args"] = _redact((tool_input.get("args") or "")[:200])
            elif name == "Bash":
                entry["command_line"] = _redact((tool_input.get("command") or "")[:200])
            elif name in ("Read", "Edit", "Write"):
                entry["file_path"] = tool_input.get("file_path")
            self._tool_calls.append(entry)
            self._span("tool_call", **entry)

        elif event.raw_type == "user" and event.content_type == "tool_result" and event.is_error:
            self._is_error = True
            self._span("tool_error", text=_redact((event.text or "")[:300]))

        elif event.raw_type == "assistant" and event.content_type == "text":
            if event.text and "[SKIP]" in event.text:
                self._skipped = True

        elif event.raw_type == "error":
            self._is_error = True
            self._span("error", text=event.text)

        elif event.raw_type == "result":
            if self._finished:
                return
            self._finished = True
            if event.is_error:
                self._is_error = True
            usage = event.usage or {}
            row = {
                "ts": time.time(),
                "trace_id": self.trace_id,
                "thread_ts": self.thread_ts,
                "channel": self.channel,
                "user_id": self.user_id,
                "role": self.role,
                "model": self.model,
                "command": self.command,
                "cost_usd": event.cost_usd,
                "duration_ms": event.duration_ms,
                "wall_ms": int((time.time() - self.started_at) * 1000),
                "num_turns": event.num_turns,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
                "tool_call_count": len(self._tool_calls),
                "skills_used": self._skills,
                "tools_used": sorted({t["name"] for t in self._tool_calls}),
                "is_error": self._is_error,
                "skipped": self._skipped,
            }
            self._span("result", cost_usd=event.cost_usd, duration_ms=event.duration_ms,
                        is_error=self._is_error, skipped=self._skipped)
            _enqueue(USAGE_FILE, row)


def record_external_run(*, thread_ts: str, channel: str, command: str, cost_usd: float | None,
                         duration_ms: int | None, usage: dict | None, tools_used: list[str],
                         skills_used: list[str], is_error: bool, role: str = "admin",
                         model: str | None = None, user_id: str | None = None):
    """For runs that don't go through ClaudeRunner (currently: !crawl workers/supervisors,
    which are spawned directly via subprocess.Popen with logs on disk). Writes the same
    USAGE_FILE row shape as RunTracer's result span, so !usage and usage_viewer.py never need
    to know the difference. Never raises."""
    usage = usage or {}
    row = {
        "ts": time.time(),
        "trace_id": str(uuid.uuid4()),
        "thread_ts": thread_ts,
        "channel": channel,
        "user_id": user_id,
        "role": role,
        "model": model,
        "command": command,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "wall_ms": duration_ms,
        "num_turns": None,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "tool_call_count": len(tools_used),
        "skills_used": skills_used,
        "tools_used": sorted(set(tools_used)),
        "is_error": is_error,
        "skipped": False,
    }
    _enqueue(USAGE_FILE, row)


# --- reading / aggregating (shared by !usage and usage_viewer.py) ---------------------------

def load_rows(since_days: float | None = None) -> list[dict]:
    """Read USAGE_FILE, newest last. since_days=None means all history."""
    if not os.path.exists(USAGE_FILE):
        return []
    cutoff = time.time() - since_days * 86400 if since_days else 0
    rows = []
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ts", 0) >= cutoff:
                rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Aggregate a list of usage rows into the numbers !usage / the dashboard show."""
    total_cost = sum(r.get("cost_usd") or 0 for r in rows)
    total_runs = len(rows)
    total_tools = sum(r.get("tool_call_count") or 0 for r in rows)
    errors = sum(1 for r in rows if r.get("is_error"))
    avg_duration_ms = (sum(r.get("duration_ms") or 0 for r in rows) / total_runs) if total_runs else 0

    by_user = defaultdict(float)
    by_command = defaultdict(float)
    tool_counts = Counter()
    skill_counts = Counter()
    for r in rows:
        cost = r.get("cost_usd") or 0
        by_user[r.get("user_id") or r.get("command", "?")] += cost
        by_command[r.get("command") or "?"] += cost
        for t in r.get("tools_used") or []:
            tool_counts[t] += 1
        for s in r.get("skills_used") or []:
            skill_counts[s] += 1

    return {
        "total_cost_usd": total_cost,
        "total_runs": total_runs,
        "total_tool_calls": total_tools,
        "error_count": errors,
        "avg_duration_ms": avg_duration_ms,
        "by_user": sorted(by_user.items(), key=lambda kv: kv[1], reverse=True),
        "by_command": sorted(by_command.items(), key=lambda kv: kv[1], reverse=True),
        "top_tools": tool_counts.most_common(10),
        "top_skills": skill_counts.most_common(10),
    }


def load_spans(trace_id: str, ts: float) -> list[dict]:
    """Spans for one run, for the dashboard's trace drill-down. `ts` (from the run's usage
    row) picks the day's trace file directly instead of scanning every file on disk."""
    path = os.path.join(TRACES_DIR, time.strftime("%Y-%m-%d", time.localtime(ts)) + ".jsonl")
    spans = []
    if not os.path.exists(path):
        return spans
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("trace_id") == trace_id:
                spans.append(row)
    spans.sort(key=lambda s: s.get("ts", 0))
    return spans


def daily_costs(rows: list[dict], days: int = 14) -> list[tuple[str, float]]:
    """Cost per calendar day (YYYY-MM-DD) for the trailing `days` days, oldest first —
    zero-filled so a quiet day still shows up as a bar at 0."""
    by_day = defaultdict(float)
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0)))
        by_day[day] += r.get("cost_usd") or 0
    today = time.localtime()
    out = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(time.mktime(today) - i * 86400))
        out.append((day, round(by_day.get(day, 0.0), 6)))
    return out
