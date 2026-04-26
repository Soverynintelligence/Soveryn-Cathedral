"""
SOVERYN Cathedral — continuous identity layer for single-user sovereign AI.

The minimal usable surface:

    from soveryn_cathedral import IdentityCathedral

    cathedral = IdentityCathedral(storage_path="~/.myapp/identity.json")
    prompt_block = cathedral.format_for_prompt()
    cathedral.append_turn(user="hi", assistant="hi back", surface="desktop")

For async self-summarization, see Summarizer. For real-time cross-surface
sync, see the optional `socket` extra (`pip install soveryn-cathedral[socket]`).
"""
from __future__ import annotations

from .state import IdentityCathedral, is_pollution_response
from .summarizer import Summarizer

__all__ = [
    "IdentityCathedral",
    "Summarizer",
    "is_pollution_response",
    "__version__",
]

__version__ = "0.1.0"
