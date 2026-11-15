"""SSE 端点。

连接建立时先补发历史日志与状态快照，客户端不会看到空白页。
断线由浏览器 EventSource 自动重连，服务端只需要正确清理订阅。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..engine import engine
from ..hub import hub

router = APIRouter()

HEARTBEAT_SECONDS = 15.0


def _frame(event_type: str, data) -> str:
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"event: message\ndata: {payload}\n\n"


@router.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = hub.subscribe()

    async def stream():
        try:
            # 补发历史：先日志，再全量状态
            for entry in hub.history(200):
                yield _frame("log", entry)
            snapshot = engine.snapshot()
            yield _frame("state", snapshot)
            yield _frame("services", _service_snapshot())
            yield _frame("runtime", _bootstrap_snapshot())

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # 注释行当心跳，防代理掐连接
                    yield ": ping\n\n"
                    continue
                yield _frame(payload["type"], payload["data"])
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _service_snapshot() -> dict:
    try:
        from ..services import services
        services.refresh()
        return services.status()
    except Exception:                               # noqa: BLE001
        return {}


def _bootstrap_snapshot() -> dict:
    try:
        from .. import bootstrap
        return bootstrap.probe()
    except Exception:                               # noqa: BLE001
        return {}
