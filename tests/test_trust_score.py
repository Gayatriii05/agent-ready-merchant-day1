"""
Unit tests for the Adaptive Trust Score system.
Covers initial value, score adjustments, floor/ceiling clamping,
and trust-adjusted limit calculation for all tiers.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.policy import (
    SessionState,
    update_trust_score,
    calculate_trust_adjusted_limit,
    MAX_TRANSACTION_AMOUNT,
)


class TestTrustScoreInitialValue:
    def test_new_session_starts_at_75(self):
        session = SessionState()
        assert session.trust_score == 75.0


class TestTrustScoreAdjustments:
    def test_successful_purchase_increases_score(self):
        session = SessionState()
        initial = session.trust_score
        update_trust_score(session, "purchased")
        assert session.trust_score == initial + 5

    def test_blocked_purchase_decreases_score(self):
        session = SessionState()
        initial = session.trust_score
        update_trust_score(session, "blocked")
        assert session.trust_score == initial - 4

    def test_blocked_floor_at_zero(self):
        session = SessionState(trust_score=2.0)
        update_trust_score(session, "blocked")
        assert session.trust_score == 0.0
        update_trust_score(session, "blocked")
        assert session.trust_score == 0.0

    def test_purchased_cap_at_100(self):
        session = SessionState(trust_score=98.0)
        update_trust_score(session, "purchased")
        assert session.trust_score == 100.0
        update_trust_score(session, "purchased")
        assert session.trust_score == 100.0

    def test_browse_increases_score(self):
        session = SessionState()
        initial = session.trust_score
        update_trust_score(session, "browse")
        assert session.trust_score == initial + 2

    def test_unknown_outcome_no_change(self):
        session = SessionState()
        update_trust_score(session, "unknown")
        assert session.trust_score == 75.0


class TestTrustAdjustedLimit:
    def test_trusted_tier_multiplier(self):
        session = SessionState(trust_score=80.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["multiplier"] == 1.5
        assert result["adjusted_ceiling"] == MAX_TRANSACTION_AMOUNT * 1.5
        assert result["trust_tier"] == "trusted"

    def test_established_tier_multiplier(self):
        session = SessionState(trust_score=65.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["multiplier"] == 1.0
        assert result["adjusted_ceiling"] == MAX_TRANSACTION_AMOUNT
        assert result["trust_tier"] == "established"

    def test_restricted_tier_multiplier(self):
        session = SessionState(trust_score=20.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["multiplier"] == 0.5
        assert result["adjusted_ceiling"] == MAX_TRANSACTION_AMOUNT * 0.5
        assert result["trust_tier"] == "restricted"

    def test_boundary_39_is_restricted(self):
        session = SessionState(trust_score=39.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["trust_tier"] == "restricted"

    def test_boundary_40_is_established(self):
        session = SessionState(trust_score=40.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["trust_tier"] == "established"

    def test_boundary_74_is_established(self):
        session = SessionState(trust_score=74.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["trust_tier"] == "established"

    def test_boundary_75_is_trusted(self):
        session = SessionState(trust_score=75.0)
        result = calculate_trust_adjusted_limit(session)
        assert result["trust_tier"] == "trusted"
