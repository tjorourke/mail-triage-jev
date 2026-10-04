#!/bin/bash
# Run mail-triage in the background on macOS (starts at login, restarts if it crashes).
#   ./service.sh install | uninstall | restart | status | logs
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

case "$1" in
  install)   mkdir -p "$DIR/logs"; write_plist; launchctl bootstrap gui/$UID_ "$PLIST" && echo "installed and started" ;;
  uninstall) launchctl bootout gui/$UID_ "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; echo "stopped and removed" ;;
  restart)   launchctl kickstart -k gui/$UID_/$LABEL && echo "restarted" ;;
  status)    launchctl print gui/$UID_/$LABEL 2>&1 | grep -E "state|pid|last exit" ;;
  logs)      tail -n 40 -f "$DIR/logs/triage.log" ;;
  *) echo "usage: $0 install|uninstall|restart|status|logs"; exit 1 ;;
esac
