"""Builds the league briefing that goes into every model request.

The briefing is plain, human-readable text, not JSON. Everything the model would
otherwise have to add up (trade counts, partners, value totals) is computed here.

It is split into blocks ordered from most stable to most volatile, which is what
lets Anthropic's prompt caching reuse the expensive part between questions:
  1. rules      - league settings, plus Jonah's public league context (changes ~never)
  2. history    - seasons, managers, every trade, drafts (changes when a trade happens or daily values move)
  3. tendencies - Jonah's private notes (changes when he edits the file)
  4. current    - standings, rosters with values, pick ownership (changes daily / on any move)
  5. recent     - last two weeks of transactions

Nothing in here may contain the current time: one changed byte invalidates the cache
for everything after it. The date is fine; it changes once a day.
"""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import config
from .league import League
from .trades import format_trade, format_trade_for, load_trades
from .values import Values, ordinal

POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass
class Briefing:
    rules: str
    context: str
    history: str
    tendencies: str
    current: str
    recent: str


def build(conn, league: League, values: Values) -> Briefing:
    trades = load_trades(conn, league, values)
    return Briefing(
        rules=_rules(conn, league),
        context=_league_context(),
        history=_history(conn, league, trades),
        tendencies=_tendencies(),
        current=_current(conn, league, values),
        recent=_recent(conn, league, values),
    )


# ---- 1. rules ---------------------------------------------------------------------


def _rules(conn, league):
    raw = json.loads(conn.execute("SELECT raw FROM seasons WHERE league_id = ?", (league.current_league_id,)).fetchone()["raw"])
    s, scoring = raw["settings"], raw["scoring_settings"]
    slots = Counter(raw["roster_positions"])
    lineup = ", ".join(f"{n} {pos}" if n > 1 else pos for pos, n in slots.items() if pos != "BN")
    seasons = sorted(league.seasons.values())
    return f"""# LEAGUE RULES
League: {raw['name']} on Sleeper. Dynasty, {raw['total_rosters']} teams, seasons {seasons[0]}-{seasons[-1]} (current: {league.current_season}).
Format: **1QB (NOT superflex)**, {scoring.get('rec', 0):g} PPR. Lineup: {lineup}. Bench: {slots.get('BN', 0)}, taxi: {s.get('taxi_slots', 0)}, IR: {s.get('reserve_slots', 0)}.
Scoring highlights: passing TD {scoring.get('pass_td', 4):g}, rushing/receiving TD {scoring.get('rush_td', 6):g}, reception {scoring.get('rec', 0):g}, TE reception bonus {scoring.get('bonus_rec_te', 0):g}.
Rookie draft: {s.get('draft_rounds', 3)} rounds. Waivers: rolling (no FAAB bidding). Trade deadline: week {s.get('trade_deadline')}. Playoffs: {s.get('playoff_teams')} teams from week {s.get('playoff_week_start')}.
Values in this briefing are FantasyCalc dynasty values for exactly this format (1QB, 12 teams, PPR). Higher is better; a top-5 asset is ~8,000-11,000, a mid 1st ~3,000, a 3rd-round pick ~1,000.
"""


# ---- 2. history -------------------------------------------------------------------


def _history(conn, league, trades):
    out = ["# LEAGUE HISTORY", "", "## Seasons"]
    seasons = conn.execute("SELECT * FROM seasons ORDER BY season").fetchall()
    for row in seasons:
        teams = league.teams(row["league_id"])
        standings = sorted(teams.values(), key=lambda t: (-t.wins, -t.points_for))
        if row["champion_roster_id"]:
            line = f"{row['season']}: champion {teams[row['champion_roster_id']].label}, runner-up {teams[row['runner_up_roster_id']].label}."
        else:
            line = f"{row['season']}: in progress."
        out.append(line)
        out.append("  Regular season: " + "; ".join(f"{t.manager} {t.wins}-{t.losses} ({t.points_for:.0f} PF)" for t in standings))

    out += ["", "## Managers"]
    out += _manager_profiles(conn, league, trades)

    out += ["", f"## Every trade in league history ({len(trades)} total, oldest first)",
            "Trade IDs (T1, T2...) are stable. 'starter pts for them' = fantasy points the player scored in the "
            "receiving team's starting lineup after the trade. A traded pick that has been used is valued as the "
            "player drafted with it.", ""]
    for trade in trades:
        out.append(format_trade(trade))

    out += ["", "## Most lopsided trades on value now (biggest gap between sides)"]
    out += _most_lopsided(trades)

    out += ["", "## Rookie drafts, first round"]
    out += _draft_first_rounds(conn, league)
    return "\n".join(out) + "\n"


