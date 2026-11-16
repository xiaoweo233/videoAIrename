"""媒体处理：探测、抽帧、抽音频、分片 MP4 重封装。

抽帧是**逐时间点抽单帧**，不是用 `fps=` 滤镜批量抽。原因是关键帧分布不均，
按时间点抽能保证覆盖片头/中段/结尾，且每帧一次 ffmpeg 调用可以并发跑。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, proc
from .hub import hub

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp",
}

_FRAGMENTED_EXTS = {".mp4", ".mov", ".m4v"}

PROBE_TIMEOUT = 20
FRAME_TIMEOUT = 30
AUDIO_TIMEOUT = 600
REMUX_TIMEOUT = 900


class MediaError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    path: str
    name: str = ""
    size: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    has_video: bool = False
    has_audio: bool = False
    creation_time: str = ""
    keyframes: list[float] = field(default_factory=list)

    @property
    def resolution(self) -> str:
        h = self.height
        if h >= 2160:
            return "4K"
        if h >= 1440:
            return "2K"
        if h >= 1080:
            return "1080p"
        if h >= 720:
            return "720p"
        return f"{h}p" if h else "未知"

    @property
    def duration_label(self) -> str:
        total = int(self.duration)
        return f"{total // 60}:{total % 60:02d}"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name or os.path.basename(self.path),
            "size": self.size,
            "duration": round(self.duration, 2),
            "duration_label": self.duration_label,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "codec": self.codec,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "creation_time": self.creation_time,
            "keyframes": len(self.keyframes),
        }


def is_video(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def probe(path: str | os.PathLike[str]) -> VideoInfo:
    """探测视频：时长、分辨率、创建时间、关键帧时间戳、有无音轨。"""
    ffprobe = paths.tool("ffprobe")
    if not ffprobe:
        raise MediaError("未找到 ffprobe，请把 ffmpeg 放进仓库的 ffmpeg/ 目录")

    target = str(path)
    info = VideoInfo(path=target, name=Path(target).name)

    try:
        stat = os.stat(proc.long_path(target))
        info.size = stat.st_size
    except OSError:
        pass

    # 第一路：时长 / creation_time / 分辨率 / 关键帧时间戳
    primary = proc.run(
        [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0", "-skip_frame", "nokey",
            "-show_entries",
            "format=duration:format_tags=creation_time:stream=width,height,codec_name,codec_type:frame=pts_time",
            target,
        ],
        timeout=PROBE_TIMEOUT,
    )
    data: dict = {}
    if primary.returncode == 0:
        try:
            data = json.loads(proc.decode(primary.stdout) or "{}")
        except json.JSONDecodeError:
            data = {}

    fmt = data.get("format") or {}
    try:
        info.duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        info.duration = 0.0

    creation = (fmt.get("tags") or {}).get("creation_time") or ""
    # 很多设备写 0000-00-00，这种是无效值，宁可留空走文件时间
    info.creation_time = "" if "0000" in str(creation) else str(creation)

    for stream in data.get("streams") or []:
        info.width = int(stream.get("width") or 0) or info.width
        info.height = int(stream.get("height") or 0) or info.height
        info.codec = stream.get("codec_name") or info.codec

    for frame in data.get("frames") or []:
        raw = frame.get("pts_time")
        if raw in (None, "N/A"):
            continue
        try:
            info.keyframes.append(float(raw))
        except (TypeError, ValueError):
            continue

    # 第二路：完整流列表（权威判定有没有音轨）
    secondary = proc.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", target],
        timeout=PROBE_TIMEOUT,
    )
    if secondary.returncode == 0:
        try:
            full = json.loads(proc.decode(secondary.stdout) or "{}")
        except json.JSONDecodeError:
            full = {}
        for stream in full.get("streams") or []:
            kind = stream.get("codec_type")
            if kind == "video":
                info.has_video = True
                if not info.width:
                    info.width = int(stream.get("width") or 0)
                if not info.height:
                    info.height = int(stream.get("height") or 0)
            elif kind == "audio":
                info.has_audio = True
        if not info.duration:
            try:
                info.duration = float((full.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                pass
    else:
        info.has_video = bool(info.width)

    return info


def select_timestamps(duration: float, keyframes: list[float], count: int) -> list[float]:
    """挑 `count` 个抽帧时间点。

    优先用真实关键帧时间戳（解码最快、画面最完整）；没有就按时长均分。
    """
    count = max(1, int(count))
    if duration <= 0:
        return [0.0] * count

    if duration < 10:
        base = [0.0, duration / 2, max(0.0, duration - 0.1)]
        out: list[float] = []
        while len(out) < count:
            out.append(base[len(out) % len(base)])
        return out[:count]

    if keyframes:
        usable = [k for k in keyframes if 0.0 <= k <= duration]
        if len(usable) >= count:
            # 等距挑，避免全挤在片头
            step = (len(usable) - 1) / (count - 1) if count > 1 else 0
            picked = [usable[round(i * step)] for i in range(count)]
            return _dedupe(picked)
        if usable:
            return _dedupe(usable + [duration * (i + 1) / (count + 1) for i in range(count)])[:count]

    return [duration * (i + 1) / (count + 1) for i in range(count)]


def _dedupe(values: list[float]) -> list[float]:
    seen: list[float] = []
    for value in values:
        if not seen or abs(seen[-1] - value) > 0.01:
            seen.append(value)
    return seen


def extract_frame(
    video: str | os.PathLike[str],
    ts: float,
    max_side: int = 640,
    hwaccel: str = "none",
) -> bytes | None:
    """抽单帧，返回 JPEG 字节；失败返回 None。"""
    ffmpeg = paths.tool("ffmpeg")
    if not ffmpeg:
        raise MediaError("未找到 ffmpeg，请把 ffmpeg 放进仓库的 ffmpeg/ 目录")

    args = [ffmpeg, "-y", "-nostdin"]
    if hwaccel and hwaccel != "none":
        args += ["-hwaccel", hwaccel]
    # -ss 必须在 -i 之前：快速 seek，否则每个时间点都要从头解码
    args += [
        "-ss", f"{max(0.0, ts):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease",
        "-q:v", "5",
        "-f", "image2", "pipe:1",
    ]
    try:
        result = proc.run(args, timeout=FRAME_TIMEOUT)
    except proc.ToolError:
        return None
    if result.returncode != 0 or len(result.stdout or b"") <= 1024:
        return None
    return result.stdout


def extract_frames(
    video: str | os.PathLike[str],
    info: VideoInfo,
    *,
    count: int = 10,
    max_side: int = 640,
    hwaccel: str = "none",
) -> list[bytes]:
    """按时间点抽帧。一帧都没抽到就用 0.0 兜底重试一次。"""
    stamps = select_timestamps(info.duration, info.keyframes, count)
    frames: list[bytes] = []
    for ts in stamps:
        raw = extract_frame(video, ts, max_side, hwaccel)
        if raw:
            frames.append(raw)
    if not frames:
        raw = extract_frame(video, 0.0, max_side, hwaccel)
        if raw:
            frames.append(raw)
    return frames


def frames_to_data_urls(frames: list[bytes]) -> list[str]:
    return ["data:image/jpeg;base64," + base64.b64encode(f).decode("ascii") for f in frames]


def extract_audio(video: str | os.PathLike[str], out_dir: str | os.PathLike[str] | None = None) -> Path:
    """抽 16k 单声道 wav，给内置 / 远程 ASR 用。"""
    ffmpeg = paths.tool("ffmpeg")
    if not ffmpeg:
        raise MediaError("未找到 ffmpeg")

    target_dir = Path(out_dir) if out_dir else paths.CACHE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix="audio_", suffix=".wav", dir=str(target_dir)
    )
    os.close(handle)
    out = Path(tmp_name)

    result = proc.run(
        [ffmpeg, "-y", "-nostdin", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        timeout=AUDIO_TIMEOUT,
    )
    if result.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
        out.unlink(missing_ok=True)
        raise MediaError(f"抽取音频失败：{proc.decode(result.stderr)[-500:]}")
    return out


def is_fragmented_mp4(path: str | os.PathLike[str]) -> bool:
    """分片 MP4 判定：头部同时出现 moov 与 moof。

    这类文件（OBS 分片录像）写不进 exiftool 元数据，必须先重封装。
    """
    p = Path(path)
    if p.suffix.lower() not in _FRAGMENTED_EXTS:
        return False
    try:
        with open(proc.long_path(p), "rb") as fh:
            head = fh.read(8 * 1024 * 1024)
    except OSError:
        return False
    return b"moov" in head and b"moof" in head


def remux_mp4(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """无损重封装。成功返回 (True, 新路径)；失败保留原文件并返回原因。"""
    ffmpeg = paths.tool("ffmpeg")
    if not ffmpeg:
        return False, "未找到 ffmpeg"

    src = Path(path)
    tmp = src.with_name(src.stem + ".__remux__" + src.suffix)

    result = proc.run(
        [ffmpeg, "-y", "-nostdin", "-i", str(src),
         "-map", "0", "-c", "copy", "-movflags", "+faststart",
         "-f", "mp4", str(tmp)],
        timeout=REMUX_TIMEOUT,
    )
    if result.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        return False, f"重封装失败：{proc.decode(result.stderr)[-300:]}"

    try:
        src_size = src.stat().st_size
        out_size = tmp.stat().st_size
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return False, f"重封装校验失败：{exc}"

    # 缩水过半说明转码了或者截断了，宁可保留原文件
    if src_size and out_size < src_size * 0.5:
        tmp.unlink(missing_ok=True)
        return False, f"重封装结果异常（{out_size} < {src_size}/2），已保留原文件"

    try:
        original_times = (src.stat().st_atime, src.stat().st_mtime)
        os.replace(proc.long_path(tmp), proc.long_path(src))
        os.utime(proc.long_path(src), original_times)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return False, f"重封装替换失败：{exc}"

    return True, str(src)


def clean_cache(max_age_seconds: float = 6 * 3600) -> int:
    """清理过期的抽帧中间产物。返回删除的文件数。"""
    removed = 0
    if not paths.CACHE_DIR.is_dir():
        return 0
    import time as _time

    now = _time.time()
    for item in paths.CACHE_DIR.iterdir():
        try:
            if item.is_file() and now - item.stat().st_mtime > max_age_seconds:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name)[:80] or "video"
