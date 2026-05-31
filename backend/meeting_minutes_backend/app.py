from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from . import __version__
from .ipc import make_error, make_event, make_response


STARTED_AT = datetime.now(timezone.utc).isoformat()

app = FastAPI(title="meeting-minutes-backend", version=__version__)


@app.on_event("shutdown")
async def _shutdown_cleanup() -> None:
    """FastAPI shutdown 觸發時清理 MLX worker subprocess，避免變孤兒。"""
    try:
        from .faster_asr import _shutdown_all_executors
        _shutdown_all_executors()
    except Exception:
        pass


# Single-user desktop app: one active session at a time.
# The /ws/audio endpoint looks up the active session to push PCM data.
_active_session: "Session | None" = None


class Session:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._stream = None

    async def _send_event(self, event: dict[str, Any]) -> None:
        await self.websocket.send_json(make_event(event["type"], event))

    @property
    def stream(self):
        if self._stream is None:
            from .stream_service import StreamService

            self._stream = StreamService(emit=self._send_event)
        return self._stream


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "started_at": STARTED_AT,
    }


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    global _active_session
    await websocket.accept()
    session = Session(websocket)
    _active_session = session
    try:
        while True:
            msg = await websocket.receive_json()
            await handle_message(session, msg)
    except WebSocketDisconnect:
        if session._stream is not None:
            session.stream.request_stop()
        if _active_session is session:
            _active_session = None


@app.websocket("/ws/audio")
async def ws_audio(websocket: WebSocket) -> None:
    """Binary WebSocket that receives raw Float32-LE mono PCM from the Electron renderer.

    The Electron renderer uses getUserMedia() + ScriptProcessorNode to capture the
    microphone at 16 kHz and sends each audio buffer as a binary ArrayBuffer over
    this connection. The active StreamService accumulates the frames and cuts them
    into WAV chunks for the faster-whisper ASR pipeline.
    """
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_bytes()
            if _active_session is not None and _active_session._stream is not None:
                await _active_session.stream.push_pcm(data)
    except WebSocketDisconnect:
        if _active_session is not None and _active_session._stream is not None:
            _active_session.stream.notify_recording_ended()


async def _download_model_bg(session: "Session", repo: str, fname: str) -> None:
    """背景執行 GGUF 模型下載，完成後以 WebSocket 事件通知前端。"""
    import asyncio as _asyncio
    import concurrent.futures as _cf
    from .correction_worker import download_model

    loop = _asyncio.get_running_loop()

    def _emit_log(payload: dict) -> None:
        _asyncio.run_coroutine_threadsafe(session._send_event(payload), loop)

    def _do_download():
        return download_model(repo, fname, _emit_log)

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            ok = await loop.run_in_executor(pool, _do_download)
        event_type = "correction.local_model_ready" if ok else "correction.local_model_error"
        await session.websocket.send_json(make_event(event_type, {"cached": ok}))
    except Exception as err:
        try:
            await session.websocket.send_json(make_event("correction.local_model_error", {"error": str(err)}))
        except Exception:
            pass


async def _run_summary_bg(session: "Session") -> None:
    """背景執行摘要生成，完成後以 WebSocket 事件通知前端。"""
    result = await session.stream.generate_summary()
    try:
        from .ipc import make_event
        if result.get("ok"):
            await session.websocket.send_json(make_event("summary.done", {"path": result["path"]}))
        else:
            await session.websocket.send_json(make_event("summary.error", {"message": result.get("error", "")}))
    except Exception:
        pass


async def handle_message(session: Session, msg: dict[str, Any]) -> None:
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "")
    payload = msg.get("payload") or {}

    try:
        if msg_type == "ping":
            await session.websocket.send_json(make_response(msg_id, msg_type, {"pong": True}))
        elif msg_type == "stream.devices":
            # Device listing is now handled by the Electron renderer via
            # navigator.mediaDevices.enumerateDevices(). Return an empty list so
            # older callers don't break.
            await session.websocket.send_json(
                make_response(msg_id, msg_type, {"devices": []})
            )
        elif msg_type == "stream.start":
            state = await session.stream.start(
                output_dir=payload.get("output_dir"),
                segment_seconds=float(payload.get("segment_seconds", 2.0)),

                language=payload.get("language") or None,
                dual_track=bool(payload.get("dual_track", False)),
            )
            await session.websocket.send_json(make_response(msg_id, msg_type, {"state": state}))
        elif msg_type == "stream.stop":
            state = await session.stream.stop()
            await session.websocket.send_json(make_response(msg_id, msg_type, {"state": state}))
        elif msg_type == "stream.state":
            await session.websocket.send_json(
                make_response(msg_id, msg_type, {"state": session.stream.snapshot()})
            )
        elif msg_type == "stream.asr_ready":
            from .stream_service import _preload_ready
            await session.websocket.send_json(
                make_response(msg_id, msg_type, {"ready": _preload_ready.is_set()})
            )
        elif msg_type == "settings.get":
            from . import settings as _settings_mod
            await session.websocket.send_json(make_response(msg_id, msg_type, {
                "values": _settings_mod.load(),
                "schema": _settings_mod.schema(),
                "defaults": _settings_mod.defaults(),
            }))
        elif msg_type == "settings.update":
            from . import settings as _settings_mod
            incoming = payload.get("values") or {}
            saved = _settings_mod.save(incoming)
            await session.websocket.send_json(make_response(msg_id, msg_type, {"values": saved}))
        elif msg_type == "summary.generate":
            # 立即回 ack，避免前端 15s timeout；實際生成在背景跑，完成後推事件
            await session.websocket.send_json(make_response(msg_id, msg_type, {"ack": True}))
            import asyncio as _asyncio
            _asyncio.ensure_future(_run_summary_bg(session))
        elif msg_type == "correction.local_model_status":
            from .correction_worker import check_model_cached
            repo = payload.get("model_repo", "")
            fname = payload.get("model_file", "")
            cached = check_model_cached(repo, fname) if repo and fname else False
            await session.websocket.send_json(make_response(msg_id, msg_type, {"cached": cached}))
        elif msg_type == "correction.local_model_download":
            repo = payload.get("model_repo", "")
            fname = payload.get("model_file", "")
            await session.websocket.send_json(make_response(msg_id, msg_type, {"ack": True}))
            import asyncio as _asyncio
            _asyncio.ensure_future(_download_model_bg(session, repo, fname))
        else:
            await session.websocket.send_json(make_error(msg_id, msg_type, f"unknown message: {msg_type}"))
    except Exception as err:  # noqa: BLE001
        await session.websocket.send_json(make_error(msg_id, msg_type, str(err)))