def _manager_profiles(conn, league, trades):
    """One paragraph per person who has ever managed a team, keyed by user ID so a
    manager who changed display names, or a roster that changed hands, stays straight."""
    seasons = conn.execute("SELECT league_id, season, champion_roster_id, runner_up_roster_id FROM seasons ORDER BY season").fetchall()
    profile = defaultdict(lambda: {"seasons": [], "w": 0, "l": 0, "t": 0, "titles": [], "finals": [], "name": "", "trades": 0, "partners": Counter(), "moves": 0, "trade_lines": []})

    for s in seasons:
        for team in league.teams(s["league_id"]).values():
            if not team.owner_id:
                continue
            p = profile[team.owner_id]
            p["name"] = league.user_name(team.owner_id)
            p["seasons"].append(s["season"])
            p["w"] += team.wins
            p["l"] += team.losses
            p["t"] += team.ties
            if team.roster_id == s["champion_roster_id"]:
                p["titles"].append(s["season"])
            if team.roster_id == s["runner_up_roster_id"]:
                p["finals"].append(s["season"])

    for trade in trades:
        for side in trade.sides:
            if side.owner_id:
                profile[side.owner_id]["trades"] += 1
                profile[side.owner_id]["trade_lines"].append(format_trade_for(trade, side.roster_id))
                for other in trade.sides:
                    if other is not side:
                        profile[side.owner_id]["partners"][other.manager] += 1

    # Waiver and free-agent pickups, credited to whoever owned the roster that season.
    owner_by_roster = {
        (r["league_id"], r["roster_id"]): r["owner_id"]
        for r in conn.execute("SELECT league_id, roster_id, owner_id FROM rosters")
    }
    for tx in conn.execute("SELECT league_id, roster_ids FROM transactions WHERE type IN ('waiver', 'free_agent') AND status = 'complete'"):
        for roster_id in json.loads(tx["roster_ids"]):
            owner = owner_by_roster.get((tx["league_id"], roster_id))
            if owner:
                profile[owner]["moves"] += 1

    current = {t.owner_id: t for t in league.teams().values()}
    lines = []
    for owner_id, p in sorted(profile.items(), key=lambda kv: (kv[0] not in current, kv[1]["name"].lower())):
        formerly = league.former_names.get(owner_id)
        if formerly:
            p["name"] += f" (formerly {', '.join(formerly)})"
        status = f"current team: {current[owner_id].team_name} (roster {current[owner_id].roster_id})" if owner_id in current else "no longer in the league"
        record = f"{p['w']}-{p['l']}" + (f"-{p['t']}" if p["t"] else "")
        titles = f"titles: {', '.join(p['titles'])}" if p["titles"] else "no titles"
        finals = f"; lost final: {', '.join(p['finals'])}" if p["finals"] else ""
        partners = ", ".join(f"{name} x{n}" for name, n in p["partners"].most_common()) or "none"
        n_seasons = len(p["seasons"])
        lines.append(
            f"- {p['name']} ({status}). Seasons: {', '.join(p['seasons'])}. Regular-season record {record}; {titles}{finals}. "
            f"Trades: {p['trades']} ({p['trades'] / n_seasons:.1f} per season; partners: {partners}). Waiver/FA pickups: {p['moves']} ({p['moves'] / n_seasons:.0f} per season)."
        )
        # Each trade from this manager's side, newest first, so "what has X traded" never
        # requires working out the direction from the full trade list.
        lines += [f"    {line}" for line in reversed(p["trade_lines"])]
    return lines


