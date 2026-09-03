import json
import os
import uuid
from datetime import datetime, timezone

from config import SESSION_FILE, DEFAULT_CWD, DEFAULT_MODEL


class SessionManager:
    def __init__(self):
        self._sessions = {}
        self._load()

    @staticmethod
    def _upgrade_model(model: str | None) -> str | None:
        """Bump a bare model alias to its 1M-context variant.

        Sessions store their model at creation and reuse it on every resume, so
        threads created before the 1M switch stay pinned to the small (200K)
        window and keep autocompact-thrashing on big files/tool outputs. Any
        model with no `[...]` window suffix (e.g. "sonnet", "opus") is upgraded
        to `<model>[1m]`. Already-suffixed models are left untouched.
        """
        if isinstance(model, str) and model and "[" not in model:
            return f"{model}[1m]"
        return model

    def _load(self):
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                self._sessions = json.load(f)
        # One-time migration: upgrade any stale bare-model sessions to the 1M window.
        changed = False
        for sess in self._sessions.values():
            if isinstance(sess, dict):
                upgraded = self._upgrade_model(sess.get("model"))
                if upgraded != sess.get("model"):
                    sess["model"] = upgraded
                    changed = True
        if changed:
            self._save()

    def _save(self):
        with open(SESSION_FILE, "w") as f:
            json.dump(self._sessions, f, indent=2)

    def get_session(self, thread_ts: str) -> dict | None:
        sess = self._sessions.get(thread_ts)
        if isinstance(sess, dict):
            upgraded = self._upgrade_model(sess.get("model"))
            if upgraded != sess.get("model"):
                sess["model"] = upgraded
                self._save()
        return sess

    def create_session(self, thread_ts: str, cwd: str | None = None, model: str | None = None) -> dict:
        session = {
            "session_id": str(uuid.uuid4()),
            "cwd": cwd or DEFAULT_CWD,
            "model": model or DEFAULT_MODEL,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        self._sessions[thread_ts] = session
        self._save()
        return session

    def update_last_active(self, thread_ts: str):
        if thread_ts in self._sessions:
            self._sessions[thread_ts]["last_active"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def update_cwd(self, thread_ts: str, cwd: str):
        if thread_ts in self._sessions:
            self._sessions[thread_ts]["cwd"] = cwd
            self._save()

    def remove_session(self, thread_ts: str):
        self._sessions.pop(thread_ts, None)
        self._save()

    def all_sessions(self) -> dict:
        return dict(self._sessions)

    def join_external_session(self, thread_ts: str, session_id: str, cwd: str, model: str | None = None) -> dict:
        """Map a Slack thread to an existing external Claude session."""
        session = {
            "session_id": session_id,
            "cwd": cwd,
            "model": model or DEFAULT_MODEL,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_active": datetime.now(timezone.utc).isoformat(),
            "external": True,
        }
        self._sessions[thread_ts] = session
        self._save()
        return session

    @staticmethod
    def list_claude_sessions() -> list[dict]:
        """List all active Claude sessions from ~/.claude/sessions/."""
        sessions_dir = os.path.expanduser("~/.claude/sessions")
        if not os.path.isdir(sessions_dir):
            return []
        results = []
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                pid = data.get("pid")
                # Check if process is still alive
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except OSError:
                        alive = False
                results.append({
                    "session_id": data.get("sessionId", "?"),
                    "cwd": data.get("cwd", "?"),
                    "name": data.get("name", "unnamed"),
                    "kind": data.get("kind", "?"),
                    "entrypoint": data.get("entrypoint", "?"),
                    "pid": pid,
                    "alive": alive,
                })
            except (json.JSONDecodeError, IOError):
                continue
        return results

    def prune_old(self, max_age_hours: int = 24):
        now = datetime.now(timezone.utc)
        to_remove = []
        for thread_ts, session in self._sessions.items():
            last_active = datetime.fromisoformat(session["last_active"])
            if (now - last_active).total_seconds() > max_age_hours * 3600:
                to_remove.append(thread_ts)
        for thread_ts in to_remove:
            del self._sessions[thread_ts]
        if to_remove:
            self._save()
