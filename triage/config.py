import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    with open(ROOT / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    cfg["root"] = ROOT
    cfg["db_path"] = ROOT / "triage.db"
    cfg["log_dir"] = ROOT / "logs"
    return cfg


def read_list(name):
    """Read allowlist.txt / blocklist.txt into a set of lowercase entries."""
    path = ROOT / name
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out
