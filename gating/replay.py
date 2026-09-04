"""
Session Replay
--------------
Converts the flat audit trail into a plain-English, numbered story so a
reviewer (or an auditor) can follow exactly what happened, in order,
without reading raw JSON.
"""

from datetime import datetime, timezone


def _ts(ev: dict) -> str:
    raw = ev.get("timestamp") or ""
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return raw


def _money(v) -> str:
    try:
        return f"\u20b9{v:,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _describe_event(ev: dict) -> str:
    et = ev.get("event_type", "")

    if et == "policy_decision":
        product = ev.get("product_name") or ev.get("product_id") or "item"
        price = _money(ev.get("price", 0))
        if ev.get("allowed"):
            note = f"Approved the purchase of '{product}' for {price}."
        else:
            rule = (ev.get("rule_triggered") or "policy").replace("_", " ")
            note = f"Blocked the purchase of '{product}' ({price}) because of the {rule} rule."
        if ev.get("requires_confirmation"):
            note += " This one needed explicit user confirmation."
        return note

    if et == "purchase_executed":
        product = ev.get("product_name") or ev.get("product_id") or "item"
        amount = _money(ev.get("amount", ev.get("order", {}).get("amount_charged", 0)))
        order_id = ev.get("order", {}).get("order_id", "")
        s = f"Successfully purchased '{product}' for {amount}."
        if order_id:
            s += f" Order reference: {order_id}."
        return s

    if et == "purchase_failed":
        product = ev.get("product_name") or ev.get("product_id") or "item"
        err = ev.get("error", "an unknown error")
        return f"The purchase of '{product}' failed due to {err}."

    if et == "purchase_attempt":
        product = ev.get("product_id") or "item"
        status = ev.get("result", {}).get("status", "")
        reason = ev.get("result", {}).get("reason", "")
        line = f"Attempted to purchase '{product}'."
        if reason:
            line += f" Result: {reason}."
        else:
            line += f" Result status: {status}."
        return line

    if et == "browse_catalog":
        filters = ev.get("filters", {})
        count = ev.get("results_count", 0)
        cat = filters.get("category") or "any"
        return f"The agent browsed the catalog (category '{cat}', {count} results)."

    if et == "agent_reasoning":
        tool = ev.get("tool_name", "")
        return f"The agent reasoned and chose to call the '{tool}' tool."

    if et == "agent_to_agent":
        sub = ev.get("sub_type", "")
        amount = ev.get("amount", ev.get("price"))
        product = ev.get("product_id") or ev.get("item") or ""
        s = f"(Buyer-agent interaction: {sub.replace('_', ' ')})"
        if product:
            s += f" regarding '{product}'."
        if amount:
            s += f" Amount {_money(amount)}."
        return s

    if et == "upsell_suggested":
        product = ev.get("product_id") or ev.get("product")
        line = "The agent suggested an upsell."
        if product:
            line += f" Related item: '{product}'."
        return line

    if et == "upsell_declined":
        return "The buyer declined the suggested upsell."

    if et == "campaign_offer_surfaced":
        product = ev.get("product_id") or ev.get("product")
        line = "A clearance campaign offer was surfaced."
        if product:
            line += f" Item: '{product}'."
        return line

    return f"Recorded event: {et}."


def _group(audit_events: list) -> list:
    """Collapse an allowed policy_decision immediately followed by its
    purchase_executed into a single visual step (e.g. "Bought X for Y"),
    so the replay reads as a story rather than raw, duplicate entries."""
    steps: list[dict] = []
    i = 0
    n = len(audit_events)
    while i < n:
        ev = audit_events[i]
        et = ev.get("event_type", "")
        if et == "policy_decision" and ev.get("allowed"):
            # Look ahead for the matching purchase_executed for the SAME product,
            # adjacent in the list.
            nxt = audit_events[i + 1] if i + 1 < n else None
            match = (
                nxt is not None
                and nxt.get("event_type") == "purchase_executed"
                and nxt.get("product_id") == ev.get("product_id")
            )
            if match:
                steps.append({
                    "time": _ts(ev),
                    "product_id": ev.get("product_id"),
                    "text": (f"Bought {nxt.get('product_name') or ev.get('product_name') or 'item'} "
                             f"for {_money(nxt.get('amount', ev.get('price', 0)))}."),
                })
                i += 2
                continue
        steps.append({
            "time": _ts(ev),
            "product_id": ev.get("product_id"),
            "text": _describe_event(ev),
        })
        i += 1
    return steps


def build_replay(audit_events: list) -> dict:
    """Return a numbered, plain-English story of the session."""
    steps = _group(audit_events)
    lines = []
    for j, step in enumerate(steps):
        lines.append({
            "index": j + 1,
            "time": step.get("time", ""),
            "event_type": "grouped",
            "text": step.get("text", ""),
        })
    return {
        "total_events": len(steps),
        "story": lines,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
