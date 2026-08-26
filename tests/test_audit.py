"""
Unit tests for the audit trail module.
"""

import pytest
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from logs.audit import log_event, read_log, clear_log, LOG_FILE


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    """Each test gets a fresh temporary log file."""
    temp_log = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr("logs.audit.LOG_FILE", str(temp_log))
    return temp_log


class TestLogEvent:
    def test_writes_event(self, isolated_log):
        entry = log_event("test_event", {"key": "value"})
        assert isolated_log.exists()
        lines = isolated_log.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_entry_has_timestamp(self):
        entry = log_event("test", {})
        assert "timestamp" in entry
        assert "T" in entry["timestamp"]  # ISO format contains T

    def test_entry_has_event_type(self):
        entry = log_event("my_event", {"foo": 1})
        assert entry["event_type"] == "my_event"

    def test_entry_spreads_details(self):
        entry = log_event("test", {"a": 1, "b": "two"})
        assert entry["a"] == 1
        assert entry["b"] == "two"

    def test_multiple_events_appended(self, isolated_log):
        log_event("event_1", {"n": 1})
        log_event("event_2", {"n": 2})
        log_event("event_3", {"n": 3})
        events = read_log()
        assert len(events) == 3
        assert events[0]["n"] == 1
        assert events[2]["n"] == 3

    def test_returns_entry(self):
        entry = log_event("return_test", {"x": 42})
        assert isinstance(entry, dict)
        assert entry["event_type"] == "return_test"
        assert entry["x"] == 42


class TestReadLog:
    def test_empty_when_no_file(self, isolated_log):
        if isolated_log.exists():
            isolated_log.unlink()
        assert read_log() == []

    def test_reads_valid_jsonl(self, isolated_log):
        log_event("a", {"v": 10})
        log_event("b", {"v": 20})
        events = read_log()
        assert len(events) == 2
        assert events[0]["event_type"] == "a"
        assert events[1]["event_type"] == "b"

    def test_skips_blank_lines(self, isolated_log):
        isolated_log.write_text('{"event_type":"a"}\n\n{"event_type":"b"}\n')
        events = read_log()
        assert len(events) == 2

    def test_ordering_preserved(self, isolated_log):
        for i in range(5):
            log_event(f"event_{i}", {"i": i})
        events = read_log()
        assert [e["i"] for e in events] == [0, 1, 2, 3, 4]


class TestClearLog:
    def test_clear_removes_file(self, isolated_log):
        log_event("test", {})
        assert isolated_log.exists()
        clear_log()
        assert not isolated_log.exists()

    def test_clear_when_no_file(self, isolated_log):
        if isolated_log.exists():
            isolated_log.unlink()
        clear_log()  # Should not raise
        assert not isolated_log.exists()
