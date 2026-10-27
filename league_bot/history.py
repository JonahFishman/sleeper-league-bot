"""Pull league history from Sleeper into SQLite.

Two entry points:
  build_all(conn)   - the daily job. Walks every season back through previous_league_id
                      and (re)stores everything. Safe to re-run; it upserts.
  sync_recent(conn) - the per-question freshness step. Fetches only the current
                      season's rosters and any transactions newer than a cursor, so a
                      trade made five minutes ago shows up in both the rosters and the
                      trade ledger at the same time.
"""

import json
from datetime import datetime, timezone

from . import config, db, sleeper

MAX_WEEK = 18  # Regular season + playoffs never run past week 18


def season_chain():
    """League objects from the current season back to the first, newest first."""
    if not config.LEAGUE_ID:
        raise RuntimeError("Set LEAGUE_ID in .env to your Sleeper league ID (it's in the league's URL).")
    chain = []
    league_id = config.LEAGUE_ID
    while league_id and league_id != "0":
        league = sleeper.league(league_id)
        if league is None:
            break
        chain.append(league)
        league_id = league.get("previous_league_id")
    return chain


def build_all(conn, log=print):
    state = sleeper.nfl_state()
    for league in season_chain():
        league_id, season = league["league_id"], league["season"]
        is_current = league["status"] != "complete"
        log(f"Season {season} ({league_id}, {league['status']})")

        _store_league(conn, league)
        _store_users(conn, league_id)
        _store_rosters(conn, league_id)

        # Weeks to walk. For the current season, stop at the current NFL week.
        last_week = int(state["week"]) if is_current and state["season"] == season else MAX_WEEK
        tx_count = 0
        for week in range(0, last_week + 1):
            tx_count += _store_transactions(conn, league_id, season, week)
        log(f"  {tx_count} transactions")

        # Only completed weeks have final scores.
        scored_through = league["settings"].get("last_scored_leg") or 0
        if not is_current:
            scored_through = MAX_WEEK
        for week in range(1, scored_through + 1):
            if not _store_matchups(conn, league_id, season, week):
                break  # past the end of the season

        for draft in sleeper.drafts(league_id):
            if draft["status"] == "complete":
                _store_draft(conn, draft)

        if is_current:
            _store_traded_picks(conn, league_id)
            db.set_meta(conn, "current_league_id", league_id)
            db.set_meta(conn, "tx_cursor_week", last_week)
        conn.commit()

    db.set_meta(conn, "last_full_build", _now_iso())
    conn.commit()


def sync_recent(conn):
    """Bring the current season up to date. Returns IDs of transactions that are new
    (or that just changed status, e.g. a pending trade that completed)."""
    league_id = db.get_meta(conn, "current_league_id")
    if league_id is None:
        raise RuntimeError("No league data yet. Run: python -m league_bot build")
    season = conn.execute("SELECT season FROM seasons WHERE league_id = ?", (league_id,)).fetchone()["season"]

    before = {
        row["transaction_id"]: row["status"]
        for row in conn.execute("SELECT transaction_id, status FROM transactions WHERE league_id = ?", (league_id,))
    }

    # Re-read the cursor week too: Sleeper can still add to it (e.g. a trade accepted late).
    cursor_week = int(db.get_meta(conn, "tx_cursor_week", 0))
    current_week = max(int(sleeper.nfl_state()["week"]), cursor_week)
    for week in range(cursor_week, current_week + 1):
        _store_transactions(conn, league_id, season, week)
    _store_rosters(conn, league_id)
    _store_traded_picks(conn, league_id)
    db.set_meta(conn, "tx_cursor_week", current_week)
    conn.commit()

    after = conn.execute("SELECT transaction_id, status FROM transactions WHERE league_id = ?", (league_id,))
    return [row["transaction_id"] for row in after if before.get(row["transaction_id"]) != row["status"]]


# ---- storing each kind of data ----------------------------------------------------


