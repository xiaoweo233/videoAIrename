"""任务引擎：队列、并发调度、统计、审批、熔断、停止。

并发模型：
- `runtime.ai_workers` 个文件同时处理（视觉模型是瓶颈，按显存调）。
- 每个文件内部的抽帧走一个共享线程池（`frames.workers`），避免多个文件各开一个池把机器打死。
- 本地 Whisper 在 `transcribe.py` 里自己加锁串行 —— GPU 只有一个。

停止语义：`StopToken` 只在**文件边界**检查，正在跑的文件让它跑完。这样不会留下半截文件。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import media, naming, outputs, paths
from .hub import hub
from .pipeline import CANCELED, FAILED, PENDING, REVIEW, RUNNING, SKIPPED, SUCCESS, FileItem, Pipeline
from .services import services
from .transcribe import transcriber

CIRCUIT_BREAKER_THRESHOLD = 5       # 连续失败这么多个文件就自动停
REVIEWS_FILE = "reviews.json"

IDLE, RUNNING_T, STOPPED, DONE, ERROR = "idle", "running", "stopped", "done", "error"


class Engine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.queue: list[FileItem] = []
        self.reviews: list[dict[str, Any]] = []

        self.status = IDLE
        self.progress = 0.0
        self.current_id: str | None = None
        self.started_at = 0.0
        self.finished_at = 0.0
        self.total = 0
        self.done_count = 0
        self.message = ""

        self.stats = {"success": 0, "skipped": 0, "failed": 0, "canceled": 0,
                      "seconds": 0.0, "completed": 0}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_pool: ThreadPoolExecutor | None = None
        self._pipeline = Pipeline(self._on_item)
        self._consecutive_failures = 0
        self._load_reviews()

    # ---------- 队列管理 ----------

    def add_paths(self, raw_paths: list[str], *, recursive: bool | None = None) -> dict[str, Any]:
        """加入素材。文件夹会被展开成其中的视频文件。"""
        cfg = config_mod.load()
        if recursive is None:
            recursive = bool(cfg["input"].get("recursive", True))

        existing = {item.path.lower() for item in self.queue}
        added: list[FileItem] = []
        skipped: list[str] = []
        errors: list[str] = []

        for raw in raw_paths:
            raw = (raw or "").strip().strip('"')
            if not raw:
                continue
            target = Path(raw)
            if target.is_dir():
                found = naming.scan_folder(target, recursive=recursive)
                if not found:
                    errors.append(f"{raw}：文件夹里没找到视频")
                candidates = found
            elif target.is_file():
                if not media.is_video(target):
                    errors.append(f"{target.name}：不是支持的视频格式")
                    continue
                candidates = [str(target)]
            else:
                errors.append(f"{raw}：路径不存在")
                continue

            for candidate in candidates:
                if candidate.lower() in existing:
                    skipped.append(Path(candidate).name)
                    continue
                item = self._make_item(candidate)
                self.queue.append(item)
                existing.add(candidate.lower())
                added.append(item)

        if added:
            self._broadcast_queue()
        return {
            "added": len(added),
            "skipped": len(skipped),
            "errors": errors,
            "queue": [i.as_dict() for i in self.queue],
        }

    def _make_item(self, path: str) -> FileItem:
        return FileItem(
            id="f" + uuid.uuid4().hex[:10],
            path=path,
            name=Path(path).name,
            size=_safe_size(path),
        )

    def remove_item(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.queue)
            self.queue = [i for i in self.queue if i.id != item_id]
            changed = len(self.queue) != before
        if changed:
            self._broadcast_queue()
        return changed

    def clear_queue(self) -> None:
        if self.status == RUNNING_T:
            raise RuntimeError("任务运行中，先停止再清空队列")
        with self._lock:
            self.queue.clear()
        self._broadcast_queue()

    # ---------- 任务控制 ----------

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.status == RUNNING_T:
                return {"ok": False, "message": "任务已在运行中"}
            runnable = [i for i in self.queue if i.status in (PENDING, CANCELED, FAILED)]
            if not runnable:
                return {"ok": False, "message": "队列里没有待处理的素材"}

            self._reset_run_state(runnable)
            self.status = RUNNING_T
            self._stop.clear()
            self._consecutive_failures = 0
            transcriber.reset_task_state()
            self._thread = threading.Thread(target=self._run, daemon=True, name="vair-engine")
            self._thread.start()

        hub.info(f"任务启动 · {len(runnable)} 个文件"
                 + ("（预览模式）" if config_mod.load()["output"].get("dry_run") else ""))
        self._broadcast_task()
        return {"ok": True, "message": f"已开始处理 {len(runnable)} 个文件"}

    def _reset_run_state(self, runnable: list[FileItem]) -> None:
        for item in runnable:
            item.status = PENDING
            item.stage = ""
            item.progress = 0.0
            item.error = ""
        self.total = len(runnable)
        self.done_count = 0
        self.progress = 0.0
        self.started_at = time.time()
        self.finished_at = 0.0
        self.message = ""
        self.stats = {"success": 0, "skipped": 0, "failed": 0, "canceled": 0,
                      "seconds": 0.0, "completed": 0}

    def stop(self) -> dict[str, Any]:
        if self.status != RUNNING_T:
            return {"ok": False, "message": "当前没有运行中的任务"}
        self._stop.set()
        hub.warn("收到停止请求 · 正在跑的文件会跑完，之后不再取新文件")
        self.message = "正在停止…"
        self._broadcast_task()
        return {"ok": True, "message": "已请求停止"}

    # ---------- 执行 ----------

    def _run(self) -> None:
        cfg = config_mod.load()

        # 若上一批任务结束后把 AI 服务关了（services.stop_after_task），
        # 这里先把它们拉起来再开工；已经跑着的会立刻跳过，代价只是一次端口探测。
        if not self._stop.is_set():
            self.message = "正在准备 AI 服务…"
            self._broadcast_task()
            try:
                services.ensure_running()
            except Exception as exc:                # noqa: BLE001
                hub.warn(f"AI 服务准备阶段出错：{exc}")

        workers = max(1, int(cfg["runtime"].get("ai_workers") or 4))
        frame_workers = max(1, int(cfg["frames"].get("workers") or 8))
        recursive_ok = True  # 队列已展开完毕

        pending = [i for i in self.queue if i.status == PENDING]
        self._frame_pool = ThreadPoolExecutor(max_workers=frame_workers, thread_name_prefix="vair-frames")
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vair-ai")

        try:
            futures = [pool.submit(self._process_one, item) for item in pending]
            for future in futures:
                future.result()
        finally:
            pool.shutdown(wait=True)
            if self._frame_pool:
                self._frame_pool.shutdown(wait=True)
                self._frame_pool = None

        with self._lock:
            if self._stop.is_set():
                self.status = STOPPED
                self.message = "已手动停止"
            elif self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                self.status = ERROR
                self.message = f"连续失败 {CIRCUIT_BREAKER_THRESHOLD} 个文件，已自动停止"
            else:
                self.status = DONE
                self.message = "全部处理完成"
            self.progress = 100.0
            self.finished_at = time.time()
            self.current_id = None

        hub.log("warn" if self.status == STOPPED else "info",
                f"任务结束 · 成功 {self.stats['success']} · 跳过 {self.stats['skipped']} · "
                f"失败 {self.stats['failed']} · 取消 {self.stats['canceled']}",
                f"{self.stats['seconds']:.1f}s")
        self._broadcast_task()

        # 文件已经落盘，AI 服务不再需要占着显存。放到后台线程里停，
        # 别让前端等进程退出（每个模型卸载要几秒）。
        if config_mod.load().get("services", {}).get("stop_after_task", True):
            threading.Thread(target=services.stop_after_task, daemon=True,
                             name="vair-stop-services").start()

    def _process_one(self, item: FileItem) -> None:
        if self._stop.is_set():
            return

        with self._lock:
            self.current_id = item.id
        self._broadcast_task()

        started = time.perf_counter()
        self._pipeline.run(item, self._stop)
        elapsed = time.perf_counter() - started

        with self._lock:
            self.done_count += 1
            self.progress = self.done_count / max(1, self.total) * 100.0
            self.stats["seconds"] += elapsed

            if item.status == SUCCESS:
                self.stats["success"] += 1
                self.stats["completed"] += 1
                self._consecutive_failures = 0
            elif item.status == REVIEW:
                self.stats["completed"] += 1     # 已算出结果，等审批
                self._consecutive_failures = 0
            elif item.status == SKIPPED:
                self.stats["skipped"] += 1
                self._consecutive_failures = 0
            elif item.status == CANCELED:
                self.stats["canceled"] += 1
            elif item.status == FAILED:
                self.stats["failed"] += 1
                self._consecutive_failures += 1

            hit_breaker = self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD

        if item.status == REVIEW:
            self._push_review(item)
        if item.status == FAILED and config_mod.load()["output"].get("move_failed", True) \
                and not config_mod.load()["output"].get("dry_run"):
            ok, detail = naming.move_to_failed(item.path)
            if ok:
                item.notes.append(f"已移入 _failed：{Path(detail).name}")
            else:
                item.notes.append(f"移入 _failed 失败：{detail}")

        self._broadcast_item(item)
        self._broadcast_stats()
        self._broadcast_task()

        if hit_breaker:
            hub.error(f"连续失败 {CIRCUIT_BREAKER_THRESHOLD} 个文件，触发熔断自动停止")
            self._stop.set()

    # ---------- 审批 ----------

    def _push_review(self, item: FileItem) -> None:
        entry = {
            "id": item.id,
            "original": item.name,
            "path": item.path,
            "name": item.new_name,
            "title": item.title,
            "plot": item.plot,
            "tags": item.tags,
            "confidence": 0.9,
            "duration": item.duration,
            "size": item.size,
            "status": "pending",
            "elapsed": round(item.elapsed, 1),
            "model": item.model,
            "frames": item.frames,
            "chars": item.chars,
            # 不通过 GET /api/reviews 返回文本；只在服务端持久化供通过时写 SRT。
            "srt_content": item.srt_content,
            "creation_time": item.creation_time,
            # 预览模式下这条只是演算结果，不能拿来真的改文件
            "dry_run": bool(config_mod.load()["output"].get("dry_run")),
        }
        with self._lock:
            self.reviews = [r for r in self.reviews if r["id"] != item.id]
            self.reviews.insert(0, entry)
            self.reviews = self.reviews[:300]
        self._save_reviews()
        hub.publish("review", {k: v for k, v in entry.items() if k != "srt_content"})

    def list_reviews(self) -> list[dict[str, Any]]:
        """只给前端展示字段；转写 SRT 可能很长，且不应无谓广播到浏览器。"""
        with self._lock:
            return [{k: v for k, v in row.items() if k != "srt_content"}
                    for row in self.reviews]

    def resolve_review(self, review_id: str, action: str, new_name: str | None = None) -> dict[str, Any]:
        """通过 = 真正落盘；忽略 = 丢弃结果。"""
        with self._lock:
            entry = next((r for r in self.reviews if r["id"] == review_id), None)
        if entry is None:
            return {"ok": False, "message": "找不到这条审批记录"}
        if entry.get("status") == "approved":
            # 文件名已经在磁盘上改变；仅把状态改回 pending 会制造一个不可能存在的状态。
            return {"ok": False, "message": "文件已经改名，不能撤销已通过的审批；请在文件管理器里手动改回"}
        if action == "approve" and entry.get("status") != "pending":
            return {"ok": False, "message": "这条建议不是待审批状态，请先恢复为待审批"}

        if action == "reject":
            if entry.get("status") == "rejected":
                return {"ok": True, "message": "已经忽略过"}
            entry["status"] = "rejected"
            entry.pop("srt_content", None)
            self._save_reviews()
            hub.publish("review", {k: v for k, v in entry.items() if k != "srt_content"})
            hub.info(f"已忽略命名建议 · {entry['original']}")
            return {"ok": True, "message": "已忽略"}

        if action == "pending":
            entry["status"] = "pending"
            self._save_reviews()
            hub.publish("review", {k: v for k, v in entry.items() if k != "srt_content"})
            return {"ok": True, "message": "已恢复为待审批"}

        # approve：用户可能改过名字，用用户给的
        if entry.get("dry_run"):
            return {"ok": False,
                    "message": "预览模式下的建议只是演算结果，不能直接应用。"
                               "请关掉「预览模式」再跑一遍，然后审批。"}
        final_name = (new_name or entry["name"] or "").strip()
        if not final_name:
            return {"ok": False, "message": "新文件名为空"}

        source = Path(entry["path"])
        if not source.is_file():
            entry["status"] = "failed"
            entry["error"] = "源文件已不存在"
            self._save_reviews()
            hub.publish("review", {k: v for k, v in entry.items() if k != "srt_content"})
            return {"ok": False, "message": "源文件已不存在，可能在文件系统里被移动或删除了"}

        # 原文件扩展名不可改（避免浏览器传来的名字把 mp4 改成 exe 或其它格式）。
        proposed = Path(final_name).name
        if proposed != final_name or proposed in (".", "..") or "\x00" in proposed:
            return {"ok": False, "message": "文件名不能包含路径或非法字符"}
        if Path(proposed).suffix.lower() != source.suffix.lower():
            return {"ok": False, "message": f"请保留原始扩展名 {source.suffix}"}

        target = naming.unique_target(source.with_name(proposed), source)
        result = naming.rename_file(source, target, dry_run=False)
        if result.startswith("error:"):
            return {"ok": False, "message": f"重命名失败：{result[6:]}"}

        # 通过后才写元数据、NFO、SRT。拒绝与待审批期间原视频完全不动。
        notes = outputs.write_all(
            target,
            title=entry.get("title") or "",
            plot=entry.get("plot") or "",
            tags=entry.get("tags") or [],
            original_name=entry["original"],
            duration=entry.get("duration") or 0,
            srt_content=entry.get("srt_content") or "",
            dry_run=False,
        )
        hub.info(f"已应用命名 · {entry['original']} → {target.name}")
        entry["status"] = "approved"
        entry["name"] = target.name
        entry["path"] = str(target)
        entry.pop("srt_content", None)
        self._save_reviews()
        hub.publish("review", entry.copy())
        return {"ok": True, "message": f"已重命名为 {target.name}", "notes": notes}

    def _reviews_path(self) -> Path:
        return paths.DATA_DIR / REVIEWS_FILE

    def _save_reviews(self) -> None:
        try:
            paths.ensure_dirs()
            self._reviews_path().write_text(
                json.dumps(self.reviews[:300], ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _load_reviews(self) -> None:
        path = self._reviews_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.reviews = [r for r in data if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            pass

    # ---------- 广播 ----------

    def _on_item(self, item: FileItem) -> None:
        self._broadcast_item(item)

    def _broadcast_item(self, item: FileItem) -> None:
        hub.publish("file", item.as_dict())

    def _broadcast_queue(self) -> None:
        hub.publish("queue", [i.as_dict() for i in self.queue])

    def _broadcast_stats(self) -> None:
        hub.publish("stats", dict(self.stats))

    def _broadcast_task(self) -> None:
        hub.publish("task", self.task_dict())

    # ---------- 快照 ----------

    def task_dict(self) -> dict[str, Any]:
        current = next((i for i in self.queue if i.id == self.current_id), None)
        return {
            "status": self.status,
            "progress": round(self.progress, 1),
            "current": current.as_dict() if current else None,
            "total": self.total,
            "done": self.done_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message": self.message,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "task": self.task_dict(),
                "queue": [i.as_dict() for i in self.queue],
                "stats": dict(self.stats),
                "reviews": list(self.reviews),
                "timelines": self._timelines(),
            }

    def _timelines(self, limit: int = 40) -> list[dict[str, Any]]:
        """日志页的「单文件推理时间线」。"""
        out: list[dict[str, Any]] = []
        for item in reversed(self.queue):
            if item.status not in (SUCCESS, REVIEW):
                continue
            out.append({
                "name": item.new_name or item.name,
                "original": item.name,
                "model": item.model or "-",
                "frames": item.frames,
                "chars": item.chars,
                "cost": round(item.elapsed, 1),
                "timings": item.timings,
            })
            if len(out) >= limit:
                break
        return out


def _safe_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


engine = Engine()
