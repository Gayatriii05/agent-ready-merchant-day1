"""
Unit tests for the Session Replay grouping logic.
Covers pairing an approved policy_decision with its adjacent purchase_executed,
and leaving ungrouped / blocked events as individual entries.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.replay import build_replay


def ev(event_type, product_id="prod_001", product_name="Tote Bag",
       price=349, allowed=True, amount=None):
    d = {
        "timestamp": "2026-08-31T05:58:43.000000+00:00",
        "event_type": event_type,
        "product_id": product_id,
        "product_name": product_name,
    }
    if price is not None:
        d["price"] = price
    if allowed is not None:
        d["allowed"] = allowed
    if amount is not None:
        d["amount"] = amount
    return d


def texts(result):
    return [s["text"] for s in result["story"]]


class TestReplayGrouping:
    def test_matched_pair_groups_into_one_step(self):
        events = [
            ev("policy_decision", allowed=True),
            ev("purchase_executed", amount=349),
        ]
        result = build_replay(events)
        assert result["total_events"] == 1
        assert len(result["story"]) == 1
        assert result["story"][0]["event_type"] == "grouped"
        assert "Bought Tote Bag for" in result["story"][0]["text"]
        assert "349" in result["story"][0]["text"]

    def test_unmatched_policy_decision_stays_separate(self):
        # Approved decision with NO following purchase_executed (e.g. failed).
        events = [
            ev("policy_decision", allowed=True),
            ev("purchase_failed", product_name=None),
        ]
        result = build_replay(events)
        assert result["total_events"] == 2
        texts_out = texts(result)
        assert "Approved the purchase of 'Tote Bag'" in texts_out[0]
        assert "failed" in texts_out[1]

    def test_blocked_decision_never_grouped(self):
        events = [
            ev("policy_decision", allowed=False, product_name="Sunglasses"),
        ]
        result = build_replay(events)
        assert result["total_events"] == 1
        assert result["story"][0]["event_type"] == "grouped"
        assert "Blocked the purchase of 'Sunglasses'" in result["story"][0]["text"]

    def test_pair_requires_same_product_id(self):
        # policy_decision for one product, purchase_executed for a different one
        # should NOT be grouped together.
        events = [
            ev("policy_decision", product_id="prod_001", allowed=True),
            ev("purchase_executed", product_id="prod_002", amount=999),
        ]
        result = build_replay(events)
        assert result["total_events"] == 2
        assert "Bought" not in " ".join(texts(result))

    def test_browse_event_stays_single(self):
        events = [
            ev("browse_catalog", product_id=None, price=None, allowed=None),
        ]
        result = build_replay(events)
        assert result["total_events"] == 1
        assert "browsed the catalog" in result["story"][0]["text"]

    def test_mixed_sequence_grouping_and_standalone(self):
        events = [
            ev("policy_decision", product_id="prod_001", allowed=True),
            ev("purchase_executed", product_id="prod_001", amount=349),
            ev("browse_catalog", product_id=None, price=None, allowed=None),
            ev("policy_decision", product_id="prod_010", allowed=False),
            ev("purchase_attempt", product_id=None, price=None, allowed=None),
        ]
        result = build_replay(events)
        # Grouped pair (1) + browse (1) + blocked (1) + purchase_attempt (1) = 4
        assert result["total_events"] == 4
        steps = texts(result)
        assert "Bought Tote Bag for" in steps[0]
        assert "browsed the catalog" in steps[1]
        assert "Blocked the purchase of 'Tote Bag'" in steps[2]
        assert "Attempted to purchase" in steps[3]
