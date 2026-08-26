"""
Campaign Engine (clearance / growth campaigns)
----------------------------------------------
The agent is allowed to *notice* slow-moving or scarce inventory and offer a
discount to move it. This module decides WHICH products qualify and WHAT the
discount is - again deterministic rules, not LLM judgement, so every discount
is explainable to the merchant.

Guardrails:
- Discounts only apply to low-stock, in-stock items.
- The discount percentage can never exceed MAX_DISCOUNT_PCT.
- A discounted purchase still goes through evaluate_transaction() with the
  FINAL (post-discount) price - the policy engine remains the gatekeeper.
"""

LOW_STOCK_THRESHOLD = 5   # stock at/below this counts as "low"
CAMPAIGN_DISCOUNT_PCT = 20  # standard clearance discount
MAX_DISCOUNT_PCT = 50     # hard cap - never discount more than this


def is_low_stock(product: dict) -> bool:
    """A product is 'low stock' when only a few units remain but it isn't sold out."""
    return 0 < product.get("stock", 0) <= LOW_STOCK_THRESHOLD


def compute_discount(product: dict, discount_pct: int = CAMPAIGN_DISCOUNT_PCT) -> dict:
    """
    Returns the pricing breakdown for a campaign purchase:
    original price, discount %, rupee amount saved, final charged price.
    Prices are rounded down to whole rupees so Razorpay amounts stay clean.
    """
    discount_pct = min(discount_pct, MAX_DISCOUNT_PCT)  # enforce hard cap
    original = product["price"]
    discount_amount = int(original * discount_pct / 100)
    final_price = original - discount_amount
    return {
        "original_price": original,
        "discount_pct": discount_pct,
        "discount_amount": discount_amount,
        "final_price": final_price,
    }


def get_active_campaigns(catalog: dict) -> list[dict]:
    """
    Scan the catalog and return one campaign offer per eligible product.
    Used by the agent's get_campaign_offers tool and by /metrics.
    """
    offers = []
    for p in catalog["products"]:
        if is_low_stock(p):
            d = compute_discount(p)
            offers.append({
                "product_id": p["id"],
                "product_name": p["name"],
                "stock_remaining": p["stock"],
                **d,
                "pitch": (
                    f"Only {p['stock']} left! Grab the {p['name']} at "
                    f"{d['discount_pct']}% off - ₹{d['final_price']} instead of ₹{d['original_price']}."
                ),
            })
    # Sort by scarcest first - the most urgent campaign leads the list
    offers.sort(key=lambda o: o["stock_remaining"])
    return offers


def get_campaign_for_product(product_id: str, catalog: dict) -> dict | None:
    """Returns the active campaign offer for one product, or None if not eligible."""
    for offer in get_active_campaigns(catalog):
        if offer["product_id"] == product_id:
            return offer
    return None
