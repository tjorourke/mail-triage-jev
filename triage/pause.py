"""The only thing that stops the service: the pause switch. A Shortcuts automation turns it on when Do Not Disturb
turns on (bin/focus-on) and off when it turns off (bin/focus-off). `triage pause 2h` / `triage resume` do it by hand."""
import re
import time
from pathlib import Path

PAUSE_FILE = Path.home() / ".mail-triage-paused"


def set_pause(seconds=None):
    """Pause until `seconds` from now, or until `triage resume` if seconds is None."""
    PAUSE_FILE.write_text(str(int(time.time() + seconds)) if seconds else "indefinite")


def clear_pause():
    PAUSE_FILE.unlink(missing_ok=True)


def reason(cfg=None, in_run=False):
    """Why not to run right now, or None."""
    if not PAUSE_FILE.exists():
        return None
    txt = PAUSE_FILE.read_text().strip()
    if txt.isdigit():
        left = int(txt) - time.time()
        if left <= 0:
            PAUSE_FILE.unlink(missing_ok=True)   # expired
            return None
        return f"paused for another {int(left // 60) + 1} min (Do Not Disturb or `triage pause`)"
    return "paused (Do Not Disturb is on, or run: triage resume)"


def parse_duration(text):
    m = re.fullmatch(r"(\d+)\s*([mhd])", text.strip().lower())
    if not m:
        raise SystemExit("duration looks like 30m, 2h or 1d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
