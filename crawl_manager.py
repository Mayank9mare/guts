"""crawl — build data/system-map/ architectural docs of any repo.

Two-stage pipeline of DETACHED `claude -p` subprocesses so a crawl outlives a Guts restart:

  worker (crawl <repo>)  — one-shot headless Claude, cwd=repo, traces entry points inward and
                           writes flows/<name>.md into a STAGING dir (never the live map).
  supervisor (auto)      — launched once the worker dies; reconciles staging -> live system-map/
                           (dedup, cross-link, update INDEX.md), then deletes staging.

Both are spawned with subprocess.Popen(start_new_session=True) and stdout/stderr wired to real
file descriptors, then we DON'T await them — they run independently. A 30s asyncio poller per repo
tails the JSON-stream log, drives the status machine, edits one Slack message in place, and enforces
the 4h ceiling. On boot, resume_crawls() re-arms a poller for any non-terminal state (the detached
process is still running; we just re-attach the tail loop).

Status machine (per repo, in .crawl-state/<repo>.json):
  worker_running -> worker_done -> supervisor_running -> stitched   (or -> failed)

Handoff gate is PROCESS DEATH: a `result` event flips worker_done fast; otherwise
os.kill(pid,0) noticing the pid is gone forces worker_done anyway. Next tick with worker_done and no
supervisor launches the supervisor.

This module never imports main.py (main imports this). Slack client is injected via attach_client.
"""
import asyncio
import json
import logging
import os
import glob
import signal
import subprocess
import time

import usage_tracker
from config import (
    CLAUDE_CLI,
    REPOS_BASE_DIR,
    CRAWL_STATE_DIR,
    CRAWL_LOGS_DIR,
    SYSTEM_MAP_DIR,
    CRAWL_HARD_CEILING_SEC,
    CRAWL_POLL_SEC,
)

logger = logging.getLogger(__name__)

TERMINAL = {"stitched", "failed"}
_STAGING_ROOT = os.path.join(SYSTEM_MAP_DIR, ".staging")  # <repo>/ under here; deleted on stitch

# ------------------------------------------------------------------ prompts

_GUARDRAILS = """HARD GUARDRAILS (both stages):
- NO incident specifics ever — no dates, error counts, user IDs, order IDs, ticket numbers, alert
  text. This is durable ARCHITECTURE knowledge, not an incident log.
- NO speculation. Every claim must be grounded in code you actually read this run. If you didn't
  read it, write "Status: not yet captured" — a stub, not a guess.
"""

WORKER_PROMPT = """You are a one-shot architecture-crawler worker for the repo in your cwd. You are a DETACHED headless process: THE INSTANT YOU STOP CALLING TOOLS YOU ARE DEAD and anything only "in your head" is lost. So WRITE AS YOU GO — never batch to the end.

Your job: trace this service's runtime flows and write them as markdown into the STAGING dir:
    {staging}
Write ONE file per flow at {staging}/flows/<flow-name>.md, and a {staging}/services/{repo}.md summary. You may READ the live map at {map} for existing conventions, but you may ONLY WRITE under {staging}.

NUMBERED CRAWL STRATEGY — follow in order:
1. Read build files (build.gradle/pom.xml/package.json) + main config to learn the service's shape.
2. Enumerate ENTRY POINTS: @RestController / @RequestMapping, @KafkaListener / @StreamListener, @Scheduled, gRPC handlers, SQS/SNS consumers, StepFunction task handlers.
3. For EACH entry point, trace INWARD: controller -> orchestrator/service -> providers/clients/repos. Note branches, state mutations (DB tables, caches, queues written), and which downstream service each outbound call hits.
4. STOP at the wire boundary the moment a call leaves this repo's own checkout (a different service's HTTP/gRPC/queue endpoint, not a local package) — record "Downstream: <service> via <how>" and stop; a separate crawl of that repo owns its own internals.
5. After tracing each flow, IMMEDIATELY write its flows/<name>.md before moving on.

DO NOT spawn background sub-Tasks/agents — a past run zeroed its own output that way. Do the tracing yourself with Read/Grep/Glob.

FIXED per-flow template (use exactly these headings):
    # <Flow name>
    ## Triggers        (what starts it — endpoint/topic/schedule)
    ## Steps           (ordered: class#method -> class#method, key branches)
    ## State Touched   (DB tables, caches, queues/topics written)
    ## Downstream Triggers   (outbound calls; mark cross-repo boundaries)
    ## Failure modes   (retries, DLQs, error paths grounded in code)
    ## Gotchas         (non-obvious behavior you actually saw in code)
    ## Related         (other flows/services to cross-link)

""" + _GUARDRAILS + """
When you've covered the entry points, write services/{repo}.md (overview + list of the flows you wrote) and then you're done. Be thorough but keep moving — you have a hard time budget."""

