"""
Agent-Readable Merchant API
----------------------------
Exposes the merchant's catalog in a structured, agent-consumable format, the
live audit trail, growth metrics, and a /chat endpoint that drives the agent
conversationally. This is what makes the merchant "AI-buyer-ready" - any
external agent (not just our own demo agent) could discover products and
understand the transaction contract via this API.

Run: uvicorn main:reload  (or: python main.py)
Frontend is served at http://localhost:8000/
"""

import uuid
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.agent import MerchantAgent
from catalog.store import load_catalog
from logs.audit import read_log
from metrics.metrics import compute_metrics
from gating.policy import _build_mandate

app = FastAPI(
    title="Agent-Ready Merchant API",
    description="Structured catalog + gated agentic commerce + audit trail (Razorpay test-mode).",
)

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
# One MerchantAgent per browser session. Each holds its own conversation
# history AND its own policy SessionState, so rate limits and spend tracking
# apply per user session rather than globally.
# A lock guards the dict because FastAPI may serve requests on different threads.
_sessions: dict[str, MerchantAgent] = {}
_sessions_lock = threading.Lock()


def get_session(session_id: str | None) -> tuple[str, MerchantAgent]:
    """Return the existing session for this id, or create a fresh one."""
    with _sessions_lock:
        if session_id and session_id in _sessions:
            return session_id, _sessions[session_id]
        new_id = session_id or str(uuid.uuid4())[:8]
        _sessions[new_id] = MerchantAgent()
        return new_id, _sessions[new_id]


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str


# ---------------------------------------------------------------------------
# Catalog + policy (existing Day-1 surface, kept unchanged)
# ---------------------------------------------------------------------------

@app.get("/catalog")
def get_catalog():
    """Returns the full merchant catalog in agent-readable JSON."""
    return load_catalog()


@app.get("/catalog/{product_id}")
def get_product(product_id: str):
    catalog = load_catalog()
    product = next((p for p in catalog["products"] if p["id"] == product_id), None)
    if not product:
        return {"error": "not found"}
    return product


@app.get("/policy")
def get_policy():
    """Exposes the gating rules so any AI buyer knows the transaction contract upfront."""
    from gating.policy import (
        MAX_TRANSACTION_AMOUNT, CONFIRMATION_THRESHOLD,
        ALLOWED_CATEGORIES, MAX_TRANSACTIONS_PER_SESSION,
    )
    from gating.campaign import LOW_STOCK_THRESHOLD, CAMPAIGN_DISCOUNT_PCT
    return {
        "max_transaction_amount": MAX_TRANSACTION_AMOUNT,
        "confirmation_threshold": CONFIRMATION_THRESHOLD,
        "allowed_categories": list(ALLOWED_CATEGORIES),
        "max_transactions_per_session": MAX_TRANSACTIONS_PER_SESSION,
        "campaign": {
            "low_stock_threshold": LOW_STOCK_THRESHOLD,
            "discount_pct": CAMPAIGN_DISCOUNT_PCT,
        },
        "mandate": _build_mandate(),
    }


# ---------------------------------------------------------------------------
# Audit trail + metrics
# ---------------------------------------------------------------------------

@app.get("/audit-log")
def get_audit_log(after: int = 0):
    """
    Returns the audit trail. `after` = number of leading events the client
    already has; only newer events are returned. Lets the frontend poll
    cheaply every second instead of re-downloading everything.
    """
    events = read_log()
    return {
        "total": len(events),
        "events": events[after:],
    }


@app.get("/metrics")
def get_metrics():
    """Growth counters derived from the audit trail (single source of truth)."""
    return compute_metrics()


# ---------------------------------------------------------------------------
# Agent chat
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """
    Main conversational endpoint. The whole agent loop runs here synchronously:
    model turns + tool calls (browse / upsell / campaigns / gated purchases).
    Typically takes a few seconds per message because of model round-trips.
    """
    session_id, agent = get_session(req.session_id)

    # A plain "no thanks" to an offered suggestion gets logged as a decline
    # so the audit trail shows the upsell outcome even when nothing was bought.
    lowered = req.message.strip().lower()
    if agent.last_suggestion and any(w in lowered for w in ("no thanks", "nope", "decline", "not interested")):
        agent.record_user_rejection(agent.last_suggestion["product"]["id"])

    reply = agent.chat(req.message)
    return ChatResponse(session_id=session_id, reply=reply)


@app.post("/session/reset")
def reset_session(session_id: str | None = None):
    """
    Drop one (or all) chat session(s). Handy between demo takes: a fresh
    session also resets policy counters like the per-session transaction cap.
    """
    with _sessions_lock:
        if session_id:
            _sessions.pop(session_id, None)
        else:
            _sessions.clear()
    return {"status": "reset"}


# ---------------------------------------------------------------------------
# Frontend (static single-page app)
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse("static/index.html")


# CSS/JS assets for the dashboard
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
