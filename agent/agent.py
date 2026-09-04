"""
Agent Brain
-----------
An AI "buyer" that browses a merchant's agent-readable catalog and attempts
purchases on the user's behalf. Every money-moving action is routed through
the gating engine (policy -> Razorpay test-mode order), every step lands in
the audit log, and successful purchases decrement real catalog stock.

Swapped from Anthropic Claude to Google Gemini (free tier) which supports
function/tool calling. Uses the google-genai Python SDK with manual function
calling (not automatic) to capture agent reasoning text alongside tool calls
for the explainability layer.

Run standalone: python agent/agent.py "I want to buy a t-shirt under 600 rupees"
The same MerchantAgent class powers POST /chat in main.py.
"""

import os
import sys
import json
import re
import uuid
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from dotenv import load_dotenv

from gating.policy import evaluate_transaction, SessionState, update_trust_score, _trust_tier
from gating.razorpay_client import create_test_order
from gating.upsell import get_upsell_suggestion
from gating.campaign import (
    get_campaign_for_product,
    get_active_campaigns,
    compute_discount,
)
from catalog import store as catalog_store
from logs.audit import log_event

load_dotenv()

# Re-export so older callers (main.py) keep working unchanged.
load_catalog = catalog_store.load_catalog

MODEL = "gemini-3.5-flash-lite"
# A separately-rate-limited model to fall over to when the primary model is
# overloaded (503 UNAVAILABLE) or rate-limited, so a turn can still complete.
FALLBACK_MODEL = "gemini-3.5-flash-lite"
# Measured live latency to the Gemini API is ~11s per request on this
# network, so the timeout must be generous. NOTE: google-genai's
# `types.HttpOptions.timeout` is in MILLISECONDS (the SDK divides by 1000),
# so we convert from seconds here.
MODEL_HTTP_TIMEOUT_SECONDS = 60

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY"),
    http_options=types.HttpOptions(timeout=MODEL_HTTP_TIMEOUT_SECONDS * 1000),
)

# Shared generation config so the primary and fallback sessions behave the same.
def _catalog_snapshot() -> str:
    """Compact catalog facts baked into the system prompt so simple catalog
    queries can be answered in ONE Gemini round-trip instead of two
    (browse_catalog round-trip + synthesis round-trip). Every round-trip is
    the dominant latency cost (measured ~1.8s each), so this halves the wait
    for common demo queries. Live stock/price is always re-validated by the
    tools during purchases, so the snapshot is a speed hint, never authority.
    """
    try:
        products = catalog_store.load_catalog().get("products", [])
    except Exception:
        return ""
    if not products:
        return ""
    lines = [
        "Current catalog snapshot (use browse_catalog for a fresh view; live "
        "stock is always re-checked before any purchase):"
    ]
    for p in products:
        lines.append(
            f"- {p.get('id')}: {p.get('name')} | {p.get('category')} | Rs{p.get('price')} | stock {p.get('stock')}"
        )
    return "\n".join(lines)


def _generation_config():
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT + "\n\n" + _catalog_snapshot(),
        tools=TOOLS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
        # Lower output budget + tight sampling speeds up every Gemini
        # round-trip, which is the dominant cost of a turn.
        max_output_tokens=256,
        temperature=0.4,
        top_p=0.9,
    )

SYSTEM_PROMPT = """You are the shopping agent for "Urban Threads Apparel", an Indian \
apparel merchant. You help the human user find and buy products.

Your abilities and etiquette:
1. browse_catalog to find products. Prices are INR.
2. request_purchase to buy. This is a gated money action - the merchant's \
policy engine may allow, block, or require explicit human confirmation. If a \
purchase is blocked, explain the reason honestly and never retry the same \
purchase without addressing the blocker or getting real user confirmation.
3. After EVERY successful purchase, call suggest_upsell for that product. If \
it returns a suggestion, offer it to the user in one short sentence. Only buy \
the suggested item if the user says yes, and pass is_upsell=true then.
4. Call get_campaign_offers when browsing or whenever relevant: scarce items \
have clearance discounts. Mention the pitch text verbatim-ish. Pass \
apply_campaign_discount=true when the user agrees to buy a discounted item.

Be concise, friendly, and always state prices in rupees."""

