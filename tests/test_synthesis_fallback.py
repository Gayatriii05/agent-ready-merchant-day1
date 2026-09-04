"""
Tests for the "audit updated but no reply" fallback in agent.agent.

When our tools execute successfully (browse / purchase / policy) and land in
the audit trail, but the FINAL Gemini synthesis call that turns tool results
into a reply fails, chat() must still return a non-empty, accurate summary
built directly from the executed actions/products. If nothing ran, the
existing graceful-error contract is preserved.
"""

import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.agent as agent_mod
from agent.agent import MerchantAgent


class FakeStore:
    """In-memory catalog store so purchases don't touch the real products.json."""
    def __init__(self, products):
        self.products = [dict(p) for p in products]

    def get_product(self, product_id):
        return next((dict(p) for p in self.products if p["id"] == product_id), None)

    def load_catalog(self):
        return {"products": [dict(p) for p in self.products]}

    def decrement_stock(self, product_id):
        for p in self.products:
            if p["id"] == product_id:
                if p["stock"] > 0:
                    p["stock"] -= 1
                return dict(p)
        raise KeyError(f"No product with id {product_id}")


PRODUCTS = [
    {"id": "prod_004", "name": "Canvas Tote Bag", "price": 349, "category": "accessories", "stock": 99, "description": "A roomy canvas tote."},
]


def _tool_call(name, args):
    """A chat response that asks for ONE function call, plus no text."""
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text="", function_call=SimpleNamespace(name=name, args=args or {}))
        ]))]
    )


class AlwaysDown:
    def send_message(self, m):
        raise Exception("503 UNAVAILABLE - high demand")

    def get_history(self):
        return []


class ToolThenFail:
    """Primary chat: first send returns a tool call, later sends raise 503."""
    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.calls = 0
        self.history = []

    def send_message(self, m):
        self.calls += 1
        if self.calls == 1:
            return _tool_call(self.name, self.args)
        raise Exception("503 UNAVAILABLE - high demand")

    def get_history(self):
        return self.history


def _make_client(create_fn):
    client = MagicMock()
    client.chats.create.side_effect = create_fn
    return client


def test_purchase_synthesis_failure_returns_summary():
    """Purchase tool runs, then the final LLM call fails -> accurate summary."""
    fake_store = FakeStore(PRODUCTS)

    created = []

    def create_fn(model=None, config=None, history=None):
        created.append(model)
        # First create call = primary session, later creates = fallback.
        if len(created) > 1:
            return AlwaysDown()
        return ToolThenFail("request_purchase", {"product_id": "prod_004"})

    client = _make_client(create_fn)
    with patch("agent.agent.client", client), \
         patch("agent.agent.catalog_store", fake_store), \
         patch("agent.agent.create_test_order", return_value={"id": "order_test_123"}), \
         patch("agent.agent.time.sleep"):
        agent = MerchantAgent()
        reply, structured = agent.chat("buy the tote bag")

    assert isinstance(reply, str) and len(reply) > 0
    assert "Purchased" in reply
    assert "Canvas Tote Bag" in reply
    assert structured["actions"] and structured["actions"][0]["action"] == "purchased"


def test_browse_synthesis_failure_returns_summary():
    """Browse tool runs, then the final LLM call fails -> browsed summary."""
    fake_store = FakeStore(PRODUCTS)

    created = []

    def create_fn(model=None, config=None, history=None):
        created.append(model)
        # First create call = primary session, later creates = fallback.
        if len(created) > 1:
            return AlwaysDown()
        return ToolThenFail("browse_catalog", {})

    client = _make_client(create_fn)
    with patch("agent.agent.client", client), \
         patch("agent.agent.catalog_store", fake_store), \
         patch("agent.agent.time.sleep"):
        agent = MerchantAgent()
        reply, structured = agent.chat("recommend something nice for me")

    assert isinstance(reply, str) and len(reply) > 0
    assert "Browsed" in reply
    assert "1" in reply
    assert structured["products"]


def test_no_tools_synthesis_failure_uses_offline_fallback():
    """If nothing ran before the LLM failed, return the offline-mode fallback."""
    def create_fn(model=None, config=None, history=None):
        return AlwaysDown()

    client = _make_client(create_fn)
    with patch("agent.agent.client", client), \
         patch("agent.agent.time.sleep"):
        agent = MerchantAgent()
        reply, structured = agent.chat("hi")

    assert isinstance(reply, str)
    assert "offline mode" in reply.lower()
    assert isinstance(structured, dict)
