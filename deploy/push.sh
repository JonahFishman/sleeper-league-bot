#!/usr/bin/env bash
# Run from your PC (repo root) to deploy or update the server:
#   deploy/push.sh <server-ip>
# Uploads the code plus the private files git doesn't track (.env, tendencies.txt,
# league_context.txt, data/) over SSH, then runs deploy/setup.sh on the server.
# Re-run it any time to ship changes; the server keeps its own database.
set -euo pipefail

SERVER_IP=${1:?usage: deploy/push.sh <server-ip>}
KEY=${SSH_KEY:-$HOME/.ssh/oracle_league_bot.key}   # the private key Oracle had you download
TARGET="ubuntu@$SERVER_IP"                           # Oracle's Ubuntu images log in as "ubuntu"

for f in .env tendencies.txt league_context.txt; do
    [ -f "$f" ] || echo "warning: $f not found; the server won't have it" >&2
done
grep -q '^DUCKDNS_SUBDOMAIN=' .env && grep -q '^DUCKDNS_TOKEN=' .env || {
    echo "Add DUCKDNS_SUBDOMAIN=... and DUCKDNS_TOKEN=... to .env first." >&2
    exit 1
}

# Fold SQLite's write-ahead log into the main file so the copy is consistent even if
# the local test server is running.
.venv/bin/python -c "import sqlite3; sqlite3.connect('data/league.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"

rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
    --exclude .git --exclude .venv --exclude '__pycache__' --exclude evals/results \
    ./ "$TARGET:/tmp/league-bot-upload/"
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$TARGET" "sudo bash /tmp/league-bot-upload/deploy/setup.sh"
