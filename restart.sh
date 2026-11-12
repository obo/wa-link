#!/bin/bash
# (Re)start the bridge in the background, after an optional delay (so that a running Claude answer
# can still be posted). Settings come from wa-link.env (see wa-link.env.example).
# Usage: setsid nohup ./restart.sh [DELAY_S] >/dev/null 2>&1 &
HERE="$(cd "$(dirname "$0")" && pwd)"
sleep "${1:-0}"
# cron (watcher.sh) runs with a minimal PATH; claude is usually installed in ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"
set -a; . "$HERE/wa-link.env"; set +a
pkill -f "^$HERE/venv/bin/python $HERE/wa_link.py"
sleep 3
cd "${WA_WORKDIR:-$HERE/workspace}" || exit 1
mkdir -p "$HERE/state"
# shellcheck disable=SC2086  # WA_ARGS is a list of options
PYTHONUNBUFFERED=1 nohup "$HERE/wa-link" $WA_ARGS >> "$HERE/state/wa-link.log" 2>&1 &
# If WhatsApp logged us out, the bridge archives the old session (state/whatsapp.sqlite3.logged-out-*)
# on start and shows a pairing QR code in state/wa-link.log and state/pairing-qr.png.
