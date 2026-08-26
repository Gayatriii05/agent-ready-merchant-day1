"""
Unit tests for the policy gating engine.
Covers all 5 gating rules, the mandate field, and edge cases.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.policy import (
    evaluate_transaction,
    PolicyDecision,
    SessionState,
    MAX_TRANSACTION_AMOUNT,
    CONFIRMATION_THRESHOLD,
    ALLOWED_CATEGORIES,
    MAX_TRANSACTIONS_PER_SESSION,
    _build_mandate,
)


def _product(**overrides):
    """Build a minimal product dict for testing."""
    base = {"id": "test_001", "name": "Test Product", "category": "apparel", "price": 500, "stock": 10}
    base.update(overrides)
    return base


class TestCategoryAllowlist:
    def test_allowed_category_passes(self):
        for cat in ALLOWED_CATEGORIES:
            result = evaluate_transaction(_product(category=cat), SessionState())
            assert result.allowed is True

    def test_blocked_category(self):
        result = evaluate_transaction(_product(category="electronics"), SessionState())
        assert result.allowed is False
        assert result.rule_triggered == "category_allowlist"
        assert "electronics" in result.reason

    def test_empty_category_blocked(self):
        result = evaluate_transaction(_product(category=""), SessionState())
        assert result.allowed is False
        assert result.rule_triggered == "category_allowlist"


class TestStockCheck:
    def test_in_stock_passes(self):
        result = evaluate_transaction(_product(stock=5), SessionState())
        assert result.allowed is True

    def test_zero_stock_blocked(self):
        result = evaluate_transaction(_product(stock=0), SessionState())
        assert result.allowed is False
        assert result.rule_triggered == "stock_check"

    def test_negative_stock_blocked(self):
        result = evaluate_transaction(_product(stock=-1), SessionState())
        assert result.allowed is False
        assert result.rule_triggered == "stock_check"


class TestMaxTransactionAmount:
    def test_under_limit_passes(self):
        result = evaluate_transaction(
            _product(price=MAX_TRANSACTION_AMOUNT), SessionState(), user_confirmed=True
        )
        assert result.allowed is True

    def test_over_limit_blocked(self):
        result = evaluate_transaction(_product(price=MAX_TRANSACTION_AMOUNT + 1), SessionState())
        assert result.allowed is False
        assert result.rule_triggered == "max_transaction_amount"
        assert str(MAX_TRANSACTION_AMOUNT) in result.reason

    def test_exactly_at_limit_passes(self):
        result = evaluate_transaction(
            _product(price=3000), SessionState(), user_confirmed=True
        )
        assert result.allowed is True


class TestSessionRateLimit:
    def test_within_limit_passes(self):
        session = SessionState(transactions_this_session=MAX_TRANSACTIONS_PER_SESSION - 1)
        result = evaluate_transaction(_product(), session)
        assert result.allowed is True

    def test_at_limit_blocked(self):
        session = SessionState(transactions_this_session=MAX_TRANSACTIONS_PER_SESSION)
        result = evaluate_transaction(_product(), session)
        assert result.allowed is False
        assert result.rule_triggered == "session_rate_limit"

    def test_zero_transactions_passes(self):
        session = SessionState(transactions_this_session=0)
        result = evaluate_transaction(_product(), session)
        assert result.allowed is True


class TestConfirmationThreshold:
    def test_below_threshold_no_confirm_needed(self):
        result = evaluate_transaction(_product(price=CONFIRMATION_THRESHOLD), SessionState())
        assert result.allowed is True
        assert result.requires_confirmation is False

    def test_above_threshold_without_confirm_blocked(self):
        result = evaluate_transaction(
            _product(price=CONFIRMATION_THRESHOLD + 1), SessionState()
        )
        assert result.allowed is False
        assert result.requires_confirmation is True
        assert result.rule_triggered == "confirmation_threshold"

    def test_above_threshold_with_confirm_passes(self):
        result = evaluate_transaction(
            _product(price=CONFIRMATION_THRESHOLD + 1), SessionState(), user_confirmed=True
        )
        assert result.allowed is True
        assert result.requires_confirmation is False


class TestRulePriority:
    """Category check runs before price check, so a blocked category + high price
    should fail on category first."""

    def test_category_checked_before_price(self):
        result = evaluate_transaction(
            _product(category="electronics", price=99999), SessionState()
        )
        assert result.rule_triggered == "category_allowlist"

    def test_stock_checked_before_price(self):
        result = evaluate_transaction(
            _product(stock=0, price=100), SessionState()
        )
        assert result.rule_triggered == "stock_check"

    def test_price_checked_before_session_limit(self):
        session = SessionState(transactions_this_session=MAX_TRANSACTIONS_PER_SESSION)
        result = evaluate_transaction(_product(price=MAX_TRANSACTION_AMOUNT + 1), session)
        assert result.rule_triggered == "max_transaction_amount"


class TestMandateField:
    def test_mandate_present_on_every_decision(self):
        for allowed in [True, False]:
            for rule in ["category_allowlist", "stock_check", "max_transaction_amount"]:
                if allowed:
                    result = evaluate_transaction(_product(), SessionState())
                elif rule == "category_allowlist":
                    result = evaluate_transaction(_product(category="food"), SessionState())
                elif rule == "stock_check":
                    result = evaluate_transaction(_product(stock=0), SessionState())
                else:
                    result = evaluate_transaction(_product(price=5000), SessionState())
                assert result.mandate is not None, f"mandate missing for rule={rule}, allowed={allowed}"

    def test_mandate_content(self):
        result = evaluate_transaction(_product(), SessionState())
        m = result.mandate
        assert m["max_amount"] == MAX_TRANSACTION_AMOUNT
        assert m["confirmation_required_above"] == CONFIRMATION_THRESHOLD
        assert sorted(m["allowed_categories"]) == sorted(ALLOWED_CATEGORIES)
        assert m["session_limit"] == MAX_TRANSACTIONS_PER_SESSION

    def test_build_mandate_matches_constants(self):
        m = _build_mandate()
        assert m["max_amount"] == 3000
        assert m["confirmation_required_above"] == 1500
        assert m["session_limit"] == 3
        assert set(m["allowed_categories"]) == {"apparel", "footwear", "accessories"}
