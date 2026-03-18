"""
SAFE Framework Integration — Law Gazelle (root)
================================================
Pigeon bus helpers for dropping messages to Willow.

Drop point: POST /api/pigeon/drop
Topics: ask, query, contribute, connect, status
"""

import os
import uuid
import requests
from typing import Optional

WILLOW_URL = os.environ.get("WILLOW_URL", "http://localhost:8420")
PIGEON_URL = f"{WILLOW_URL}/api/pigeon/drop"
APP_ID = "safe-app-law-gazelle"

_session_id = str(uuid.uuid4())


def ask(prompt: str, persona: Optional[str] = None, tier: str = "free") -> str:
    """Ask Willow a question. Returns the LLM response as a string."""
    result = _drop("ask", {"prompt": prompt, "persona": persona, "tier": tier})
    if result.get("ok"):
        return result.get("result", "")
    return f"[Error: {result.get('error', 'unknown')}]"


def query(q: str, limit: int = 5) -> list:
    """Query Willow's knowledge graph. Returns a list of matching atoms."""
    result = _drop("query", {"q": q, "limit": limit})
    if result.get("ok"):
        return result.get("result", [])
    return []


def contribute(content: str, category: str = "note", metadata: Optional[dict] = None) -> dict:
    """Contribute content to Willow's knowledge graph."""
    return _drop("contribute", {
        "content": content,
        "category": category,
        "metadata": metadata or {},
    })


def status() -> dict:
    """Check if Willow bus is reachable."""
    return _drop("status", {})


def _drop(topic: str, payload: dict) -> dict:
    """Internal: drop a message onto the Pigeon bus."""
    try:
        r = requests.post(PIGEON_URL, json={
            "topic": topic,
            "app_id": APP_ID,
            "session_id": _session_id,
            "payload": payload,
        }, timeout=30)
        return r.json() if r.ok else {"ok": False, "error": r.text}
    except requests.ConnectionError:
        return {
            "ok": False,
            "guest_mode": True,
            "error": f"Willow not reachable at {WILLOW_URL}. "
                     "Set WILLOW_URL env var or run Willow locally."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Re-export SAFESession from src for backward compatibility
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent / "src"))
    from safe_integration import SAFESession
except ImportError:
    SAFESession = None


# ── Willow Consent Helpers ────────────────────────────────────────────────────

def get_consent_status(token=None):
    """Check if this app has consent to contribute to the user's Willow."""
    try:
        import requests as _r
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = _r.get(f"{WILLOW_URL}/api/apps", headers=headers, timeout=10)
        apps = resp.json().get("apps", [])
        return next((a["consented"] for a in apps if a["app_id"] == APP_ID), False)
    except Exception:
        return False


def request_consent_url():
    """Return the Willow URL where the user can grant consent to this app."""
    return f"{WILLOW_URL}/apps?highlight={APP_ID}"



def send(to_app, subject, body, thread_id=None):
    """Send a message to another app's Pigeon inbox."""
    return _drop("send", {"to": to_app, "subject": subject, "body": body, "thread_id": thread_id})


def check_inbox(unread_only=True):
    """Fetch this app's Pigeon inbox from Willow."""
    try:
        import requests as _r
        r = _r.get(
            f"{WILLOW_URL}/api/pigeon/inbox",
            params={"app_id": APP_ID, "unread_only": str(unread_only).lower()},
            timeout=10
        )
        return r.json().get("messages", []) if r.ok else []
    except Exception:
        return []

