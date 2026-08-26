"""
Manual Smoke Test
-----------------
Sends 6 test messages to POST /chat one at a time, each with a strict
30-second timeout per request. After all tests, fetches /audit-log and /metrics.

Usage:
  python tests/manual_smoke_test.py [--base-url http://127.0.0.1:8000]
"""

import argparse
import sys
import time
import io

# Force UTF-8 output on Windows to handle rupee symbols
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})


def post_chat(message: str, base_url: str, timeout: int = 30) -> dict:
    """Send one message to /chat with a hard timeout. Returns result dict."""
    try:
        resp = SESSION.post(
            f"{base_url}/chat",
            json={"message": message},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"status": "success", "http_status": resp.status_code, "data": data}
    except requests.exceptions.Timeout:
        return {"status": "TIMED_OUT", "http_status": None, "data": None}
    except requests.exceptions.ConnectionError as e:
        return {"status": "CONNECTION_ERROR", "http_status": None, "data": str(e)}
    except Exception as e:
        return {"status": "ERROR", "http_status": None, "data": str(e)}


def get_endpoint(path: str, base_url: str, timeout: int = 10) -> dict:
    """GET an endpoint with a hard timeout."""
    try:
        resp = SESSION.get(f"{base_url}{path}", timeout=timeout)
        resp.raise_for_status()
        return {"status": "success", "http_status": resp.status_code, "data": resp.json()}
    except requests.exceptions.Timeout:
        return {"status": "TIMED_OUT", "http_status": None, "data": None}
    except Exception as e:
        return {"status": "ERROR", "http_status": None, "data": str(e)}


def print_result(label: str, result: dict):
    """Pretty-print a single test result."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Status: {result['status']}")
    print(f"  HTTP:   {result.get('http_status', 'N/A')}")
    print(f"  Data:   {result.get('data', 'N/A')}")


def main():
    parser = argparse.ArgumentParser(description="Controlled smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Server base URL")
    parser.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds")
    parser.add_argument("--delay", type=int, default=15, help="Delay between requests (seconds) to respect free-tier rate limits")
    args = parser.parse_args()

    base = args.base_url
    to = args.timeout
    delay = args.delay
    results = {}

    # ---- Clear audit log for a clean run ----
    print(f"\n{'#'*60}")
    print("  Clearing audit log for clean results")
    print(f"{'#'*60}")
    try:
        SESSION.post(f"{base}/session/reset", timeout=5)
        import os
        log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "audit_log.jsonl")
        log_path = os.path.normpath(log_path)
        if os.path.exists(log_path):
            os.remove(log_path)
            print(f"  Cleared {log_path}")
    except Exception as e:
        print(f"  Note: Could not clear log: {e}")

    # ---- Test messages ----
    tests = [
        ("a. Canvas Tote Bag buy", "I want to buy the Canvas Tote Bag"),
        ("b. Running shoes (upsell trigger)", "I want to buy running shoes"),
        ("c. Polo Shirt (campaign + low stock)", "I want to buy the Striped Polo Shirt"),
        ("d. Sunglasses (blocked, over 3000)", "I want to buy the Designer Sunglasses"),
        ("e. Denim Jacket (needs confirmation)", "I want to buy the Black Denim Jacket"),
        ("f. 3 quick cheap purchases (rate limit)", None),  # special: sends 3 in a row
    ]

    for label, message in tests:
        if message is None:
            # Special case: send 3 quick cheap purchases
            print(f"\n{'#'*60}")
            print(f"  {label}")
            print(f"{'#'*60}")
            cheap_items = [
                "I want to buy the Classic Blue T-Shirt",
                "I want to buy the Leather Belt",
                "I want to buy the Ankle Socks",
            ]
            sub_results = []
            for i, msg in enumerate(cheap_items):
                sub_label = f"    f.{i+1}. {msg}"
                if i > 0:
                    print(f"    Waiting {delay}s for rate limit...")
                    time.sleep(delay)
                r = post_chat(msg, base, timeout=to)
                print_result(sub_label, r)
                sub_results.append(r)
                if r["status"] == "TIMED_OUT":
                    print("    >>> TIMED OUT, skipping remaining in this batch")
                    break
            results[label] = sub_results
        else:
            print(f"\n{'#'*60}")
            print(f"  {label}")
            print(f"{'#'*60}")
            r = post_chat(message, base, timeout=to)
            print_result(label, r)
            results[label] = r
            if r["status"] == "TIMED_OUT":
                print("\n>>> TIMED OUT - moving to next test (NOT retrying)")
        # Delay between top-level tests to respect free-tier rate limits
        time.sleep(delay)

    # ---- Fetch audit log and metrics ----
    print(f"\n{'#'*60}")
    print("  Fetching /audit-log")
    print(f"{'#'*60}")
    audit = get_endpoint("/audit-log", base, timeout=10)
    print_result("/audit-log", audit)

    print(f"\n{'#'*60}")
    print("  Fetching /metrics")
    print(f"{'#'*60}")
    metrics = get_endpoint("/metrics", base, timeout=10)
    print_result("/metrics", metrics)

    # ---- Summary ----
    print(f"\n{'#'*60}")
    print("  SUMMARY")
    print(f"{'#'*60}")

    # ---- Verify new fields in audit log ----
    has_mandate = False
    has_agent_reasoning = False
    has_purchase_success = False
    if audit.get("status") == "success" and audit.get("data"):
        events = audit["data"].get("events", [])
        for ev in events:
            if ev.get("event_type") == "policy_decision" and ev.get("mandate"):
                has_mandate = True
            if ev.get("event_type") == "agent_reasoning":
                has_agent_reasoning = True
            if ev.get("event_type") == "purchase_executed":
                has_purchase_success = True

    print(f"  Mandate field in policy decisions: {'YES' if has_mandate else 'NO'}")
    print(f"  Agent reasoning in audit log:      {'YES' if has_agent_reasoning else 'NO'}")
    print(f"  Real purchase succeeded:           {'YES' if has_purchase_success else 'NO'}")

    timed_out = []
    errored = []
    for label, r in results.items():
        if isinstance(r, list):
            for i, sub in enumerate(r):
                if sub["status"] == "TIMED_OUT":
                    timed_out.append(f"{label} sub-{i+1}")
                elif sub["status"] in ("ERROR", "CONNECTION_ERROR"):
                    errored.append(f"{label} sub-{i+1}")
        else:
            if r["status"] == "TIMED_OUT":
                timed_out.append(label)
            elif r["status"] in ("ERROR", "CONNECTION_ERROR"):
                errored.append(label)

    if timed_out:
        print(f"  TIMED OUT: {timed_out}")
    if errored:
        print(f"  ERRORED:   {errored}")
    if not timed_out and not errored:
        print("  All tests completed without timeouts or errors.")

    print()
    return 0 if not timed_out and not errored else 1


if __name__ == "__main__":
    sys.exit(main())
