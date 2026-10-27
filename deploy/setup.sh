#!/usr/bin/env bash
# Sets up the bot on a fresh Ubuntu server (written for Oracle Cloud's Always Free VM).
# deploy/push.sh uploads the code to /tmp/league-bot-upload and runs this with sudo.
# Safe to run again: every step checks before it changes anything.
#
# Needs in .env: ANTHROPIC_API_KEY, DUCKDNS_SUBDOMAIN (e.g. "yourbot"), DUCKDNS_TOKEN.
set -euo pipefail

UPLOAD=/tmp/league-bot-upload
APP=/opt/league-bot
USER_NAME=leaguebot

# Read the DuckDNS settings without printing them.
DUCKDNS_SUBDOMAIN=$(grep -E '^DUCKDNS_SUBDOMAIN=' "$UPLOAD/.env" | cut -d= -f2- | tr -d "\"' ")
if [ -z "$DUCKDNS_SUBDOMAIN" ]; then
    echo "DUCKDNS_SUBDOMAIN is missing from .env" >&2
    exit 1
fi
DOMAIN="$DUCKDNS_SUBDOMAIN.duckdns.org"

echo "== 1/8 System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv curl iptables-persistent debian-keyring debian-archive-keyring apt-transport-https gpg

echo "== 2/8 Swap (the free VM has 1 GB of RAM; swap keeps the daily build from running out)"
if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 3/8 Firewall"
# Oracle's Ubuntu image rejects everything not explicitly allowed in iptables, on top of
# the cloud-side security list. Insert ACCEPT rules just above its catch-all REJECT rule.
open_port() {
    local proto=$1 port=$2
    if ! iptables -C INPUT -p "$proto" -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
        local reject_line
        reject_line=$(iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')
        iptables -I INPUT "${reject_line:-1}" -p "$proto" -m state --state NEW --dport "$port" -j ACCEPT
    fi
}
open_port tcp 80
open_port tcp 443
open_port udp 443   # HTTP/3
netfilter-persistent save

echo "== 4/8 Caddy (official repository; commands from caddyserver.com/docs/install)"
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
fi

echo "== 5/8 App files"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$APP"
# Copy the upload over the app, but keep the server's own database once it has one:
# it holds the daily value snapshots and the question log.
if [ -f "$APP/data/league.db" ]; then
    rm -rf "$UPLOAD/data"
fi
cp -a "$UPLOAD/." "$APP/"
chown -R "$USER_NAME:$USER_NAME" "$APP"
chmod 600 "$APP/.env"
[ -f "$APP/tendencies.txt" ] && chmod 600 "$APP/tendencies.txt"

echo "== 6/8 Python packages"
sudo -u "$USER_NAME" python3 -m venv "$APP/.venv"
sudo -u "$USER_NAME" "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== 7/8 League data (first build takes a few minutes on this small VM)"
if [ ! -f "$APP/data/league.db" ]; then
    sudo -u "$USER_NAME" bash -c "cd $APP && .venv/bin/python -m league_bot build"
fi

echo "== 8/8 Services"
cp "$APP/deploy/league-bot.service" /etc/systemd/system/league-bot.service
systemctl daemon-reload
systemctl enable league-bot
systemctl restart league-bot

# Daily rebuild, plus a DuckDNS update every 5 minutes so the name always points here.
# The token is read from .env at run time, so it never sits in the crontab itself.
cat > /tmp/league-bot.cron <<CRON
0 6 * * * cd $APP && .venv/bin/python -m league_bot build >> data/build.log 2>&1
*/5 * * * * . $APP/.env && curl -s -o $APP/data/duckdns.log "https://www.duckdns.org/update?domains=\$DUCKDNS_SUBDOMAIN&token=\$DUCKDNS_TOKEN&ip="
CRON
crontab -u "$USER_NAME" /tmp/league-bot.cron
rm /tmp/league-bot.cron
# Point DuckDNS here right away instead of waiting up to 5 minutes.
sudo -u "$USER_NAME" bash -c ". $APP/.env && curl -s -o $APP/data/duckdns.log \"https://www.duckdns.org/update?domains=\$DUCKDNS_SUBDOMAIN&token=\$DUCKDNS_TOKEN&ip=\""
echo "DuckDNS update: $(cat $APP/data/duckdns.log) (OK = worked, KO = check the subdomain and token)"

printf '%s {\n\treverse_proxy 127.0.0.1:8000\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

rm -rf "$UPLOAD"
echo
echo "Done. The bot should be live at https://$DOMAIN (the HTTPS certificate can take a minute on first visit)."