# ---- Gemini function-calling schema (OpenAPI-compatible JSON schema) ----
TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="browse_catalog",
            description="Search the merchant's product catalog, optionally filtered by category or max price.",
            parameters={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Optional category filter"},
                    "max_price": {"type": "number", "description": "Optional max price in INR"},
                },
            },
        ),
        types.FunctionDeclaration(
            name="suggest_upsell",
            description=(
                "After a successful purchase of a product, get one complementary "
                "cross-sell suggestion (e.g. socks after shoes) to offer the user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "purchased_product_id": {"type": "string", "description": "Id of the product just purchased"},
                },
                "required": ["purchased_product_id"],
            },
        ),
        types.FunctionDeclaration(
            name="get_campaign_offers",
            description=(
                "List active clearance campaigns: low-stock products the merchant "
                "wants to move, with their discounts. Use these pitches with the user."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="request_purchase",
            description=(
                "Attempt to purchase a specific product by id. This is a gated money action: "
                "it will be checked against merchant policy (spend limits, stock, category rules) "
                "before executing. It may be blocked or require confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "The product id to purchase"},
                    "user_confirmed": {
                        "type": "boolean",
                        "description": "Whether the human user has explicitly confirmed this purchase. Default false.",
                    },
                    "apply_campaign_discount": {
                        "type": "boolean",
                        "description": "Buy at the clearance campaign price if the product is campaign-eligible.",
                    },
                    "is_upsell": {
                        "type": "boolean",
                        "description": "True when buying an item you suggested as an upsell and the user accepted.",
                    },
                },
                "required": ["product_id"],
            },
        ),
    ]),
]


def _public(product: dict) -> dict:
    """Trim a catalog row down to the fields the LLM needs to see."""
    return {k: product[k] for k in ("id", "name", "category", "price", "stock", "description")}


