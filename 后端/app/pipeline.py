"""单文件流水线：探测 → 抽帧/转写 → 合并 → AI 分析 → 落盘。

抽帧与转写是**并行**的两个分支（一个吃 CPU，一个吃 GPU），合并后再交给视觉模型。
每个阶段都通过 `on_progress` 把进度报给引擎，前端据此点亮流水线节点。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from . import media, naming, outputs, proc
from .hub import hub
from .transcribe import transcriber
from .vision import vision

# 阶段边界（与前端 STAGES 的百分比一一对应）
STAGES = ("probe", "frames", "transcribe", "merge", "analyze", "commit")

PENDING = "pending"
RUNNING = "running"
REVIEW = "review"
SUCCESS = "success"
SKIPPED = "skipped"
FAILED = "failed"
CANCELED = "canceled"


@dataclass
class FileItem:
    id: str
    path: str
    name: str = ""
    size: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    resolution: str = ""
    has_audio: bool = False
    creation_time: str = ""

    status: str = PENDING
    stage: str = ""
    progress: float = 0.0

    title: str = ""
    plot: str = ""
    tags: list[str] = field(default_factory=list)
    new_name: str = ""
    new_path: str = ""

    frames: int = 0
    chars: int = 0
    transcribe_source: str = ""
    model: str = ""

    error: str = ""
    # 审批通过后生成 SRT 必须保留原始时间轴；待审批时只在内存中保存，绝不提前落盘。
    srt_content: str = ""
    notes: list[str] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "duration": self.duration,
            "resolution": self.resolution,
            "has_audio": self.has_audio,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 1),
            "title": self.title,
            "plot": self.plot,
            "tags": self.tags,
            "new_name": self.new_name,
            "new_path": self.new_path,
            "frames": self.frames,
            "chars": self.chars,
            "transcribe_source": self.transcribe_source,
            "model": self.model,
            "error": self.error,
            "notes": self.notes,
            "elapsed": round(self.elapsed, 2),
            "timings": dict(self.timings),      # 各阶段毫秒，前端「实时进度」用
        }


ProgressFn = Callable[[FileItem], None]


class Pipeline:
    def __init__(self, on_item: Callable[[FileItem], None] | None = None) -> None:
        self._on_item = on_item or (lambda item: None)

    def _touch(self, item: FileItem, stage: str, progress: float) -> None:
        item.stage = stage
        item.progress = progress
        self._on_item(item)

    # ---------- 主流程 ----------

    def run(self, item: FileItem, stop: threading.Event) -> None:
        cfg = config_mod.load()
        started = time.perf_counter()
        item.status = RUNNING

        try:
            self._process(item, cfg, stop)
        except Exception as exc:                    # noqa: BLE001 - 单文件失败不能拖垮整批
            item.status = FAILED
            item.error = str(exc)
            hub.error(f"处理失败 · {item.name} · {exc}")
        finally:
            item.elapsed = time.perf_counter() - started
            item.stage = STAGES[-1] if item.status in (SUCCESS, REVIEW) else item.stage
            self._on_item(item)

    def _process(self, item: FileItem, cfg: dict, stop: threading.Event) -> None:
        # ---- 阶段 1：探测 ----
        self._touch(item, "probe", 0.0)
        probe_started = time.perf_counter()
        info = media.probe(item.path)
        item.timings["probe"] = int((time.perf_counter() - probe_started) * 1000)
        item.size = info.size
        item.duration = info.duration
        item.width = info.width
        item.height = info.height
        item.resolution = info.resolution
        item.has_audio = info.has_audio
        item.creation_time = info.creation_time
        item.name = item.name or info.name
        self._touch(item, "probe", 6.0)
        hub.debug(f"{item.name} · {info.resolution} · {info.duration_label} · "
                  f"音轨 {'有' if info.has_audio else '无'}")

        if stop.is_set():
            item.status = CANCELED
            return

        # ---- 跳过已处理 ----
        if cfg["naming"].get("enable_skip", True):
            marker = str(cfg["naming"].get("marker") or "AI_RENAMED")
            if naming.has_name_marker(item.path, marker):
                item.status = SKIPPED
                item.notes.append(f"文件名含标记 {marker}，跳过")
                item.progress = 100.0
                return
            if outputs.has_watermark(item.path):
                item.status = SKIPPED
                item.notes.append("视频内已有软水印，跳过")
                item.progress = 100.0
                return

        # ---- 阶段 2/3：抽帧与转写并行 ----
        buckets: dict[str, Any] = {}

        def do_frames() -> None:
            begin = time.perf_counter()
            try:
                buckets["frames"] = media.extract_frames(
                    item.path, info,
                    count=int(cfg["frames"].get("max_keyframes") or 10),
                    max_side=int(cfg["frames"].get("max_side") or 640),
                    hwaccel=str(cfg["frames"].get("hwaccel") or "none"),
                )
            except Exception as exc:                # noqa: BLE001
                buckets["frames_error"] = str(exc)
                buckets["frames"] = []
            buckets["frames_ms"] = int((time.perf_counter() - begin) * 1000)

        def do_transcribe() -> None:
            begin = time.perf_counter()
            if not cfg["whisper"].get("enable", True) or not info.has_audio:
                buckets["transcript"] = None
                buckets["transcribe_skip"] = "无音轨" if not info.has_audio else "转写已关闭"
                buckets["transcribe_ms"] = 0
                return
            audio = None
            try:
                audio = media.extract_audio(item.path)
                buckets["transcript"] = transcriber.run(audio)
            except Exception as exc:                # noqa: BLE001
                buckets["transcribe_error"] = str(exc)
                buckets["transcript"] = None
            finally:
                if audio is not None:
                    try:
                        os.unlink(proc.long_path(audio))
                    except OSError:
                        pass
            buckets["transcribe_ms"] = int((time.perf_counter() - begin) * 1000)

        workers: list[threading.Thread] = []
        for target in (do_frames, do_transcribe):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            workers.append(thread)

        self._touch(item, "frames", 10.0)
        # 抽帧报告一次细粒度进度，让前端看到在动
        for step in range(1, 5):
            if all(not w.is_alive() for w in workers):
                break
            if stop.wait(0.4):
                break
            self._touch(item, "frames", 10.0 + step * 4.0)

        for thread in workers:
            thread.join()

        if stop.is_set():
            item.status = CANCELED
            return

        frames = buckets.get("frames") or []
        item.frames = len(frames)
        item.timings["frames"] = buckets.get("frames_ms", 0)
        item.timings["transcribe"] = buckets.get("transcribe_ms", 0)
        self._touch(item, "transcribe", 30.0)

        if buckets.get("frames_error"):
            raise RuntimeError(f"抽帧失败：{buckets['frames_error']}")

        transcript = buckets.get("transcript")
        transcript_text = ""
        if transcript is not None:
            item.transcribe_source = transcript.source
            item.chars = len(transcript.text or "")
            transcript_text = transcript.text or ""
            if transcript.error:
                item.notes.append(f"转写未完成：{transcript.error}")
                hub.warn(f"{item.name} · 转写未完成：{transcript.error}")
            else:
                hub.debug(f"{item.name} · 转写 {item.chars} 字 · 来源 {transcript.source}")
        elif buckets.get("transcribe_error"):
            item.notes.append(f"转写失败：{buckets['transcribe_error']}")
        elif buckets.get("transcribe_skip"):
            item.notes.append(f"跳过转写（{buckets['transcribe_skip']}）")

        # ---- 阶段 4：合并（分片 MP4 重封装 + 上下文构建）----
        self._touch(item, "merge", 32.0)
        if cfg["output"].get("remux_fragmented", True) and media.is_fragmented_mp4(item.path):
            hub.info(f"{item.name} · 检测到分片 MP4，重封装以便写入元数据")
            ok, message = media.remux_mp4(item.path)
            item.notes.append(("已重封装分片 MP4" if ok else f"重封装跳过：{message}"))
            if not ok:
                hub.warn(f"{item.name} · {message}")
        self._touch(item, "merge", 62.0)

        if stop.is_set():
            item.status = CANCELED
            return

        # ---- 阶段 5：AI 分析 ----
        self._touch(item, "analyze", 65.0)
        result = vision.analyze(
            frames,
            transcript=transcript_text,
            original_name=Path(item.path).name,
            duration=item.duration,
            stop=stop,
        )
        item.model = result.model
        item.timings["analyze"] = result.elapsed_ms

        if not result.ok:
            raise RuntimeError(f"AI 分析失败：{result.error}")

        item.title = result.title
        item.plot = result.plot
        item.tags = result.tags
        self._touch(item, "analyze", 90.0)
        hub.info(f"{item.name} → {result.title}", f"{result.elapsed_ms / 1000:.1f}s")

        if stop.is_set():
            item.status = CANCELED
            return

        # ---- 阶段 6：落盘 ----
        self._touch(item, "commit", 92.0)
        self._commit(item, cfg, transcript)
        self._touch(item, "commit", 100.0)

    # ---------- 落盘 ----------

    def _commit(self, item: FileItem, cfg: dict, transcript: Any) -> None:
        dry_run = bool(cfg["output"].get("dry_run", False))
        source = Path(item.path)

        stem = naming.build_stem(
            title=item.title,
            original_name=source.name,
            creation_time=item.creation_time,
            path=item.path,
            cfg=cfg["naming"],
        )
        target = naming.unique_target(source.with_name(stem + source.suffix), source)
        item.new_name = target.name
        item.new_path = str(target)

        if dry_run or cfg["output"].get("require_approval", True):
            item.status = REVIEW
            item.srt_content = transcript.to_srt() if transcript is not None and transcript.segments else ""
            item.notes.append("预览模式：仅计算，不落盘" if dry_run
                              else "等待命名审批：尚未修改原视频或写入任何附属产物")
            return

        # 先把附属产物写到「重命名后」的路径上，避免出现半截状态
        srt_content = transcript.to_srt() if transcript is not None and transcript.segments else ""
        status = naming.rename_file(item.path, target, dry_run=False)

        if status.startswith("error:"):
            raise RuntimeError(f"重命名失败：{status[6:]}")
        if status == "skipped":
            item.notes.append("新文件名与原名相同")

        notes = outputs.write_all(
            target,
            title=item.title,
            plot=item.plot,
            tags=item.tags,
            original_name=source.name,
            duration=item.duration,
            srt_content=srt_content,
            dry_run=False,
        )
        item.notes.extend(n for n in notes if n)
        item.path = str(target)
        item.status = SUCCESS


class ItemSink:
    """把流水线回调转成 hub 事件，顺带做节流，避免每帧都推 SSE。"""

    def __init__(self, emit: Callable[[FileItem], None]) -> None:
        self._emit = emit
        self._last: dict[str, tuple[str, int]] = {}

    def __call__(self, item: FileItem) -> None:
        key = (item.stage, int(item.progress))
        if self._last.get(item.id) == key and item.status == RUNNING:
            return
        self._last[item.id] = key
        self._emit(item)
