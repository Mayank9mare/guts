"""Zenduty API client — list and acknowledge incidents."""
import json
import logging
import urllib.request

from config import ZENDUTY_TOKEN, ZENDUTY_USER_ID, ZENDUTY_TEAM_ID

logger = logging.getLogger(__name__)

BASE = "https://www.zenduty.com/api"

# Incident status codes
STATUS_TRIGGERED = 1
STATUS_ACKNOWLEDGED = 2
STATUS_RESOLVED = 3


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    url = f"{BASE}{path}"
    headers = {
        "Authorization": f"Token {ZENDUTY_TOKEN}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        logger.warning(f"Zenduty {method} {path} failed: {e.code} {e.read().decode()[:200]}")
        return e.code, None
    except Exception as e:
        logger.warning(f"Zenduty {method} {path} error: {e}")
        return 0, None


def get_my_triggered_incidents(from_date: str, to_date: str) -> list[dict]:
    """Return triggered (unacked) incidents assigned to the configured user."""
    body = {
        "status": STATUS_TRIGGERED,
        "team_ids": [ZENDUTY_TEAM_ID],
        "user_ids": [ZENDUTY_USER_ID],
        "from_date": from_date,
        "to_date": to_date,
    }
    status, data = _request("POST", "/incidents/filter/", body)
    if status != 200 or not data:
        return []
    results = data.get("results", []) if isinstance(data, dict) else data
    # Filter strictly to triggered + assigned to me (API filter can be loose)
    return [
        i for i in results
        if i.get("status") == STATUS_TRIGGERED and i.get("assigned_to") == ZENDUTY_USER_ID
    ]


def ack_incident(unique_id: str) -> bool:
    """Acknowledge an incident. Returns True on success."""
    status, _ = _request("PATCH", f"/incidents/{unique_id}/", {"status": STATUS_ACKNOWLEDGED})
    return status == 200


def is_configured() -> bool:
    return bool(ZENDUTY_TOKEN and ZENDUTY_USER_ID and ZENDUTY_TEAM_ID)
