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
import json
import csv
import io

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict) -> Response:
        response: Response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

from agent.agent import MerchantAgent
from agent.buyer_agent import BuyerAgent
from catalog.store import load_catalog
from logs.audit import read_log, clear_log
from metrics.metrics import compute_metrics
from gating.policy import _build_mandate, SessionState, _trust_tier
from gating.compliance_report import generate_compliance_report
from gating.replay import build_replay

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
    products: list[dict] = []
    actions: list[dict] = []
    trust_score: float | None = None


# ---------------------------------------------------------------------------
# Catalog + policy (existing Day-1 surface, kept unchanged)
# ---------------------------------------------------------------------------

@app.get("/catalog")
def get_catalog():
    """Returns the full merchant catalog in agent-readable JSON."""
    return load_catalog()


@app.get("/products")
def get_products():
    """Compatibility alias for clients that expect a /products endpoint."""
    return load_catalog()["products"]


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
        "trust": {
            "default_trust_score": SessionState().trust_score,
            "trust_tier": _trust_tier(SessionState().trust_score),
            "scale": "0-100, starts trusted at 75",
        },
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


@app.get("/audit")
def get_audit(after: int = 0):
    """Compatibility alias for clients that expect /audit instead of /audit-log."""
    return get_audit_log(after=after)


@app.get("/metrics")
def get_metrics():
    """Growth counters derived from the audit trail (single source of truth)."""
    return compute_metrics()


# ---------------------------------------------------------------------------
# Multi-agent negotiation simulation
# ---------------------------------------------------------------------------

class SimulateRequest(BaseModel):
    budget: int = 2000
    goal: str = "buy a complete outfit"


@app.post("/simulate-negotiation")
def simulate_negotiation(req: SimulateRequest):
    """
    Trigger a BuyerAgent vs MerchantAgent negotiation simulation.
    The buyer has a fixed budget and shopping goal, and programmatically
    attempts purchases through the same policy gate. All events land in
    the audit trail with event_type 'agent_to_agent' for visual distinction.
    """
    # Reset session state for a clean negotiation
    buyer = BuyerAgent(budget=req.budget, goal=req.goal)
    result = buyer.run()
    return {
        "status": "complete",
        "summary": {
            "budget": result["budget"],
            "total_spent": result["total_spent"],
            "items_purchased": result["items_purchased"],
            "items_rejected": result["items_rejected"],
        },
        "purchases": result["purchases"],
        "rejections": result["rejections"],
        "log": result["log"],
    }


# ---------------------------------------------------------------------------
# Audit trail export
# ---------------------------------------------------------------------------

