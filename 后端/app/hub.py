"""事件总线：日志环形缓冲 + SSE 广播。

流水线跑在线程池里，而 SSE 订阅者活在 asyncio 事件循环上。跨这条边界的安全做法是
`loop.call_soon_threadsafe`，本模块把这件事收口，业务代码只需要 `hub.log(...)`。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from typing import Any

MAX_HISTORY = 1000
SUBSCRIBER_QUEUE_SIZE = 512

# 事件类型（前端按 type 分发）
EVENT_LOG = "log"
EVENT_TASK = "task"
EVENT_FILE = "file"
EVENT_STATS = "stats"
EVENT_REVIEW = "review"
EVENT_SERVICE = "service"
EVENT_PROGRESS = "progress"      # 下载 / 安装等长任务进度


class Hub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._history: list[dict[str, Any]] = []
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = itertools.count(1)
        self._started = time.time()

    # ---------- 事件循环绑定 ----------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由 FastAPI 启动钩子调用一次。"""
        self._loop = loop

    # ---------- 日志 ----------

    def log(self, level: str, message: str, meta: str = "", source: str = "engine") -> dict[str, Any]:
        entry = {
            "id": next(self._seq),
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "meta": meta,
            "source": source,
        }
        with self._lock:
            self._history.append(entry)
            if len(self._history) > MAX_HISTORY:
                del self._history[: len(self._history) - MAX_HISTORY]
        self.publish(EVENT_LOG, entry)
        return entry

    def debug(self, message: str, meta: str = "") -> None:
        self.log("debug", message, meta)

    def info(self, message: str, meta: str = "") -> None:
        self.log("info", message, meta)

    def warn(self, message: str, meta: str = "") -> None:
        self.log("warn", message, meta)

    def error(self, message: str, meta: str = "") -> None:
        self.log("error", message, meta)

    # ---------- 广播 ----------

    def publish(self, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": data}
        loop = self._loop
        with self._lock:
            targets = list(self._subscribers)
        if not targets:
            return
        if loop is None or loop.is_closed():
            # 事件循环还没起来（启动早期）或已关闭：只入历史，不广播。
            return
        for queue in targets:
            try:
                loop.call_soon_threadsafe(self._offer, queue, payload)
            except RuntimeError:
                # 循环正在关闭，丢弃这一条即可。
                pass

    @staticmethod
    def _offer(queue: asyncio.Queue, payload: dict[str, Any]) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 订阅者消费不过来（比如浏览器卡住）：丢最旧的一条，保证推最新的。
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ---------- 订阅 ----------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ---------- 历史 ----------

    def history(self, limit: int = 300, level: str = "all") -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if level and level != "all":
            items = [e for e in items if e["level"] == level]
        return items[-limit:]

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def reset_history(self) -> None:
        """清空历史并通知所有订阅者清屏。"""
        self.clear_history()
        self.publish("logs:clear", {})

    @property
    def started_at(self) -> float:
        return self._started


hub = Hub()
