"""Command line entry point. Run from the repo root:

    python -m league_bot build       # daily job: full history walk + players + values
    python -m league_bot rosters     # print every roster with values (sanity check)
    python -m league_bot briefing    # print the full briefing the model sees
    python -m league_bot ask "who won the Chase trade?"
"""

import sys

from . import briefing, db, history, sleeper, values
from .league import League


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "help"
    conn = db.connect()

    if command == "build":
        sleeper.players(force_refresh=True)
        history.build_all(conn)
        print(f"FantasyCalc: {values.refresh(conn)} values saved")

    elif command == "rosters":
        league, vals = League(conn), values.Values(conn)
        for team in league.teams().values():
            print(f"\n{team.label}  {team.wins}-{team.losses}")
            for pid in sorted(team.players, key=vals.player, reverse=True):
                print(f"  {league.player_tag(pid):<45} {vals.player(pid):>6}")

    elif command == "briefing":
        b = briefing.build(conn, League(conn), values.Values(conn))
        print("\n".join([b.rules, b.context, b.history, b.tendencies, b.current, b.recent]))

    elif command == "ask":
        from .chat import Bot  # imported here so the other commands don't need an API key

        print(Bot().ask(" ".join(sys.argv[2:])).text)

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
