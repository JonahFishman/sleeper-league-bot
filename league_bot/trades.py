"""Trade analysis: who got what, and how it has worked out.

FantasyCalc has no history, so for old trades "how it worked out" is measured two ways:
  - value now: what the assets each side received are worth today (picks that have
    been used count as the player drafted with them)
  - production: fantasy points those players scored *in the receiving team's starting
    lineup* after the trade
For trades made after we started saving daily value snapshots, the value on the day
of the trade is included too.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .league import League
from .values import Values, ordinal


@dataclass
class Asset:
    description: str          # "Puka Nacua (WR, LAR, 25)" or "2027 1st (orig. mgr_alex)"
    value_now: int
    starter_points: float     # points scored in the receiver's starting lineup since the trade
    value_then: int | None = None
    note: str = ""            # e.g. "became Jeremiyah Love (pick 1.01)"; "no longer on roster"
    from_roster_id: int | None = None  # who sent it (matters in 3-team trades)
    short_name: str = ""      # "Puka Nacua" or "2027 1st (orig. mgr_alex)", for one-line summaries


@dataclass
class Side:
    roster_id: int
    manager: str              # who ran this roster when the trade happened
    owner_id: str | None
    assets: list = field(default_factory=list)
    faab: int = 0             # waiver budget dollars received (negative if sent)

    @property
    def value_now(self):
        return sum(a.value_now for a in self.assets)

    @property
    def value_then(self):
        if any(a.value_then is None for a in self.assets):
            return None
        return sum(a.value_then for a in self.assets)

    @property
    def starter_points(self):
        return sum(a.starter_points for a in self.assets)


@dataclass
class Trade:
    trade_id: str             # "T12": short, stable, ordered by date (T1 is the oldest)
    transaction_id: str
    league_id: str
    season: str
    week: int
    date: str                 # YYYY-MM-DD
    sides: list


def load_trades(conn, league: League, values: Values):
    rows = conn.execute(
        "SELECT * FROM transactions WHERE type = 'trade' AND status = 'complete' "
        "ORDER BY coalesce(status_updated_ms, created_ms)"
    ).fetchall()

    current_rosters = {t.roster_id: set(t.players) for t in league.teams().values()}
    trades = []
    for n, row in enumerate(rows, start=1):
        when = row["status_updated_ms"] or row["created_ms"]
        trade_date = datetime.fromtimestamp(when / 1000, tz=timezone.utc).date().isoformat()
        values_then = Values.nearest(conn, trade_date)
        adds = json.loads(row["adds"])
        drops = json.loads(row["drops"])  # {player_id: roster that sent them}
        picks = json.loads(row["draft_picks"])

        sides = []
        for roster_id in json.loads(row["roster_ids"]):
            team = league.teams(row["league_id"]).get(roster_id)
            side = Side(roster_id, league.manager_of(row["league_id"], roster_id), team.owner_id if team else None)

            for player_id, to_roster in adds.items():
                if to_roster != roster_id:
                    continue
                points = _starter_points(conn, roster_id, player_id, row["season"], row["week"])
                note = "" if player_id in current_rosters.get(roster_id, ()) else "no longer on this roster"
                side.assets.append(Asset(
                    league.player_tag(player_id), values.player(player_id), points,
                    values_then.player(player_id) if values_then else None, note,
                    from_roster_id=drops.get(player_id), short_name=league.player_name(player_id),
                ))

            for pick in picks:
                if pick["owner_id"] == roster_id:
                    asset = _pick_asset(conn, league, values, values_then, pick)
                    asset.from_roster_id = pick["previous_owner_id"]
                    asset.short_name = asset.description
                    side.assets.append(asset)

            sides.append(side)

        # FAAB can be traded too.
        for budget in json.loads(row["waiver_budget"] or "[]"):
            for side in sides:
                if side.roster_id == budget["receiver"]:
                    side.faab += budget["amount"]
                if side.roster_id == budget["sender"]:
                    side.faab -= budget["amount"]

        trades.append(Trade(f"T{n}", row["transaction_id"], row["league_id"], row["season"], row["week"], trade_date, sides))
    return trades


def format_trade(trade: Trade):
    """Multi-line, human-readable description used in the briefing and by tools."""
    names = " / ".join(s.manager for s in trade.sides)
    lines = [f"[{trade.trade_id}] {trade.date} ({trade.season} week {trade.week}): {names}"]
    for side in trade.sides:
        got = []
        for a in side.assets:
            detail = f"now {a.value_now}"
            if a.value_then is not None:
                detail = f"then {a.value_then}, " + detail
            if a.starter_points:
                detail += f", {a.starter_points:.0f} starter pts for them"
            if a.note:
                detail += f"; {a.note}"
            got.append(f"{a.description} [{detail}]")
        if side.faab:
            got.append(f"${side.faab} FAAB")
        # Spell out what each side gave up too. With only "got" lines, the model sometimes
        # flips a trade's direction when it summarizes.
        gave = [a.short_name for other in trade.sides if other is not side for a in other.assets if a.from_roster_id == side.roster_id]
        lines.append(f"  {side.manager} got: " + ("; ".join(got) or "nothing")
                     + f" | {side.manager} gave up: " + (", ".join(gave) or "nothing"))

    totals = ", ".join(f"{s.manager} {s.value_now}" for s in trade.sides)
    summary = f"  Value now: {totals}."
    if all(s.value_then is not None for s in trade.sides):
        summary += " Value at trade time: " + ", ".join(f"{s.manager} {s.value_then}" for s in trade.sides) + "."
    else:
        summary += " Value at trade time: not available (made before value tracking began)."
    pts = ", ".join(f"{s.manager} {s.starter_points:.0f}" for s in trade.sides)
    lines.append(summary + f" Starter points since: {pts}.")
    return "\n".join(lines)


def format_trade_for(trade: Trade, roster_id):
    """One line describing a trade from one manager's side: what they sent and what they
    got. Spelling out the direction stops the model mixing up who gave what."""
    me = next(s for s in trade.sides if s.roster_id == roster_id)
    others = [s for s in trade.sides if s is not me]
    sent = [a.short_name for s in others for a in s.assets if a.from_roster_id == roster_id]
    got = [a.short_name for a in me.assets]
    return (f"{trade.trade_id} {trade.date}, with {' and '.join(s.manager for s in others)}: "
            f"sent {', '.join(sent) or 'nothing'}; got {', '.join(got) or 'nothing'}")


# ---- helpers ---------------------------------------------------------------------


def _starter_points(conn, roster_id, player_id, season, week):
    """Points a player scored in this roster's starting lineup from the trade week onward."""
    row = conn.execute(
        """SELECT coalesce(sum(points), 0) AS pts FROM player_points
           WHERE roster_id = ? AND player_id = ? AND started = 1
             AND (season > ? OR (season = ? AND week >= ?))""",
        (roster_id, str(player_id), season, season, week),
    ).fetchone()
    return row["pts"]


