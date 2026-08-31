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
import uuid
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from dotenv import load_dotenv

from gating.policy import evaluate_transaction, SessionState
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

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-3.5-flash-lite"

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
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=TOOLS,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
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
        })

        if not decision.allowed:
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

    # ------------------------------------------------------------------
    # Conversation loop (Gemini function calling via Chat API)
    # ------------------------------------------------------------------

    def chat(self, user_message: str) -> tuple[str, dict]:
        """Return (text_reply, structured_data) where structured_data contains
        products and actions seen during this conversation turn."""
        self._turn_products = []
        self._turn_actions = []
        try:
            while True:
                response = None
                for attempt in range(3):
                    try:
                        response = self.chat_session.send_message(user_message)
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
                            wait = min(15, 5 * (attempt + 1))
                            time.sleep(wait)
                            continue
                        raise
                if response is None:
                    return ("(agent error, handled gracefully: rate limit exceeded after retries)",
                            {"products": [], "actions": []})

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
            return (f"(agent error, handled gracefully: {e})", {
                "products": self._turn_products,
                "actions": self._turn_actions,
            })


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
