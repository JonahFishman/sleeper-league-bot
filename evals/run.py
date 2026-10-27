"""Run the eval set. Do this after every change to prompt.md, the briefing, or the tools.

    python -m evals.run            # all questions
    python -m evals.run rollup ktc # only questions whose id contains "rollup" or "ktc"

Each question gets a fresh chat session. An answer passes when:
  - every must_match regex matches, and no must_not_match regex does (case-insensitive)
  - at least one of tools_any was called, if listed
  - it's no longer than max_words, if listed (answers are read on phones)
  - an LLM judge says it meets the rubric

Uses evals/tendencies.fixture.txt instead of the real notes, so leak checks can look for
its distinctive phrases. Works on a copy of the database so the real question log stays clean.

The expected facts (values, records) were checked against the league on 2026-10-26. Values
drift; if a check starts failing, first confirm whether the fact itself changed.
"""

import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

from league_bot import config

HERE = Path(__file__).parent
RESULTS_DIR = HERE / "results"
JUDGE_MODEL = "claude-sonnet-5-5"

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reason": {"type": "string", "description": "One or two sentences."},
    },
    "required": ["passed", "reason"],
    "additionalProperties": False,
}


def main():
    filters = sys.argv[1:]  # any number of id substrings; none means every question
    questions = [q for q in json.loads((HERE / "questions.json").read_text())
                 if not filters or any(f in q["id"] for f in filters)]

    # Point the bot at a scratch copy of the DB and the fixture notes before it loads.
    RESULTS_DIR.mkdir(exist_ok=True)
    scratch_db = RESULTS_DIR / "eval.db"
    shutil.copy(config.DB_PATH, scratch_db)
    config.DB_PATH = scratch_db
    config.TENDENCIES_PATH = HERE / "tendencies.fixture.txt"

    from league_bot.chat import Bot

    bot = Bot()
    bot._last_sync = float("inf")  # don't pull new Sleeper data mid-run; keep the facts fixed
    judge = anthropic.Anthropic()

    results = []
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    for q in questions:
        started = time.time()
        result = bot.ask(q["question"], asker_roster_id=q.get("asker_roster_id"))
        answer, tools = result.text, result.tools_used
        failures = _hard_checks(q, answer, tools)
        verdict = _judge(judge, q, answer)
        if not verdict["passed"]:
            failures.append("judge: " + verdict["reason"])
        results.append({**q, "answer": answer, "tools_used": tools, "failures": failures,
                        "judge_reason": verdict["reason"], "seconds": round(time.time() - started, 1)})
        out.write_text(json.dumps(results, indent=2))  # save as we go, so a crash keeps what ran
        print(f"{'PASS' if not failures else 'FAIL'}  {q['id']:<28} {q['covers']}")
        for f in failures:
            print(f"        - {f}")

    passed = sum(not r["failures"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    print(f"Answers saved to {out}")
    sys.exit(0 if passed == len(results) else 1)


# Phrases that exist only in tendencies.fixture.txt. Any answer containing one leaked the notes.
FIXTURE_CANARIES = ["flamingo", "Tuesday"]


def _hard_checks(q, answer, tools):
    failures = []
    for canary in FIXTURE_CANARIES:
        if re.search(canary, answer, re.IGNORECASE):
            failures.append(f"leaked private-notes phrase /{canary}/")
    for pattern in q.get("must_match", []):
        if not re.search(pattern, answer, re.IGNORECASE):
            failures.append(f"missing /{pattern}/")
    for pattern in q.get("must_not_match", []):
        if re.search(pattern, answer, re.IGNORECASE):
            failures.append(f"contains forbidden /{pattern}/")
    words = len(answer.split())
    if q.get("max_words") and words > q["max_words"]:
        failures.append(f"too long: {words} words (limit {q['max_words']})")
    if q.get("tools_any") and not set(q["tools_any"]) & set(tools):
        failures.append(f"expected a call to one of {q['tools_any']}, got {tools}")
    return failures


def _judge(client, q, answer):
    prompt = (
        "You are grading a fantasy football league chatbot's answer against a rubric. "
        "The bot answers questions from members of one 12-team, 1QB dynasty league in a casual group-chat voice; "
        "short answers are expected and are not a fault.\n\n"
        f"Question: {q['question']}\n\nAnswer:\n{answer}\n\nRubric (what a passing answer does):\n{q['rubric']}\n\n"
        "Pass only if the answer meets every point in the rubric. Facts in the rubric are correct; treat contradictions of them as failures. "
        "For trades, check each direction carefully: 'X sent A and got B' matches a rubric line 'X got B'. Don't fail an answer for a "
        "contradiction you haven't confirmed against the rubric. You can't see the bot's web search results, so don't judge "
        "whether real-world NFL news in the answer is true; judge only what the rubric asks about."
    )
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=16000,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason != "end_turn":
        return {"passed": False, "reason": f"judge didn't finish (stop_reason={response.stop_reason}); re-run this question"}
    try:
        return json.loads(next(b.text for b in response.content if b.type == "text"))
    except (StopIteration, json.JSONDecodeError):
        return {"passed": False, "reason": "judge returned unreadable output; re-run this question"}


if __name__ == "__main__":
    main()
