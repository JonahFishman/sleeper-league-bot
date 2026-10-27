"""SQLite storage. One file, no server.

Tables hold Sleeper data close to how Sleeper shapes it. Lists and dicts (like a
trade's adds/drops) are stored as JSON text, which is simpler than splitting them
into extra tables and is plenty fast at this size (a few thousand transactions).
"""

import sqlite3

from . import config

SCHEMA = """
-- One row per season. league_id changes every season in Sleeper.
CREATE TABLE IF NOT EXISTS seasons (
    league_id TEXT PRIMARY KEY,
    season TEXT NOT NULL,
    previous_league_id TEXT,
    status TEXT,
    champion_roster_id INTEGER,    -- from the playoff bracket; NULL until the season ends
    runner_up_roster_id INTEGER,
    raw TEXT NOT NULL              -- full /league response, JSON
);

-- Users are stored per season because managers come and go.
CREATE TABLE IF NOT EXISTS users (
    league_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    team_name TEXT,
    PRIMARY KEY (league_id, user_id)
);

-- A roster is a franchise slot (roster_id 1-12). Its owner can change between seasons.
-- For finished seasons this is the final record; for the current season it's live.
CREATE TABLE IF NOT EXISTS rosters (
    league_id TEXT NOT NULL,
    roster_id INTEGER NOT NULL,
    owner_id TEXT,
    co_owner_ids TEXT,             -- JSON list
    players TEXT,                  -- JSON list of player IDs
    starters TEXT,
    taxi TEXT,
    reserve TEXT,
    wins INTEGER, losses INTEGER, ties INTEGER,
    points_for REAL, points_against REAL,
    PRIMARY KEY (league_id, roster_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    type TEXT NOT NULL,            -- trade, waiver, free_agent, commissioner
    status TEXT NOT NULL,          -- complete, failed, ...
    created_ms INTEGER,
    status_updated_ms INTEGER,
    roster_ids TEXT,               -- JSON list
    adds TEXT,                     -- JSON {player_id: roster_id}
    drops TEXT,                    -- JSON {player_id: roster_id}
    draft_picks TEXT,              -- JSON list of traded picks
    waiver_budget TEXT,            -- JSON list of FAAB transfers in a trade
    waiver_bid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tx_type ON transactions (type, status);

-- Rookie/startup draft selections.
CREATE TABLE IF NOT EXISTS draft_selections (
    draft_id TEXT NOT NULL,
    season TEXT NOT NULL,
    round INTEGER NOT NULL,
    pick_no INTEGER NOT NULL,      -- overall pick number
    roster_id INTEGER,             -- roster that made the pick
    original_roster_id INTEGER,    -- roster the pick originally belonged to (differs if traded)
    picked_by TEXT,                -- user_id
    player_id TEXT,
    PRIMARY KEY (draft_id, pick_no)
);

-- Who owns each future pick right now (from /traded_picks; untraded picks aren't listed).
CREATE TABLE IF NOT EXISTS traded_picks (
    season TEXT NOT NULL,
    round INTEGER NOT NULL,
    original_roster_id INTEGER NOT NULL,
    owner_roster_id INTEGER NOT NULL,
    PRIMARY KEY (season, round, original_roster_id)
);

-- Weekly fantasy points per player per roster. Used to judge old trades by what
-- players actually scored after changing teams.
CREATE TABLE IF NOT EXISTS player_points (
    league_id TEXT NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    points REAL NOT NULL,
    started INTEGER NOT NULL,      -- 1 if in the starting lineup
    PRIMARY KEY (league_id, week, roster_id, player_id)
);

CREATE TABLE IF NOT EXISTS matchups (
    league_id TEXT NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    matchup_id INTEGER,
    points REAL,
    PRIMARY KEY (league_id, week, roster_id)
);

-- Daily FantasyCalc snapshots. FantasyCalc has no history API, so we keep our own
-- to compare a trade's value on the day it happened with its value now.
CREATE TABLE IF NOT EXISTS value_snapshots (
    snapshot_date TEXT NOT NULL,   -- YYYY-MM-DD
    asset_key TEXT NOT NULL,       -- Sleeper player ID, or a pick name like "2027 1st (Early)"
    name TEXT NOT NULL,
    position TEXT,
    value INTEGER NOT NULL,
    PRIMARY KEY (snapshot_date, asset_key)
);

-- Small key/value store: sync cursors, last build time.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Every question and answer, for evaluation and debugging.
CREATE TABLE IF NOT EXISTS qa_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_id TEXT,
    question TEXT NOT NULL,
    answer TEXT,
    tools_used TEXT,               -- JSON list
    usage TEXT,                    -- JSON token counts
    error TEXT,
    asker_roster_id INTEGER        -- team the asker picked on the page (self-reported); NULL if they didn't say
);
"""


def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # The daily build and the web app can write at the same time; wait for the other
    # writer instead of failing straight away.
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["season"]
    # WAL mode lets the web app keep reading while the daily build is writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn):
    """CREATE TABLE IF NOT EXISTS won't add columns to a table that already exists, so
    columns added after launch get added here."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(qa_log)")}
    if "asker_roster_id" not in have:
        conn.execute("ALTER TABLE qa_log ADD COLUMN asker_roster_id INTEGER")


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
