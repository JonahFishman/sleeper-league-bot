"""Tools the model can call for questions the briefing doesn't cover.

Each tool is a JSON schema the model sees (TOOLS) plus a Python function that runs
it (run_tool). Results are plain text, written the same way as the briefing.
"""

import json
from collections import defaultdict

from .briefing import describe_transaction
from .league import League
from .values import Values, ordinal

# Anthropic runs this one on their side; we only declare it.
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}

TOOLS = [
    {
        "name": "search_transactions",
        "description": (
            "Search every transaction in league history (trades, waiver claims, free-agent adds/drops, "
            "commissioner moves). Filter by any combination of manager, player, season, and type. "
            "Use for questions about specific pickups or drops, or anything the trade list in the briefing doesn't cover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "manager": {"type": "string", "description": "Manager display name or team name (partial is fine)."},
                "player": {"type": "string", "description": "Player name (partial is fine)."},
                "season": {"type": "string", "description": "e.g. \"2024\"."},
                "type": {"type": "string", "enum": ["trade", "waiver", "free_agent", "commissioner"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "player_report",
        "description": (
            "Everything the league data says about one player: current owner and value, every move involving "
            "them in this league, fantasy points by season, and this season's week-by-week points."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"player": {"type": "string", "description": "Player name."}},
            "required": ["player"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate_trade",
        "description": (
            "Add up a hypothetical trade with current FantasyCalc values (1QB). Give what each side would RECEIVE. "
            "Returns per-asset values, totals, the gap, and warnings if an asset isn't owned by the other side. "
            "Use this instead of doing arithmetic yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sides": {
                    "type": "array",
                    "minItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "manager": {"type": "string", "description": "Manager who receives these assets."},
                            "players": {"type": "array", "items": {"type": "string"}},
                            "picks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "season": {"type": "string"},
                                        "round": {"type": "integer"},
                                        "original_manager": {"type": "string", "description": "Whose pick it originally was."},
                                    },
                                    "required": ["season", "round", "original_manager"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["manager"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["sides"],
            "additionalProperties": False,
        },
    },
    {
        "name": "head_to_head",
        "description": "All-time matchup record and every game between two managers (regular season and playoffs).",
        "input_schema": {
            "type": "object",
            "properties": {
                "manager_a": {"type": "string"},
                "manager_b": {"type": "string"},
            },
            "required": ["manager_a", "manager_b"],
            "additionalProperties": False,
        },
    },
]


def run_tool(conn, league: League, values: Values, name, args):
    """Run a tool and return (text, is_error)."""
    try:
        handler = {
            "search_transactions": search_transactions,
            "player_report": player_report,
            "evaluate_trade": evaluate_trade,
            "head_to_head": head_to_head,
        }[name]
        return handler(conn, league, values, **args), False
    except LookupError as e:  # a name we couldn't match: tell the model so it can rephrase
        return str(e), True


# ---- tool implementations ---------------------------------------------------------


def search_transactions(conn, league, values, manager=None, player=None, season=None, type=None, limit=40):
    sql, params = "SELECT * FROM transactions WHERE status = 'complete'", []
    if season:
        sql += " AND season = ?"
        params.append(season)
    if type:
        sql += " AND type = ?"
        params.append(type)
    rows = conn.execute(sql + " ORDER BY coalesce(status_updated_ms, created_ms)", params).fetchall()

    if manager:
        owned = _rosters_run_by(conn, league.find_manager(manager))
        rows = [r for r in rows if any((r["league_id"], rid) in owned for rid in json.loads(r["roster_ids"]))]
    if player:
        player_ids = set(_find_player_ids(league, player))
        rows = [r for r in rows if player_ids & (set(json.loads(r["adds"])) | set(json.loads(r["drops"])))]

    if not rows:
        return "No matching transactions."
    shown = rows[-limit:]
    header = f"{len(rows)} matching transactions" + (f", showing the latest {limit}" if len(rows) > limit else "") + ":"
    return "\n".join([header] + [describe_transaction(league, values, r) for r in shown])


def player_report(conn, league, values, player):
    pid = _find_player_ids(league, player)[0]
    info = league.players.get(pid) or {}
    owner = next((t for t in league.teams().values() if pid in t.players), None)
    lines = [
        f"{league.player_tag(pid)}. Current FantasyCalc value: {values.player(pid)}. "
        f"NFL status: {info.get('injury_status') or 'healthy/none listed'}; depth chart: {info.get('depth_chart_position') or '?'} {info.get('depth_chart_order') or ''}.",
        f"Owner in this league: {owner.label if owner else 'free agent'}.",
    ]

    drafted = conn.execute("SELECT * FROM draft_selections WHERE player_id = ?", (pid,)).fetchone()
    if drafted:
        lid = league.league_id_for_season(drafted["season"])
        lines.append(f"Drafted {drafted['season']} round {drafted['round']}, pick {drafted['pick_no']} overall, by {league.manager_of(lid, drafted['roster_id'])}.")

    moves = [r for r in conn.execute("SELECT * FROM transactions WHERE status = 'complete' ORDER BY coalesce(status_updated_ms, created_ms)")
             if pid in json.loads(r["adds"]) or pid in json.loads(r["drops"])]
    lines.append("Moves in this league: " + ("none" if not moves else ""))
    lines += [describe_transaction(league, values, r) for r in moves]

    by_season = conn.execute(
        """SELECT season, round(sum(points), 1) AS pts, count(*) AS games, round(sum(points * started), 1) AS started_pts
           FROM player_points WHERE player_id = ? GROUP BY season ORDER BY season""", (pid,)
    ).fetchall()
    if by_season:
        lines.append("Fantasy points by season (while on a league roster): " + "; ".join(
            f"{r['season']}: {r['pts']} in {r['games']} rostered weeks ({r['started_pts']} as a starter)" for r in by_season))
    weekly = conn.execute(
        "SELECT week, points, started FROM player_points WHERE player_id = ? AND season = ? ORDER BY week",
        (pid, league.current_season),
    ).fetchall()
    if weekly:
        lines.append(f"{league.current_season} by week: " + ", ".join(f"wk{r['week']} {r['points']}" + ("" if r["started"] else " (bench)") for r in weekly))
    return "\n".join(lines)


def evaluate_trade(conn, league, values, sides):
    teams = league.teams()
    owner_of_player = {pid: t.roster_id for t in teams.values() for pid in t.players}
    picks = {(p.season, p.round, p.original_roster_id): p for p in league.future_picks(values)}
    roster_of = {}  # manager query -> roster_id, for ownership checks
    for side in sides:
        roster_of[side["manager"]] = _current_roster(league, side["manager"])

    lines, totals, warnings = [], {}, []
    for side in sides:
        me = roster_of[side["manager"]]
        others = {rid for m, rid in roster_of.items() if rid != me}
        entries, total = [], 0
        for name in side.get("players", []):
            pid = _find_player_ids(league, name)[0]
            v = values.player(pid)
            total += v
            entries.append(f"{league.player_tag(pid)} {v}")
            if owner_of_player.get(pid) not in others:
                holder = teams[owner_of_player[pid]].manager if pid in owner_of_player else "nobody (free agent)"
                warnings.append(f"{league.player_name(pid)} is owned by {holder}, not by the other side of this trade.")
        for p in side.get("picks", []):
            original = _current_roster(league, p["original_manager"])
            pick = picks.get((str(p["season"]), p["round"], original))
            if pick is None:
                warnings.append(f"No unmade {p['season']} {ordinal(p['round'])} from {p['original_manager']} exists.")
                continue
            total += pick.value
            tier = f", proj. {pick.tier}" if pick.tier else ""
            entries.append(f"{pick.season} {ordinal(pick.round)} (orig. {teams[original].manager}{tier}) {pick.value}")
            if pick.owner_roster_id not in others:
                warnings.append(f"That {pick.season} {ordinal(pick.round)} is currently owned by {teams[pick.owner_roster_id].manager}, not by the other side.")
        totals[teams[me].manager] = total
        lines.append(f"{teams[me].manager} receives: " + ("; ".join(entries) or "nothing") + f". Total {total:,}.")

    best, worst = max(totals.values()), min(totals.values())
    gap = best - worst
    pct = gap / best * 100 if best else 0
    lines.append(f"Gap: {gap:,} ({pct:.0f}% of the larger side). Under ~10% is roughly even; consolidation trades (fewer, better assets) usually need the side getting the best player to pay a premium.")
    if warnings:
        lines.append("Warnings: " + " ".join(warnings))
    return "\n".join(lines)


def head_to_head(conn, league, values, manager_a, manager_b):
    a, b = league.find_manager(manager_a), league.find_manager(manager_b)
    rosters_a, rosters_b = _rosters_run_by(conn, a), _rosters_run_by(conn, b)

    games = []
    rows = conn.execute("SELECT * FROM matchups WHERE matchup_id IS NOT NULL ORDER BY season, week").fetchall()
    by_game = defaultdict(list)
    for r in rows:
        by_game[(r["league_id"], r["season"], r["week"], r["matchup_id"])].append(r)
    for (lid, season, week, _), pair in by_game.items():
        points_a = [r["points"] for r in pair if (lid, r["roster_id"]) in rosters_a]
        points_b = [r["points"] for r in pair if (lid, r["roster_id"]) in rosters_b]
        if len(pair) == 2 and points_a and points_b:
            games.append((season, week, points_a[0], points_b[0]))
    if not games:
        return "These two have never played each other."

    name_a, name_b = league.user_name(a), league.user_name(b)
    wins_a = sum(pa > pb for _, _, pa, pb in games)
    wins_b = sum(pb > pa for _, _, pa, pb in games)
    ties = len(games) - wins_a - wins_b
    lines = [f"{name_a} vs {name_b}: {wins_a}-{wins_b}" + (f"-{ties}" if ties else "") + f" all time over {len(games)} games."]
    lines += [f"{season} wk{week}: {name_a} {pa:.1f} - {pb:.1f} {name_b}" for season, week, pa, pb in games]
    return "\n".join(lines)


# ---- name matching ----------------------------------------------------------------


def _rosters_run_by(conn, user_id):
    """Every (league_id, roster_id) this person ran, as owner or co-owner."""
    return {
        (r["league_id"], r["roster_id"])
        for r in conn.execute("SELECT league_id, roster_id, owner_id, co_owner_ids FROM rosters")
        if r["owner_id"] == user_id or user_id in json.loads(r["co_owner_ids"] or "[]")
    }


def _current_roster(league, manager_query):
    owner_id = league.find_manager(manager_query)
    for team in league.teams().values():
        if team.owner_id == owner_id or league.user_name(owner_id) in team.manager:
            return team.roster_id
    raise LookupError(f"{league.user_name(owner_id)} doesn't have a team this season.")


def _find_player_ids(league, query):
    """Matching player IDs, rostered players first (they're almost always who people mean)."""
    hits = league.find_players(query, limit=20)
    if not hits:
        raise LookupError(f"No player matches '{query}'.")
    rostered = {pid for t in league.teams().values() for pid in t.players}
    return sorted(hits, key=lambda pid: pid not in rostered)
