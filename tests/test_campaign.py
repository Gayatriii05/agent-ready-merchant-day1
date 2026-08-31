"""
Unit tests for the campaign / clearance engine.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.campaign import (
    is_low_stock,
    compute_discount,
    get_active_campaigns,
    get_campaign_for_product,
    LOW_STOCK_THRESHOLD,
    CAMPAIGN_DISCOUNT_PCT,
    MAX_DISCOUNT_PCT,
)
from catalog.store import load_catalog


@pytest.fixture
def catalog():
    return load_catalog()


class TestIsLowStock:
    def test_below_threshold(self):
        assert is_low_stock({"stock": 1}) is True
        assert is_low_stock({"stock": LOW_STOCK_THRESHOLD}) is True

    def test_above_threshold(self):
        assert is_low_stock({"stock": LOW_STOCK_THRESHOLD + 1}) is False
        assert is_low_stock({"stock": 100}) is False

    def test_zero_stock_not_low(self):
        assert is_low_stock({"stock": 0}) is False

    def test_missing_stock(self):
        assert is_low_stock({}) is False


class TestComputeDiscount:
    def test_basic_discount(self):
        d = compute_discount({"price": 1000})
        assert d["original_price"] == 1000
        assert d["discount_pct"] == CAMPAIGN_DISCOUNT_PCT
        assert d["discount_amount"] == 200
        assert d["final_price"] == 800

    def test_custom_discount(self):
        d = compute_discount({"price": 1000}, discount_pct=30)
        assert d["discount_pct"] == 30
        assert d["discount_amount"] == 300
        assert d["final_price"] == 700

    def test_discount_cap_enforced(self):
        d = compute_discount({"price": 1000}, discount_pct=99)
        assert d["discount_pct"] == MAX_DISCOUNT_PCT
        assert d["discount_amount"] == 500
        assert d["final_price"] == 500

    def test_zero_discount(self):
        d = compute_discount({"price": 1000}, discount_pct=0)
        assert d["discount_amount"] == 0
        assert d["final_price"] == 1000

    def test_final_price_is_integer(self):
        d = compute_discount({"price": 99})
        assert isinstance(d["final_price"], int)
        assert isinstance(d["discount_amount"], int)


class TestActiveCampaigns:
    def test_returns_low_stock_products(self, catalog):
        offers = get_active_campaigns(catalog)
        for offer in offers:
            product = next(p for p in catalog["products"] if p["id"] == offer["product_id"])
            assert is_low_stock(product)

    def test_sorted_by_scarcity(self, catalog):
        offers = get_active_campaigns(catalog)
        stocks = [o["stock_remaining"] for o in offers]
        assert stocks == sorted(stocks)

    def test_offer_has_expected_fields(self, catalog):
        offers = get_active_campaigns(catalog)
        for offer in offers:
            assert "product_id" in offer
            assert "product_name" in offer
            assert "stock_remaining" in offer
            assert "original_price" in offer
            assert "final_price" in offer
            assert "discount_pct" in offer
            assert "pitch" in offer

    def test_discount_math_in_offers(self, catalog):
        offers = get_active_campaigns(catalog)
        for offer in offers:
            expected = offer["original_price"] - offer["discount_amount"]
            assert offer["final_price"] == expected

    def test_polo_in_campaigns(self, catalog):
        offers = get_active_campaigns(catalog)
        polo_offer = next((o for o in offers if o["product_id"] == "prod_007"), None)
        assert polo_offer is not None
        assert polo_offer["stock_remaining"] >= 1
        assert polo_offer["stock_remaining"] <= 5


class TestCampaignForProduct:
    def test_eligible_product(self, catalog):
        offer = get_campaign_for_product("prod_007", catalog)
        assert offer is not None
        assert offer["product_id"] == "prod_007"

    def test_ineligible_product(self, catalog):
        offer = get_campaign_for_product("prod_004", catalog)  # Tote Bag (stock=20)
        assert offer is None

    def test_nonexistent_product(self, catalog):
        offer = get_campaign_for_product("nonexistent", catalog)
        assert offer is None
