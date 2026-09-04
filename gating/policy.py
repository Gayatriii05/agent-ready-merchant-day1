"""
Policy / Gating Engine
-----------------------
Every money-moving action the agent wants to take must pass through here first.
This is the 'bounded and gated' requirement from the brief: hard, explainable
rules that decide whether an agent-initiated transaction is allowed to proceed.

Design choice: rules are simple and hard-coded (not ML-based) on purpose.
Explainability matters more than sophistication for financial actions.
"""

from dataclasses import dataclass, field
from typing import Optional


# ---- Policy configuration (tune these for your demo) ----
MAX_TRANSACTION_AMOUNT = 3000        # INR - hard ceiling per transaction
CONFIRMATION_THRESHOLD = 1500        # INR - above this, requires explicit confirm step
ALLOWED_CATEGORIES = {"apparel", "footwear", "accessories"}
MAX_TRANSACTIONS_PER_SESSION = 3     # simple rate limit


def _build_mandate() -> dict:
    """Build the mandate (boundary constraints) for every policy decision.

    This mirrors the AP2 intent/mandate pattern: external AI buyers can read
    these boundaries from the /policy endpoint *before* attempting a transaction,
    so they understand the rules upfront rather than discovering them via
    hard failures. Each policy decision also carries this same mandate so
    audit entries are fully self-describing.
    """
    return {
        "max_amount": MAX_TRANSACTION_AMOUNT,
        "confirmation_required_above": CONFIRMATION_THRESHOLD,
        "allowed_categories": sorted(ALLOWED_CATEGORIES),
        "session_limit": MAX_TRANSACTIONS_PER_SESSION,
    }


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False
    rule_triggered: Optional[str] = None
    mandate: Optional[dict] = None


@dataclass
class SessionState:
    """Tracks per-session counters used by rate-limiting rules."""
    transactions_this_session: int = 0
    total_spent: float = 0.0
    trust_score: float = 75.0


def evaluate_transaction(
    product: dict,
    session: SessionState,
    user_confirmed: bool = False,
) -> PolicyDecision:
    """
    Runs a proposed purchase through every gating rule in order.
    Returns the first failing rule's explanation, or an ALLOW decision.
    This is what gets written to the audit log for every attempt.
    """

    price = product.get("price", 0)
    category = product.get("category", "")
    stock = product.get("stock", 0)

    mandate = _build_mandate()
    mandate["adaptive"] = calculate_trust_adjusted_limit(session)

    # Rule 1: category allow-list
    if category not in ALLOWED_CATEGORIES:
        return PolicyDecision(
            allowed=False,
            reason=f"Category '{category}' is not in the allowed set {ALLOWED_CATEGORIES}.",
            rule_triggered="category_allowlist",
            mandate=mandate,
        )

    # Rule 2: stock availability
    if stock <= 0:
        return PolicyDecision(
            allowed=False,
            reason=f"Product '{product.get('name')}' is out of stock.",
            rule_triggered="stock_check",
            mandate=mandate,
        )

    # Rule 3: hard spend ceiling per transaction
    if price > MAX_TRANSACTION_AMOUNT:
        return PolicyDecision(
            allowed=False,
            reason=(
                f"Transaction amount Rs.{price} exceeds the hard ceiling of "
                f"Rs.{MAX_TRANSACTION_AMOUNT}. Agents cannot self-authorize purchases "
                f"above this limit under any circumstance."
            ),
            rule_triggered="max_transaction_amount",
            mandate=mandate,
        )

    # Rule 4: session-level rate limit
    if session.transactions_this_session >= MAX_TRANSACTIONS_PER_SESSION:
        return PolicyDecision(
            allowed=False,
            reason=(
                f"Session has already completed {session.transactions_this_session} "
                f"transactions, at the limit of {MAX_TRANSACTIONS_PER_SESSION}."
            ),
            rule_triggered="session_rate_limit",
            mandate=mandate,
        )

    # Rule 5: confirmation required above a threshold
    if price > CONFIRMATION_THRESHOLD and not user_confirmed:
        return PolicyDecision(
            allowed=False,
            reason=(
                f"Transaction amount Rs.{price} exceeds the confirmation threshold "
                f"of Rs.{CONFIRMATION_THRESHOLD}. Explicit user confirmation is "
                f"required before this action can proceed."
            ),
            requires_confirmation=True,
            rule_triggered="confirmation_threshold",
            mandate=mandate,
        )

    # All checks passed
    return PolicyDecision(
        allowed=True,
        reason="All policy checks passed.",
        rule_triggered=None,
        mandate=mandate,
    )


# ---------------------------------------------------------------------------
# Adaptive Trust Score (additive layer on top of the static rules above)
# ---------------------------------------------------------------------------
# A dynamic, per-session trust signal (0-100, starts at 75 in the trusted
# tier so a fresh session gets the maximum spending latitude for a smooth demo). It does
# NOT replace any of the 5 hard rules — it is an ADDITIONAL dimension exposed
# via the mandate so external buyers see how much latitude the session has.
# ---------------------------------------------------------------------------

def _trust_tier(score: float) -> str:
    """Classify a trust score into a human-readable tier."""
    if score >= 75:
        return "trusted"
    if score < 40:
        return "restricted"
    return "established"


def calculate_trust_adjusted_limit(session: SessionState) -> dict:
    """
    Compute a trust-adjusted spending ceiling based on the session's current
    trust score. Trusted sessions get more room, suspicious ones get tighter.
    Returns the adjusted ceiling, the multiplier used, and trust metadata.
    """
    if session.trust_score >= 75:
        multiplier = 1.5
        tier = "trusted"
    elif session.trust_score < 40:
        multiplier = 0.5
        tier = "restricted"
    else:
        multiplier = 1.0
        tier = "established"

    adjusted = MAX_TRANSACTION_AMOUNT * multiplier
    return {
        "adjusted_ceiling": adjusted,
        "multiplier": multiplier,
        "trust_score": session.trust_score,
        "trust_tier": tier,
    }


def update_trust_score(session: SessionState, outcome: str) -> None:
    """
    Adjust the session's trust score based on observed behavior.
    - "purchased": +5 (cap at 100)
    - "blocked":   -4 (floor at 0)
    - "browse":    +2 (small positive signal, cap at 100)
    """
    delta = {"purchased": 5, "blocked": -4, "browse": 2}.get(outcome, 0)
    session.trust_score = max(0.0, min(100.0, session.trust_score + delta))
