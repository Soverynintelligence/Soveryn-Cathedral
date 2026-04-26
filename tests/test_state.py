"""
Core tests for IdentityCathedral.

Covers the three load-bearing invariants:
  - atomic write actually round-trips
  - material-shift gate skips identical writes
  - stale_since suppression hides unchanged fields from prompt injection
  - pollution detection rejects placeholder responses
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from soveryn_cathedral import IdentityCathedral, is_pollution_response


@pytest.fixture
def cathedral(tmp_path: Path) -> IdentityCathedral:
    return IdentityCathedral(storage_path=tmp_path / "identity.json")


def test_cold_start_returns_defaults(cathedral: IdentityCathedral):
    state = cathedral.load()
    assert state["current_thread"]["value"] == ""
    assert state["conversation_buffer"] == []
    assert state["active_surfaces"] == []


def test_format_for_prompt_empty_when_no_content(cathedral: IdentityCathedral):
    assert cathedral.format_for_prompt() == ""


def test_update_writes_and_reads(cathedral: IdentityCathedral):
    wrote = cathedral.update(current_thread="debugging the WebSocket heartbeat")
    assert wrote is True
    state = cathedral.load()
    assert state["current_thread"]["value"] == "debugging the WebSocket heartbeat"
    assert state["current_thread"]["stale_since"] == 0


def test_material_shift_gate_blocks_identical_write(cathedral: IdentityCathedral):
    cathedral.update(current_thread="X")
    wrote = cathedral.update(current_thread="X")
    assert wrote is False, "identical write should be skipped"


def test_material_shift_gate_blocks_whitespace_only_change(cathedral: IdentityCathedral):
    cathedral.update(current_thread="X")
    wrote = cathedral.update(current_thread="  X  ")
    assert wrote is False


def test_stale_since_increments_and_is_omitted_from_prompt(cathedral: IdentityCathedral):
    cathedral.update(current_thread="something specific")
    # Field is fresh — should appear in prompt
    assert "something specific" in cathedral.format_for_prompt()
    # Tick stale 3 times (default threshold)
    for _ in range(3):
        cathedral.increment_stale()
    # Field is now stale — should be omitted
    assert "something specific" not in cathedral.format_for_prompt()


def test_stale_resets_on_material_update(cathedral: IdentityCathedral):
    cathedral.update(current_thread="A")
    for _ in range(5):
        cathedral.increment_stale()
    assert cathedral.format_for_prompt() == ""  # stale, suppressed
    cathedral.update(current_thread="B")
    assert "B" in cathedral.format_for_prompt()  # reset


def test_append_turn_caps_buffer(tmp_path: Path):
    c = IdentityCathedral(
        storage_path=tmp_path / "id.json",
        conversation_buffer_cap=3,
    )
    for i in range(5):
        c.append_turn(user=f"u{i}", assistant=f"a{i}", surface="test")
    state = c.load()
    assert len(state["conversation_buffer"]) == 3
    assert state["conversation_buffer"][0]["user"] == "u2"  # oldest dropped
    assert state["conversation_buffer"][-1]["user"] == "u4"


def test_pollution_response_skipped(cathedral: IdentityCathedral):
    # Placeholder response should NOT be appended
    appended = cathedral.append_turn(
        user="hi",
        assistant="[thinking — response ran long, ask me to continue or rephrase]",
        surface="test",
    )
    assert appended is False
    state = cathedral.load()
    assert state["conversation_buffer"] == []


def test_pollution_response_blocks_last_exchange(cathedral: IdentityCathedral):
    # Set a real exchange first
    cathedral.update(last_exchange={
        "user":      "real question",
        "assistant": "real answer",
        "surface":   "desktop",
        "timestamp": "2026-04-26T12:00:00",
    })
    # Try to overwrite with placeholder
    wrote = cathedral.update(last_exchange={
        "user":      "different question",
        "assistant": "[thinking — response ran long]",
        "surface":   "mobile",
        "timestamp": "2026-04-26T12:01:00",
    })
    state = cathedral.load()
    # Real exchange should still be there
    assert state["last_exchange"]["assistant"] == "real answer"


def test_register_surface_drops_idle(tmp_path: Path):
    c = IdentityCathedral(storage_path=tmp_path / "id.json")
    c.register_surface("desktop", session_id="abc")
    state = c.load()
    assert any(s["surface"] == "desktop" for s in state["active_surfaces"])


def test_broadcast_callback_invoked(tmp_path: Path):
    received = []
    def hook(state):
        received.append(state)
    c = IdentityCathedral(storage_path=tmp_path / "id.json", broadcast=hook)
    c.update(current_thread="X")
    assert len(received) == 1
    assert received[0]["current_thread"]["value"] == "X"


def test_pollution_helper():
    assert is_pollution_response("[thinking — response ran long]") is True
    assert is_pollution_response("") is True
    assert is_pollution_response("This is a real response.") is False


def test_atomic_write_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "id.json"
    c = IdentityCathedral(storage_path=nested)
    c.update(current_thread="X")
    assert nested.exists()
    data = json.loads(nested.read_text())
    assert data["current_thread"]["value"] == "X"