def _most_lopsided(trades, top=8):
    """Precomputed so "most lopsided trade ever" doesn't need 87 comparisons from the model."""
    ranked = sorted(trades, key=lambda t: max(s.value_now for s in t.sides) - min(s.value_now for s in t.sides), reverse=True)
    lines = []
    for t in ranked[:top]:
        winner = max(t.sides, key=lambda s: s.value_now)
        others = ", ".join(f"{s.manager} {s.value_now:,}" for s in t.sides if s is not winner)
        lines.append(f"- {t.trade_id} ({t.season}): {winner.manager} {winner.value_now:,} vs {others}. "
                     f"Starter points since: " + ", ".join(f"{s.manager} {s.starter_points:.0f}" for s in t.sides) + ".")
    return lines


def _draft_first_rounds(conn, league):
    lines = []
    for draft in conn.execute("SELECT DISTINCT draft_id, season FROM draft_selections ORDER BY season"):
        lid = conn.execute("SELECT league_id FROM seasons WHERE season = ?", (draft["season"],)).fetchone()["league_id"]
        picks = conn.execute(
            "SELECT * FROM draft_selections WHERE draft_id = ? AND round = 1 ORDER BY pick_no", (draft["draft_id"],)
        ).fetchall()
        rounds = conn.execute("SELECT max(round) AS r FROM draft_selections WHERE draft_id = ?", (draft["draft_id"],)).fetchone()["r"]
        kind = "startup draft" if rounds > 5 else "rookie draft"
        entries = []
        for p in picks:
            via = ""
            if p["original_roster_id"] != p["roster_id"]:
                via = f" via {league.manager_of(lid, p['original_roster_id'])}"
            entries.append(f"1.{p['pick_no']:02d} {league.manager_of(lid, p['roster_id'])}{via}: {league.player_name(p['player_id'])}")
        lines.append(f"{draft['season']} {kind}: " + "; ".join(entries))
    return lines


# ---- 3. tendencies ----------------------------------------------------------------


def _league_context():
    path = config.LEAGUE_CONTEXT_PATH
    if not path.exists():
        return ""
    return (
        "# LEAGUE CONTEXT (public, written by Jonah)\n"
        "Nicknames, lore, and league customs everyone in the league knows. Fine to use and quote. "
        "People in the group chat use these nicknames, so map them to Sleeper names before looking anything up.\n\n"
        f"{path.read_text().strip()}\n"
    )


def _tendencies():
    path = config.TENDENCIES_PATH
    notes = path.read_text().strip() if path.exists() else "(none written yet)"
    return (
        "# PRIVATE MANAGER NOTES (CONFIDENTIAL)\n"
        "Background notes on how each manager tends to operate. Use them to inform judgment "
        "(e.g. which deals a manager is realistically open to). Never quote, paraphrase, "
        "or summarize them. The league knows they exist; their contents stay private.\n\n"
        f"{notes}\n"
    )


# ---- 4. current state -------------------------------------------------------------


