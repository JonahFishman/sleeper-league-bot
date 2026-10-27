"""The web page: one input box, one answer. No accounts.

Run locally:  python app.py   (then open http://localhost:8000)
In production it runs under gunicorn behind Caddy; see deploy/.
"""

import os
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template, request

from league_bot import db
from league_bot.chat import Bot
from league_bot.league import League

app = Flask(__name__)
bot = Bot()

# The page has no login, so anyone with the link can ask questions on Jonah's API bill.
# These limits are far above what 12 people will use but stop a script hammering it.
MAX_QUESTION_CHARS = 1000
PER_IP_PER_HOUR = 40
PER_DAY_TOTAL = 500
_recent_by_ip = defaultdict(deque)
_daily = {"day": None, "count": 0}


def _over_limit(ip):
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if _daily["day"] != today:
        _daily.update(day=today, count=0)
    hits = _recent_by_ip[ip]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= PER_IP_PER_HOUR or _daily["count"] >= PER_DAY_TOTAL:
        return True
    hits.append(now)
    _daily["count"] += 1
    return False


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/teams")
def teams():
    """The 12 teams for the page's "which team are you?" picker."""
    conn = db.connect()
    try:
        choices = League(conn, bot.players).team_choices()
    finally:
        conn.close()
    return jsonify(teams=[{"roster_id": rid, "label": label} for rid, label in sorted(choices.items(), key=lambda kv: kv[1].lower())])


@app.post("/ask")
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify(error="Ask me something."), 400
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify(error="That's a long one. Keep it under 1,000 characters."), 400

    # Caddy forwards the real client IP in X-Forwarded-For.
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _over_limit(ip):
        return jsonify(error="Slow down. Too many questions for now, try again later."), 429

    try:
        team = data.get("team")
        answer = bot.ask(question, data.get("session_id"), team if isinstance(team, int) else None)
    except Exception:
        app.logger.exception("ask failed")
        return jsonify(error="Something broke on my end. Try again in a minute."), 500
    return jsonify(answer=answer.text, session_id=answer.session_id)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), debug=False)