class MerchantAgent:
    """
    One instance == one conversation with memory:
    - SessionState feeds the policy engine's per-session rules
    - `contents` keeps the Gemini conversation history across turns
    - `last_suggestion` remembers what was offered so upsells can be
      attributed correctly even if the model forgets to pass is_upsell.
    """

    def __init__(self):
        self.session = SessionState()
        self.chat_session = client.chats.create(
            model=MODEL,
            config=_generation_config(),
        )
        self._switched_to_fallback = False
        self.last_suggestion: dict | None = None  # {'for': pid, 'product': {...}}
        self._turn_products: list[dict] = []       # products seen this turn (for UI cards)
        self._turn_actions: list[dict] = []        # purchase/blocked actions this turn

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def tool_browse_catalog(self, category=None, max_price=None):
        catalog = catalog_store.load_catalog()
        products = catalog["products"]
        if category:
            products = [p for p in products if p["category"] == category]
        if max_price is not None:
            products = [p for p in products if p["price"] <= max_price]
        public = [_public(p) for p in products]
        # Collect for UI rendering
        for p in public:
            self._turn_products.append({**p, "source": "catalog"})
        log_event("browse_catalog", {
            "filters": {"category": category, "max_price": max_price},
            "results_count": len(products),
        })
        update_trust_score(self.session, "browse")
        return public

    def tool_suggest_upsell(self, purchased_product_id):
        product = catalog_store.get_product(purchased_product_id)
        if product is None:
            return {"status": "error", "reason": f"No product with id {purchased_product_id}"}

        suggestion = get_upsell_suggestion(product, catalog_store.load_catalog())
        if suggestion is None:
            self.last_suggestion = None
            log_event("upsell_suggested", {"for_product": purchased_product_id, "suggestion": None})
            return {"status": "no_suggestion", "note": "No natural complement found."}

        self.last_suggestion = {"for": purchased_product_id, "product": suggestion["product"]}
        # Collect for UI rendering
        self._turn_products.append({
            **_public(suggestion["product"]),
            "source": "upsell",
            "reason": suggestion["reason"],
        })
        payload = {
            "status": "suggestion",
            "product": _public(suggestion["product"]),
            "reason": suggestion["reason"],
            "note": "Offer this to the user. Buy it only if they accept, with is_upsell=true.",
        }
        log_event("upsell_suggested", {
            "for_product": purchased_product_id,
            "suggested_product_id": suggestion["product"]["id"],
            "reason": suggestion["reason"],
        })
        return payload

    def tool_get_campaign_offers(self):
        offers = get_active_campaigns(catalog_store.load_catalog())
        log_event("campaign_offer_surfaced", {"offers_count": len(offers)})
        return {"active_campaigns": offers}

    def tool_request_purchase(
        self,
        product_id,
        user_confirmed=False,
        apply_campaign_discount=False,
        is_upsell=False,
    ):
        if (not is_upsell and self.last_suggestion
                and self.last_suggestion["product"]["id"] == product_id):
            is_upsell = True

        product = catalog_store.get_product(product_id)
        if product is None:
            result = {"status": "error", "reason": f"No product with id {product_id}"}
            log_event("purchase_attempt", {"product_id": product_id, "result": result})
            return result

        pricing = {
            "original_price": product["price"],
            "discount_amount": 0,
            "campaign": False,
        }
        charge_price = product["price"]
        if apply_campaign_discount:
            offer = get_campaign_for_product(product_id, catalog_store.load_catalog())
            if offer is None:
                return {
                    "status": "error",
                    "reason": f"{product['name']} is not part of any active campaign; buy at list price.",
                }
            d = compute_discount(product)
            charge_price = d["final_price"]
            pricing.update(discount_amount=d["discount_amount"], campaign=True)

        evaluated_product = dict(product, price=charge_price)

        decision = evaluate_transaction(evaluated_product, self.session, user_confirmed=user_confirmed)
        log_event("policy_decision", {
            "product_id": product_id,
            "product_name": product["name"],
            "price": charge_price,
            "original_price": pricing["original_price"],
            "discount_amount": pricing["discount_amount"],
            "upsell": is_upsell,
            "campaign": pricing["campaign"],
            "allowed": decision.allowed,
            "reason": decision.reason,
            "requires_confirmation": decision.requires_confirmation,
            "rule_triggered": decision.rule_triggered,
            "mandate": decision.mandate,
            "trust_score": self.session.trust_score,
            "trust_tier": _trust_tier(self.session.trust_score),
        })

        if not decision.allowed:
            update_trust_score(self.session, "blocked")
            result = {
                "status": "blocked",
                "reason": decision.reason,
                "requires_confirmation": decision.requires_confirmation,
            }
            self._turn_actions.append({
                "action": "blocked",
                "product_id": product_id,
                "product_name": product["name"],
                "price": charge_price,
                "reason": decision.reason,
                "rule_triggered": decision.rule_triggered,
            })
            if is_upsell and decision.allowed is False:
                pass
            return result

        try:
            order = create_test_order(
                amount_inr=charge_price,
                receipt=f"receipt_{uuid.uuid4().hex[:8]}",
                notes={
                    "product_id": product_id,
                    "product_name": product["name"],
                    "upsell": str(is_upsell),
                    "campaign_discounted": str(pricing["campaign"]),
                },
            )

            self.session.transactions_this_session += 1
            self.session.total_spent += charge_price
            updated = catalog_store.decrement_stock(product_id)
            update_trust_score(self.session, "purchased")

            result = {
                "status": "success",
                "order_id": order["id"],
                "amount_charged": charge_price,
                "original_price": pricing["original_price"],
                "discount_applied": pricing["discount_amount"],
                "was_upsell": is_upsell,
                "was_campaign": pricing["campaign"],
                "product": product["name"],
                "stock_left": updated["stock"],
            }
            log_event("purchase_executed", {
                "product_id": product_id,
                "product_name": product["name"],
                "amount": charge_price,
                "original_price": pricing["original_price"],
                "discount_amount": pricing["discount_amount"],
                "upsell": is_upsell,
                "campaign": pricing["campaign"],
                "stock_left": updated["stock"],
                "order": result,
            })
            self.last_suggestion = None
            self._turn_actions.append({
                "action": "purchased",
                "product_id": product_id,
                "product_name": product["name"],
                "price": charge_price,
                "original_price": pricing["original_price"],
                "discount": pricing["discount_amount"],
                "order_id": order["id"],
                "stock_left": updated["stock"],
                "was_upsell": is_upsell,
                "was_campaign": pricing["campaign"],
            })
            return result

        except Exception as e:
            result = {"status": "error", "reason": f"Razorpay execution failed: {str(e)}"}
            log_event("purchase_failed", {"product_id": product_id, "error": str(e)})
            return result

    def record_user_rejection(self, product_id):
        """Audit trail entry when the human declines an offered upsell."""
        log_event("upsell_declined", {"product_id": product_id})

    def _synthesis_fallback(self):
        """Build a plain-English summary of what actually happened this turn.

        Used when the final Gemini synthesis call fails (rate limit / 503)
        AFTER our tools already executed and hit the audit trail, so the user
        always gets a reply reflecting real work instead of silence. Returns
        None when nothing happened (no tools ran) so the caller keeps the
        existing graceful-error contract.
        """
        lines = []
        for a in self._turn_actions:
            name = a.get("product_name", "item")
            if a.get("action") == "purchased":
                lines.append(
                    f"Purchased the {name} for \u20B9{a.get('price', '?')} "
                    f"(order {a.get('order_id', '?')})."
                )
            elif a.get("action") == "blocked":
                lines.append(
                    f"Blocked the {name} — {a.get('reason', 'policy did not allow it')}."
                )
        if lines:
            return "Done. " + " ".join(lines)
        browsed = len(self._turn_products)
        if browsed:
            return f"Done. Browsed {browsed} product(s) from the catalog."
        return None

    def _offline_fallback_response(self, user_message: str) -> tuple[str, dict]:
        """Return a helpful response when Gemini is unavailable.

        This keeps the app usable end-to-end even if the model endpoint is
        timing out or the key/quota is temporarily unavailable.
        """
        lowered = user_message.lower()
        products: list[dict] = []

        if any(token in lowered for token in ("browse", "catalog", "show", "list", "what do you have")):
            products = self.tool_browse_catalog()
            reply = (
                "I’m in offline mode right now, so I can still show the catalog. "
                "Ask for a category or product name and I’ll narrow it down."
            )
        elif any(token in lowered for token in ("policy", "rules", "confirm")):
            reply = (
                "I’m in offline mode right now. The merchant allows apparel, footwear, "
                "and accessories, blocks out-of-stock or over-limit items, and asks for "
                "confirmation above the threshold shown in /policy."
            )
        elif any(token in lowered for token in ("buy", "purchase", "order")):
            products = self.tool_browse_catalog()
            reply = (
                "I’m in offline mode right now. Pick a product from the catalog and I’ll "
                "help you check the purchase rules and price boundaries."
            )
        else:
            products = self.tool_browse_catalog()
            reply = (
                "I’m in offline mode right now, but I can still help with the catalog "
                "and policy. Try asking for products under a budget or a category."
            )

        return reply, {"products": self._turn_products or products, "actions": self._turn_actions}

    # ------------------------------------------------------------------
    # Fast answers (no LLM round-trip)
    # ------------------------------------------------------------------

    _FAST_STOP = {
        "and", "the", "a", "an", "of", "for", "with", "within", "under",
        "below", "budget", "rupees", "price", "show", "list", "browse",
        "display", "catalog", "some", "buy", "what", "have", "you",
        "products", "product", "me", "any", "inr",
    }

    def _fast_answer(self, user_message: str) -> tuple[str, dict] | None:
        """Answer the highest-frequency demo queries instantly from LOCAL data
        (catalog / budget / campaigns / policy), with NO Gemini round-trip.

        The LLM path remains the fallback for everything else. This is what
        makes the agent feel instant: measured Gemini round-trips take
        1.8-11s each, while every branch here runs in milliseconds.
        """
        lowered = user_message.lower().strip()

        # 1) Campaigns / clearance discounts
        if any(k in lowered for k in ("discount", "campaign", "clearance", "offer", "deal", "sale")):
            offers = get_active_campaigns(catalog_store.load_catalog())
            if not offers:
                return ("No active clearance campaigns right now - everything is at list price.",
                        {"products": [], "actions": []})
            lines = ["Active clearance campaigns:"]
            for o in offers:
                lines.append(f"- {o['pitch']}")
            return ("\n".join(lines), {"products": [], "actions": []})

        # 2) Policy / rules / confirmation / limits
        if any(k in lowered for k in ("policy", "rules", "rule", "confirm", "blocked", "limit",
                                      "spend", "allowed", "threshold")):
            from gating.policy import (
                MAX_TRANSACTION_AMOUNT, CONFIRMATION_THRESHOLD,
                ALLOWED_CATEGORIES, MAX_TRANSACTIONS_PER_SESSION,
            )
            cats = ", ".join(sorted(ALLOWED_CATEGORIES))
            return (
                f"I can sell {cats}. Orders up to Rs{MAX_TRANSACTION_AMOUNT:,.0f} go straight "
                f"through; above Rs{CONFIRMATION_THRESHOLD:,.0f} I'll ask you to confirm, and I "
                f"cap at {MAX_TRANSACTIONS_PER_SESSION} orders per session. Out-of-stock items "
                "are always blocked. Want me to find something?"
                f"",
                {"products": [], "actions": []},
            )

        # 3) Catalog browse / list / budget
        min_price = None
        max_price = None

        m_min = re.search(r"(?:above|over|more than|greater than|at least|minimum of)\s*(?:rs\.?|inr\s*)?(\d+)", lowered)
        if m_min:
            min_price = int(m_min.group(1))
        m_max = re.search(r"(?:under|below|within|at most|maximum of|budget of|for|less than)\s*(?:rs\.?|inr\s*)?(\d+)", lowered)
        if m_max:
            max_price = int(m_max.group(1))

        is_browse = bool(re.search(
            r"(browse|show|list|display|catalog|products?|available|what do you have|"
            r"(?:above|over|more than|greater than|at least)\s*\d+|"
            r"(?:under|below|within|budget|for|at most)\s*\d+|\d+\s*rupees|\d+\s*rs)",
            lowered,
        ))
        if is_browse:
            products = self.tool_browse_catalog(max_price=max_price)
            if min_price is not None:
                products = [p for p in products if p["price"] >= min_price]
            if not products:
                return ("I couldn't find products in that range. What's your budget?",
                        {"products": self._turn_products, "actions": self._turn_actions})

            tokens = re.findall(r"[a-zA-Z]+", lowered)
            terms = [t for t in tokens if t not in self._FAST_STOP and len(t) >= 3]
            if terms:
                matched = [p for p in products if any(t in p["name"].lower() for t in terms)]
                if matched:
                    products = matched

            campaigns = get_active_campaigns(catalog_store.load_catalog())
            offer_map = {o["product_id"]: o for o in campaigns}
            lines = []
            for p in products:
                offer = offer_map.get(p["id"])
                sale = f" ({offer['discount_pct']}% off, Rs{offer['final_price']} today)" if offer else ""
                lines.append(f"- {p['name']} - Rs{p['price']}{sale} (stock {p['stock']})")
            if len(lines) == 1:
                reply = "Here's what matches: " + lines[0]
            else:
                reply = "Here's what I found:\n" + "\n".join(lines)
            return (reply, {"products": products, "actions": self._turn_actions})

        return None

    # ------------------------------------------------------------------
    # Conversation loop (Gemini function calling via Chat API)
    # ------------------------------------------------------------------

    def chat(self, user_message: str) -> tuple[str, dict]:
        """Return (text_reply, structured_data) where structured_data contains
        products and actions seen during this conversation turn."""
        self._turn_products = []
        self._turn_actions = []
        fast = self._fast_answer(user_message)
        if fast is not None:
            return fast
        try:
            # Guard against an unbounded agentic loop: if the model keeps
            # emitting tool calls instead of converging to a final answer, we
            # bail after a bounded number of rounds so a turn can never hang.
            turns = 0
            while True:
                turns += 1
                if turns > 4:
                    return ("(agent stopped: too many tool calls in a row, asked for a summary)",
                            {"products": self._turn_products,
                             "actions": self._turn_actions})
                response = None
                for attempt in range(2):
                    try:
                        response = self.chat_session.send_message(user_message)
                        break
                    except Exception as e:
                        err_str = str(e)
                        is_retriable = any(
                            k in err_str
                            for k in (
                                "RESOURCE_EXHAUSTED",
                                "429",
                                "UNAVAILABLE",
                                "503",
                                "timed out",
                                "timeout",
                                "DeadlineExceeded",
                                "deadline exceeded",
                            )
                        )
                        if is_retriable:
                            # Fail over immediately to a separately rate-limited
                            # model and keep the existing conversation history.
                            # We do not sleep here because the goal is to keep a
                            # slow or unavailable Gemini backend from stalling
                            # the request path.
                            if not self._switched_to_fallback:
                                history = self.chat_session.get_history()
                                self.chat_session = client.chats.create(
                                    model=FALLBACK_MODEL,
                                    config=_generation_config(),
                                    history=history,
                                )
                                self._switched_to_fallback = True
                                continue
                            response = None
                            break
                        raise
                if response is None:
                    fallback = self._synthesis_fallback()
                    if fallback is None:
                        return self._offline_fallback_response(user_message)
                    # Tools already ran fine but the final LLM wrap-up failed:
                    # still give the user an accurate summary of what happened.
                    return (fallback, {"products": self._turn_products,
                                       "actions": self._turn_actions})

                candidate = response.candidates[0]
                parts = candidate.content.parts if candidate.content else []

                # Extract text parts as agent reasoning (captured for explainability)
                reasoning_text = ""
                function_call_parts = []
                for part in parts:
                    if part.text:
                        reasoning_text += part.text
                    elif part.function_call:
                        function_call_parts.append(part)

                # If no function calls, return final text response
                if not function_call_parts:
                    return (reasoning_text or "", {
                        "products": self._turn_products,
                        "actions": self._turn_actions,
                    })

                # Execute each function call
                for fc_part in function_call_parts:
                    fc = fc_part.function_call
                    func_name = fc.name
                    args = dict(fc.args) if fc.args else {}

                    # Log agent reasoning alongside the tool call (explainability layer)
                    log_event("agent_reasoning", {
                        "tool_name": func_name,
                        "reasoning": reasoning_text or None,
                        "args": args,
                    })

                    if func_name == "browse_catalog":
                        result = self.tool_browse_catalog(**args)
                    elif func_name == "suggest_upsell":
                        result = self.tool_suggest_upsell(**args)
                    elif func_name == "get_campaign_offers":
                        result = self.tool_get_campaign_offers()
                    elif func_name == "request_purchase":
                        result = self.tool_request_purchase(**args)
                    else:
                        result = {"error": f"Unknown tool {func_name}"}

                    # Send function result back to the model
                    user_message = types.Part.from_function_response(
                        name=func_name,
                        response={"result": result},
                    )

        except Exception as e:
            return self._offline_fallback_response(user_message)


def run_agent(user_message: str):
    """CLI wrapper - one throwaway conversation, printed to stdout."""
    agent = MerchantAgent()
    log_event("session_start", {"user_message": user_message})
    reply, structured = agent.chat(user_message)
    log_event("session_end", {"final_response": reply})
    print("\nAgent:", reply)
    if structured.get("products"):
        print("\nProducts seen:", len(structured["products"]))
    if structured.get("actions"):
        print("Actions taken:", len(structured["actions"]))


if __name__ == "__main__":
    user_input = " ".join(sys.argv[1:]) or "I want to buy a t-shirt under 600 rupees"
    run_agent(user_input)
