"""
Unit tests for Razorpay failure handling.
Mocks the Razorpay client to confirm the agent catches exceptions gracefully
and logs a purchase_failed event instead of crashing.

A controlled, in-memory catalog store patches the real on-disk store so these
tests are deterministic and never mutate catalog/products.json (running them
against live stock slowly sold out prod_004 and made the suite state-dependent).
"""

import pytest
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.policy import SessionState
from agent.agent import MerchantAgent


PRODUCTS = [
    {"id": "prod_001", "name": "Classic Blue T-Shirt", "price": 499, "category": "apparel", "stock": 11},
    {"id": "prod_004", "name": "Canvas Tote Bag", "price": 349, "category": "accessories", "stock": 99},
    {"id": "prod_012", "name": "Designer Sunglasses", "price": 3499, "category": "accessories", "stock": 6},
]


class FakeStore:
    """In-memory stand-in for catalog.store with guaranteed fresh stock."""
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


@pytest.fixture
def agent():
    """Fresh MerchantAgent with a mocked Chat session + fake store, kept patched
    (in-memory) for the whole test so purchases never touch the on-disk catalog."""
    fake_store = FakeStore(PRODUCTS)
    with patch("agent.agent.client"), patch("agent.agent.catalog_store", fake_store):
        a = MerchantAgent()
        a.catalog_store = fake_store
        yield a


class TestRazorpayFailure:
    def test_purchase_failure_logged(self, agent):
        """When Razorpay raises, a purchase_failed event is logged, not a crash."""
        with patch("agent.agent.create_test_order", side_effect=Exception("Razorpay timeout")):
            result = agent.tool_request_purchase("prod_004")
            assert result["status"] == "error"
            assert "Razorpay" in result["reason"]

    def test_purchase_failure_does_not_crash(self, agent):
        with patch("agent.agent.create_test_order", side_effect=RuntimeError("network error")):
            result = agent.tool_request_purchase("prod_001")
            assert isinstance(result, dict)
            assert result["status"] == "error"

    def test_stock_not_decremented_on_failure(self, agent):
        with patch("agent.agent.create_test_order", side_effect=Exception("fail")):
            result = agent.tool_request_purchase("prod_004")
            assert result["status"] == "error"
        # In-memory store stock must be unchanged after a failure.
        assert next(p for p in agent.catalog_store.products if p["id"] == "prod_004")["stock"] == 99

    def test_policy_still_blocks_before_razorpay(self, agent):
        """Over-limit products get blocked by policy BEFORE reaching Razorpay."""
        result = agent.tool_request_purchase("prod_012")  # Sunglasses Rs.3499
        assert result["status"] == "blocked"
        assert "Rs.3000" in result["reason"] or "3000" in result["reason"]

    def test_unknown_product_error(self, agent):
        result = agent.tool_request_purchase("nonexistent_id")
        assert result["status"] == "error"
        assert "nonexistent_id" in result["reason"]

    def test_successful_purchase_with_mock(self, agent):
        mock_order = {"id": "order_test_123"}
        with patch("agent.agent.create_test_order", return_value=mock_order):
            result = agent.tool_request_purchase("prod_004")
            assert result["status"] == "success"
            assert result["order_id"] == "order_test_123"
            assert result["amount_charged"] == 349
