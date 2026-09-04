"""
Tests for Gemini 503 UNAVAILABLE fallback handling in agent.agent.

Confirms that when the primary model raises 503/429, chat() switches to a
separately-rate-limited FALLBACK_MODEL (preserving conversation history), and
that when BOTH models are down the call still returns the existing graceful
error tuple instead of crashing.
"""

import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.agent as agent_mod
from agent.agent import MerchantAgent


def _ok_response(text):
    """A minimal GenerateContentResponse-like object with only a text part."""
    return SimpleNamespace(
        candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text=text, function_call=None)])
        )]
    )


class FakeChat:
    def __init__(self, failures, ok_text="done", history=None):
        self.failures = failures
        self.ok_text = ok_text
        self.calls = 0
        self.history = history if history is not None else [{"role": "user", "parts": []}]

    def send_message(self, user_message):
        self.calls += 1
        if self.calls <= self.failures:
            raise Exception("503 UNAVAILABLE - high demand")
        return _ok_response(self.ok_text)

    def get_history(self):
        return self.history


def _make_client(create_fn):
    """Return a MagicMock client whose chats.create uses create_fn."""
    client = MagicMock()
    client.chats.create.side_effect = create_fn
    return client


def test_503_triggers_fallback_to_fallback_model():
    """Second failure (503) switches to FALLBACK_MODEL and keeps history."""
    created_calls = []
    # Primary fails on its first two sends (so the 2nd attempt's send raises
    # and triggers the fallover), then we move to the fallback.
    primary = FakeChat(failures=2, ok_text="primary reply")

    def create_fn(model, config=None, history=None):
        created_calls.append({"model": model, "history": history})
        # MODEL and FALLBACK_MODEL can be the same string, so the primary
        # session is the first create call; any later create is the fallback.
        if len(created_calls) == 1:
            return primary
        return FakeChat(failures=0, ok_text="fallback reply")

    client = _make_client(create_fn)
    with patch("agent.agent.client", client), \
         patch("agent.agent.time.sleep"):
        agent = MerchantAgent()
        reply, structured = agent.chat("tell me something interesting")

    assert reply == "fallback reply"
    assert agent._switched_to_fallback is True
    # The fallback session is the second create call (after the primary).
    assert len(created_calls) == 2
    assert created_calls[1]["model"] == agent_mod.FALLBACK_MODEL
    # history carried from the primary session is preserved on the fallback
    assert created_calls[1]["history"] == primary.history


def test_429_also_uses_fallback_path():
    """A 429 RESOURCE_EXHAUSTED also triggers the same fallback logic."""

    created = []

    def create_fn(model=None, config=None, history=None):
        created.append(model)
        # First create call = primary session, later creates = fallback.
        if len(created) > 1:
            return FakeChat(failures=0, ok_text="recovered")
        return FakeChat(failures=2, ok_text="primary")

    client = _make_client(create_fn)
    with patch("agent.agent.client", client), \
         patch("agent.agent.time.sleep"):
        agent = MerchantAgent()
        reply, _ = agent.chat("hi")

    assert reply == "recovered"
    assert agent._switched_to_fallback is True


def test_both_models_fail_uses_offline_fallback():
    """When primary AND fallback are down, return the offline-mode fallback."""

    class AlwaysDown:
        def send_message(self, m):
            raise Exception("503 UNAVAILABLE - high demand")

        def get_history(self):
            return []

    def create_fn(model=None, config=None, history=None):
        return AlwaysDown()

    client = _make_client(create_fn)
    with patch("agent.agent.client", client), \
         patch("agent.agent.time.sleep"):
        agent = MerchantAgent()
        result = agent.chat("buy something")

    assert isinstance(result, tuple)
    reply, structured = result
    assert "offline mode" in reply.lower()
    assert isinstance(structured, dict)
