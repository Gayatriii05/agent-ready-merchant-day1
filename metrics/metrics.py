"""
Metrics
-------
Growth/health counters computed FROM the audit log itself.

Design choice: we don't keep a separate metrics database. The JSONL audit
trail is the single source of truth - if an event isn't in the trail it
didn't happen. That guarantees the numbers shown in the demo UI always
reconcile with the decisions listed right next to them.

Counters:
- total sales (count + revenue)
- upsell revenue added (accepted upsells only)
- blocked attempts (hard blocks by policy)
- confirmation-required events (the 'yellow' path)
- campaign revenue + discounts given ('revenue recovered' - sales that would
  likely have been lost on scarce inventory without the discount push)
- failed executions (e.g. Razorpay errors)
"""

from collections import defaultdict

from logs.audit import read_log


def compute_metrics() -> dict:
    """Walk the audit trail once and aggregate all business metrics."""
    m = {
        "total_sales": 0,
        "total_sales_revenue": 0.0,
        "upsell_sales": 0,
        "upsell_revenue": 0.0,
        "blocked_attempts": 0,
        "confirmation_required": 0,
        "campaign_sales": 0,
        "campaign_revenue": 0.0,      # actual money collected on discounted items
        "discount_given": 0.0,        # rupees shaved off via clearance campaigns
        "failed_executions": 0,
        "revenue_per_product": defaultdict(float),
    }

    for event in read_log():
        et = event.get("event_type")

        if et == "purchase_executed":
            amount = event.get("amount", 0)
            m["total_sales"] += 1
            m["total_sales_revenue"] += amount
            m["revenue_per_product"][event.get("product_id", "?")] += amount
            if event.get("upsell"):
                m["upsell_sales"] += 1
                m["upsell_revenue"] += amount
            if event.get("campaign"):
                m["campaign_sales"] += 1
                m["campaign_revenue"] += amount
                m["discount_given"] += event.get("discount_amount", 0)

        elif et == "policy_decision":
            if not event.get("allowed"):
                # 'yellow' confirmation events are tracked separately from hard blocks
                if event.get("requires_confirmation"):
                    m["confirmation_required"] += 1
                else:
                    m["blocked_attempts"] += 1

        elif et == "purchase_failed":
            m["failed_executions"] += 1

    # defaultdict -> plain dict so it JSON-serializes cleanly over the API
    m["revenue_per_product"] = dict(m["revenue_per_product"])
    return m


def format_summary(m: dict) -> str:
    """Human-readable one-liner, handy for CLI runs and quick sanity checks."""
    return (
        f"Sales: {m['total_sales']} (₹{m['total_sales_revenue']:.0f}) | "
        f"Upsell: {m['upsell_sales']} (₹{m['upsell_revenue']:.0f}) | "
        f"Campaign: {m['campaign_sales']} (₹{m['campaign_revenue']:.0f}, "
        f"discounts ₹{m['discount_given']:.0f}) | "
        f"Blocked: {m['blocked_attempts']} | "
        f"Confirmations: {m['confirmation_required']} | "
        f"Failed: {m['failed_executions']}"
    )
