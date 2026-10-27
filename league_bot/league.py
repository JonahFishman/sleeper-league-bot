"""Read-side view of the league: turns the IDs stored in SQLite into names.

Everything that needs to say "who" or "which player" goes through League, so the
naming rules live in one place.
"""

import json
import re
from dataclasses import dataclass, field

from . import config, db, sleeper
from .values import ordinal

TIER_BY_THIRD = ("Early", "Mid", "Late")  # worst third of standings picks Early


@dataclass
class Pick:
    season: str
    round: int
    original_roster_id: int
    owner_roster_id: int
    tier: str | None = None   # projected Early/Mid/Late, only for next year's draft
    value: int = 0


@dataclass
class Team:
    roster_id: int
    manager: str              # "mgr_alex" or "mgr_hal & co-owner"
    team_name: str
    owner_id: str | None
    wins: int
    losses: int
    ties: int
    points_for: float
    players: list = field(default_factory=list)
    starters: list = field(default_factory=list)
    taxi: list = field(default_factory=list)
    reserve: list = field(default_factory=list)

    @property
    def label(self):
        return f"{self.team_name} ({self.manager})"


class League:
    def __init__(self, conn, players=None):
        self.conn = conn
        self.players = players if players is not None else sleeper.players()
        self.current_league_id = db.get_meta(conn, "current_league_id")
        self.seasons = {
            row["league_id"]: row["season"]
            for row in conn.execute("SELECT league_id, season FROM seasons ORDER BY season")
        }
        self._users = {}  # (league_id, user_id) -> row
        # People rename their Sleeper accounts. Always call someone by their latest name,
        # and remember older ones so the bot can say "formerly dan_old_name".
        self._latest_name = {}
        self.former_names = {}  # user_id -> [old names]
        for row in conn.execute("SELECT u.* FROM users u JOIN seasons s USING (league_id) ORDER BY s.season"):
            self._users[(row["league_id"], row["user_id"])] = row
            previous = self._latest_name.get(row["user_id"])
            if previous and previous != row["display_name"]:
                self.former_names.setdefault(row["user_id"], []).append(previous)
            self._latest_name[row["user_id"]] = row["display_name"]
        self._teams_cache = {}  # league_id -> {roster_id: Team}; call refresh() after a sync

    def refresh(self):
        """Forget cached rosters, e.g. after history.sync_recent() pulled a new trade."""
        self._teams_cache.clear()

    @property
    def current_season(self):
        return self.seasons[self.current_league_id]

    @property
    def next_draft_season(self):
        """The next rookie draft that hasn't happened: this season's if it's still to come, else next year's."""
        drafted = self.conn.execute(
            "SELECT 1 FROM draft_selections WHERE season = ? LIMIT 1", (self.current_season,)
        ).fetchone()
        return str(int(self.current_season) + 1) if drafted else self.current_season

    # ---- people -----------------------------------------------------------------

    def user_name(self, user_id):
        return self._latest_name.get(user_id, "unknown manager")

    def teams(self, league_id=None):
        """Every roster in a season (default: current), keyed by roster_id."""
        league_id = league_id or self.current_league_id
        if league_id in self._teams_cache:
            return self._teams_cache[league_id]
        teams = {}
        for r in self.conn.execute("SELECT * FROM rosters WHERE league_id = ? ORDER BY roster_id", (league_id,)):
            owner = self._users.get((league_id, r["owner_id"]))
            names = [self.user_name(r["owner_id"])] if r["owner_id"] else ["(no owner)"]
            names += [self.user_name(uid) for uid in json.loads(r["co_owner_ids"] or "[]")]
            display = owner["display_name"] if owner else f"Roster {r['roster_id']}"
            teams[r["roster_id"]] = Team(
                roster_id=r["roster_id"],
                manager=" & ".join(names),
                # Sleeper's own fallback when a manager hasn't named their team.
                team_name=(owner["team_name"] if owner and owner["team_name"] else f"Team {display}"),
                owner_id=r["owner_id"],
                wins=r["wins"], losses=r["losses"], ties=r["ties"],
                points_for=r["points_for"],
                players=json.loads(r["players"] or "[]"),
                starters=json.loads(r["starters"] or "[]"),
                taxi=json.loads(r["taxi"] or "[]"),
                reserve=json.loads(r["reserve"] or "[]"),
            )
        self._teams_cache[league_id] = teams
        return teams

    def league_id_for_season(self, season):
        """League ID for a season. Future seasons don't exist yet, so things in them
        (like a 2028 pick) are described using the current season's managers."""
        for league_id, s in self.seasons.items():
            if s == str(season):
                return league_id
        return self.current_league_id

    def find_manager(self, query):
        """User ID for a manager, matched on current or former display name or any team
        name they've used. Raises LookupError with the options if it's not exactly one."""
        q = query.lower().strip().lstrip("@")
        names = {}  # user_id -> every name they've gone by, lowercased
        for (_, user_id), row in self._users.items():
            for name in (row["display_name"], row["team_name"], self.user_name(user_id)):
                if name:
                    names.setdefault(user_id, set()).add(name.lower())
        # Real names from league_context.txt ("Hal Doe -> mgr_hal"), so "Hal" works too.
        by_sleeper_name = {self.user_name(uid).lower(): uid for uid in names}
        for real_name, sleeper_name in _real_names():
            uid = by_sleeper_name.get(sleeper_name.lower())
            if uid:
                names[uid].add(real_name.lower())
                names[uid].update(part.lower() for part in real_name.split())
        matches = [uid for uid, n in names.items() if q in n] or [uid for uid, n in names.items() if any(q in x for x in n)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"No manager matches '{query}'. Managers: " + ", ".join(sorted({self.user_name(u) for u in names})))
        raise LookupError(f"'{query}' matches several managers: " + ", ".join(self.user_name(u) for u in matches))

    def team_choices(self):
        """{roster_id: "Hal Doe & Ian Roe (Team Name Here)"} for this season,
        using real names from league_context.txt where known, for the page's team picker."""
        real = {sleeper.lower(): name for name, sleeper in _real_names()}
        choices = {}
        for team in self.teams().values():
            sleeper_names = team.manager.split(" & ")
            known = [real[n.lower()] for n in sleeper_names if n.lower() in real]
            # Prefer real names; drop unmapped co-owners (e.g. one who has left) once we have one.
            names = known or sleeper_names
            choices[team.roster_id] = f"{' & '.join(names)} ({team.team_name})"
        return choices

    def manager_of(self, league_id, roster_id):
        """Who ran this roster in that season. Transactions only name roster IDs, and
        a roster can change hands between seasons, so always resolve per season."""
        team = self.teams(league_id).get(roster_id)
        return team.manager if team else f"roster {roster_id}"

    # ---- players ------------------------------------------------------------------

    def player_name(self, player_id):
        p = self.players.get(str(player_id))
        if not p:
            return f"unknown player {player_id}"
        if p.get("position") == "DEF":
            return f"{player_id} DEF"
        return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

    def player_tag(self, player_id):
        """"Puka Nacua (WR, LAR, 25)" - name plus the context a reader wants."""
        p = self.players.get(str(player_id)) or {}
        bits = [p.get("position") or "?", p.get("team") or "FA"]
        if p.get("age"):
            bits.append(str(p["age"]))
        return f"{self.player_name(player_id)} ({', '.join(bits)})"

    def find_players(self, query, limit=5):
        """Fuzzy-ish name search: every word in the query must appear in the name."""
        words = query.lower().split()
        hits = []
        for pid, p in self.players.items():
            name = (p.get("full_name") or "").lower()
            if name and all(w in name for w in words):
                # Prefer active, fantasy-relevant players when names collide.
                relevant = p.get("active") and p.get("position") in ("QB", "RB", "WR", "TE", "K")
                hits.append((not relevant, name, pid))
        return [pid for _, _, pid in sorted(hits)[:limit]]

    # ---- picks --------------------------------------------------------------------

    def future_picks(self, values=None, years=3):
        """Every unmade pick for the next few drafts, with current owner.

        Sleeper's /traded_picks only lists picks that changed hands, so start from
        "everyone owns their own picks" and apply the trades on top.
        """
        seasons = [str(int(self.next_draft_season) + i) for i in range(years)]
        rounds = json.loads(self.conn.execute(
            "SELECT raw FROM seasons WHERE league_id = ?", (self.current_league_id,)
        ).fetchone()["raw"])["settings"].get("draft_rounds", 3)

        teams = self.teams()
        traded = {
            (row["season"], row["round"], row["original_roster_id"]): row["owner_roster_id"]
            for row in self.conn.execute("SELECT * FROM traded_picks")
        }
        tiers = self.projected_pick_tiers(teams)

        picks = []
        for season in seasons:
            for round_ in range(1, rounds + 1):
                for roster_id in teams:
                    tier = tiers.get(roster_id) if season == self.next_draft_season else None
                    pick = Pick(season, round_, roster_id, traded.get((season, round_, roster_id), roster_id), tier)
                    if values:
                        pick.value = values.pick(season, round_, tier)
                    picks.append(pick)
        return picks

    def projected_pick_tiers(self, teams):
        """Early/Mid/Late for next year's picks from current standings (worst record picks
        earliest). Returns {} before week 4, when standings say too little."""
        games = max((t.wins + t.losses + t.ties for t in teams.values()), default=0)
        if games < 4:
            return {}
        # Worst first: fewest wins, then fewest points as the tiebreaker.
        order = sorted(teams.values(), key=lambda t: (t.wins + t.ties / 2, t.points_for))
        size = len(order) / 3
        return {t.roster_id: TIER_BY_THIRD[min(int(i // size), 2)] for i, t in enumerate(order)}

    def pick_label(self, season, round_, original_roster_id, league_id=None):
        """"2027 1st (orig. mgr_alex)" - picks are named by whose pick it originally was."""
        team = self.teams(league_id).get(original_roster_id)
        return f"{season} {ordinal(round_)} (orig. {team.manager if team else original_roster_id})"


def _real_names():
    """(real name, Sleeper name) pairs from the "Who's who" lines in league_context.txt,
    e.g. "- Jay Poe -> mgr_jay, co-owned with Kai Moe -> mgr_kai"."""
    path = config.LEAGUE_CONTEXT_PATH
    if not path.exists():
        return []
    return re.findall(r"([A-Z][A-Za-z'\-]+(?: [A-Z][A-Za-z'\-]+)*) -> (\w+)", path.read_text())
