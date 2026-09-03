"""Background AI loop tasks — run Claude prompts on a schedule or iterate-until-done,
async in parallel with the Slack controller. Mirrors OnCallManager: each loop is its own
asyncio.Task, started/stopped from the !loop command handler, and the specs persist to
loops.json so loops survive a watchdog restart.

Two modes:
- scheduled: re-run a prompt fresh every interval and post the result. Stateless ticks.
- iterate:   re-prompt with prior output until the result contains [LOOP_DONE] or the
             max_iterations cap is hit. Stateful (resumes the same Claude session).

This module never imports main.py (main imports this). The actual per-tick Claude run is
injected via set_tick_runner(fn) — same dependency inversion as oncall's send_report.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from config import (
    LOOPS_FILE,
    MAX_CONCURRENT_LOOPS,
    MAX_ITERATIONS_CEILING,
)

logger = logging.getLogger(__name__)

LOOP_DONE_TOKEN = "[LOOP_DONE]"
_SLEEP_SLICE = 5  # seconds — wake this often during a long gap to honor stop quickly

# Terminal statuses — a loop in any of these is not running and its name can be reused.
TERMINAL = {"stopped", "done", "error", "exhausted"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_iterative_prompt(base: str, prev: str) -> str:
    """Wrap the base task with the prior iteration's output and the done-sentinel rule."""
    prev_block = prev.strip() if prev.strip() else "(this is the first iteration)"
    return (
        f"{base}\n\n"
        "Previous iteration output (continue from here; do NOT repeat finished work):\n"
        f"---\n{prev_block}\n---\n\n"
        f"When the GOAL is fully met, end your reply with the exact token {LOOP_DONE_TOKEN} "
        "on its own line. If it is not done yet, describe what you did this iteration and "
        "what remains — you will be re-invoked to continue."
    )


