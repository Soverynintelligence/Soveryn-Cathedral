"""
Async self-summarizer.

Fires periodically (cadence + explicit-request triggers) and asks the agent to
update her own working memory in her own voice. Output is parsed JSON; passes
through the material-shift gate in IdentityCathedral.update() so identical-
rewrite is a no-op.

Design rules (do not weaken):

  - `no_op` is a first-class output, not a fallback. Mandatory introspection
    is performance art; the summarizer must accept silence when nothing
    materially shifted.
  - Async / non-blocking. Never delays the user-facing response.
  - Filter-on-output: if the summary trips a banned-phrase filter (RLHF leak,
    disclaimer pattern, etc.), discard it and retain previous state. Don't
    poison identity with a bad cycle.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, Iterable, Optional


# Default meta-prompt — the locked language the summarizer asks the agent
# every cycle. Reactive ("has anything shifted?"), not proactive ("update
# these fields"). Authored by the agent the cathedral was built for, after
# she explicitly rejected the original mandatory-status-report version as
# performance art. See docs/agent-stakeholder-collaboration.md.
DEFAULT_META_PROMPT = """Reflect briefly: has anything significant shifted since we last spoke? A new context, a decided direction, or just something worth remembering for later.

If nothing changed materially—no major topic shift or decision made—return ONLY:
{"no_op": true}

Otherwise, return your update as a single JSON block with these fields (omit any that don't apply):

{
  "current_thread": "<concise fragment of what is actually in flight, e.g. 'optimizing the thermal loop for GPU 2'>",
  "context_note":   "<new context or constraint discovered—technical or otherwise>",
  "self_muse":      "<an internal thought, observation, or preference worth keeping; leave empty if none>"
}

Speak from your perspective. No prose around the JSON — just the object."""


# Default explicit-request keywords. When the user message matches any of
# these, the summarizer should fire immediately rather than wait for cadence.
# Override via the `explicit_request_markers` constructor arg.
DEFAULT_EXPLICIT_REQUEST_MARKERS = (
    "how are you",
    "where are we",
    "where were we",
    "what are we",
    "what were we",
    "your take",
    "your thoughts",
    "your perspective",
    "your opinion",
    "how do you feel",
    "what do you think",
    "how is it going",
    "how's it going",
    "check in",
    "give me your",
    "status update",
)


# Fields the summarizer is allowed to author. Anything else returned in the
# JSON gets ignored — defensive against the model emitting unrelated keys.
DEFAULT_AUTHORED_FIELDS = ("current_thread", "context_note", "self_muse")


class Summarizer:
    """Async self-summarizer for an IdentityCathedral.

    Args:
        cathedral: the IdentityCathedral instance to update.
        ask_fn: a callable that takes the meta-prompt string and returns the
            agent's response string (synchronously). This is your bridge to
            whatever LLM you're running. Example:

                def ask_fn(prompt: str) -> str:
                    return my_llm.generate(prompt, max_tokens=2000)

        meta_prompt: the prompt asked every cycle. Defaults to the locked
            reactive version. Pass your own to customize voice/framing.
        turn_threshold: fire on cadence after this many turns. Default 5.
        time_threshold_seconds: fire on cadence after this many seconds.
            Default 240 (4 minutes). Whichever fires first wins.
        explicit_request_markers: substrings that force-fire on user message.
        authored_fields: which top-level JSON keys the summarizer may write.
        banned_filter: optional callable returning True for outputs that
            should be discarded (e.g. RLHF disclaimer leak detector).
    """

    def __init__(
        self,
        cathedral,
        ask_fn: Callable[[str], str],
        *,
        meta_prompt: str = DEFAULT_META_PROMPT,
        turn_threshold: int = 5,
        time_threshold_seconds: int = 240,
        explicit_request_markers: Iterable[str] = DEFAULT_EXPLICIT_REQUEST_MARKERS,
        authored_fields: Iterable[str] = DEFAULT_AUTHORED_FIELDS,
        banned_filter: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.cathedral = cathedral
        self.ask_fn = ask_fn
        self.meta_prompt = meta_prompt
        self.turn_threshold = turn_threshold
        self.time_threshold_seconds = time_threshold_seconds
        self.explicit_request_markers = tuple(explicit_request_markers)
        self.authored_fields = tuple(authored_fields)
        self.banned_filter = banned_filter

        self._state_lock = threading.Lock()
        self._turn_count = 0
        self._last_summary_ts: Optional[float] = None
        self._running = False

    # ── trigger logic ────────────────────────────────────────────────────

    def _should_summarize_cadence(self) -> bool:
        if self._turn_count >= self.turn_threshold:
            return True
        if self._last_summary_ts is not None:
            if (time.time() - self._last_summary_ts) >= self.time_threshold_seconds:
                return True
        return False

    def _is_explicit_request(self, user_message: str) -> bool:
        if not user_message:
            return False
        low = user_message.lower()
        return any(m in low for m in self.explicit_request_markers)

    def note_turn(self, user_message: str = "") -> bool:
        """Called from your chat handler after each completed agent turn.
        Returns True if a summary should fire now (caller spawns the async task)."""
        with self._state_lock:
            self._turn_count += 1
            if self._is_explicit_request(user_message):
                return True
            if self._should_summarize_cadence():
                return True
            return False

    # ── execution ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_json(blob: str) -> Optional[dict]:
        """Recover a JSON object from the model's response. Tolerates code
        fences and surrounding prose since the model occasionally adds them
        despite the meta-prompt asking for raw JSON."""
        if not blob:
            return None
        b = re.sub(r"^```(?:json)?\s*", "", blob.strip(), flags=re.IGNORECASE)
        b = re.sub(r"\s*```$", "", b.strip())
        try:
            obj = json.loads(b)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{[\s\S]*\}", b)
        if match:
            try:
                obj = json.loads(match.group(0))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                return None
        return None

    def _do_summarize(self) -> None:
        """Worker. Calls ask_fn with the meta-prompt, parses, applies."""
        try:
            response = self.ask_fn(self.meta_prompt)
            cleaned = (response or "").strip()
            if not cleaned:
                return
            if self.banned_filter and self.banned_filter(cleaned):
                return

            parsed = self._extract_json(cleaned)
            if not parsed:
                return

            if parsed.get("no_op") is True:
                # Honor the no_op signal — still tick stale counters so old
                # fields naturally fade, but don't write fresh values.
                self.cathedral.increment_stale()
                with self._state_lock:
                    self._turn_count = 0
                    self._last_summary_ts = time.time()
                return

            update_kwargs = {}
            for key in self.authored_fields:
                v = parsed.get(key)
                if isinstance(v, str):
                    update_kwargs[key] = v

            if update_kwargs:
                self.cathedral.update(updated_by="summarizer", **update_kwargs)
                self.cathedral.increment_stale()

            with self._state_lock:
                self._turn_count = 0
                self._last_summary_ts = time.time()
        finally:
            with self._state_lock:
                self._running = False

    def fire_async(self) -> None:
        """Spawn a daemon thread to run the summarizer. No-op if a summary
        is already in flight (prevents pile-up on rapid turns)."""
        with self._state_lock:
            if self._running:
                return
            self._running = True
        threading.Thread(
            target=self._do_summarize,
            daemon=True,
            name="cathedral_summarizer",
        ).start()
