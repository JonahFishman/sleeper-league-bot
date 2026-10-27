"""Dynasty trade values from FantasyCalc (1QB, 12-team, PPR).

FantasyCalc only publishes *current* values, so refresh() also saves a dated
snapshot. Over time those snapshots let us say what a trade was worth on the day
it happened, not just today.
"""

import json
import urllib.request
from datetime import date

from . import config


def ordinal(n):
    """1 -> "1st", 2 -> "2nd", 11 -> "11th". Pick rounds are named this way, here and in FantasyCalc."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def refresh(conn):
    """Fetch today's values and store them as today's snapshot. Returns how many were stored."""
    req = urllib.request.Request(config.FANTASYCALC_URL, headers={"User-Agent": "league-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.load(resp)

    today = date.today().isoformat()
    conn.execute("DELETE FROM value_snapshots WHERE snapshot_date = ?", (today,))
    for row in rows:
        player = row["player"]
        # Picks have position "PICK" and no Sleeper ID; key them by name ("2027 1st (Early)").
        key = player["name"] if player.get("position") == "PICK" else player.get("sleeperId")
        if not key:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO value_snapshots (snapshot_date, asset_key, name, position, value) VALUES (?, ?, ?, ?, ?)",
            (today, str(key), player["name"], player.get("position"), row["value"]),
        )
    conn.commit()
    return len(rows)


class Values:
    """Values from one snapshot, with lookups for players and picks."""

    def __init__(self, conn, snapshot_date=None):
        if snapshot_date is None:
            row = conn.execute("SELECT max(snapshot_date) AS d FROM value_snapshots").fetchone()
            snapshot_date = row["d"]
        self.snapshot_date = snapshot_date
        self._values = {
            row["asset_key"]: row["value"]
            for row in conn.execute("SELECT asset_key, value FROM value_snapshots WHERE snapshot_date = ?", (snapshot_date,))
        }

    @staticmethod
    def nearest(conn, on_date):
        """The snapshot taken on or just before a date (or None if we have none that old)."""
        row = conn.execute(
            "SELECT max(snapshot_date) AS d FROM value_snapshots WHERE snapshot_date <= ?", (on_date,)
        ).fetchone()
        return Values(conn, row["d"]) if row["d"] else None

    def player(self, player_id):
        """Value of a player, or 0 if FantasyCalc doesn't list them (deep bench / no market)."""
        return self._values.get(str(player_id), 0)

    def pick(self, season, round_, tier=None):
        """Value of a pick. tier is "Early", "Mid", "Late" or None for an unknown slot.

        FantasyCalc only splits next year's picks into Early/Mid/Late; later years are a
        single generic value. Years it doesn't list at all fall back to its furthest year.
        """
        round_name = ordinal(round_)
        for key in (f"{season} {round_name} ({tier})", f"{season} {round_name}"):
            if key in self._values:
                return self._values[key]
        # Further out than FantasyCalc lists: use the furthest year it does list.
        # Keys look like "2029 1st", so max() picks the latest year.
        generic = [k for k in self._values if k[:4].isdigit() and k.endswith(f" {round_name}")]
        return self._values[max(generic)] if generic else 0
