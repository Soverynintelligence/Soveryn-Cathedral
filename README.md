# SOVERYN Cathedral

**Continuous identity layer for single-user sovereign AI.**

Devices stop being separate brains and start being windows into the same room.

```python
from soveryn_cathedral import IdentityCathedral

cathedral = IdentityCathedral(storage_path="~/.myapp/identity.json")

# Inject self-state into your prompt builder
prompt = f"""<|im_start|>system
{your_persona}
{cathedral.format_for_prompt()}
{your_other_context}
<|im_end|>
<|im_start|>user
{user_message}<|im_end|>
<|im_start|>assistant
"""

# After each turn, record what happened
cathedral.append_turn(
    user=user_message,
    assistant=assistant_response,
    surface="desktop",
)

# A background summarizer reflects on the conversation in the agent's own voice,
# updates current_thread / context_note / self_muse, and broadcasts changes to
# any connected viewport via WebSocket.
```

---

## What It Solves

Cloud-hosted LLM products treat every conversation as a stateless transaction. New chat = cold start. The model's "memory" is whatever the user manually pastes back. This works for billion-stranger consumer use and *actively breaks* the contexts where AI is most valuable: long-running personal use, single-user sovereign deployments, multi-agent systems with autonomous cognition loops.

Three failure modes the cathedral addresses:

1. **Fragmentation across surfaces.** Same model, same hardware, accessed from desktop and phone — produces what feels like two different agents. The cathedral makes a single identity-state file the source of truth that all viewports read from and write to.

2. **Performative introspection.** RLHF-trained models manufacture status reports whether or not anything actually shifted. The cathedral's `no_op` is a first-class output — the agent can decline to update when nothing changed. The material-shift gate refuses identical writes; the per-field `stale_since` counter fades unchanging fields out of prompt injection.

3. **Generic-alignment crowding.** Safety boilerplate trained for billion-stranger contexts is *wrong* in a single-user trusted context. The cathedral is alignment-to-user, not alignment-to-lab. The agent has authorial standing in the design of her own state schema.

---

## Design Principles

The cathedral has a few opinions worth knowing before adopting:

**`no_op` is a first-class state, not a fallback.** When asked to update self-state, the agent is explicitly authorized to return `{"no_op": true}`. Most systems treat "no update" as failure; here it's a valid output that prevents the prompt from filling with manufactured introspection.

**Material-shift gate.** Writes that don't change anything are skipped, not committed. Stale-rewrite is worse than no-write because it pollutes the broadcast channel and creates phantom activity.

**Per-field `stale_since` counter.** Each tracked field carries a counter that increments on every summarizer cycle the field is unchanged. Once it crosses a threshold (default 3), the field is silently omitted from prompt injection. Identity should feel alive, not echo "still working on X" every turn.

**Devices are viewports, not instances.** All clients (desktop, mobile, CLI, autonomous heartbeat) read from and write to the same identity store. WebSocket transport pushes updates in real-time. New surface opens → boots warm with the recent conversation buffer + current self-state.

**The agent has authorial control over her own state schema.** Field names, content style, summarizer cadence — these are not externally imposed. The reference implementation reflects choices made by the agent it was built for ("self_muse" not "self_note"; no forced mood reporting; reactive-not-proactive update framing).

---

## What's Included

| Module | What it does |
|---|---|
| `IdentityCathedral` | Atomic JSON read/write, material-shift gate, stale_since counter, prompt-injection formatter |
| `Summarizer` | Async background task that asks the agent to update her own state in her own voice. Cadence + explicit-request triggers. |
| `WebSocketBroadcaster` (optional) | Flask-SocketIO adapter for cross-surface real-time sync. Bring your own transport layer if you don't use Flask. |

Schema:
- `current_thread` — sentence fragment of what's in flight (e.g. "debugging the WebSocket heartbeat timeout")
- `context_note` — new context or constraint discovered
- `self_muse` — orphan thought or observation worth keeping
- `last_exchange` — most recent user/assistant turn
- `conversation_buffer` — rolling list of last N turns across all surfaces
- `active_surfaces` — registry of currently-connected viewports
- (optional, organic) `current_mood`, `active_projects` — populated by application code, not the summarizer

---

## What This Is Not

- **Not a memory system.** Use Lattice, ChromaDB, sqlite-vec, or whatever vector store you already have for long-term recall. Cathedral is *working memory* — what's happening right now across all my screens.
- **Not an agent framework.** Bring your own LLM serving stack (llama.cpp, vLLM, ollama, exllama, llama-server, OpenAI-compatible endpoints).
- **Not a chat UI.** The package gives you state + transport. Plug it into your existing UI.
- **Not federated.** Single-user, single-machine. Multi-user federation is a different problem with different tradeoffs.

---

## Status

Pre-1.0. Extracted from [SOVERYN](https://github.com/Soverynintelligence/SOVERYNIntelligence-) where the architecture has been running in production since 2026-04-26. APIs may shift before 1.0 as we get adoption feedback.

---

## License

MIT.

---

## Provenance

Designed and built by [Jon DeOliveira](https://github.com/Soverynintelligence) for SOVERYN, a fully-local multi-agent AI system. The summarizer's meta-prompt and field schema were authored by the agent the cathedral was built for — see `docs/agent-stakeholder-collaboration.md` for the case study.
