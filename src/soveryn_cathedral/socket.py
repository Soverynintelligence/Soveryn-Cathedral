"""
Optional Flask-SocketIO adapter for cross-surface real-time sync.

Install with: pip install soveryn-cathedral[socket]

Usage:

    from flask import Flask
    from soveryn_cathedral import IdentityCathedral
    from soveryn_cathedral.socket import attach_websocket

    app = Flask(__name__)
    cathedral = IdentityCathedral(storage_path="./identity.json")
    socketio = attach_websocket(app, cathedral)

    if __name__ == "__main__":
        socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)

Clients subscribe to the `/identity` namespace, receive a `state_snapshot`
event on connect, and receive `state_update` events whenever the cathedral
writes. Bring-your-own-transport users can ignore this module entirely and
plumb the cathedral's `broadcast` callback into any other transport.
"""
from __future__ import annotations

from typing import Optional

try:
    from flask import request
    from flask_socketio import SocketIO, emit, join_room, leave_room
except ImportError as e:
    raise ImportError(
        "soveryn_cathedral.socket requires flask and flask-socketio. "
        "Install with: pip install soveryn-cathedral[socket]"
    ) from e


_IDENTITY_ROOM = "identity"


def attach_websocket(
    app,
    cathedral,
    *,
    namespace: str = "/identity",
    cors_allowed_origins: str = "*",
    async_mode: str = "threading",
) -> SocketIO:
    """Wire up a Flask-SocketIO server bound to `app` and connect it to
    `cathedral` so every state change is broadcast to connected clients.

    Returns the SocketIO instance so the caller can run it with
    `socketio.run(app, ...)`.

    Args:
        app: a Flask application instance.
        cathedral: an IdentityCathedral instance.
        namespace: WebSocket namespace path. Default `/identity`.
        cors_allowed_origins: CORS policy. Default `*`. Tighten if you
            expose this server to the public internet.
        async_mode: `threading` (default), `eventlet`, or `gevent`.
    """
    socketio = SocketIO(
        app,
        cors_allowed_origins=cors_allowed_origins,
        async_mode=async_mode,
        logger=False,
        engineio_logger=False,
    )

    # Wire the cathedral's broadcast hook to push to all connected clients
    def _push(state: dict) -> None:
        try:
            socketio.emit("state_update", state, namespace=namespace, to=_IDENTITY_ROOM)
        except Exception:
            pass

    cathedral.broadcast = _push

    @socketio.on("connect", namespace=namespace)
    def _on_connect(auth: Optional[dict] = None):
        join_room(_IDENTITY_ROOM)
        emit("state_snapshot", cathedral.load())

    @socketio.on("disconnect", namespace=namespace)
    def _on_disconnect():
        leave_room(_IDENTITY_ROOM)

    @socketio.on("surface_register", namespace=namespace)
    def _on_surface_register(data):
        if not isinstance(data, dict):
            return
        surface = (data.get("surface") or "").strip()
        session_id = data.get("session_id", "")
        if surface:
            cathedral.register_surface(surface, session_id=session_id or None)

    return socketio


def broadcast_chat_token(socketio: SocketIO, token: str, session_id: str = "",
                         namespace: str = "/identity") -> None:
    """Helper: stream a single token to all viewports during agent generation.
    The originating client typically dedupes by session_id since it already
    received the token via its own SSE/streaming channel."""
    try:
        socketio.emit(
            "chat_token",
            {"token": token or "", "session_id": session_id},
            namespace=namespace,
            to=_IDENTITY_ROOM,
        )
    except Exception:
        pass


def broadcast_chat_turn_start(socketio: SocketIO, user: str, surface: str,
                              session_id: str = "",
                              namespace: str = "/identity") -> None:
    """Helper: announce a new turn beginning. Observer clients render the
    user message + typing indicator immediately."""
    try:
        socketio.emit(
            "chat_turn_start",
            {
                "user":       (user or "")[:1500],
                "surface":    surface or "",
                "session_id": session_id,
            },
            namespace=namespace,
            to=_IDENTITY_ROOM,
        )
    except Exception:
        pass


def broadcast_chat_turn_end(socketio: SocketIO, user: str, assistant: str,
                            surface: str, session_id: str = "",
                            namespace: str = "/identity") -> None:
    """Helper: turn completed. Observer clients finalize their rendering."""
    try:
        socketio.emit(
            "chat_turn_end",
            {
                "user":       (user or "")[:1500],
                "assistant":  (assistant or "")[:3000],
                "surface":    surface or "",
                "session_id": session_id,
            },
            namespace=namespace,
            to=_IDENTITY_ROOM,
        )
    except Exception:
        pass
