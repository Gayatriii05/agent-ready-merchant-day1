"""
Audit Trail
-----------
Every agent action attempt (allowed or blocked) gets logged here with:
- what was attempted
- what the policy engine decided and why
- what actually happened (success / failure / blocked)

This satisfies the 'show the audit trail' requirement from the brief.
"""

import json
import os
from datetime import datetime, timezone

LOG_FILE = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")


def log_event(event_type: str, details: dict):
    """
    Append a structured event to the audit log (JSON Lines format,
    easy to read back and easy to show in the pitch/demo).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        **details,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_log():
    """Read back the full audit trail, in order."""
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return [json.loads(line) for line in f if line.strip()]


def clear_log():
    """Useful for resetting between demo runs."""
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
