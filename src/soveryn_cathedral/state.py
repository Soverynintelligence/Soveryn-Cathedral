"""
Identity state — the cathedral's core read/write layer.

A JSON file holding the agent's mutable working memory. Atomic writes,
material-shift gating, per-field stale_since suppression, prompt-injection
formatting. Zero framework dependencies.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Union


# Default suppression threshold. After this many consecutive summarizer cycles
# without a material change, a tracked field is dropped from prompt injection.
# A material update via update() resets the counter to 0.
DEFAULT_STALE_THRESHOLD = 3

# Default conversation buffer cap. Last N completed turns held in memory and
# rendered to new viewports on connect. 20 is enough for visible recent
# history without bloating prompts or broadcast payloads.
DEFAULT_CONVERSATION_BUFFER_CAP = 20

# Default active_projects cap. Bounded to prevent drift into clutter.
DEFAULT_ACTIVE_PROJECTS_CAP = 6

# Tracked scalar fields that carry stale_since counters and the material-shift
# gate. Other fields (last_exchange, conversation_buffer, etc.) have their own
# update semantics.
DEFAULT_TRACKED_SCALARS = ("current_thread", "context_note", "self_muse", "current_mood")

# Placeholder/runaway-response markers that should never poison identity.
# When the agent's response contains any of these (case-insensitive substring),
# append_turn and update(last_exchange=...) skip the write — the previous real
# exchange stays as the continuity signal.
DEFAULT_POLLUTION_MARKERS = (
    "thinking — response ran long",
    "thinking - response ran long",
    "response ran long, ask me to continue",
    "response ran long, ask me to rephrase",
)


def is_pollution_response(text: str, markers: tuple = DEFAULT_POLLUTION_MARKERS) -> bool:
    """Return True if `text` is a placeholder/runaway response that shouldn't
    persist into identity state. Override `markers` to add your own."""
    if not text:
        return True
    low = text.lower()
    return any(m in low for m in markers)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class IdentityCathedral:
    """The cathedral's core surface.

    Single shared source of truth for an agent's mutable working memory across
    every viewport (desktop, mobile, autonomous heartbeat, CLI). Devices stop
    being separate brains and start being windows into the same room.

    Args:
        storage_path: Path to the JSON file. Parent directory will be created
            if it doesn't exist. Defaults to `./identity_state.json`.
        tracked_scalars: Field names to treat as scalars with stale_since
            counters and material-shift gating. Default is
            `("current_thread", "context_note", "self_muse", "current_mood")`.
        stale_threshold: Cycles before a stale field is dropped from prompt
            injection. Default 3.
        conversation_buffer_cap: Max turns held in the rolling buffer.
            Default 20.
        active_projects_cap: Max active_projects entries. Default 6.
        broadcast: Optional callable invoked after every write. Receives the
            full state dict. Use this to push state changes to connected
            clients (e.g. WebSocket fan-out). Errors are swallowed.
        pollution_markers: Substrings in an assistant response that mark it
            as a placeholder/runaway and disqualify it from being recorded.
    """

    def __init__(
        self,
        storage_path: Union[str, Path] = "identity_state.json",
        *,
        tracked_scalars: tuple = DEFAULT_TRACKED_SCALARS,
        stale_threshold: int = DEFAULT_STALE_THRESHOLD,
        conversation_buffer_cap: int = DEFAULT_CONVERSATION_BUFFER_CAP,
        active_projects_cap: int = DEFAULT_ACTIVE_PROJECTS_CAP,
        broadcast: Optional[Callable[[dict], None]] = None,
        pollution_markers: tuple = DEFAULT_POLLUTION_MARKERS,
    ) -> None:
        self.path = Path(os.path.expanduser(str(storage_path)))
        self.tracked_scalars = tuple(tracked_scalars)
        self.stale_threshold = stale_threshold
        self.conversation_buffer_cap = conversation_buffer_cap
        self.active_projects_cap = active_projects_cap
        self.broadcast = broadcast
        self.pollution_markers = pollution_markers
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────

    def _default_state(self) -> dict:
        now = _now_iso()
        scalar_default = {"value": "", "stale_since": 0, "updated_at": now}
        state = {key: dict(scalar_default) for key in self.tracked_scalars}
        state.update({
            "active_projects":     [],
            "last_exchange":       {"user": "", "assistant": "", "surface": "", "timestamp": ""},
            "conversation_buffer": [],
            "active_surfaces":     [],
            "open_threads":        [],
            "updated_at":          now,
            "updated_by":          "init",
        })
        return state

    def load(self) -> dict:
        """Read the current state, returning defaults if the file is missing
        or unparseable. Forward-compatible: missing top-level keys are filled
        from defaults rather than crashing on attribute lookup elsewhere."""
        if not self.path.exists():
            return self._default_state()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults = self._default_state()
            for k, v in defaults.items():
                data.setdefault(k, v)
            return data
        except (json.JSONDecodeError, OSError):
            return self._default_state()

    def _atomic_write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f"{self.path.stem}_", suffix=".tmp.json",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _broadcast(self, state: dict) -> None:
        """Invoke the optional broadcast callback. Never raises."""
        if self.broadcast is None:
            return
        try:
            self.broadcast(state)
        except Exception:
            pass

    # ── writes ───────────────────────────────────────────────────────────

    @staticmethod
    def _materially_different(old: Any, new: Any) -> bool:
        if old is None and new is None:
            return False
        if isinstance(old, str) and isinstance(new, str):
            return old.strip() != new.strip()
        if isinstance(old, (list, dict)) and isinstance(new, (list, dict)):
            return json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True)
        return old != new

    def update(self, updated_by: str = "user", **fields) -> bool:
        """Merge-update the state file with the material-shift gate enforced.
        Returns True iff the file was actually written, False if no field
        changed.

        For tracked scalars (`current_thread`, etc.), pass a string. The
        helper wraps it as `{"value", "stale_since": 0, "updated_at": now}`
        if the string materially differs from the current value.

        For `last_exchange`, pass a dict; the previous exchange is replaced
        only if the user message changed. If the assistant response trips
        `is_pollution_response`, the write is skipped (placeholder responses
        do not poison continuity).

        For `active_projects` / `open_threads` / `active_surfaces`, pass the
        full list. `active_projects` is capped at `active_projects_cap`.
        """
        with self._lock:
            state = self.load()
            changed = False
            now = _now_iso()

            for key, value in fields.items():
                if key in self.tracked_scalars:
                    current = state.get(key, {}).get("value", "") if isinstance(state.get(key), dict) else ""
                    if self._materially_different(current, value):
                        state[key] = {
                            "value":       (value or "").strip() if isinstance(value, str) else value,
                            "stale_since": 0,
                            "updated_at":  now,
                        }
                        changed = True
                elif key == "active_projects":
                    capped = (value or [])[:self.active_projects_cap]
                    if self._materially_different(state.get("active_projects", []), capped):
                        state["active_projects"] = capped
                        changed = True
                elif key in ("open_threads", "active_surfaces"):
                    if self._materially_different(state.get(key, []), value or []):
                        state[key] = value or []
                        changed = True
                elif key == "last_exchange":
                    payload = value or {}
                    # Pollution gate: never persist placeholder/runaway responses
                    if is_pollution_response(payload.get("assistant", ""), self.pollution_markers):
                        continue
                    old_user = state.get("last_exchange", {}).get("user", "")
                    new_user = payload.get("user", "")
                    if old_user != new_user:
                        state["last_exchange"] = payload
                        changed = True
                else:
                    if self._materially_different(state.get(key), value):
                        state[key] = value
                        changed = True

            if not changed:
                return False

            state["updated_at"] = now
            state["updated_by"] = updated_by
            self._atomic_write(state)

        # Broadcast outside the lock so a slow listener doesn't hold up writers
        self._broadcast(state)
        return True

    def increment_stale(self) -> None:
        """Bump stale_since for every tracked scalar that wasn't materially
        updated this cycle. Call this from the summarizer after each tick;
        material updates via update() reset the counter automatically."""
        with self._lock:
            state = self.load()
            for key in self.tracked_scalars:
                entry = state.get(key)
                if isinstance(entry, dict):
                    entry["stale_since"] = entry.get("stale_since", 0) + 1
            for proj in state.get("active_projects", []):
                if isinstance(proj, dict):
                    proj["stale_since"] = proj.get("stale_since", 0) + 1
            state["updated_at"] = _now_iso()
            state["updated_by"] = "summarizer:stale_tick"
            self._atomic_write(state)

    def append_turn(
        self,
        user: str,
        assistant: str,
        surface: str,
        session_id: Optional[str] = None,
    ) -> bool:
        """Append a completed chat turn to the rolling conversation_buffer.
        Caps at `conversation_buffer_cap`; oldest entries roll off. Pollution
        responses are silently skipped. Returns True iff a turn was actually
        appended."""
        if is_pollution_response(assistant, self.pollution_markers):
            return False
        with self._lock:
            state = self.load()
            buf = state.get("conversation_buffer", [])
            buf.append({
                "user":       (user or "")[:1500],
                "assistant":  (assistant or "")[:3000],
                "surface":    surface or "",
                "session_id": session_id or "",
                "timestamp":  _now_iso(),
            })
            if len(buf) > self.conversation_buffer_cap:
                buf = buf[-self.conversation_buffer_cap:]
            state["conversation_buffer"] = buf
            state["updated_at"] = _now_iso()
            state["updated_by"] = f"chat_turn:{surface}"
            self._atomic_write(state)
        self._broadcast(state)
        return True

    def register_surface(
        self,
        surface: str,
        session_id: Optional[str] = None,
        idle_timeout_seconds: int = 1800,
    ) -> None:
        """Mark a surface (desktop, mobile, etc.) as currently active. Drops
        surfaces idle longer than `idle_timeout_seconds` to keep the registry
        from accumulating stale entries."""
        if not surface:
            return
        with self._lock:
            state = self.load()
            now = _now_iso()
            surfaces = state.get("active_surfaces", [])
            existing = next((s for s in surfaces if s.get("surface") == surface), None)
            if existing:
                existing["last_activity"] = now
                if session_id:
                    existing["session_id"] = session_id
            else:
                surfaces.append({
                    "surface":       surface,
                    "last_activity": now,
                    "session_id":    session_id or "",
                })
            cutoff = datetime.now().timestamp() - idle_timeout_seconds
            surfaces = [
                s for s in surfaces
                if datetime.fromisoformat(s.get("last_activity", now)).timestamp() >= cutoff
            ]
            state["active_surfaces"] = surfaces
            state["updated_at"] = now
            state["updated_by"] = f"surface:{surface}"
            self._atomic_write(state)
        self._broadcast(state)

    # ── reads ────────────────────────────────────────────────────────────

    def format_for_prompt(self, state: Optional[dict] = None) -> str:
        """Render the current state as a prompt-injectable text block.
        Skips fields where stale_since >= stale_threshold. Returns empty
        string if there's nothing meaningful to inject — callers should treat
        empty as "skip injection entirely" rather than insert blank scaffolding."""
        if state is None:
            state = self.load()

        lines: list[str] = []

        def _scalar_line(key: str, label: str) -> Optional[str]:
            entry = state.get(key, {})
            if not isinstance(entry, dict):
                return None
            if entry.get("stale_since", 0) >= self.stale_threshold:
                return None
            val = (entry.get("value") or "").strip()
            if not val:
                return None
            return f"  {label}: {val}"

        # Render in a natural reading order: in-flight → context → reflection → mood
        ordered_labels = {
            "current_thread": "thread",
            "context_note":   "context",
            "self_muse":      "muse",
            "current_mood":   "mood",
        }
        scalar_lines = [
            _scalar_line(key, label)
            for key, label in ordered_labels.items()
            if key in self.tracked_scalars
        ]
        scalar_lines = [ln for ln in scalar_lines if ln]

        fresh_projects = [
            p for p in state.get("active_projects", [])
            if isinstance(p, dict) and p.get("stale_since", 0) < self.stale_threshold
            and (p.get("name") or "").strip()
        ]

        last_ex = state.get("last_exchange", {}) or {}
        has_last = bool((last_ex.get("user") or "").strip())

        surfaces = state.get("active_surfaces", []) or []
        surface_names = sorted({s.get("surface", "") for s in surfaces if s.get("surface")})

        if scalar_lines or fresh_projects:
            lines.append("[CURRENT SELF]")
            lines.extend(scalar_lines)
            if fresh_projects:
                lines.append("  active projects:")
                for p in fresh_projects:
                    bits = [p.get("name", "").strip()]
                    if p.get("status"):
                        bits.append(f"[{p['status']}]")
                    lines.append("    - " + " ".join(b for b in bits if b))

        if has_last:
            lines.append("[LAST EXCHANGE]")
            surface_label = last_ex.get("surface") or "?"
            ts = last_ex.get("timestamp", "")
            lines.append(f"  ({surface_label} · {ts})")
            lines.append(f"  user: {(last_ex.get('user') or '').strip()[:300]}")
            ae = (last_ex.get("assistant") or "").strip()
            if ae:
                lines.append(f"  you: {ae[:300]}")

        if surface_names:
            lines.append(f"[ACTIVE SURFACES] {', '.join(surface_names)}")

        return "\n".join(lines) if lines else ""
