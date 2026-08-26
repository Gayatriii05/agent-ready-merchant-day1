"""
Upsell / Cross-sell Engine
--------------------------
After a successful purchase, this module suggests one relevant complementary
product ("socks after shoes", "belt after a jacket"). The suggestion is pure
data + rules - deterministic and explainable, same philosophy as policy.py.

Important boundary: an upsell SUGGESTION is not a transaction. The suggested
product still has to go through evaluate_transaction() like any other
purchase before any money moves.
"""

# Explicit complementary-product mapping (product_id -> ranked list of
# complementary product_ids). Kept as merchant-editable data so the demo
# story is easy to change without touching code.
COMPLEMENTARY_MAP = {
    # Footwear -> socks is the classic add-on
    "prod_003": ["prod_010"],           # White Sneakers   -> Ankle Socks
    "prod_011": ["prod_010"],           # Canvas Sneakers  -> Ankle Socks
    # Outerwear/tops pair well with a belt or a tote
    "prod_002": ["prod_009", "prod_004"],  # Denim Jacket     -> Leather Belt, Tote Bag
    "prod_006": ["prod_009"],              # Wool Sweater     -> Leather Belt
    "prod_001": ["prod_010", "prod_004"],  # Blue T-Shirt     -> Socks, Tote Bag
    "prod_007": ["prod_010", "prod_004"],  # Striped Polo     -> Socks, Tote Bag
    "prod_008": ["prod_010"],              # Running Shorts   -> Socks
}

# Fallback rule when a product has no explicit mapping: suggest a cheap
# accessory from these categories, in order of preference.
FALLBACK_CATEGORIES = ["accessories"]
FALLBACK_MAX_PRICE = 500  # INR - keep upsells impulse-purchase sized


def get_upsell_suggestion(purchased_product: dict, catalog: dict) -> dict | None:
    """
    Returns {'product': <full product dict>, 'reason': <human-readable why>}
    for the best available complement, or None if there is nothing sensible
    to suggest (no mapping, everything out of stock, etc.).

    Selection order:
      1. First in-stock entry from the explicit COMPLEMENTARY_MAP.
      2. Otherwise first in-stock cheap accessory (fallback rule).
    """
    products = catalog["products"]

    def find(pid):
        return next((p for p in products if p["id"] == pid), None)

    # Strategy 1: explicit mapping, ranked by merchant preference
    for pid in COMPLEMENTARY_MAP.get(purchased_product.get("id"), []):
        candidate = find(pid)
        if candidate and candidate.get("stock", 0) > 0:
            return {
                "product": candidate,
                "reason": (
                    f"Customers who buy the {purchased_product['name']} commonly "
                    f"add the {candidate['name']} (₹{candidate['price']})."
                ),
            }

    # Strategy 2: generic fallback - a cheap accessory that's in stock and
    # isn't the product just purchased.
    for category in FALLBACK_CATEGORIES:
        candidates = [
            p for p in products
            if p["category"] == category
            and p["id"] != purchased_product.get("id")
            and p["price"] <= FALLBACK_MAX_PRICE
            and p.get("stock", 0) > 0
        ]
        if candidates:
            best = min(candidates, key=lambda p: p["price"])
            return {
                "product": best,
                "reason": (
                    f"The {best['name']} (₹{best['price']}) pairs well with "
                    f"your {purchased_product['name']} and is a popular add-on."
                ),
            }

    return None
