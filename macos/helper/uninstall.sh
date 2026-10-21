#!/bin/bash
# Remove the root helper and every app service it manages.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
for plist in /Library/LaunchDaemons/ha-macos*.app.*.plist; do
  [ -e "$plist" ] || continue
  label=$(basename "$plist" .plist)
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "$plist"
done
launchctl bootout system/ha-macos.helper 2>/dev/null || true
rm -f /Library/LaunchDaemons/ha-macos.helper.plist /var/run/ha-macos-helper.sock
rm -rf /usr/local/ha-macos
echo "ha-macos.helper removed"
