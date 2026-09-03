"""On-call window manager — auto-ack Zenduty alerts for a duration, then report."""
import asyncio
import logging

import zenduty
from config import ZENDUTY_POLL_INTERVAL

logger = logging.getLogger(__name__)


class OnCallManager:
    def __init__(self):
        self._active = False
        self._task: asyncio.Task | None = None
        self._end_ts: float = 0.0
        self._acked: list[dict] = []      # incidents acked this window
        self._seen: set[str] = set()      # unique_ids already handled
        self._started_at: float = 0.0
        self._last_completed: dict | None = None  # summary of last finished window

    @property
    def active(self) -> bool:
        return self._active

    def status_text(self, now: float) -> str:
        if self._active:
            remaining = max(0, int((self._end_ts - now) / 60))
            return f"On-call *active*. ~{remaining} min left. {len(self._acked)} alerts acked so far."
        if self._last_completed:
            lc = self._last_completed
            import datetime
            ended = datetime.datetime.fromtimestamp(lc["ended_at"]).strftime("%H:%M")
            return f"On-call *completed* at {ended}. Acked {lc['count']} alerts during the watch. No window running now."
        return "No on-call window running, and none completed this session."

    async def start(self, hours: float, now: float, loop_clock, send_report):
        """
        Start an on-call window.
        loop_clock: callable returning current epoch seconds (passed in to avoid Date.now ban).
        send_report: async callable(text) to DM the final report.
        """
        if self._active:
            return False  # already running
        self._active = True
        self._acked = []
        self._seen = set()
        self._started_at = now
        self._end_ts = now + hours * 3600
        self._task = asyncio.create_task(self._run(hours, loop_clock, send_report))
        return True

    async def stop(self, loop_clock, send_report):
        """End the window early and send the report."""
        if not self._active:
            return False
        self._active = False
        if self._task:
            self._task.cancel()
        await self._send_final_report(send_report)
        return True

    async def _run(self, hours: float, loop_clock, send_report):
        try:
            while self._active and loop_clock() < self._end_ts:
                await self._poll_and_ack(loop_clock)
                await asyncio.sleep(ZENDUTY_POLL_INTERVAL)
            # window elapsed naturally
            if self._active:
                self._active = False
                await self._send_final_report(send_report)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"On-call loop error: {e}")
            self._active = False

    async def _poll_and_ack(self, loop_clock):
        # The filter endpoint's date window is unreliable for untouched triggered
        # incidents, so use a generous 7-day lookback and rely on the per-incident
        # status check in get_my_triggered_incidents to find anything still triggered.
        import datetime
        start_dt = datetime.datetime.utcfromtimestamp(loop_clock()) - datetime.timedelta(days=7)
        end_dt = datetime.datetime.utcfromtimestamp(loop_clock()) + datetime.timedelta(days=1)
        from_date = start_dt.strftime("%Y-%m-%d")
        to_date = end_dt.strftime("%Y-%m-%d")

        try:
            incidents = await asyncio.to_thread(
                zenduty.get_my_triggered_incidents, from_date, to_date
            )
        except Exception as e:
            logger.warning(f"Poll failed: {e}")
            return

        for inc in incidents:
            uid = inc.get("unique_id")
            if not uid or uid in self._seen:
                continue
            self._seen.add(uid)
            ok = await asyncio.to_thread(zenduty.ack_incident, uid)
            if ok:
                self._acked.append({
                    "number": inc.get("incident_number"),
                    "title": inc.get("title", "")[:120],
                    "service": inc.get("service_object", {}).get("name", "?"),
                    "time": inc.get("creation_date", ""),
                })
                logger.info(f"Auto-acked incident #{inc.get('incident_number')}")

    async def _send_final_report(self, send_report):
        import time as _t
        self._last_completed = {"ended_at": _t.time(), "count": len(self._acked)}
        if not self._acked:
            await send_report("*On-call window ended.* No alerts fired. Quiet shift, warrior.")
            return
        lines = [f"*On-call window ended.* Acked *{len(self._acked)}* alerts:\n"]
        for a in self._acked:
            ts = a["time"][:16].replace("T", " ") if a["time"] else "?"
            lines.append(f"• `#{a['number']}` [{a['service']}] {a['title']}  _({ts} UTC)_")
        lines.append("\nThe Dragonslayer rests.")
        await send_report("\n".join(lines))


# Singleton
oncall_manager = OnCallManager()
