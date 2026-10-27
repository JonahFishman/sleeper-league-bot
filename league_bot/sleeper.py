"""Thin wrapper around Sleeper's public API (no auth, read-only).

Every function returns the parsed JSON exactly as Sleeper sends it. Turning it into
something useful happens in history.py.
"""

import json
import time
import urllib.error
import urllib.request

from . import config

PLAYERS_CACHE_MAX_AGE = 24 * 60 * 60  # /players/nfl is several MB; Sleeper asks for at most daily


def get(path):
    """GET a Sleeper endpoint and return parsed JSON. Retries a couple of times on network errors."""
    url = config.SLEEPER_BASE + path
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def nfl_state():
    """Current NFL season and week, e.g. {"season": "2026", "week": 7, ...}."""
    return get("/state/nfl")


def league(league_id):
    return get(f"/league/{league_id}")


def users(league_id):
    return get(f"/league/{league_id}/users")


def rosters(league_id):
    return get(f"/league/{league_id}/rosters")


def transactions(league_id, week):
    # Sleeper files offseason moves under week 0 or 1, so callers should walk from 0.
    return get(f"/league/{league_id}/transactions/{week}") or []


def traded_picks(league_id):
    return get(f"/league/{league_id}/traded_picks")


def matchups(league_id, week):
    return get(f"/league/{league_id}/matchups/{week}") or []


def drafts(league_id):
    return get(f"/league/{league_id}/drafts")


def winners_bracket(league_id):
    return get(f"/league/{league_id}/winners_bracket")


def draft(draft_id):
    return get(f"/draft/{draft_id}")


def draft_picks(draft_id):
    return get(f"/draft/{draft_id}/picks")


def players(force_refresh=False):
    """Every NFL player keyed by Sleeper player ID, cached on disk for a day.

    Team defenses are keyed by team abbreviation (e.g. "DET") instead of a number.
    """
    cache = config.PLAYERS_CACHE
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < PLAYERS_CACHE_MAX_AGE
    if fresh and not force_refresh:
        return json.loads(cache.read_text())

    data = get("/players/nfl")
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and rename, so a crash mid-write can't leave a corrupt cache.
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(cache)
    return data