@app.get("/audit-log/export")
def export_audit_log(fmt: str = "json"):
    """
    Export the current session's audit trail as a downloadable JSON or CSV file.
    Useful for compliance reporting and pitch demos.
    """
    events = read_log()

    if fmt == "csv":
        output = io.StringIO()
        if events:
            # Flatten nested dicts for CSV
            flat_events = []
            all_keys = []
            for ev in events:
                flat = {}
                for k, v in ev.items():
                    if isinstance(v, dict):
                        flat[k] = json.dumps(v)
                    elif isinstance(v, list):
                        flat[k] = json.dumps(v)
                    else:
                        flat[k] = v
                    if k not in all_keys:
                        all_keys.append(k)
                flat_events.append(flat)

            writer = csv.DictWriter(output, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat_events)
        else:
            output.write("No audit events recorded.\n")

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_trail.csv"},
        )
    else:
        # JSON format
        json_data = json.dumps(events, indent=2, ensure_ascii=False)
        return StreamingResponse(
            iter([json_data]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit_trail.json"},
        )


# ---------------------------------------------------------------------------
# Compliance report
# ---------------------------------------------------------------------------

@app.get("/compliance-report")
def compliance_report():
    """
    Generate a business/compliance summary of the current audit log.
    Returns structured summary statistics plus a plain-English narrative.
    """
    events = read_log()
    return generate_compliance_report(events)


@app.get("/session-replay")
def session_replay():
    """
    Replay the session's audit trail as a numbered, plain-English story,
    so a reviewer can follow exactly what happened in order.
    """
    events = read_log()
    return build_replay(events)


@app.get("/compliance-report/download")
def compliance_report_download(fmt: str = "html"):
    """
    Download the compliance report as a formatted file.
    reportlab/fpdf are not dependencies, so we produce a clean self-contained
    HTML report (printable to PDF from any browser) rather than a real .pdf.
    """
    events = read_log()
    report = generate_compliance_report(events)
    s = report["summary"]

    flags_html = ""
    violations = s["policy_violations_by_rule"]
    if violations:
        rows = "".join(
            f"<tr><td>{rule.replace('_', ' ')}</td><td>{count}</td></tr>"
            for rule, count in sorted(violations.items())
        )
        flags_html = (
            "<h3>Policy Violations by Rule</h3>"
            "<table><tr><th>Rule</th><th>Count</th></tr>" + rows + "</table>"
        )
    else:
        flags_html = "<p class='muted'>No policy violations recorded.</p>"

    trajectory_rows = "".join(
        f"<li>Trust score: {ts:.0f}</li>" for ts in s["trust_score_trajectory"]
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Compliance Report - Agent-Ready Merchant</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 40px; color: #1f2937; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #6366f1; padding-bottom: 8px; }}
  h2 {{ font-size: 16px; color: #4f46e5; margin-top: 24px; }}
  h3 {{ font-size: 14px; margin-top: 18px; }}
  table {{ border-collapse: collapse; width: 60%; margin-top: 8px; }}
  th, td {{ border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }}
  th {{ background: #f3f4f6; }}
  .summary p {{ margin: 4px 0; }}
  .narrative {{ background: #eef2ff; border-left: 4px solid #6366f1;
    padding: 12px 16px; margin: 16px 0; font-style: italic; }}
  .muted {{ color: #6b7280; }}
  .meta {{ color: #6b7280; font-size: 12px; margin-top: 24px; }}
</style></head>
<body>
<h1>Compliance Report &mdash; Agent-Ready Merchant</h1>

<div class="narrative">{report["narrative"]}</div>

<h2>Summary</h2>
<div class="summary">
  <p><strong>Total actions:</strong> {s["total_actions"]}</p>
  <p><strong>Purchases completed:</strong> {s["purchases_completed"]}</p>
  <p><strong>Purchases blocked:</strong> {s["purchases_blocked"]}</p>
  <p><strong>Total revenue:</strong> &#8377;{s["total_revenue"]:,.0f}</p>
  <p><strong>Total value blocked:</strong> &#8377;{s["total_value_blocked"]:,.0f}</p>
  <p><strong>Confirmations required:</strong> {s["confirmation_required_count"]}</p>
</div>

{flags_html}

<h2>Trust Score Trajectory</h2>
<ul>
  {"".join(trajectory_rows) if trajectory_rows else "<li class='muted'>No policy decisions recorded yet.</li>"}
</ul>

<p class="meta">Generated at {report["generated_at"]} &middot; Agent-Ready Merchant</p>
</body></html>"""

    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": "attachment; filename=compliance_report.html"},
    )


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

    reply, structured = agent.chat(req.message)
    return ChatResponse(
        session_id=session_id,
        reply=reply,
        products=structured.get("products", []),
        actions=structured.get("actions", []),
        trust_score=agent.session.trust_score,
    )


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
app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    def _warm_up_gemini():
        """Best-effort: establish the TLS connection + model router for the
        primary model while the server starts, so the FIRST user chat isn't
        slowed by an ~11s cold Gemini call. Never blocks startup and never
        crashes the app (a warm-up failure just means the first chat is slower)."""
        import threading
        import os
        from dotenv import load_dotenv
        from agent.agent import MODEL, client
        load_dotenv()
        if not os.getenv("GEMINI_API_KEY"):
            return
        try:
            client.models.generate_content(model=MODEL, contents="ping")
        except Exception:
            pass

    threading.Thread(target=_warm_up_gemini, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)