class LoopManager:
    def __init__(self):
        self._loops: dict[str, dict] = {}          # name -> spec
        self._tasks: dict[str, asyncio.Task] = {}  # name -> live background task
        self._client = None                        # AsyncWebClient (bot token)
        self._run_tick = None                      # injected async callable(spec) -> str
        self._load()

    # --- wiring (called from main at startup) ---

    def attach_client(self, client):
        self._client = client

    def set_tick_runner(self, fn):
        """fn: async callable(spec: dict) -> str (the tick's final text)."""
        self._run_tick = fn

    # --- persistence (SessionManager style) ---

    def _load(self):
        if os.path.exists(LOOPS_FILE):
            try:
                with open(LOOPS_FILE, "r") as f:
                    self._loops = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Could not load {LOOPS_FILE}: {e}. Starting empty.")
                self._loops = {}

    def _save(self):
        try:
            with open(LOOPS_FILE, "w") as f:
                json.dump(self._loops, f, indent=2)
        except IOError as e:
            logger.warning(f"Could not save {LOOPS_FILE}: {e}")

    def _persist_spec(self, name: str):
        # spec objects are mutated in place; just rewrite the file
        self._save()

    # --- queries ---

    def _running_count(self) -> int:
        return sum(1 for s in self._loops.values() if s.get("status") == "running")

    def list_text(self) -> str:
        if not self._loops:
            return "_No loops. `!loop add <name> scheduled|iterate ...` to start one._"
        lines = ["*Loops:*"]
        for name, s in self._loops.items():
            mode = s.get("mode")
            status = s.get("status")
            done = s.get("iterations_done", 0)
            if mode == "scheduled":
                every = f"every {int(s.get('interval_sec', 0) // 60)}m"
                cap = s.get("max_iterations")
                capstr = f", cap {cap}" if cap else ""
                lines.append(f"• `{name}` [scheduled {every}{capstr}] — *{status}*, {done} ticks")
            else:
                lines.append(
                    f"• `{name}` [iterate, max {s.get('max_iterations')}] — *{status}*, {done} iters"
                )
        return "\n".join(lines)

    def status_text(self, name: str) -> str:
        name = (name or "").strip()
        s = self._loops.get(name)
        if not s:
            return f"_No loop named `{name}`. `!loop list` to see them._"
        lines = [
            f"*Loop `{name}`* — {s.get('mode')} | *{s.get('status')}*",
            f"iterations: {s.get('iterations_done', 0)}"
            + (f" / {s.get('max_iterations')}" if s.get("max_iterations") else ""),
        ]
        if s.get("mode") == "scheduled":
            lines.append(f"interval: {int(s.get('interval_sec', 0) // 60)}m")
        lines.append(f"model: {s.get('model')} | cwd: `{s.get('cwd')}`")
        if s.get("last_run"):
            lines.append(f"last run: {s['last_run'][:19].replace('T', ' ')} UTC")
        if s.get("last_result"):
            tail = s["last_result"][-300:]
            lines.append(f"last result:\n```{tail}```")
        lines.append(f"prompt: _{s.get('prompt', '')[:200]}_")
        return "\n".join(lines)

    # --- lifecycle ---

    async def add(self, spec: dict) -> tuple[bool, str]:
        """Validate, persist, and spawn a loop. Caller fills the spec (see main._parse_loop_add)."""
        if self._run_tick is None or self._client is None:
            return False, "_Loop runner not ready yet — try again in a moment._"

        name = spec["name"]
        existing = self._loops.get(name)
        if existing and existing.get("status") not in TERMINAL:
            return False, f"_Loop `{name}` already exists and is {existing.get('status')}. `!loop stop {name}` first._"

        if self._running_count() >= MAX_CONCURRENT_LOOPS:
            return False, f"_Too many loops running (max {MAX_CONCURRENT_LOOPS}). Stop one first._"

        # Clamp iteration cap defensively (parser also validates).
        cap = spec.get("max_iterations")
        if cap and cap > MAX_ITERATIONS_CEILING:
            spec["max_iterations"] = MAX_ITERATIONS_CEILING

        spec.setdefault("iterations_done", 0)
        spec.setdefault("created_at", _iso_now())
        spec.setdefault("session_id", str(uuid.uuid4()))
        spec.setdefault("last_run", None)
        spec.setdefault("last_result", "")
        spec["status"] = "running"

        self._loops[name] = spec
        self._save()
        self._spawn(name)

        if spec["mode"] == "scheduled":
            every = int(spec["interval_sec"] // 60)
            extra = f" (cap {spec['max_iterations']} ticks)" if spec.get("max_iterations") else ""
            return True, f"Loop *{name}* armed — runs every {every}m{extra}. First tick now. `!loop stop {name}` to end."
        return True, f"Loop *{name}* armed — iterating (max {spec['max_iterations']}, Opus). `!loop status {name}` to watch."

    def _spawn(self, name: str):
        """Create the background asyncio.Task for a loop (must be called inside the event loop)."""
        spec = self._loops[name]
        import time as _time
        if spec["mode"] == "scheduled":
            coro = self._run_scheduled(name, _time.time)
        else:
            coro = self._run_iterative(name, _time.time)
        self._tasks[name] = asyncio.create_task(coro)

    async def stop(self, name: str, loop_clock) -> bool:
        name = (name or "").strip()
        spec = self._loops.get(name)
        if not spec:
            return False
        spec["status"] = "stopped"
        self._persist_spec(name)
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
        return True

    async def rearm_all(self, loop_clock):
        """On startup, re-spawn a task for every loop still marked running."""
        rearmed = 0
        for name, spec in list(self._loops.items()):
            if spec.get("status") == "running":
                self._spawn(name)
                rearmed += 1
        if rearmed:
            logger.info(f"Re-armed {rearmed} loop(s) from {LOOPS_FILE}")

    # --- posting + sleeping ---

    async def _post(self, spec: dict, text: str):
        if not self._client:
            logger.warning("LoopManager has no client; cannot post.")
            return
        try:
            await self._client.chat_postMessage(
                channel=spec["channel"], thread_ts=spec["thread_ts"], text=text
            )
        except Exception:
            logger.exception(f"Loop {spec.get('name')} failed to post to Slack")

    async def _sleep_interruptible(self, name: str, total: float):
        """Sleep up to `total` seconds, waking every _SLEEP_SLICE to break early if stopped."""
        slept = 0.0
        while slept < total:
            if self._loops.get(name, {}).get("status") != "running":
                return
            chunk = min(_SLEEP_SLICE, total - slept)
            await asyncio.sleep(chunk)
            slept += chunk

    # --- the two loop bodies ---

    async def _run_scheduled(self, name: str, loop_clock):
        spec = self._loops[name]
        try:
            while spec.get("status") == "running":
                spec["last_run"] = _iso_now()
                try:
                    result = await self._run_tick(spec)
                    spec["last_result"] = (result or "")[-500:]
                except Exception as e:
                    logger.exception(f"loop {name} scheduled tick error")
                    await self._post(spec, f"_Loop `{name}` tick errored: `{e}` — continuing._")
                spec["iterations_done"] += 1
                self._persist_spec(name)

                cap = spec.get("max_iterations")
                if cap and spec["iterations_done"] >= cap:
                    spec["status"] = "exhausted"
                    self._persist_spec(name)
                    await self._post(spec, f"_Loop `{name}` hit its run cap ({cap}). Stopping._")
                    return

                await self._sleep_interruptible(name, spec["interval_sec"])
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"loop {name} scheduled crashed")
            spec["status"] = "error"
            self._persist_spec(name)
        finally:
            self._tasks.pop(name, None)

    async def _run_iterative(self, name: str, loop_clock):
        spec = self._loops[name]
        prev = spec.get("last_result", "") or ""
        try:
            while spec.get("status") == "running":
                tick = dict(spec)
                tick["prompt"] = _build_iterative_prompt(spec["prompt"], prev)
                tick["resume"] = spec["iterations_done"] > 0
                spec["last_run"] = _iso_now()

                try:
                    result = await self._run_tick(tick)
                except Exception as e:
                    logger.exception(f"loop {name} iterate error")
                    spec["status"] = "error"
                    self._persist_spec(name)
                    await self._post(spec, f"_Loop `{name}` errored: `{e}` — stopping to be safe._")
                    return

                # A skip (thread busy) returns "" — don't count it, retry after a short gap.
                if result == "" and spec["iterations_done"] > 0 and tick.get("resume"):
                    await self._sleep_interruptible(name, _SLEEP_SLICE)
                    continue

                prev = result or ""
                spec["last_result"] = prev[-500:]
                spec["iterations_done"] += 1
                self._persist_spec(name)

                if LOOP_DONE_TOKEN in prev:
                    spec["status"] = "done"
                    self._persist_spec(name)
                    await self._post(
                        spec,
                        f"*Loop `{name}` reports done.* ({spec['iterations_done']} iterations) The Dragonslayer rests.",
                    )
                    return

                if spec["iterations_done"] >= spec["max_iterations"]:
                    spec["status"] = "exhausted"
                    self._persist_spec(name)
                    await self._post(
                        spec,
                        f"_Loop `{name}` hit max iterations ({spec['max_iterations']}) without {LOOP_DONE_TOKEN}. Stopping._",
                    )
                    return

                # tiny breather so a stop can land between iterations
                await self._sleep_interruptible(name, spec.get("interval_sec", 0) or 2)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"loop {name} iterate crashed")
            spec["status"] = "error"
            self._persist_spec(name)
        finally:
            self._tasks.pop(name, None)


# Singleton
loop_manager = LoopManager()
