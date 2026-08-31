"""
Buyer Agent
-----------
A lightweight AI buyer that simulates a multi-item purchase session against
the MerchantAgent's catalog, policy gate, and audit trail. Used for the
"Simulate AI Buyer" demo feature where two agents negotiate programmatically
without human chat input.

The BuyerAgent has:
- A fixed budget (e.g. Rs.2000)
- A shopping goal ("buy an outfit under the budget")
- It browses the catalog, selects items, and attempts purchases
- Each purchase goes through the same policy gate as human-initiated buys
- All events are logged with event_type "agent_to_agent" for visual distinction
"""

import json
from catalog.store import load_catalog
from gating.policy import evaluate_transaction, SessionState
from gating.razorpay_client import create_test_order
from gating.upsell import get_upsell_suggestion
from logs.audit import log_event

import uuid


class BuyerAgent:
    """
    A budget-constrained buyer agent that programmatically selects and
    purchases items, producing a negotiation log for demo purposes.
    """

    def __init__(self, budget: int = 2000, goal: str = "buy a complete outfit"):
        self.budget = budget
        self.spent = 0
        self.goal = goal
        self.session = SessionState()
        self.purchases = []
        self.rejections = []
        self.log = []

    def _log(self, msg: str):
        self.log.append(msg)

    def _a2a(self, event_type: str, details: dict):
        """Log with agent_to_agent event type for visual distinction."""
        entry = log_event("agent_to_agent", {
            "sub_type": event_type,
            "buyer_id": "buyer_agent_001",
            **details,
        })
        return entry

    def run(self) -> dict:
        """
        Execute the full negotiation simulation:
        1. Announce the shopping goal
        2. Browse catalog within budget
        3. Strategically select items (outfit: top + bottom + accessory)
        4. Attempt purchases through the policy gate
        5. Handle blocks, confirmations, and budget constraints
        6. Return summary

        Returns a dict with the full log and purchase results.
        """
        self._log(f"BUYER AGENT activated. Goal: {self.goal} | Budget: Rs.{self.budget}")
        self._a2a("buyer_init", {
            "goal": self.goal,
            "budget": self.budget,
            "message": f"BuyerAgent started: '{self.goal}' with budget Rs.{self.budget}",
        })

        catalog = load_catalog()
        products = catalog["products"]

        # Filter products the buyer can afford
        affordable = [p for p in products if p["price"] <= self.budget and p["stock"] > 0]
        affordable.sort(key=lambda p: p["price"])

        self._log(f"Buyer browses catalog: {len(affordable)} items within budget of Rs.{self.budget}")
        self._a2a("browse_catalog", {
            "filters": {"max_price": self.budget, "in_stock": True},
            "results_count": len(affordable),
            "message": f"Buyer browsed catalog, found {len(affordable)} affordable in-stock items",
        })

        # Strategy: pick a top + accessory combo that fits the budget
        # Prioritize getting the most value within budget
        selected = self._select_items(affordable)

        if not selected:
            self._log("Buyer: No suitable items found within budget.")
            self._a2a("no_items_found", {
                "message": "Buyer could not find any items to purchase within budget",
            })
            return self._result()

        self._log(f"Buyer selected {len(selected)} items for purchase:")
        for p in selected:
            self._log(f"  - {p['name']} (Rs.{p['price']})")
        self._a2a("item_selection", {
            "items": [{"id": p["id"], "name": p["name"], "price": p["price"]} for p in selected],
            "total_planned": sum(p["price"] for p in selected),
            "message": f"Buyer selected {len(selected)} items totaling Rs.{sum(p['price'] for p in selected)}",
        })

        # Attempt to purchase each item
        for product in selected:
            self._attempt_purchase(product)
            if self.spent >= self.budget:
                self._log("Buyer: Budget exhausted, stopping.")
                self._a2a("budget_exhausted", {
                    "spent": self.spent,
                    "budget": self.budget,
                    "message": f"Buyer budget exhausted: spent Rs.{self.spent} of Rs.{self.budget}",
                })
                break

        # Summary
        self._log(f"Buyer session complete. Spent: Rs.{self.spent}/{self.budget}. Items: {len(self.purchases)}")
        self._a2a("buyer_complete", {
            "total_spent": self.spent,
            "budget": self.budget,
            "items_purchased": len(self.purchases),
            "items_rejected": len(self.rejections),
            "message": (
                f"Buyer session done: {len(self.purchases)} items purchased "
                f"for Rs.{self.spent}, {len(self.rejections)} rejected"
            ),
        })

        return self._result()

    def _select_items(self, affordable: list) -> list:
        """Simple greedy strategy: pick items from different categories to build an outfit."""
        categories_wanted = ["apparel", "footwear", "accessories"]
        selected = []
        remaining_budget = self.budget

        for cat in categories_wanted:
            candidates = [p for p in affordable if p["category"] == cat and p["price"] <= remaining_budget]
            if candidates:
                # Pick the most expensive one we can afford (maximize value)
                pick = candidates[-1]
                selected.append(pick)
                remaining_budget -= pick["price"]

        return selected

    def _attempt_purchase(self, product: dict):
        """Try to buy one product through the policy gate."""
        price = product["price"]

        self._log(f"Buyer requests purchase: {product['name']} (Rs.{price})")
        self._a2a("purchase_request", {
            "product_id": product["id"],
            "product_name": product["name"],
            "price": price,
            "budget_remaining": self.budget - self.spent,
            "message": f"Buyer requests: '{product['name']}' at Rs.{price} (budget left: Rs.{self.budget - self.spent})",
        })

        # Policy evaluation (no user confirmation - agent-to-agent is fully automated)
        decision = evaluate_transaction(product, self.session, user_confirmed=False)

        if not decision.allowed:
            reason = decision.reason
            self._log(f"Merchant policy BLOCKED: {reason}")
            self._a2a("purchase_blocked", {
                "product_id": product["id"],
                "product_name": product["name"],
                "price": price,
                "reason": reason,
                "rule_triggered": decision.rule_triggered,
                "requires_confirmation": decision.requires_confirmation,
                "mandate": decision.mandate,
                "message": f"Merchant policy blocked '{product['name']}': {reason}",
            })
            self.rejections.append({"product": product["name"], "reason": reason})
            return

        # Would be confirmed in a real agent-to-agent flow
        self._a2a("policy_approved", {
            "product_id": product["id"],
            "product_name": product["name"],
            "price": price,
            "mandate": decision.mandate,
            "message": f"Merchant policy approved '{product['name']}' at Rs.{price}",
        })

        # Execute purchase via Razorpay test mode
        import random
        receipt = f"buyer_{uuid.uuid4().hex[:8]}"
        try:
            order = create_test_order(
                amount_inr=price,
                receipt=receipt,
                notes={
                    "product_id": product["id"],
                    "product_name": product["name"],
                    "buyer_agent": "true",
                },
            )

            self.session.transactions_this_session += 1
            self.session.total_spent += price
            self.spent += price

            from catalog.store import decrement_stock
            updated = decrement_stock(product["id"])

            self.purchases.append({
                "product_id": product["id"],
                "product_name": product["name"],
                "amount": price,
                "order_id": order["id"],
            })

            self._log(f"Purchase SUCCESS: {product['name']} charged Rs.{price} (Order: {order['id']})")
            self._a2a("purchase_executed", {
                "product_id": product["id"],
                "product_name": product["name"],
                "amount": price,
                "order_id": order["id"],
                "stock_left": updated["stock"],
                "budget_remaining": self.budget - self.spent,
                "message": f"Buyer purchased '{product['name']}' for Rs.{price} (budget left: Rs.{self.budget - self.spent})",
            })

            # Check for upsell opportunity
            upsell = get_upsell_suggestion(product, load_catalog())
            if upsell and upsell["product"]["price"] <= (self.budget - self.spent):
                self._log(f"Buyer considers upsell: {upsell['product']['name']} (Rs.{upsell['product']['price']})")
                self._a2a("upsell_considered", {
                    "for_product": product["id"],
                    "suggested_product": upsell["product"]["name"],
                    "suggested_price": upsell["product"]["price"],
                    "reason": upsell["reason"],
                    "message": f"Buyer considers upsell: '{upsell['product']['name']}' at Rs.{upsell['product']['price']}",
                })

        except Exception as e:
            self._log(f"Purchase FAILED: {product['name']} - {e}")
            self._a2a("purchase_failed", {
                "product_id": product["id"],
                "product_name": product["name"],
                "error": str(e),
                "message": f"Buyer purchase of '{product['name']}' failed: {e}",
            })

    def _result(self) -> dict:
        return {
            "budget": self.budget,
            "total_spent": self.spent,
            "items_purchased": len(self.purchases),
            "items_rejected": len(self.rejections),
            "purchases": self.purchases,
            "rejections": self.rejections,
            "log": self.log,
        }
