"""
Compliance Report Generator
---------------------------
One-click report that summarizes a session's activity in business/compliance
language. Mirrors Razorpay's stated judging bar: "measured impact...with
compliant escalation, stopping rules, and an audit trail."
"""

from datetime import datetime, timezone


def generate_compliance_report(audit_events: list) -> dict:
    """
    Process the audit log and return a structured compliance report with
    summary statistics and a plain-English narrative.
    """
    total_actions = len(audit_events)
    purchases_completed = 0
    purchases_blocked = 0
    total_revenue = 0.0
    total_value_blocked = 0.0
    policy_violations_by_rule: dict[str, int] = {}
    trust_score_trajectory: list[float] = []
    confirmation_required_count = 0

    for ev in audit_events:
        event_type = ev.get("event_type", "")

        if event_type == "policy_decision":
            trust_val = ev.get("trust_score")
            if trust_val is not None:
                trust_score_trajectory.append(trust_val)

            if ev.get("requires_confirmation"):
                confirmation_required_count += 1

            allowed = ev.get("allowed", False)
            price = ev.get("price", 0)
            rule = ev.get("rule_triggered")

            if allowed:
                purchases_completed += 1
                total_revenue += price
            else:
                purchases_blocked += 1
                total_value_blocked += price
                if rule:
                    policy_violations_by_rule[rule] = policy_violations_by_rule.get(rule, 0) + 1

    # Build the narrative
    parts: list[str] = []

    if total_actions == 0:
        narrative = "No audit events recorded yet. The compliance report will populate as session activity occurs."
    else:
        if purchases_completed > 0:
            parts.append(
                f"This session completed {purchases_completed} purchase{'s' if purchases_completed != 1 else ''} "
                f"totaling \u20b9{total_revenue:,.0f}."
            )
        else:
            parts.append("No purchases were completed during this session.")

        if purchases_blocked > 0:
            blocking_rules = sorted(
                policy_violations_by_rule.items(), key=lambda x: x[1], reverse=True
            )
            top_rule = blocking_rules[0][0].replace("_", " ") if blocking_rules else "policy"
            parts.append(
                f"The policy engine blocked {purchases_blocked} attempt{'s' if purchases_blocked != 1 else ''} "
                f"worth \u20b9{total_value_blocked:,.0f} combined, primarily due to {top_rule} violations."
            )

        if trust_score_trajectory:
            start_ts = trust_score_trajectory[0]
            end_ts = trust_score_trajectory[-1]
            if len(trust_score_trajectory) == 1 or start_ts == end_ts:
                parts.append(f"Trust score is currently at {end_ts:.0f}.")
            else:
                direction = "increased" if end_ts > start_ts else "decreased"
                sentiment = "positive" if end_ts > start_ts else "negative"
                parts.append(
                    f"Trust score {direction} from {start_ts:.0f} to {end_ts:.0f}, "
                    f"indicating {sentiment} session behavior."
                )

        if confirmation_required_count > 0:
            parts.append(
                f"{confirmation_required_count} transaction{'s' if confirmation_required_count != 1 else ''} "
                f"required explicit user confirmation."
            )

        narrative = " ".join(parts)

    return {
        "summary": {
            "total_actions": total_actions,
            "purchases_completed": purchases_completed,
            "purchases_blocked": purchases_blocked,
            "total_revenue": total_revenue,
            "total_value_blocked": total_value_blocked,
            "policy_violations_by_rule": policy_violations_by_rule,
            "trust_score_trajectory": trust_score_trajectory,
            "confirmation_required_count": confirmation_required_count,
        },
        "narrative": narrative,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
