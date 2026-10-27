"""The chat handler: takes a question, returns an answer.

Per question:
  1. Sync the latest Sleeper transactions and rosters (at most once a minute).
  2. Start a session with a frozen copy of the briefing, or continue one. If moves
     happened since the session started, they're appended as a mid-conversation system
     note instead of rewriting the briefing. (Changing the system prompt mid-conversation
     breaks prompt caching and, on newer models, is rejected outright.)
  3. Call Claude, running any tools it asks for, until it answers.
  4. Log the question and answer.
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import anthropic

from . import briefing, config, db, history, sleeper
from .league import League
from .tools import TOOLS, WEB_SEARCH, run_tool
from .values import Values

SYSTEM_PROMPT = (Path(__file__).parent / "prompt.md").read_text()
SYNC_INTERVAL_SECONDS = 60
MAX_MODEL_CALLS = 10  # per question; stops a runaway tool loop


@dataclass
class Answer:
    text: str
    session_id: str
    tools_used: list


@dataclass
class Session:
    id: str
    system: list                    # frozen at the session's start
    seen_through_ms: int            # newest transaction the frozen briefing includes
    messages: list = field(default_factory=list)
    last_used: float = field(default_factory=time.time)


class Bot:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self._load_players()
        self.sessions = {}
        self._lock = threading.Lock()   # guards syncing, the briefing cache and the sessions dict
        self._last_sync = 0.0
        self._briefing_key = None
        self._briefing_blocks = None

    # ---- public -------------------------------------------------------------------

    def ask(self, question, session_id=None, asker_roster_id=None):
        """Answer a question, continuing session_id's conversation if it's still alive.
        asker_roster_id is the team the asker picked on the page, if any (self-reported)."""
        conn = db.connect()  # one connection per request: Flask serves requests on several threads
        tools_used, usage, answer, error = [], {}, None, None
        try:
            with self._lock:
                self._sync(conn)
                league = League(conn, self.players)
                values = Values(conn)
                session = self._get_session(conn, league, values, session_id)
                session_id = session.id

            content = question
            if not session.messages and asker_roster_id in league.teams():
                # First message of the conversation: say who's asking. It goes in the user
                # turn, not the system prompt, so the cached briefing stays identical.
                content = (f"(The asker picked {league.team_choices()[asker_roster_id]} as their team on the page. "
                           f"Self-reported, not verified.)\n\n{question}")
            session.messages.append({"role": "user", "content": content})
            update = self._update_note(conn, league, values, session)
            if update:
                session.messages.append({"role": "system", "content": update})

            answer = self._run(conn, league, values, session, tools_used, usage)
            return Answer(answer, session.id, tools_used)
        except Exception as e:
            error = repr(e)
            # The conversation may now end mid-tool-call, which the API would reject on
            # the next turn. Start the next question fresh instead.
            self.sessions.pop(session_id, None)
            raise
        finally:
            conn.execute(
                "INSERT INTO qa_log (session_id, question, answer, tools_used, usage, error, asker_roster_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, question, answer, json.dumps(tools_used), json.dumps(usage), error, asker_roster_id),
            )
            conn.commit()
            conn.close()

    # ---- the model loop -----------------------------------------------------------

    def _run(self, conn, league, values, session, tools_used, usage):
        for _ in range(MAX_MODEL_CALLS):
            response = self.client.beta.messages.create(
                model=config.MODEL,
                max_tokens=16000,
                system=session.system,
                messages=session.messages,
                tools=[*TOOLS, WEB_SEARCH],
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},  # medium answered faster but contradicted its own numbers in evals
                cache_control={"type": "ephemeral"},  # also cache the growing conversation
                # If a safety classifier declines, let Anthropic retry on a suitable fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            _add_usage(usage, response.usage)
            tools_used += [b.name for b in response.content if b.type == "server_tool_use"]  # web searches
            # Always append the full content (thinking blocks included) unchanged.
            session.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tools_used.append(block.name)
                        text, is_error = run_tool(conn, league, values, block.name, block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
                session.messages.append({"role": "user", "content": results})
                continue
            if response.stop_reason == "pause_turn":
                continue  # web search paused mid-turn; sending the history back resumes it
            if response.stop_reason == "refusal":
                return "I can't help with that one. Ask me something about the league."

            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return text or "I came up empty on that one. Try asking it a different way?"
        return "That took more digging than I'm allowed. Try a narrower question?"

    # ---- sessions and freshness ---------------------------------------------------

    def _load_players(self):
        self.players = sleeper.players()
        self._players_mtime = config.PLAYERS_CACHE.stat().st_mtime

    def _sync(self, conn):
        # The daily build refreshes the players file on disk; pick up the new one.
        if config.PLAYERS_CACHE.stat().st_mtime != self._players_mtime:
            self._load_players()
        if time.time() - self._last_sync < SYNC_INTERVAL_SECONDS:
            return
        try:
            history.sync_recent(conn)
        except Exception:
            # Sleeper being down shouldn't stop the bot answering from what we have.
            pass
        self._last_sync = time.time()

    def _get_session(self, conn, league, values, session_id):
        now = time.time()
        for sid in [s for s, sess in self.sessions.items() if now - sess.last_used > config.SESSION_TTL_SECONDS]:
            del self.sessions[sid]

        session = self.sessions.get(session_id)
        if session is None:
            session = Session(uuid.uuid4().hex, self._system_blocks(conn, league, values), _newest_tx_ms(conn, league))
            self.sessions[session.id] = session
        session.last_used = now
        return session

    def _system_blocks(self, conn, league, values):
        """The system prompt + briefing, rebuilt only when the underlying data changed,
        so consecutive sessions send byte-identical text and hit the prompt cache."""
        def mtime(path):
            return path.stat().st_mtime if path.exists() else None

        key = (
            db.get_meta(conn, "last_full_build"),  # new matchup scores, drafts, etc.
            _newest_tx_ms(conn, league),
            values.snapshot_date,
            date.today().isoformat(),
            mtime(config.TENDENCIES_PATH),
            mtime(config.LEAGUE_CONTEXT_PATH),
        )
        if key != self._briefing_key:
            b = briefing.build(conn, league, values)
            # Two cache breakpoints: one after the slow-changing part, one after the rest.
            self._briefing_blocks = [
                {"type": "text", "text": SYSTEM_PROMPT},
                {"type": "text", "text": "\n".join([b.rules, b.context, b.history, b.tendencies]), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "\n".join([b.current, b.recent]), "cache_control": {"type": "ephemeral"}},
            ]
            self._briefing_key = key
        return self._briefing_blocks

    def _update_note(self, conn, league, values, session):
        """Describe any moves made since this session's briefing was frozen, or None."""
        rows = conn.execute(
            """SELECT * FROM transactions WHERE league_id = ? AND status = 'complete'
               AND coalesce(status_updated_ms, created_ms) > ? ORDER BY coalesce(status_updated_ms, created_ms)""",
            (league.current_league_id, session.seen_through_ms),
        ).fetchall()
        if not rows:
            return None
        session.seen_through_ms = _newest_tx_ms(conn, league)
        lines = [briefing.describe_transaction(league, values, r) for r in rows]
        return (
            "League update: these moves happened after the briefing above was written. "
            "Rosters and pick ownership in the briefing don't reflect them yet; treat these as current.\n"
            + "\n".join(lines)
        )


def _newest_tx_ms(conn, league):
    row = conn.execute(
        "SELECT max(coalesce(status_updated_ms, created_ms)) AS ms FROM transactions WHERE league_id = ? AND status = 'complete'",
        (league.current_league_id,),
    ).fetchone()
    return row["ms"] or 0


def _add_usage(totals, u):
    for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        totals[name] = totals.get(name, 0) + (getattr(u, name, 0) or 0)
