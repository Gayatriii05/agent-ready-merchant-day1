"""
Tests for the fast-answer path (`MerchantAgent._fast_answer`).

Common demo queries (catalog browse, budget, above-price, campaigns, policy)
must be answered from LOCAL data in milliseconds with NO Gemini round-trip.
Anything else must still fall through to the normal LLM conversation path.
"""

import sys
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.agent import MerchantAgent


class FakeStore:
    """In-memory catalog store so fast answers don't touch products.json."""
    def __init__(self, products):
        self.products = [dict(p) for p in products]

    def get_product(self, product_id):
        return next((dict(p) for p in self.products if p["id"] == product_id), None)

    def load_catalog(self):
        return {"products": [dict(p) for p in self.products]}

    def decrement_stock(self, product_id):
        for p in self.products:
            if p["id"] == product_id:
                p["stock"] -= 1
                return dict(p)
        raise KeyError(product_id)


PRODUCTS = [
    {"id": "prod_001", "name": "Classic Blue T-Shirt", "price": 499, "category": "apparel", "stock": 11, "description": "A classic tee."},
    {"id": "prod_002", "name": "Black Denim Jacket", "price": 1899, "category": "apparel", "stock": 5, "description": "A denim jacket."},
    {"id": "prod_003", "name": "White Sneakers", "price": 499, "category": "footwear", "stock": 2, "description": "White sneakers."},
    {"id": "prod_004", "name": "Premium Wool Sweater", "price": 2599, "category": "apparel", "stock": 4, "description": "A wool sweater."},
]


class Boom:
    """A chat session that FAILS if the LLM path is ever reached."""
    def __init__(self):
        self.called = False

    def send_message(self, m):
        self.called = True
        raise AssertionError("fast-answer path must never call the LLM")


@contextmanager
def _agent(fake_store, boom):
    """Agent with the client + catalog patched for the WHOLE test body."""
    client = MagicMock()
    client.chats.create.return_value = boom
    with patch("agent.agent.client", client), \
         patch("agent.agent.catalog_store", fake_store):
        yield MerchantAgent(), boom


def test_browse_is_instant_no_llm():
    with _agent(FakeStore(PRODUCTS), Boom()) as (agent, boom):
        reply, structured = agent.chat("what do you have?")
    assert boom.called is False
    assert "T-Shirt" in reply
    assert structured["products"]


def test_budget_filter_no_llm():
    with _agent(FakeStore(PRODUCTS), Boom()) as (agent, boom):
        reply, structured = agent.chat("show me products under 600 rupees")
    assert boom.called is False
    assert structured["products"]
    assert all(p["price"] <= 600 for p in structured["products"])


def test_above_price_filter_no_llm():
    with _agent(FakeStore(PRODUCTS), Boom()) as (agent, boom):
        reply, structured = agent.chat("show me products above 1500 rupees")
    assert boom.called is False
    assert structured["products"]
    assert all(p["price"] >= 1500 for p in structured["products"])
    assert "T-Shirt" not in reply and "White Sneakers" not in reply
    assert "Wool Sweater" in reply


def test_campaign_fast_answer_no_llm():
    with _agent(FakeStore(PRODUCTS), Boom()) as (agent, boom):
        reply, structured = agent.chat("any discounts or offers today?")
    assert boom.called is False
    assert "off" in reply.lower()
    assert isinstance(structured, dict)


def test_policy_fast_answer_no_llm():
    with _agent(FakeStore(PRODUCTS), Boom()) as (agent, boom):
        reply, structured = agent.chat("what are your purchase rules and limits?")
    assert boom.called is False
    assert "confirm" in reply.lower() or "session" in reply.lower()
    assert isinstance(structured, dict)


def test_unmatched_query_goes_to_llm():
    with _agent(FakeStore(PRODUCTS), Boom()) as (agent, boom):
        reply, structured = agent.chat("recommend something nice for me")
    assert boom.called is True
    assert isinstance(reply, str)
    assert isinstance(structured, dict)