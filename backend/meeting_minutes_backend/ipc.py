from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_response(
    msg_id: str,
    msg_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "type": msg_type,
        "ok": True,
        "payload": payload or {},
        "error": None,
        "timestamp": now_iso(),
    }


def make_error(msg_id: str, msg_type: str, message: str) -> dict[str, Any]:
    return {
        "id": msg_id,
        "type": msg_type,
        "ok": False,
        "payload": None,
        "error": {"code": "backend_error", "message": message},
        "timestamp": now_iso(),
    }


def make_event(msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "",
        "type": msg_type,
        "payload": payload,
        "timestamp": now_iso(),
    }
