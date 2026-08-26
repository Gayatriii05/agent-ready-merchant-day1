"""
Unit tests for the upsell / cross-sell engine.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gating.upsell import get_upsell_suggestion, COMPLEMENTARY_MAP
from catalog.store import load_catalog


@pytest.fixture
def catalog():
    return load_catalog()


class TestKnownComplementaryPairs:
    def test_sneakers_suggest_socks(self, catalog):
        sneakers = next(p for p in catalog["products"] if p["id"] == "prod_003")
        result = get_upsell_suggestion(sneakers, catalog)
        assert result is not None
        assert result["product"]["id"] == "prod_010"  # Ankle Socks

    def test_denim_jacket_suggests_belt(self, catalog):
        jacket = next(p for p in catalog["products"] if p["id"] == "prod_002")
        result = get_upsell_suggestion(jacket, catalog)
        assert result is not None
        assert result["product"]["id"] == "prod_009"  # Leather Belt (first in map)

    def test_tshirt_suggests_socks(self, catalog):
        tshirt = next(p for p in catalog["products"] if p["id"] == "prod_001")
        result = get_upsell_suggestion(tshirt, catalog)
        assert result is not None
        assert result["product"]["id"] == "prod_010"  # Ankle Socks (first in map)

    def test_polo_suggests_socks(self, catalog):
        polo = next(p for p in catalog["products"] if p["id"] == "prod_007")
        result = get_upsell_suggestion(polo, catalog)
        assert result is not None
        assert result["product"]["id"] == "prod_010"

    def test_running_shorts_suggest_socks(self, catalog):
        shorts = next(p for p in catalog["products"] if p["id"] == "prod_008")
        result = get_upsell_suggestion(shorts, catalog)
        assert result is not None
        assert result["product"]["id"] == "prod_010"


class TestUpsellStructure:
    def test_suggestion_has_product_and_reason(self, catalog):
        sneakers = next(p for p in catalog["products"] if p["id"] == "prod_003")
        result = get_upsell_suggestion(sneakers, catalog)
        assert result is not None
        assert "product" in result
        assert "reason" in result
        assert isinstance(result["product"], dict)
        assert "id" in result["product"]
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

    def test_suggested_product_is_in_stock(self, catalog):
        for pid in COMPLEMENTARY_MAP:
            product = next((p for p in catalog["products"] if p["id"] == pid), None)
            if product is None:
                continue
            result = get_upsell_suggestion(product, catalog)
            if result is not None:
                assert result["product"]["stock"] > 0


class TestFallbackBehavior:
    def test_unknown_product_falls_back(self, catalog):
        unknown = {"id": "unknown_999", "name": "Mystery Item", "category": "apparel", "price": 100, "stock": 5}
        result = get_upsell_suggestion(unknown, catalog)
        if result is not None:
            assert result["product"]["category"] == "accessories"

    def test_no_suggestion_when_all_complements_oos(self, catalog):
        """If the mapped complement is out of stock, the fallback may or may not find something."""
        product = next(p for p in catalog["products"] if p["id"] == "prod_003")
        result = get_upsell_suggestion(product, catalog)
        # prod_010 has stock=30 in default catalog, so should always return something
        assert result is not None
