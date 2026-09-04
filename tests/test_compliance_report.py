"""
Unit tests for the Compliance Report Generator.
Covers empty log handling, purchase/block counting, revenue and blocked
value sums, violation aggregation, and narrative generation.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.compliance_report import generate_compliance_report


def sample_events():
    """A realistic mix of policy decisions: 2 purchases, 2 blocks, 1 confirmation."""
    return [
        {
            "event_type": "policy_decision",
            "product_id": "prod_001",
            "price": 800,
            "allowed": True,
            "requires_confirmation": False,
            "rule_triggered": None,
            "trust_score": 50,
        },
        {
            "event_type": "policy_decision",
            "product_id": "prod_002",
            "price": 5000,
            "allowed": False,
            "requires_confirmation": False,
            "rule_triggered": "max_transaction_amount",
            "trust_score": 48,
        },
        {
            "event_type": "policy_decision",
            "product_id": "prod_003",
            "price": 1200,
            "allowed": False,
            "requires_confirmation": False,
            "rule_triggered": "stock_check",
            "trust_score": 46,
        },
        {
            "event_type": "policy_decision",
            "product_id": "prod_004",
            "price": 1600,
            "allowed": True,
            "requires_confirmation": False,
            "rule_triggered": None,
            "trust_score": 51,
        },
        {
            "event_type": "policy_decision",
            "product_id": "prod_005",
            "price": 1700,
            "allowed": False,
            "requires_confirmation": True,
            "rule_triggered": "confirmation_threshold",
            "trust_score": 49,
        },
    ]


class TestEmptyLog:
    def test_empty_log_returns_zeros(self):
        report = generate_compliance_report([])
        s = report["summary"]
        assert s["total_actions"] == 0
        assert s["purchases_completed"] == 0
        assert s["purchases_blocked"] == 0
        assert s["total_revenue"] == 0
        assert s["total_value_blocked"] == 0
        assert s["policy_violations_by_rule"] == {}
        assert s["trust_score_trajectory"] == []
        assert s["confirmation_required_count"] == 0

    def test_empty_log_narrative_not_empty(self):
        report = generate_compliance_report([])
        assert report["narrative"]
        assert "No audit events" in report["narrative"]


class TestCounting:
    def test_counts_purchases_and_blocks(self):
        report = generate_compliance_report(sample_events())
        s = report["summary"]
        assert s["total_actions"] == 5
        assert s["purchases_completed"] == 2
        assert s["purchases_blocked"] == 3
        assert s["confirmation_required_count"] == 1

    def test_sums_revenue_and_blocked_value(self):
        report = generate_compliance_report(sample_events())
        s = report["summary"]
        # Revenue: purchases 800 + 1600 = 2400
        assert s["total_revenue"] == 2400
        # Blocked: 5000 + 1200 + 1700 = 7900
        assert s["total_value_blocked"] == 7900

    def test_violations_by_rule(self):
        report = generate_compliance_report(sample_events())
        v = report["summary"]["policy_violations_by_rule"]
        assert v["max_transaction_amount"] == 1
        assert v["stock_check"] == 1
        assert v["confirmation_threshold"] == 1

    def test_trust_trajectory(self):
        report = generate_compliance_report(sample_events())
        assert report["summary"]["trust_score_trajectory"] == [50, 48, 46, 51, 49]


class TestNarrative:
    def test_narrative_non_empty_with_numbers(self):
        report = generate_compliance_report(sample_events())
        n = report["narrative"]
        assert n
        assert "2" in n
        assert "," in n or "2400" in n or "7" in n

    def test_narrative_mentions_purchases_and_blocks(self):
        report = generate_compliance_report(sample_events())
        n = report["narrative"]
        assert "purchase" in n.lower()
        assert "blocked" in n.lower()

    def test_generated_at_present(self):
        report = generate_compliance_report(sample_events())
        assert "generated_at" in report
        assert report["generated_at"]
