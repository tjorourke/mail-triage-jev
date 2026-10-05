#!/bin/bash
# Run mail-triage in the background on macOS (starts at login, restarts if it crashes).
#   ./service.sh install | uninstall | restart | status | logs
#   ./service.sh start | stop | deploy | run-now | state-json   (used by the mac-shortcuts-app panel)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.mail-triage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_="$(id -u)"

write_plist() {
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/triage</string><string>daemon</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>HF_HUB_OFFLINE</key><string>1</string>
    <key>PYTORCH_ENABLE_MPS_FALLBACK</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$DIR/logs/daemon.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/daemon.err</string>
</dict>
</plist>
PLIST_EOF
}

loaded() { launchctl print gui/$UID_/$LABEL >/dev/null 2>&1; }

state_json() {
  local svc=off pid=""
  if loaded; then
    svc=$(launchctl print gui/$UID_/$LABEL 2>/dev/null | awk -F'= ' '/^\tstate =/{print $2; exit}')
    pid=$(launchctl print gui/$UID_/$LABEL 2>/dev/null | awk -F'= ' '/^\tpid =/{print $2; exit}')
  fi
  local running=0
  "$DIR/.venv/bin/python" -c "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)" "$DIR/logs/run.lock" 2>/dev/null || running=1
  local paused
  paused=$(cd "$DIR" && "$DIR/.venv/bin/triage" status 2>/dev/null | sed -n 's/^standing down because: //p')
  SVC="$svc" PID="$pid" RUNNING="$running" PAUSED="$paused" LOG="$DIR/logs/triage.log" "$DIR/.venv/bin/python" - <<'PY'
import json, os, re
svc, running, paused = os.environ["SVC"], os.environ["RUNNING"] == "1", os.environ["PAUSED"]
last = ""
try:
    lines = open(os.environ["LOG"], errors="replace").read().splitlines()[-400:]
    last = next((l for l in reversed(lines) if re.search(r" run \[| failed|Traceback|ERROR", l)), "")
except OSError:
    pass
when = last[11:16] if last[:4].isdigit() else ""
summary = re.sub(r"^.*?run \[[^\]]*\]: ", "", last)[:80] if " run [" in last else last[20:100]
failed = bool(re.search(r"failed|Traceback|ERROR", last)) and " run [" not in last
if svc == "off":
    out = {"state": "off", "text": "Stopped", "detail": "launchd agent not loaded"}
elif running:
    out = {"state": "busy", "text": "Checking mail now", "detail": "a run is in progress"}
elif paused and not paused.startswith("nothing"):
    out = {"state": "warn", "text": "Standing down", "detail": paused}
elif failed:
    out = {"state": "error", "text": "Last run failed", "detail": f"{when} {summary}".strip()}
else:
    out = {"state": "ok", "text": "Running", "detail": f"last run {when}: {summary}" if last else "no run yet"}
out["subtitle"] = "Gmail · local model"
print(json.dumps(out))
PY
}

case "$1" in
  install)   mkdir -p "$DIR/logs"; write_plist; launchctl bootstrap gui/$UID_ "$PLIST" && echo "installed and started" ;;
  uninstall) launchctl bootout gui/$UID_ "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; echo "stopped and removed" ;;
  restart)   launchctl kickstart -k gui/$UID_/$LABEL && echo "restarted" ;;
  status)    launchctl print gui/$UID_/$LABEL 2>&1 | grep -E "state|pid|last exit" ;;
  logs)      tail -n 40 -f "$DIR/logs/triage.log" ;;
  start)     mkdir -p "$DIR/logs"; [ -f "$PLIST" ] || write_plist
             loaded || launchctl bootstrap gui/$UID_ "$PLIST"; echo "started" ;;
  stop)      launchctl bootout gui/$UID_ "$PLIST" 2>/dev/null || true; echo "stopped (back at next login; uninstall removes it)" ;;
  deploy)    (cd "$DIR" && uv sync -q); mkdir -p "$DIR/logs"
             launchctl bootout gui/$UID_ "$PLIST" 2>/dev/null || true; write_plist
             launchctl bootstrap gui/$UID_ "$PLIST" && echo "deployed and restarted" ;;
  run-now)   # Skip the hourly wait. A fresh daemon checks straight away and keeps one model in memory;
             # when paused or not loaded, a one-off run ignores the pause. run.lock stops overlaps.
             if loaded && ! (cd "$DIR" && "$DIR/.venv/bin/triage" status | grep -q '^standing down because: [^n]'); then
               launchctl kickstart -k gui/$UID_/$LABEL && echo "daemon restarted: checking new mail now"
             else
               cd "$DIR" && exec "$DIR/.venv/bin/triage" run
             fi ;;
  state-json) state_json ;;
  *) echo "usage: $0 install|uninstall|restart|status|logs|start|stop|deploy|run-now|state-json"; exit 1 ;;
esac
