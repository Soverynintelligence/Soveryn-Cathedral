"""
Minimal Flask + Cathedral example.

Run with:
    pip install soveryn-cathedral[socket] flask requests
    python minimal_flask.py

Then open http://localhost:5000/ in two browser tabs and chat in either.
Both tabs see the conversation in real-time. The agent's self-state
(current_thread, context_note, self_muse) populates as the summarizer fires.

This example uses a placeholder LLM that just echoes the message — replace
`generate_response` with your actual model call (llama.cpp, vLLM, Ollama,
OpenAI-compatible endpoint, whatever).
"""
from flask import Flask, request, jsonify, Response
from soveryn_cathedral import IdentityCathedral, Summarizer
from soveryn_cathedral.socket import attach_websocket, broadcast_chat_turn_start, broadcast_chat_turn_end


# ── Bring your own LLM ────────────────────────────────────────────────────
def generate_response(user_message: str) -> str:
    """Replace with your actual model call."""
    return f"(echo) {user_message}"


def summarizer_ask_fn(meta_prompt: str) -> str:
    """The summarizer asks the agent to update her own state. In a real
    deployment this would route through the same LLM you use for chat,
    typically with a short max_tokens cap and persist=False so the
    self-update doesn't pollute conversation history."""
    return generate_response(meta_prompt)


# ── App setup ─────────────────────────────────────────────────────────────
app = Flask(__name__)

cathedral = IdentityCathedral(storage_path="./identity_state.json")
summarizer = Summarizer(cathedral, ask_fn=summarizer_ask_fn)
socketio = attach_websocket(app, cathedral)


# ── Routes ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return """
<!doctype html>
<html><head><title>Cathedral demo</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 2em auto; padding: 0 1em; }
  #log { border: 1px solid #ccc; padding: 1em; height: 400px; overflow-y: scroll; }
  .turn { margin-bottom: 0.5em; }
  .you { color: #555; }
  .ai  { color: #06c; }
  .state { background: #f5f5f5; padding: 0.5em; margin-top: 1em; font-family: monospace; font-size: 12px; white-space: pre-wrap; }
</style></head>
<body>
<h1>Cathedral demo</h1>
<p>Open this page in two tabs to see real-time cross-surface sync.</p>
<div id="log"></div>
<form onsubmit="send(event)" style="margin-top: 1em;">
  <input id="msg" autocomplete="off" style="width:80%" placeholder="Say something..." required>
  <button>Send</button>
</form>
<div id="state" class="state">connecting...</div>
<script>
  const socket = io('/identity');
  const log = document.getElementById('log');
  const stateEl = document.getElementById('state');

  function append(role, text) {
    const div = document.createElement('div');
    div.className = 'turn';
    div.innerHTML = `<span class="${role}">${role}:</span> ${text}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  socket.on('state_snapshot', (s) => {
    stateEl.textContent = JSON.stringify(s, null, 2);
    (s.conversation_buffer || []).forEach(t => {
      append('you', t.user);
      append('ai', t.assistant);
    });
  });
  socket.on('state_update', (s) => {
    stateEl.textContent = JSON.stringify(s, null, 2);
  });
  socket.on('chat_turn_start', (e) => {
    append('you', e.user);
  });
  socket.on('chat_turn_end', (e) => {
    append('ai', e.assistant);
  });

  async function send(ev) {
    ev.preventDefault();
    const input = document.getElementById('msg');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, surface: 'web'}),
    });
  }
</script>
</body></html>
"""


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json or {}
    message = data.get("message", "").strip()
    surface = data.get("surface", "web")
    if not message:
        return jsonify({"error": "empty message"}), 400

    # Announce the incoming turn so other tabs render the user message immediately
    broadcast_chat_turn_start(socketio, user=message, surface=surface)

    # Generate the response (replace with your real LLM)
    response = generate_response(message)

    # Persist the turn into identity state (cross-surface continuity signal)
    cathedral.register_surface(surface)
    cathedral.update(updated_by=f"chat_turn:{surface}", last_exchange={
        "user":      message,
        "assistant": response,
        "surface":   surface,
        "timestamp": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    })
    cathedral.append_turn(user=message, assistant=response, surface=surface)

    # Announce turn completion so other tabs finalize their rendering
    broadcast_chat_turn_end(socketio, user=message, assistant=response, surface=surface)

    # Maybe fire the self-summarizer
    if summarizer.note_turn(user_message=message):
        summarizer.fire_async()

    return jsonify({"response": response})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
