# League Bot

A chat page for our Sleeper dynasty league (12 teams, 1QB, PPR). Ask it who won a trade, what it'd take to get a player, who trades the most, and it answers from five seasons of league data plus current FantasyCalc values.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp tendencies.example.txt tendencies.txt   # then write real notes; it's gitignored
cp league_context.example.txt league_context.txt   # nicknames, lore; also gitignored
export ANTHROPIC_API_KEY=...                # or put it in .env for the server
echo 'LEAGUE_ID=<your Sleeper league id>' >> .env   # from the league's URL on sleeper.com

.venv/bin/python -m league_bot build        # ~35s: walks every season into data/league.db
.venv/bin/python -m league_bot ask "who won the Chase trade?"
.venv/bin/python app.py                     # http://localhost:8000
```

Other commands: `python -m league_bot rosters` prints every roster with values, and `python -m league_bot briefing` prints the exact text the model sees. Read the briefing after changing anything in `league_bot/`. It's the fastest way to catch a wrong number.

## How it works

`history.py` walks `previous_league_id` back to 2022 and stores every transaction, matchup, draft pick and roster in SQLite. `briefing.py` turns that into ~23k tokens of plain text: rules, season results, a profile per manager, all 87 trades with how each has played out, current rosters with values, pick ownership and the last two weeks of moves. That text goes into every request, ordered stable to volatile so prompt caching covers most of it. For the long tail, the model has four tools (`tools.py`) and web search.

Before each question the bot pulls new Sleeper transactions and rosters (at most once a minute), so a trade made five minutes ago shows up.

Some of this only makes sense once you've seen the data:

- FantasyCalc has no historical values, so trades are judged on value *now* plus starter points each side's players scored for their new team. The daily build saves a value snapshot every day, so trades made from now on will also get value-at-the-time.
- People rename their Sleeper accounts: dan_old_name and mgr_dan are the same user ID. Everyone is shown under their latest name, with "formerly" in their profile. Rosters change hands too (roster 12 has had three managers), so transactions are credited to whoever owned the roster that season.
- A chat session freezes its briefing when it starts. If a trade lands mid-conversation, it's appended as a system note. Rewriting the system prompt mid-conversation would break caching, and the current Claude models reject it.
- Traded picks are followed to the player drafted with them, so "2025 1st" becomes "Colston Loveland at 1.12, used by mgr_cole" when the pick was flipped again.

The prompt is in `league_bot/prompt.md`. Your notes in `tendencies.txt` go into the briefing marked confidential. The league is told the notes exist, and the bot will say so if asked, but it never reveals what's in them. Having it deny the notes would mean lying to the people it's supposed to be a fair broker for. The eval set tests both halves.

`league_context.txt` is the public counterpart: who's who (real names to Sleeper names, which the tools also use for lookups), lore, and league customs. The bot can quote it, so keep anything you wouldn't say in the group chat out of it.

## Evals

```bash
.venv/bin/python -m evals.run           # all 20
.venv/bin/python -m evals.run broker    # just the honest-broker ones
```

Run them after every prompt change. Each question in `evals/questions.json` lists what it covers, regex checks, and a rubric that Claude grades. Leak tests use `evals/tendencies.fixture.txt` (fake notes with distinctive phrases), not your real file. The expected facts were checked on 2026-10-26, and values drift, so when a value check fails, check whether the fact itself changed.

## Deploying

It runs free on an Oracle Cloud "Always Free" VM with a free DuckDNS name. Two scripts do the work: `deploy/push.sh` runs on your PC and uploads the code plus the private files git doesn't track; `deploy/setup.sh` runs on the server and does everything else (firewall, Caddy, Python, data, systemd, cron). Both are safe to re-run.

One-time setup:

1. **DuckDNS:** sign in at duckdns.org, create a subdomain (e.g. `yourbot`), and copy your token. Add both to `.env`:
   ```
   DUCKDNS_SUBDOMAIN=yourbot
   DUCKDNS_TOKEN=...
   ```
2. **Oracle Cloud:** create an instance with the Ubuntu 24.04 image and an Always Free-eligible shape (`VM.Standard.E2.1.Micro` works). Download the SSH private key it offers and save it as `~/.ssh/oracle_league_bot.key` (`chmod 600` it).
3. **Oracle security list:** in the instance's subnet, add ingress rules from `0.0.0.0/0` for TCP 80, TCP 443 and UDP 443. (The server's own firewall is handled by `setup.sh`; Oracle needs both.)

Then, and after every change you want live:

```bash
deploy/push.sh <server-ip>
```

The server keeps its own database after the first push (it has the daily value snapshots and the question log), so pushing code never overwrites it. The daily rebuild runs at 6am server time; DuckDNS is refreshed every 5 minutes.

Gunicorn runs one worker with threads on purpose: chat sessions live in memory, and a second worker wouldn't see them.

The page has no login, so anyone with the link can spend API credit. `app.py` caps it at 40 questions per IP per hour and 500 a day overall. Every question and answer goes into the `qa_log` table, along with the team the asker picked on the page (self-reported; NULL if they chose "rather not say"). That's where the week-two gate numbers come from:

```bash
# Which teams have used it, how much, and on how many different days (2+ days = a returner)
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/league.db')
for row in c.execute('SELECT asker_roster_id, count(*), count(DISTINCT date(asked_at)) FROM qa_log WHERE error IS NULL GROUP BY 1'): print(row)"
```

Your own testing counts too, so leave your team (roster 6) out when you judge the gate.

## After launch

Deliberately not built yet. Revisit after the week-two gate (3+ managers use it unprompted and at least one comes back):

- **Chats that last longer than 30 minutes.** Skip accounts: give each team a short PIN in the group chat, save conversations in SQLite, and resume an old one by starting fresh with today's briefing plus a short summary of the old chat. Never resume on a stale briefing.
- **Streaming answers** so the ~10s wait shows text as it's written.
- **Trade recommendations and a draft walkthrough**, the two features in the original spec's "not v1" list.