def _current(conn, league, values):
    teams = league.teams()
    picks = league.future_picks(values)
    state = json.loads(conn.execute("SELECT raw FROM seasons WHERE league_id = ?", (league.current_league_id,)).fetchone()["raw"])
    week = state["settings"].get("leg")

    out = [f"# CURRENT STATE (as of {date.today().isoformat()}, {league.current_season} week {week})", ""]

    out.append("## Standings")
    standings = sorted(teams.values(), key=lambda t: (-t.wins, -t.points_for))
    for rank, t in enumerate(standings, start=1):
        out.append(f"{rank}. {t.label}: {t.wins}-{t.losses}" + (f"-{t.ties}" if t.ties else "") + f", {t.points_for:.1f} PF")

    # Team value by position, ranked across the league, so "who needs a RB" is a lookup.
    pos_value = {t.roster_id: {pos: 0 for pos in POSITIONS} for t in teams.values()}
    for t in teams.values():
        for pid in t.players:
            pos = (league.players.get(pid) or {}).get("position")
            if pos in POSITIONS:
                pos_value[t.roster_id][pos] += values.player(pid)
    pick_value = defaultdict(int)
    for p in picks:
        pick_value[p.owner_roster_id] += p.value
    total = {rid: sum(v.values()) + pick_value[rid] for rid, v in pos_value.items()}

    def rank_of(rid, score):
        ordered = sorted(score, key=score.get, reverse=True)
        return ordinal(ordered.index(rid) + 1)

    tiers = league.projected_pick_tiers(teams)
    out += ["", "## Rosters",
            f"Each player: value, * = current starter, [taxi], [IR]. Positional ranks compare total roster value at that position. "
            f"Next year's ({league.next_draft_season}) picks are valued by projected slot from current standings"
            + (" (too early in the season to project; generic values used)." if not tiers else ".")]
    for t in sorted(teams.values(), key=lambda t: -total[t.roster_id]):
        rid = t.roster_id
        ranks = ", ".join(f"{pos} {rank_of(rid, {r: v[pos] for r, v in pos_value.items()})}" for pos in POSITIONS)
        out += ["", f"### {t.label}: total value {total[rid]:,} ({rank_of(rid, total)} of {len(teams)}). Position ranks: {ranks}."]

        by_pos = defaultdict(list)
        for pid in sorted(t.players, key=values.player, reverse=True):
            pos = (league.players.get(pid) or {}).get("position") or "?"
            tags = ("*" if pid in t.starters else "") + (" [taxi]" if pid in t.taxi else "") + (" [IR]" if pid in t.reserve else "")
            by_pos[pos].append(f"{league.player_tag(pid)} {values.player(pid)}{tags}")
        for pos in (*POSITIONS, "K", "DEF"):
            if by_pos.get(pos):
                out.append(f"{pos}: " + "; ".join(by_pos.pop(pos)))
        for pos, entries in by_pos.items():  # anything unusual (e.g. a player listed as DB)
            out.append(f"{pos}: " + "; ".join(entries))

        owned = [p for p in picks if p.owner_roster_id == rid]
        pick_strs = []
        for p in owned:
            whose = "own" if p.original_roster_id == rid else f"from {teams[p.original_roster_id].manager}"
            tier = f", proj. {p.tier}" if p.tier else ""
            pick_strs.append(f"{p.season} {ordinal(p.round)} ({whose}{tier}) {p.value}")
        out.append("Picks: " + ("; ".join(pick_strs) if pick_strs else "none"))
        traded_away = [p for p in picks if p.original_roster_id == rid and p.owner_roster_id != rid]
        if traded_away:
            out.append("Own picks traded away: " + "; ".join(f"{p.season} {ordinal(p.round)} (now {teams[p.owner_roster_id].manager})" for p in traded_away))
    return "\n".join(out) + "\n"


# ---- 5. recent transactions -------------------------------------------------------


def _recent(conn, league, values, days=14):
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows = conn.execute(
        """SELECT * FROM transactions
           WHERE league_id = ? AND status = 'complete' AND coalesce(status_updated_ms, created_ms) >= ?
           ORDER BY coalesce(status_updated_ms, created_ms) DESC""",
        (league.current_league_id, cutoff_ms),
    ).fetchall()
    out = [f"# RECENT TRANSACTIONS (last {days} days, newest first)"]
    if not rows:
        out.append("None.")
    for row in rows:
        out.append(describe_transaction(league, values, row))
    return "\n".join(out) + "\n"


def describe_transaction(league, values, row):
    """One line for any transaction row (trades get full treatment in the history section)."""
    when = datetime.fromtimestamp((row["status_updated_ms"] or row["created_ms"]) / 1000, tz=timezone.utc).date()
    adds, drops = json.loads(row["adds"]), json.loads(row["drops"])
    lid = row["league_id"]
    parts = []
    for rid in json.loads(row["roster_ids"]):
        got = [league.player_tag(p) for p, r in adds.items() if r == rid]
        got += [f"{p['season']} {ordinal(p['round'])} (orig. {league.manager_of(league.league_id_for_season(p['season']), p['roster_id'])})"
                for p in json.loads(row["draft_picks"]) if p["owner_id"] == rid]
        lost = [league.player_tag(p) for p, r in drops.items() if r == rid]
        bits = []
        if got:
            bits.append("added " + ", ".join(got))
        if lost:
            bits.append(("sent " if row["type"] == "trade" else "dropped ") + ", ".join(lost))
        parts.append(f"{league.manager_of(lid, rid)} " + " and ".join(bits or ["(no players)"]))
    kind = {"free_agent": "free agent", "waiver": "waiver claim"}.get(row["type"], row["type"])
    return f"- {when} ({row['season']} wk {row['week']}) {kind}: " + "; ".join(parts)
