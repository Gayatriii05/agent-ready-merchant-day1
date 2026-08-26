"""
Razorpay test-mode wrapper.
Requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode keys) as env vars.
Get free test keys from the Razorpay dashboard -> Settings -> API Keys -> Test Mode.
"""

import os
import razorpay
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client():
    global _client
    if _client is None:
        key_id = os.getenv("RAZORPAY_KEY_ID")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. "
                "Add them to a .env file (test mode keys from Razorpay dashboard)."
            )
        _client = razorpay.Client(auth=(key_id, key_secret))
    return _client


def create_test_order(amount_inr: float, receipt: str, notes: dict | None = None):
    """
    Creates a Razorpay order in test mode. Amount must be in paise (INR * 100).
    This does NOT capture payment automatically - it creates an order that would
    be completed via checkout in a real flow. For the demo, order creation itself
    is the auditable 'money action' the agent performs.
    """
    client = get_client()
    order = client.order.create({
        "amount": int(amount_inr * 100),
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    })
    return order
