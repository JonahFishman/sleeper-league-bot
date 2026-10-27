"""Settings shared by every module. Override any of these with environment variables."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load secrets like ANTHROPIC_API_KEY from .env (gitignored) so they don't have to be
# exported in every terminal. Lines look like KEY=value. Real environment variables win.
_env_file = ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

# The current season's league. Older seasons are found by following previous_league_id.
# Required: your Sleeper league ID (in the league URL). Put LEAGUE_ID=... in .env.
LEAGUE_ID = os.environ.get("LEAGUE_ID", "")

# The folder for everything we generate (database, caches). It's gitignored.
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "league.db"
PLAYERS_CACHE = DATA_DIR / "players_nfl.json"

# Jonah's private notes on each manager. Gitignored; never shown to users.
TENDENCIES_PATH = Path(os.environ.get("TENDENCIES_PATH", ROOT / "tendencies.txt"))

# Public league context Jonah writes: nicknames, lore, rules Sleeper doesn't encode.
# Unlike the notes, the bot can quote this freely. Gitignored only to keep real names off GitHub.
LEAGUE_CONTEXT_PATH = Path(os.environ.get("LEAGUE_CONTEXT_PATH", ROOT / "league_context.txt"))

SLEEPER_BASE = "https://api.sleeper.app/v1"

# FantasyCalc must be asked for THIS league's format. numQbs=1 is the important one:
# superflex values would wildly overrate QBs.
FANTASYCALC_URL = (
    "https://api.fantasycalc.com/values/current"
    "?isDynasty=true&numQbs=1&numTeams=12&ppr=1"
)

MODEL = os.environ.get("BOT_MODEL", "claude-sonnet-5-5")

# Chat sessions are kept in memory and forgotten after this much inactivity.
SESSION_TTL_SECONDS = 30 * 60
