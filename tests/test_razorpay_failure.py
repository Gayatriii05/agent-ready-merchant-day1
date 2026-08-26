"""
Unit tests for Razorpay failure handling.
Mocks the Razorpay client to confirm the agent catches exceptions gracefully
and logs a purchase_failed event instead of crashing.
"""

import pytest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.policy import SessionState
from agent.agent import MerchantAgent


@pytest.fixture
def agent():
    """Create a fresh MerchantAgent with a mocked Chat session (no real API calls)."""
    with patch("agent.agent.client"):
        a = MerchantAgent()
    return a


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
        from catalog.store import load_catalog
        stock_before = next(p for p in load_catalog()["products"] if p["id"] == "prod_004")["stock"]
        with patch("agent.agent.create_test_order", side_effect=Exception("fail")):
            agent.tool_request_purchase("prod_004")
        stock_after = next(p for p in load_catalog()["products"] if p["id"] == "prod_004")["stock"]
        assert stock_after == stock_before  # stock unchanged on failure

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