SUPERVISOR_PROMPT = """You are the architecture-crawl SUPERVISOR. A worker just finished tracing the repo `{repo}` and left staged markdown at:
    {staging}
Your ONLY job: reconcile that staged output into the LIVE system map at:
    {map}

Do this:
1. Read every file under {staging} (flows/*.md, services/{repo}.md).
2. Merge into {map}: copy/update {map}/services/{repo}.md and each {map}/flows/<name>.md. When a flow already exists, RECONCILE — don't blindly overwrite: keep the richer version, and if the staged and existing versions CONTRADICT, keep both claims and add a "> CONTRADICTION: ..." note rather than silently picking one.
3. Add BIDIRECTIONAL cross-links: if flow A's "Downstream Triggers" points at service B, ensure B's service/flow docs link back under "Related".
4. Update {map}/INDEX.md: a maintenance-rules header (the guardrails below; "Status: not yet captured" = stub not absence; mandatory bidirectional links), then a services list grouped "Core" (repos already crawled) vs "1-hop neighbours" (services referenced but not themselves crawled), then links to dependencies.md/json if present.
5. Once reconciled, DELETE the staging dir {staging} entirely.

""" + _GUARDRAILS + """
Write clean, readable markdown. This map is meant to be read top-to-bottom from INDEX.md then followed outward, not queried."""


# ------------------------------------------------------------------ manager

