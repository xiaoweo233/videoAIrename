"""路径解析与内置工具定位。

目录约定（相对仓库根）：

    仓库根/
    ├── AI/               内置 AI 运行时与模型（不入库）
    ├── ffmpeg/           内置 ffmpeg 三件套（不入库）
    ├── exiftool/         内置 exiftool（不入库）
    ├── 前端/             原生 HTML/CSS/JS 前端
    ├── 后端/             Python 后端（本目录）
    ├── config.json       运行时配置（不入库）
    ├── data/             任务记录与审批数据（不入库）
    ├── logs/             运行日志（不入库）
    └── .cache/           抽帧等中间产物（不入库，可随时删）
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 后端/app/paths.py → app → 后端 → 仓库根
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT = BACKEND_DIR.parent

AI_DIR = ROOT / "AI"
FFMPEG_DIR = ROOT / "ffmpeg"
EXIFTOOL_DIR = ROOT / "exiftool"
FRONTEND_DIR = ROOT / "前端"

CACHE_DIR = ROOT / ".cache"
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"

CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = BACKEND_DIR / "config.example.json"
LIBS_DIR = BACKEND_DIR / "libs"

# 内置 AI 运行时的默认位置（「补齐 AI 依赖与模型」会往这里放）
LLAMA_DIR = AI_DIR / "llama-b12412-bin-win-cuda-13.5-x64"
VISION_DIR = AI_DIR / "models" / "Qwen3.5-4B"
ASR_MODEL_DIR = AI_DIR / "models" / "Qwen" / "Qwen3-ASR-0.6B"
ALIGNER_MODEL_DIR = AI_DIR / "models" / "Qwen" / "Qwen3-ForcedAligner-0.6B"
SD_MODEL_DIR = AI_DIR / "models" / "speaker-diarization-community-1"
ASR_SERVICE_DIR = BACKEND_DIR / "asr_service"
ASR_RUNTIME_DIR = AI_DIR / "asr-python"

IS_WINDOWS = os.name == "nt"


def ensure_dirs() -> None:
    """确保运行时目录存在。幂等。"""
    for d in (LOG_DIR, DATA_DIR, CACHE_DIR, AI_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def _resolve_executable(name: str, *candidates: Path) -> str | None:
    """按顺序定位可执行文件：显式候选 → 仓库内 → PATH。"""
    exe = name + (".exe" if IS_WINDOWS else "")
    for candidate in candidates:
        for variant in (candidate / name, candidate / exe, candidate):
            if variant.is_file():
                return str(variant)
    found = shutil.which(name) or shutil.which(exe)
    return found


_FFMPEG_CACHE: dict[str, str | None] = {}


def tool(name: str) -> str | None:
    """定位内置外部工具（ffmpeg / ffprobe / exiftool）。

    查找顺序：仓库内 `ffmpeg/`、`exiftool/` → 环境变量 → PATH。
    结果缓存，避免每次调用都做磁盘查找。
    """
    if name in _FFMPEG_CACHE:
        return _FFMPEG_CACHE[name]

    resolved: str | None
    if name in ("ffmpeg", "ffprobe", "ffplay"):
        resolved = _resolve_executable(name, FFMPEG_DIR, FFMPEG_DIR / "bin")
    elif name == "exiftool":
        resolved = _resolve_executable(name, EXIFTOOL_DIR)
    else:
        resolved = shutil.which(name)

    _FFMPEG_CACHE[name] = resolved
    return resolved


def tool_status() -> dict[str, dict[str, object]]:
    """给前端汇报外部工具是否就绪。"""
    out: dict[str, dict[str, object]] = {}
    for name in ("ffmpeg", "ffprobe", "exiftool"):
        path = tool(name)
        out[name] = {"ok": bool(path), "path": path}
    return out


def reset_tool_cache() -> None:
    _FFMPEG_CACHE.clear()


def abs_path(value: str | os.PathLike[str] | None) -> Path:
    """把配置里的相对路径按仓库根解析。"""
    if not value:
        return ROOT
    p = Path(value)
    return p if p.is_absolute() else (ROOT / p)


def rel_path(path: str | os.PathLike[str] | None) -> str:
    """尽量转成相对仓库根的展示路径，失败则原样返回。"""
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return str(path)


def describe() -> dict[str, str]:
    return {
        "root": str(ROOT),
        "backend": str(BACKEND_DIR),
        "frontend": str(FRONTEND_DIR),
        "ai": str(AI_DIR),
        "cache": str(CACHE_DIR),
        "log": str(LOG_DIR),
        "data": str(DATA_DIR),
        "config": str(CONFIG_PATH),
        "python": sys.executable,
    }
