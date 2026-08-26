"""
Catalog Store
-------------
Single place that reads/writes the merchant catalog file. Introduced when
purchases started decrementing stock: writes must be atomic (never leave a
half-written catalog behind if the process dies mid-save) and every module
should share one view instead of opening products.json directly.
"""

import json
import os
import tempfile

CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "catalog", "products.json"
)


def load_catalog() -> dict:
    """Read the full catalog from disk."""
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_catalog(catalog: dict):
    """
    Atomically write the catalog back to disk:
    write to a temp file in the same folder first, then os.replace() over the
    original. os.replace is atomic on both Windows and POSIX, so a crash can
    never corrupt the catalog.
    """
    dir_name = os.path.dirname(CATALOG_PATH)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CATALOG_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def get_product(product_id: str) -> dict | None:
    """Look up a single product by id (None if it doesn't exist)."""
    catalog = load_catalog()
    return next((p for p in catalog["products"] if p["id"] == product_id), None)


def decrement_stock(product_id: str) -> dict:
    """
    Remove one unit of stock after a successful purchase.
    Returns the updated product. Never lets stock go below zero - callers
    should have already passed the policy engine's stock check, but this is
    defense in depth.
    """
    catalog = load_catalog()
    for p in catalog["products"]:
        if p["id"] == product_id:
            if p["stock"] > 0:
                p["stock"] -= 1
                save_catalog(catalog)
            return p
    raise KeyError(f"No product with id {product_id}")