class CrawlManager:
    def __init__(self):
        self._client = None
        self._states: dict[str, dict] = {}   # repo -> state dict
        self._pollers: dict[str, asyncio.Task] = {}
        os.makedirs(CRAWL_STATE_DIR, exist_ok=True)
        os.makedirs(CRAWL_LOGS_DIR, exist_ok=True)
        os.makedirs(SYSTEM_MAP_DIR, exist_ok=True)
        self._load_all()

    def attach_client(self, client):
        self._client = client

    # --- state persistence (one JSON per repo) ---

    def _state_path(self, repo: str) -> str:
        return os.path.join(CRAWL_STATE_DIR, f"{repo}.json")

    def _load_all(self):
        for fn in os.listdir(CRAWL_STATE_DIR) if os.path.isdir(CRAWL_STATE_DIR) else []:
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(CRAWL_STATE_DIR, fn)) as f:
                        s = json.load(f)
                    self._states[s["repo"]] = s
                except Exception:
                    logger.exception(f"crawl: bad state file {fn}")

    def _save(self, repo: str):
        s = self._states.get(repo)
        if not s:
            return
        tmp = self._state_path(repo) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, self._state_path(repo))

    # --- helpers ---

    @staticmethod
    def _pid_alive(pid) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False

    def _spawn(self, prompt: str, cwd: str, log_base: str) -> int:
        """Spawn a detached `claude -p`, stdout/stderr to real fds. Returns pid. Not awaited.

        The crawl instructions are the TASK PROMPT (passed as the positional arg — `--print`
        requires input via stdin or arg), not a system prompt. We open the log fds, spawn
        detached, then close our copies so the child owns them and keeps writing after we exit."""
        out_fd = os.open(f"{log_base}.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        err_fd = os.open(f"{log_base}.err", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            cmd = [
                CLAUDE_CLI, "-p", prompt,          # prompt = the task (required by --print)
                "--output-format", "stream-json",
                "--verbose",
                "--model", "opus[1m]",
                "--permission-mode", "bypassPermissions",
                "--allowedTools", "Read,Glob,Grep,Bash,Edit,Write",
            ]
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=out_fd, stderr=err_fd,
                start_new_session=True,   # detach: own process group, survives our death
            )
            return proc.pid
        finally:
            os.close(out_fd)
            os.close(err_fd)

    def _log_base(self, repo: str, stage: str) -> str:
        return os.path.join(CRAWL_LOGS_DIR, f"{repo}-{stage}")

    def _tail_progress(self, log_path: str) -> dict:
        """Scan a stream-json log: tool-call count/last-progress for the live status line, plus
        (when present) the final result event's cost/usage/duration for usage tracking, and
        which tools/skills were invoked — same shape usage_tracker.record_external_run wants."""
        tools = 0
        last = ""
        saw_result = False
        cost_usd = None
        duration_ms = None
        usage = None
        tools_used: set[str] = set()
        skills_used: list[str] = []
        try:
            with open(log_path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "result":
                        saw_result = True
                        cost_usd = ev.get("total_cost_usd")
                        duration_ms = ev.get("duration_ms") or ev.get("duration_api_ms")
                        usage = ev.get("usage")
                    if t == "assistant":
                        for b in ev.get("message", {}).get("content", []):
                            if b.get("type") == "tool_use":
                                tools += 1
                                name = b.get("name", "")
                                inp = b.get("input", {})
                                tools_used.add(name)
                                if name == "Skill" and inp.get("skill"):
                                    skills_used.append(inp["skill"])
                                path = inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
                                if path:
                                    last = f"{name} {os.path.basename(str(path))}"
                                else:
                                    last = name
        except FileNotFoundError:
            pass
        return {
            "tools": tools, "progress": last, "saw_result": saw_result,
            "cost_usd": cost_usd, "duration_ms": duration_ms, "usage": usage,
            "tools_used": sorted(tools_used), "skills_used": skills_used,
        }

    @staticmethod
    def _record_usage(state: dict, repo: str, stage: str, p: dict):
        """Feed a finished worker/supervisor stage's cost into the same usage.jsonl the
        interactive-chat and !loop paths write — crawl workers are real Opus subprocesses and
        were previously invisible to !usage / usage_viewer.py entirely."""
        usage_tracker.record_external_run(
            thread_ts=state.get("thread_ts", ""), channel=state.get("channel", ""),
            command=f"crawl:{repo}:{stage}", cost_usd=p["cost_usd"], duration_ms=p["duration_ms"],
            usage=p["usage"], tools_used=p["tools_used"], skills_used=p["skills_used"],
            is_error=False, role="admin", model="opus[1m]",
        )

    # --- public commands ---

    @staticmethod
    def _resolve_repo(repo: str) -> tuple[str, str] | None:
        """Resolve a `!crawl` argument to (name, absolute_path). No fixed whitelist:

        - An absolute or `~`-prefixed path is used directly (its basename becomes the name).
        - A bare name is looked up under REPOS_BASE_DIR, first as a direct child, then
          one level deep (some checkouts nest, e.g. ~/Hogwards/foo/foo).
        Returns None if nothing resolves to an existing directory."""
        repo = (repo or "").strip()
        if not repo:
            return None

        expanded = os.path.expanduser(repo)
        if repo.startswith(("~", "/", "./", "../")):
            if os.path.isdir(expanded):
                return os.path.basename(os.path.normpath(expanded)), expanded
            return None

        direct = os.path.join(REPOS_BASE_DIR, repo)
        if os.path.isdir(direct):
            return repo, direct

        for match in sorted(glob.glob(os.path.join(REPOS_BASE_DIR, "*", repo))):
            if os.path.isdir(match):
                return repo, match

        return None

    async def start(self, repo: str, channel: str, thread_ts: str) -> tuple[bool, str]:
        resolved = self._resolve_repo(repo)
        if not resolved:
            return False, (
                f"_Repo not found: `{repo}`. Give a name under `{REPOS_BASE_DIR}` "
                f"(checked directly and one level deep) or an absolute/`~` path to a checkout._"
            )
        name, path = resolved
        existing = self._states.get(name)
        if existing and existing.get("status") not in TERMINAL:
            return False, f"_Crawl for `{name}` already {existing.get('status')}. `!crawl-status` to watch._"

        staging = os.path.join(_STAGING_ROOT, name)
        os.makedirs(os.path.join(staging, "flows"), exist_ok=True)
        os.makedirs(os.path.join(staging, "services"), exist_ok=True)
        os.makedirs(os.path.join(SYSTEM_MAP_DIR, "flows"), exist_ok=True)
        os.makedirs(os.path.join(SYSTEM_MAP_DIR, "services"), exist_ok=True)

        prompt = WORKER_PROMPT.format(staging=staging, map=SYSTEM_MAP_DIR, repo=name)
        pid = self._spawn(prompt, cwd=path, log_base=self._log_base(name, "worker"))
        self._states[name] = {
            "repo": name, "path": path, "staging": staging,
            "status": "worker_running", "workerPid": pid, "supervisorPid": None,
            "startedAt": time.time(), "channel": channel, "thread_ts": thread_ts,
            "statusMsgTs": None, "tools": 0, "progress": "", "failureReason": None,
            "sigkilled": False,
        }
        self._save(name)
        logger.info(f"crawl {name}: worker pid={pid} cwd={path}")
        self._pollers[name] = asyncio.create_task(self._poll(name))
        return True, f"🗡️ Crawling *{name}* (`{path}`) — worker `{pid}` detached (survives restarts). Watch: `!crawl-status`."

    async def start_all(self, repos: list[str], channel: str, thread_ts: str) -> str:
        if not repos:
            return f"_Usage: `!crawl-all <repo1> <repo2> ...` — names under `{REPOS_BASE_DIR}` or paths, space-separated._"
        out = []
        for repo in repos:
            ok, msg = await self.start(repo, channel, thread_ts)
            out.append(f"{'✅' if ok else '⏭️'} {repo}: {msg.split('—')[0].strip() if ok else msg}")
        return "*crawl-all:*\n" + "\n".join(out)

    async def stitch(self, repo: str) -> tuple[bool, str]:
        repo = (repo or "").strip()
        s = self._states.get(repo)
        if not s:
            return False, f"_No crawl state for `{repo}`._"
        if self._pid_alive(s.get("workerPid")):
            return False, f"_Worker for `{repo}` still running; let it finish first._"
        s["status"] = "worker_done"
        s["supervisorPid"] = None
        self._save(repo)
        if repo not in self._pollers or self._pollers[repo].done():
            self._pollers[repo] = asyncio.create_task(self._poll(repo))
        return True, f"Re-kicking supervisor for *{repo}*."

    def status_text(self) -> str:
        if not self._states:
            return f"_No crawls yet. `!crawl <repo>` to start — a name under `{REPOS_BASE_DIR}` or a path._"
        lines = ["*Crawls:*"]
        now = time.time()
        for repo, s in sorted(self._states.items()):
            el = int(now - s.get("startedAt", now))
            wpid = s.get("workerPid")
            alive = "alive" if self._pid_alive(wpid) else "dead"
            extra = f" — {s['failureReason']}" if s.get("failureReason") else ""
            lines.append(
                f"• `{repo}` — *{s.get('status')}* | worker {wpid}({alive}) | "
                f"{s.get('tools',0)} tools | {el//60}m{el%60}s{extra}"
            )
        return "\n".join(lines)

    async def resume_crawls(self):
        """On boot: re-arm a poller for any non-terminal crawl (detached proc still running)."""
        resumed = 0
        for repo, s in list(self._states.items()):
            if s.get("status") not in TERMINAL:
                self._pollers[repo] = asyncio.create_task(self._poll(repo))
                resumed += 1
        if resumed:
            logger.info(f"crawl: resumed {resumed} poller(s) from {CRAWL_STATE_DIR}")

    # --- the poller ---

    async def _post_or_edit(self, repo: str, text: str):
        s = self._states[repo]
        if not self._client:
            return
        try:
            if s.get("statusMsgTs"):
                await self._client.chat_update(channel=s["channel"], ts=s["statusMsgTs"], text=text)
            else:
                r = await self._client.chat_postMessage(
                    channel=s["channel"], thread_ts=s["thread_ts"], text=text
                )
                s["statusMsgTs"] = r["ts"]
                self._save(repo)
        except Exception:
            logger.exception(f"crawl {repo}: slack update failed")

    def _render(self, repo: str) -> str:
        s = self._states[repo]
        el = int(time.time() - s.get("startedAt", time.time()))
        icon = {"worker_running": "🔍", "worker_done": "⏳", "supervisor_running": "🧵",
                "stitched": "✅", "failed": "💀"}.get(s.get("status"), "•")
        base = f"{icon} *crawl {repo}* — {s.get('status')} · {s.get('tools',0)} tool-calls · {el//60}m{el%60}s"
        if s.get("progress"):
            base += f"\n_{s['progress']}_"
        if s.get("status") == "stitched":
            base += f"\nSystem map updated → `data/system-map/` (services/{repo}.md + flows/)."
        if s.get("failureReason"):
            base += f"\n:skull: {s['failureReason']} — logs in `.crawl-logs/{repo}-*`."
        return base

    async def _poll(self, repo: str):
        """30s tick: tail log, drive status machine, enforce ceiling, update Slack."""
        while True:
            s = self._states.get(repo)
            if not s or s.get("status") in TERMINAL:
                await self._post_or_edit(repo, self._render(repo))
                return
            try:
                await self._tick(repo)
                await self._post_or_edit(repo, self._render(repo))
            except Exception:
                logger.exception(f"crawl {repo}: poll tick error")
            if self._states.get(repo, {}).get("status") in TERMINAL:
                return
            await asyncio.sleep(CRAWL_POLL_SEC)

    async def _tick(self, repo: str):
        s = self._states[repo]
        status = s.get("status")

        # 4h hard ceiling on the whole crawl.
        if time.time() - s.get("startedAt", time.time()) > CRAWL_HARD_CEILING_SEC:
            self._kill_stage(s)
            s["status"] = "failed"
            s["failureReason"] = "4-hour hard ceiling exceeded; process terminated"
            self._save(repo)
            return

        if status == "worker_running":
            p = self._tail_progress(self._log_base(repo, "worker") + ".log")
            s["tools"], s["progress"] = p["tools"], p["progress"]
            worker_dead = not self._pid_alive(s.get("workerPid"))
            if p["saw_result"] or worker_dead:
                # dual-gate handoff: result event OR process death
                s["status"] = "worker_done"
                if p["saw_result"]:  # only log usage if the log actually carried cost data
                    self._record_usage(s, repo, "worker", p)
            self._save(repo)

        elif status == "worker_done":
            # launch supervisor once
            if not s.get("supervisorPid"):
                prompt = SUPERVISOR_PROMPT.format(staging=s["staging"], map=SYSTEM_MAP_DIR, repo=repo)
                # supervisor cwd = the controller dir (it writes under SYSTEM_MAP_DIR by abs path)
                pid = self._spawn(prompt, cwd=os.path.dirname(__file__),
                                  log_base=self._log_base(repo, "supervisor"))
                s["supervisorPid"] = pid
                s["status"] = "supervisor_running"
                s["startedAt_supervisor"] = time.time()
                logger.info(f"crawl {repo}: supervisor pid={pid}")
                self._save(repo)

        elif status == "supervisor_running":
            p = self._tail_progress(self._log_base(repo, "supervisor") + ".log")
            s["tools"], s["progress"] = p["tools"], p["progress"]
            sup_dead = not self._pid_alive(s.get("supervisorPid"))
            if p["saw_result"] or sup_dead:
                if p["saw_result"]:
                    self._record_usage(s, repo, "supervisor", p)
                # stitched iff the staging dir was consumed (supervisor deletes it on success)
                if not os.path.isdir(s.get("staging", "")):
                    s["status"] = "stitched"
                else:
                    s["status"] = "failed"
                    s["failureReason"] = "supervisor exited but staging not consumed; run `!crawl stitch " + repo + "`"
            self._save(repo)

    def _kill_stage(self, s: dict):
        """SIGTERM the live stage's pid; escalate to SIGKILL if it lingers to next call."""
        pid = s.get("supervisorPid") if s.get("status") == "supervisor_running" else s.get("workerPid")
        if not self._pid_alive(pid):
            return
        try:
            sig = signal.SIGKILL if s.get("sigkilled") else signal.SIGTERM
            os.kill(int(pid), sig)
            s["sigkilled"] = True
        except OSError:
            pass


crawl_manager = CrawlManager()
