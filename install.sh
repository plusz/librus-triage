#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [[ ! -f .env ]]; then
    echo "ERROR: .env missing — copy placeholders and fill LIBRUS_LOGIN / LIBRUS_PASS first." >&2
    exit 1
fi
if grep -qE '^LIBRUS_LOGIN=$|^LIBRUS_PASS=$' .env; then
    echo "WARNING: LIBRUS_LOGIN or LIBRUS_PASS still empty in .env" >&2
fi

echo ">> creating venv"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

mkdir -p .state logs

PLIST_DST="$HOME/Library/LaunchAgents/com.librus.checker.plist"
sed "s|__PROJECT_DIR__|$DIR|g" com.librus.checker.plist > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ">> installed LaunchAgent: $PLIST_DST (every 3600s)"
echo ">> logs: $DIR/logs/checker.log"
echo ">> test now: ./venv/bin/python3 librus_checker.py --list"