def _pick_asset(conn, league, values, values_then, pick):
    season, round_, original = pick["season"], pick["round"], pick["roster_id"]
    pick_league = league.league_id_for_season(season)
    label = f"{season} {ordinal(round_)} (orig. {league.manager_of(pick_league, original)})"
    value_then = values_then.pick(season, round_) if values_then else None

    used = conn.execute(
        "SELECT * FROM draft_selections WHERE season = ? AND round = ? AND original_roster_id = ?",
        (season, round_, original),
    ).fetchone()
    if used and used["player_id"]:
        # The pick became a player: judge it by that player.
        player_id = used["player_id"]
        slot = f"{round_}.{(used['pick_no'] - 1) % 12 + 1:02d}"
        note = f"became {league.player_tag(player_id)} at pick {slot}"
        if used["roster_id"] == pick["owner_id"]:
            points = _starter_points(conn, pick["owner_id"], player_id, season, 0)
        else:
            # They flipped the pick before the draft, so the player never played for them.
            note += f", but it was traded on again and used by {league.manager_of(pick_league, used['roster_id'])}"
            points = 0
        return Asset(label, values.player(player_id), points, value_then, note)

    # Not drafted yet: value the pick itself, using its projected slot if it's next year's.
    tier = league.projected_pick_tiers(league.teams()).get(original) if season == league.next_draft_season else None
    note = f"projected {tier}" if tier else ""
    return Asset(label, values.pick(season, round_, tier), 0, value_then, note)
