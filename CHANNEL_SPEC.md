# Channel Mode Support for sublime-claude

## Overview

Add support for notalone channel mode - bidirectional sync communication between external services (like FACT games) and agent sessions.

## Current State

`notalone.py` handles `inject` messages (fire-and-forget):
```python
if "inject" in msg:
    self._handle_inject(msg["inject"])
```

## Required Changes

### 1. Handle `channel` messages in `_listen_loop`

In `notalone.py`, around line 100-104, add channel handling:

```python
if "inject" in msg:
    self._handle_inject(msg["inject"])
elif "channel" in msg:
    self._handle_channel(msg["channel"])
```

### 2. Add `_handle_channel` method

```python
def _handle_channel(self, channel_msg: dict):
    """Handle a channel message - requires sync response."""
    channel_id = channel_msg.get("channel_id", "")
    session_id = channel_msg.get("session_id", "")
    data = channel_msg.get("data", {})

    # Parse session_id: "sublime.{view_id}"
    parts = session_id.split(".", 1)
    if len(parts) != 2 or parts[0] != POOL_PREFIX:
        self._send_channel_response(channel_id, {"error": "invalid session"})
        return

    try:
        view_id = int(parts[1])
    except ValueError:
        self._send_channel_response(channel_id, {"error": "invalid view_id"})
        return

    # Queue on main thread with response callback
    sublime.set_timeout(
        lambda: self._process_channel(view_id, channel_id, data), 0
    )
```

### 3. Add `_process_channel` method

```python
def _process_channel(self, view_id: int, channel_id: str, data: dict):
    """Process channel message on main thread."""
    from .session_manager import get_session_manager

    manager = get_session_manager()
    session = manager.get_session_by_view_id(view_id)

    if not session:
        self._send_channel_response(channel_id, {"error": "session not found"})
        return

    # Format data as user message
    screen = data.get("screen", "")
    state = data.get("state", {})

    user_msg = f"Game Screen:\n```\n{screen}\n```\n\nGame State: {json.dumps(state)}"

    # Send to session and wait for response
    def on_response(response_text: str):
        self._send_channel_response(channel_id, response_text)

    session.send_message_with_callback(user_msg, on_response)
```

### 4. Add `_send_channel_response` method

```python
def _send_channel_response(self, channel_id: str, response):
    """Send response back to daemon."""
    try:
        # Use sync command (new connection)
        req = {
            "method": "channel_respond",
            "channel_id": channel_id,
            "response": response
        }
        _sync_command(req)
    except Exception as e:
        logger.error(f"notalone: failed to send channel response: {e}")
```

### 5. Session needs `send_message_with_callback`

The session class needs a method to send a message and get a callback when Claude responds:

```python
def send_message_with_callback(self, message: str, callback: Callable[[str], None]):
    """Send message and call callback with Claude's response."""
    # This needs to:
    # 1. Inject message as user turn
    # 2. Wait for Claude to complete response
    # 3. Extract response text
    # 4. Call callback(response_text)
```

## Key Difference from Inject

- **Inject**: Fire-and-forget. Pool queues message, agent sees it when ready.
- **Channel**: Sync request-response. Pool must respond back with agent's answer.

## Testing

1. Start notalone daemon with channel support
2. Start a FACT game with playroom
3. Open channel to a sublime session
4. Game sends screen → Agent responds → Game executes

## Protocol Reference

See `/Volumes/prj/ai/notalone2/PROTOCOL.md` - "Channel Mode" section.
