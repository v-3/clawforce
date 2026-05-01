"""
AI Tutor — runtime orchestrator.

Two entry points:
  - daily_run(kickoff: str)         called by cron (default kickoff "DAILY RUN")
  - slack_webhook(request)          Flask route, called by Slack Events API

The orchestrator is the security boundary:
  - Hard I/O filter: drop any Slack event whose user != ALLOWED_SLACK_USER_ID.
  - Slack token never enters the agent sandbox. The agent calls
    post_slack_message as a custom tool; the orchestrator executes the actual
    Slack Web API call and returns the result.

State lives in the memory store mounted into every session — sessions
themselves are short-lived and disposable.

Required env (see .env.example):
  ANTHROPIC_API_KEY           Claude API key
  AGENT_ID                    from setup.sh (also accepts AGENT_VERSION; if
                              unset, latest version is used)
  ENV_ID                      from setup.sh
  MEMORY_STORE_ID             from setup.sh
  SLACK_BOT_TOKEN             xoxb-... bot token with chat:write, im:write
  SLACK_SIGNING_SECRET        for webhook verification
  ALLOWED_SLACK_USER_ID       e.g. U0123ABCD — only this user may interact
  DEFAULT_DM_CHANNEL          DM channel ID with the allowed user (D...)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anthropic
import requests
from flask import Flask, abort, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ai-tutor")


# ---------- config ----------

def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        log.error("missing required env var: %s", name)
        sys.exit(1)
    return v


ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
AGENT_ID = _require("AGENT_ID")
AGENT_VERSION = os.environ.get("AGENT_VERSION")  # optional — pin to a version
ENV_ID = _require("ENV_ID")
MEMORY_STORE_ID = _require("MEMORY_STORE_ID")
SLACK_BOT_TOKEN = _require("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = _require("SLACK_SIGNING_SECRET")
ALLOWED_SLACK_USER_ID = _require("ALLOWED_SLACK_USER_ID")
DEFAULT_DM_CHANNEL = _require("DEFAULT_DM_CHANNEL")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------- Slack: outbound ----------

def post_slack_message(channel: str | None, thread_ts: str | None, text: str, blocks: list | None) -> dict:
    """Call Slack Web API. Token stays here, never in the agent sandbox."""
    payload: dict[str, Any] = {
        "channel": channel or DEFAULT_DM_CHANNEL,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks

    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps(payload),
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Slack API error: {body.get('error')!r}")
    return {"ok": True, "ts": body["ts"], "channel": body["channel"]}


# ---------- session driver ----------

def _agent_ref() -> Any:
    if AGENT_VERSION:
        return {"type": "agent", "id": AGENT_ID, "version": int(AGENT_VERSION)}
    return AGENT_ID


def _create_session(title: str) -> Any:
    return client.beta.sessions.create(
        agent=_agent_ref(),
        environment_id=ENV_ID,
        title=title,
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": MEMORY_STORE_ID,
                "access": "read_write",
            }
        ],
    )


def _run_session(kickoff: str, title: str) -> None:
    """Create a session, send the kickoff, drive the event loop until idle.

    Pattern 5 (correct idle-break gate) and Pattern 7 (stream-first) from the
    Managed Agents client patterns.
    """
    session = _create_session(title)
    log.info("session %s created (%s)", session.id, title)

    # Stream-first: open the stream BEFORE sending the kickoff.
    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": kickoff}],
                }
            ],
        )

        for event in stream:
            etype = event.type
            if etype == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        log.info("[agent] %s", block.text[:200])

            elif etype == "agent.custom_tool_use":
                _handle_custom_tool(session.id, event)

            elif etype == "session.error":
                log.error("session error: %r", event)

            elif etype == "session.status_terminated":
                log.info("session terminated")
                return

            elif etype == "session.status_idle":
                # Break ONLY when stop_reason is terminal (Pattern 5).
                stop_type = getattr(event.stop_reason, "type", None)
                if stop_type == "requires_action":
                    # The agent is waiting on a custom tool result we already
                    # sent — keep iterating until the next status.
                    continue
                # end_turn or retries_exhausted — terminal.
                log.info("session idle (stop_reason=%s)", stop_type)
                return


def _handle_custom_tool(session_id: str, event: Any) -> None:
    """Execute the custom tool host-side and post the result back."""
    tool_use_id = event.id
    name = event.tool_name
    inp = event.input or {}
    log.info("custom tool: %s input=%s", name, json.dumps(inp)[:200])

    try:
        if name == "post_slack_message":
            result = post_slack_message(
                channel=inp.get("channel"),
                thread_ts=inp.get("thread_ts"),
                text=inp["text"],
                blocks=inp.get("blocks"),
            )
            content = json.dumps(result)
            is_error = False
        else:
            content = f"unknown tool: {name}"
            is_error = True
    except Exception as e:
        log.exception("custom tool failed")
        content = f"{type(e).__name__}: {e}"
        is_error = True

    client.beta.sessions.events.send(
        session_id=session_id,
        events=[
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": tool_use_id,
                "content": [{"type": "text", "text": content}],
                "is_error": is_error,
            }
        ],
    )


# ---------- entry points ----------

def daily_run(kickoff: str = "DAILY RUN") -> None:
    """Cron entry point. Set kickoff to 'ONBOARDING start' for first run."""
    _run_session(kickoff, title=f"daily-{time.strftime('%Y-%m-%d')}")


def missed_day(date: str) -> None:
    """Mark a previous day as missed (no reply received). Triggered by a
    secondary cron that checks runs/<date>.json status the next morning."""
    _run_session(f"MISSED {date}", title=f"missed-{date}")


# ---------- Slack webhook (Flask) ----------

app = Flask(__name__)

# Threads handle long-running grading sessions while the request thread acks
# Slack inside the 3-second window. max_workers=4 is generous for a 1-user
# system; sessions are CPU-light (mostly waiting on network).
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aitutor")

# Slack retries deliveries that aren't acked in 3s. We ack fast so retries are
# rare, but a network blip can still produce duplicates — dedup by event_id.
_seen_event_ids: set[str] = set()
_seen_lock = threading.Lock()


def _is_new_event(event_id: str) -> bool:
    """Return True if event_id has not been seen. Caps memory at ~1000 entries."""
    if not event_id:
        return True  # no ID — best-effort process
    with _seen_lock:
        if event_id in _seen_event_ids:
            return False
        if len(_seen_event_ids) > 1000:
            _seen_event_ids.clear()  # crude eviction; fine for our scale
        _seen_event_ids.add(event_id)
        return True


def _run_session_safe(kickoff: str, title: str) -> None:
    """Background worker entry. Wraps _run_session with logging — exceptions
    here would otherwise be swallowed by the executor."""
    try:
        _run_session(kickoff, title)
    except Exception:
        log.exception("background session failed: %s", title)


@app.get("/healthz")
def healthz():
    return ("ok", 200)


def _verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False
    # Reject replays older than 5 minutes
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False
    body = req.get_data(as_text=True)
    base = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


@app.post("/slack/events")
def slack_events():
    if not _verify_slack_signature(request):
        log.warning("invalid Slack signature")
        abort(401)

    payload = request.get_json(silent=True) or {}

    # Slack URL verification handshake
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge")})

    if payload.get("type") != "event_callback":
        return ("", 200)

    # Dedup before any other work — a retry of the same event must not
    # re-trigger a session.
    event_id = payload.get("event_id", "")
    if not _is_new_event(event_id):
        log.info("dedup: skipping repeat event %s", event_id)
        return ("", 200)

    event = payload.get("event", {})

    # ============ HARD I/O FILTER ============
    # Drop anything that isn't a message from the allowed user.
    if event.get("type") != "message":
        return ("", 200)
    if event.get("subtype"):  # bot_message, message_changed, etc.
        return ("", 200)
    if event.get("user") != ALLOWED_SLACK_USER_ID:
        log.warning(
            "dropped event from non-allowed user %r", event.get("user")
        )
        return ("", 200)
    # =========================================

    text = event.get("text") or ""
    thread_ts = event.get("thread_ts") or event.get("ts")
    classification = _classify_reply(text)

    if classification == "teaching_score":
        kickoff = f"TEACHING: {text}\nThread: {thread_ts}"
        title = "teaching-score"
    elif classification == "essay":
        kickoff = f"ONBOARDING reply:\n\n{text}"
        title = "onboarding-reply"
    elif classification == "confirm":
        kickoff = "ONBOARDING confirm"
        title = "onboarding-confirm"
    else:
        kickoff = f"GRADE: {text}\nThread: {thread_ts}"
        title = "grade-reply"

    # Async dispatch: ack Slack now, run the session in a background thread.
    # Slack will retry if we don't return 200 within ~3s, and a session
    # typically takes 1-3 min.
    _executor.submit(_run_session_safe, kickoff, title)
    return ("", 200)


def _classify_reply(text: str) -> str:
    """Light classification before handoff. The agent does the real parsing.

    Returns one of: essay | confirm | teaching_score | grade.
    The agent's mode-dispatch logic also handles the fall-through cases.
    """
    stripped = text.strip()
    if stripped.lower() in {"confirm", "looks good", "lgtm", "ok", "yes"}:
        return "confirm"
    # 0-5 single number → teaching score
    if stripped.isdigit() and 0 <= int(stripped) <= 5:
        return "teaching_score"
    # Long reply early in onboarding → essay (heuristic: > 200 chars and no
    # current run record waiting). The agent re-checks via memory state.
    if len(stripped.split()) >= 100:
        return "essay"
    return "grade"


# ---------- CLI ----------

def _main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_daily = sub.add_parser("daily", help="Run a daily-run session.")
    p_daily.add_argument("--kickoff", default="DAILY RUN")

    sub.add_parser("onboard", help="Trigger onboarding (first-run).")

    p_missed = sub.add_parser("missed", help="Mark a date as missed.")
    p_missed.add_argument("date")

    p_serve = sub.add_parser("serve", help="Run the Slack webhook server.")
    p_serve.add_argument("--port", type=int, default=8080)

    args = parser.parse_args()
    if args.cmd == "daily":
        daily_run(kickoff=args.kickoff)
    elif args.cmd == "onboard":
        daily_run(kickoff="ONBOARDING start")
    elif args.cmd == "missed":
        missed_day(args.date)
    elif args.cmd == "serve":
        app.run(host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    _main()