def _store_league(conn, league):
    champion = runner_up = None
    if league["status"] == "complete":
        # The championship game is the bracket match with p == 1 (it decides 1st place).
        for match in sleeper.winners_bracket(league["league_id"]) or []:
            if match.get("p") == 1:
                champion, runner_up = match.get("w"), match.get("l")
    conn.execute(
        """INSERT OR REPLACE INTO seasons
           (league_id, season, previous_league_id, status, champion_roster_id, runner_up_roster_id, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (league["league_id"], league["season"], league.get("previous_league_id"), league["status"],
         champion, runner_up, json.dumps(league)),
    )


def _store_users(conn, league_id):
    for user in sleeper.users(league_id):
        conn.execute(
            "INSERT OR REPLACE INTO users (league_id, user_id, display_name, team_name) VALUES (?, ?, ?, ?)",
            (league_id, user["user_id"], user["display_name"], (user.get("metadata") or {}).get("team_name")),
        )


def _store_rosters(conn, league_id):
    for r in sleeper.rosters(league_id):
        s = r.get("settings") or {}
        conn.execute(
            """INSERT OR REPLACE INTO rosters
               (league_id, roster_id, owner_id, co_owner_ids, players, starters, taxi, reserve,
                wins, losses, ties, points_for, points_against)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                league_id, r["roster_id"], r.get("owner_id"),
                json.dumps(r.get("co_owners") or []),
                json.dumps(r.get("players") or []),
                json.dumps(r.get("starters") or []),
                json.dumps(r.get("taxi") or []),
                json.dumps(r.get("reserve") or []),
                s.get("wins", 0), s.get("losses", 0), s.get("ties", 0),
                # Sleeper splits points into an integer part and a 2-digit decimal part.
                s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100,
                s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100,
            ),
        )


def _store_transactions(conn, league_id, season, week):
    txs = sleeper.transactions(league_id, week)
    for t in txs:
        conn.execute(
            """INSERT OR REPLACE INTO transactions
               (transaction_id, league_id, season, week, type, status, created_ms, status_updated_ms,
                roster_ids, adds, drops, draft_picks, waiver_budget, waiver_bid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["transaction_id"], league_id, season, week, t["type"], t["status"],
                t.get("created"), t.get("status_updated"),
                json.dumps(t.get("roster_ids") or []),
                json.dumps(t.get("adds") or {}),
                json.dumps(t.get("drops") or {}),
                json.dumps(t.get("draft_picks") or []),
                json.dumps(t.get("waiver_budget") or []),
                (t.get("settings") or {}).get("waiver_bid"),
            ),
        )
    return len(txs)


def _store_matchups(conn, league_id, season, week):
    rows = sleeper.matchups(league_id, week)
    # Sleeper returns entries with 0 points for weeks that haven't happened.
    if not rows or all(not m.get("points") for m in rows):
        return False
    for m in rows:
        conn.execute(
            "INSERT OR REPLACE INTO matchups (league_id, season, week, roster_id, matchup_id, points) VALUES (?, ?, ?, ?, ?, ?)",
            (league_id, season, week, m["roster_id"], m.get("matchup_id"), m.get("points")),
        )
        starters = set(m.get("starters") or [])
        for player_id, points in (m.get("players_points") or {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO player_points (league_id, season, week, roster_id, player_id, points, started) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (league_id, season, week, m["roster_id"], player_id, points or 0, int(player_id in starters)),
            )
    return True


def _store_draft(conn, draft):
    # A draft slot (column on the draft board) belongs to one roster. If a different
    # roster made the pick, the pick was traded. The full draft object has the mapping.
    slot_to_roster = sleeper.draft(draft["draft_id"]).get("slot_to_roster_id") or {}
    for p in sleeper.draft_picks(draft["draft_id"]):
        original = slot_to_roster.get(str(p.get("draft_slot")))
        conn.execute(
            """INSERT OR REPLACE INTO draft_selections
               (draft_id, season, round, pick_no, roster_id, original_roster_id, picked_by, player_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft["draft_id"], draft["season"], p["round"], p["pick_no"], p.get("roster_id"),
             original, p.get("picked_by"), p.get("player_id")),
        )


def _store_traded_picks(conn, league_id):
    # Replace wholesale: a pick can be traded back to its original owner, which removes it from the list.
    conn.execute("DELETE FROM traded_picks")
    for p in sleeper.traded_picks(league_id):
        conn.execute(
            "INSERT OR REPLACE INTO traded_picks (season, round, original_roster_id, owner_roster_id) VALUES (?, ?, ?, ?)",
            (p["season"], p["round"], p["roster_id"], p["owner_id"]),
        )


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
