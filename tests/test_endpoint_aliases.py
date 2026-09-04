"""
Regression tests for documented compatibility routes.

The README and project status docs advertise /products and /audit, so those
endpoints should remain available even though the primary routes are /catalog
and /audit-log.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_products_alias_returns_product_list():
    response = client.get("/products")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert data
    assert data[0]["id"].startswith("prod_")


def test_audit_alias_matches_audit_log_shape():
    response = client.get("/audit")
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"total", "events"}
    assert isinstance(data["events"], list)
