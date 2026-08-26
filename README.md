# Agent-Ready Merchant — Day 1

An agentic commerce demo that shows how a **bounded, gated AI agent** can transact on behalf of a customer within strict merchant-defined policy rules — with a full audit trail.

## What it does

A Gemini-powered chat agent acts as a merchant's sales assistant. It can:

1. **Browse the catalog** and answer product questions
2. **Execute purchases** — but only after passing through a policy gating engine
3. **Offer campaign discounts** on low-stock items
4. **Suggest upsells** after successful purchases

Every action is **logged to an audit trail** with the agent's reasoning, the mandate (boundary constraints), and the policy decision — so you can replay exactly what happened and why.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Frontend (static/index.html + app.js)                  │
│  Chat UI │ Product Grid │ Audit Trail                   │
└───────────────────┬─────────────────────────────────────┘
                    │ FastAPI
┌───────────────────▼─────────────────────────────────────┐
│  API Layer (main.py)                                    │
│  /chat  /products  /policy  /audit  /metrics            │
└───────────────────┬─────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────┐
│  Agent Layer (agent/agent.py)                            │
│  Gemini 3.5 Flash Lite │ 4 tool calls │ retry logic     │
└─────┬───────────┬───────────────┬───────────────┬───────┘
      │           │               │               │
┌─────▼──┐ ┌─────▼──────┐ ┌─────▼─────┐ ┌───────▼──────┐
│ Policy │ │  Upsell    │ │ Campaign  │ │ Razorpay     │
│ (gating│ │  (cross-   │ │ (clearance│ │ (test-mode   │
│  engine│ │   sell)    │ │  offers)  │ │  payments)   │
└───┬────┘ └────────────┘ └───────────┘ └──────────────┘
    │
┌───▼────────────────────────────────────────────────────┐
│  Audit Trail (logs/audit.py)                           │
│  JSONL │ mandate │ agent_reasoning │ policy_decision    │
└───────────────────────────────────────────────────────┘
```

## Policy Gating Engine

Every purchase attempt passes through these rules **in order** (first failure wins):

| # | Rule | Threshold | Behavior |
|---|------|-----------|----------|
| 1 | Category allowlist | `{apparel, footwear, accessories}` | Blocks non-merch categories |
| 2 | Stock check | `> 0` | Prevents overselling |
| 3 | Hard spend ceiling | ₹3,000 | Agent cannot self-authorize above this |
| 4 | Session rate limit | 3 txns/session | Prevents runaway purchasing |
| 5 | Confirmation threshold | ₹1,500 | Requires explicit user confirmation |

Every decision carries a **mandate** — the full set of boundary constraints — so external AI buyers can read the rules from `/policy` before attempting a transaction.

## Explainability Layers

Each audit event includes two layers:

- **`agent_reasoning`** — What the LLM said before calling a tool (its chain-of-thought explanation)
- **`policy_decision`** — What the deterministic gating engine decided (includes `rule_triggered`, `requires_confirmation`, and `mandate`)

This separates "what the AI wanted to do" from "what the rules allowed" — a key requirement for trustworthy agent commerce.

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```
GEMINI_API_KEY=your_key_here
RAZORPAY_KEY_ID=rzp_test_xxx
RAZORPAY_KEY_SECRET=xxx
```

## Running

```bash
# Start the server
python main.py

# Open http://localhost:8000 in your browser
```

## Running Tests

```bash
# Run all unit tests (no API calls, no real payments)
pytest -v

# Run a full smoke test against the live agent (uses real Gemini API + Razorpay test mode)
python tests/manual_smoke_test.py
```

### Test Suite Breakdown

| File | Tests | What it covers |
|------|-------|----------------|
| `test_policy.py` | 21 | All 5 gating rules, rule priority, mandate field |
| `test_upsell.py` | 9 | Complementary pairs, structure, fallback behavior |
| `test_campaign.py` | 14 | Low-stock detection, discount math, campaign offers |
| `test_audit.py` | 12 | Log write/read/clear, JSONL format, ordering |
| `test_razorpay_failure.py` | 6 | Razorpay exceptions, stock safety, mock success |

**62 unit tests total** — zero network calls, zero real payments.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a message to the agent |
| `GET` | `/products` | List catalog products |
| `GET` | `/policy` | Current policy rules + mandate |
| `GET` | `/audit` | Full audit trail |
| `GET` | `/metrics` | Dashboard metrics (revenue, conversions, blocks) |

## Tech Stack

- **LLM**: Google Gemini 3.5 Flash Lite (free tier)
- **Backend**: Python 3.12, FastAPI, Uvicorn
- **Payments**: Razorpay (test mode)
- **Frontend**: Vanilla HTML/CSS/JS (no build step)
- **Data**: JSONL audit log, JSON catalog file

## Key Files

```
agent/agent.py          — Gemini agent with tool calling
gating/policy.py        — Policy gating engine (5 rules)
gating/upsell.py        — Cross-sell suggestion engine
gating/campaign.py      — Clearance campaign logic
logs/audit.py           — Audit trail (JSONL)
main.py                 — FastAPI server
static/app.js           — Frontend application logic
catalog/products.json   — Product catalog (14 products)
tests/manual_smoke_test.py — End-to-end smoke test
```

## License

Internal demo project — not for production use.
