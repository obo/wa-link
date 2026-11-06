#!/bin/bash
# Cron watchdog: restart the bridge if it is not running (e.g. after a reboot).
# Does nothing while WhatsApp has logged us out (state/logged-out): that needs a manual re-pairing.
# crontab:  */10 * * * * /path/to/wa-link/watcher.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
pgrep -f "^$HERE/venv/bin/python $HERE/wa_link.py" >/dev/null && exit 0
[ -e "$HERE/state/logged-out" ] && exit 0
mkdir -p "$HERE/state"
echo "$(date '+%F %T') watcher: wa-link not running, restarting" >> "$HERE/state/watcher.log"
exec setsid nohup "$HERE/restart.sh" >/dev/null 2>&1
