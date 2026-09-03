"""Self-modification support: deferred restart so Guts can restart itself
without killing its own in-flight response."""
import os
import subprocess

PID_FILE = os.path.join(os.path.dirname(__file__), "guts.pid")


def schedule_restart(delay_seconds: int = 5) -> bool:
    """Spawn a detached process that, after delay, kills the running main.py.
    The watchdog (run.sh) then respawns it with the new code.
    Returns True if a restart was scheduled."""
    try:
        pid = int(open(PID_FILE).read().strip())
    except (OSError, ValueError):
        return False
    # Detached: survives the main.py kill, runs independently of this subprocess
    subprocess.Popen(
        ["bash", "-c", f"sleep {delay_seconds}; kill {pid}"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True
