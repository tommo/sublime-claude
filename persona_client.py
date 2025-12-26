"""
Persona client - fetch and acquire personas from REST API.
"""
import json
import urllib.request
import urllib.error
from typing import Optional, List, Dict, Any


DEFAULT_BASE_URL = "http://localhost:5050/personas"


def _request(url: str, method: str = "GET", data: dict = None, timeout: float = 5.0) -> dict:
    """Make HTTP request to persona API."""
    try:
        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")

        body = json.dumps(data).encode() if data else None
        with urllib.request.urlopen(req, body, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read().decode())
            return {"error": error_body.get("error", str(e))}
        except:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def list_personas(base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    """List all available personas."""
    result = _request(f"{base_url}/")
    if isinstance(result, list):
        return result
    return []


def get_persona(persona_id: int, base_url: str = DEFAULT_BASE_URL) -> Optional[Dict[str, Any]]:
    """Get persona details including ability version."""
    result = _request(f"{base_url}/{persona_id}")
    if "error" in result:
        return None
    return result


def acquire_persona(
    session_id: str,
    persona_id: int = None,
    tags: str = None,
    base_url: str = DEFAULT_BASE_URL
) -> Dict[str, Any]:
    """Acquire a persona for a session.

    Returns:
        {
            "persona": {...},
            "ability": {"system_prompt": "...", "model": "..."},
            "handoff_notes": "..."
        }
    """
    data = {"session_id": session_id}
    if persona_id:
        data["persona_id"] = persona_id
    elif tags:
        data["tags"] = tags

    return _request(f"{base_url}/acquire", method="POST", data=data)


def release_persona(
    session_id: str,
    handoff_notes: str = None,
    base_url: str = DEFAULT_BASE_URL
) -> Dict[str, Any]:
    """Release a persona from a session."""
    data = {"session_id": session_id}
    if handoff_notes:
        data["handoff_notes"] = handoff_notes

    return _request(f"{base_url}/release", method="POST", data=data)


def log_work(
    session_id: str,
    action: str,
    summary: str,
    work_item_id: str = None,
    work_provider: str = None,
    base_url: str = DEFAULT_BASE_URL
) -> Dict[str, Any]:
    """Log work for the persona's worklog."""
    data = {
        "session_id": session_id,
        "action": action,
        "summary": summary
    }
    if work_item_id:
        data["work_item_id"] = work_item_id
    if work_provider:
        data["work_provider"] = work_provider

    return _request(f"{base_url}/worklog", method="POST", data=data)
