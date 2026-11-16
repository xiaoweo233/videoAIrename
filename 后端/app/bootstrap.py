"""运行时引导与「一键补齐 AI 依赖与模型」。

两件事：

1. `early_init()` —— 必须在任何第三方 import 之前跑。把临时目录/HF 缓存重定向到仓库内、
   把 libs/ 注入 sys.path、应用国内镜像、把 stdio 切成 UTF-8。
2. AI 资产清单与下载 —— 模型体积巨大不入库，别人拿到代码后需要能一键拉回来。
   清单里每一项都带来源与校验方式，下载支持断点续传，进度实时推给前端。
"""

from __future__ import annotations

import importlib.util
import html as html_mod
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from . import paths
from .hub import hub
from .proc import decode, long_path, run

# ---------------------------------------------------------------- 镜像预设

PIP_PRESETS: dict[str, str] = {
    "official": "https://pypi.org/simple",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
}

HF_PRESETS: dict[str, str] = {
    "official": "https://huggingface.co",
    "hf-mirror": "https://hf-mirror.com",
}

# ---------------------------------------------------------------- 所需 Python 依赖
# 键 = import 用的模块名，值 = pip 安装用的包名（两者常常不一样）

REQUIRED_MODULES: dict[str, str] = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "pydantic": "pydantic",
    # 内置转写服务的 /transcribe 走 multipart；主后端的自检也顺手把它装上
    "multipart": "python-multipart",
}

OPTIONAL_MODULES: dict[str, str] = {
    # 远程转写走 Gradio 协议时需要；openai 协议和内置服务不需要
    "gradio_client": "gradio_client",
}

_INITIALIZED = False


# ---------------------------------------------------------------- 引导

def early_init() -> None:
    """必须在导入第三方库之前调用。幂等。"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    paths.ensure_dirs()

    # 把临时目录与各类缓存都收进仓库内，不污染用户系统盘
    hf_home = paths.AI_DIR / "cache" / "huggingface"
    for key, value in {
        "TMP": paths.CACHE_DIR,
        "TEMP": paths.CACHE_DIR,
        "TMPDIR": paths.CACHE_DIR,
        "HF_HOME": hf_home,
        "HUGGINGFACE_HUB_CACHE": hf_home / "hub",
        "TRANSFORMERS_CACHE": hf_home / "transformers",
        # 新版 hub 走 Xet 协议会对 hf-mirror 报 CAS 错误
        "HF_HUB_DISABLE_XET": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        # Windows 无开发者模式建不了符号链接，会让下载第 0 步就崩
        "HF_HUB_DISABLE_SYMLINKS": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }.items():
        os.environ[key] = str(value)

    for directory in (hf_home, hf_home / "hub", hf_home / "transformers"):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # libs/ 注入：零污染安装，不写系统 site-packages
    libs = str(paths.LIBS_DIR)
    if paths.LIBS_DIR.is_dir() and libs not in sys.path:
        sys.path.insert(0, libs)

    # 便携 Python 的 DLL 目录（ctranslate2 / CUDA 库）
    _inject_dll_dirs()

    apply_mirrors()

    from . import proc as proc_mod
    proc_mod.force_utf8_stdio()

    # 把 config.json 里的 log_file 目录建出来
    cfg = config_mod.load()
    log_file = paths.abs_path(str(cfg["runtime"].get("log_file") or "logs/run.log"))
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _inject_dll_dirs() -> None:
    if os.name != "nt":
        return
    candidates: list[Path] = []
    nvidia_root = paths.LIBS_DIR / "nvidia"
    if nvidia_root.is_dir():
        for pkg in nvidia_root.iterdir():
            for sub in ("bin", "lib"):
                candidate = pkg / sub
                if candidate.is_dir():
                    candidates.append(candidate)
    for candidate in candidates:
        try:
            os.add_dll_directory(str(candidate))     # type: ignore[attr-defined]
        except (OSError, AttributeError):
            pass
        os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")


def apply_mirrors(cfg: dict | None = None) -> None:
    """把配置里的镜像写进环境变量，供 pip / huggingface 下载使用。"""
    cfg = cfg or config_mod.load()
    runtime = cfg.get("runtime") or {}

    index = str(runtime.get("pip_index") or "").strip()
    if index:
        os.environ["PIP_INDEX_URL"] = index
        host = index.split("//", 1)[-1].split("/", 1)[0]
        if host:
            os.environ["PIP_TRUSTED_HOST"] = host
    else:
        os.environ.pop("PIP_INDEX_URL", None)
        os.environ.pop("PIP_TRUSTED_HOST", None)

    endpoint = str(runtime.get("hf_endpoint") or "").strip()
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    else:
        os.environ.pop("HF_ENDPOINT", None)


# ---------------------------------------------------------------- 自检

def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _cuda_report() -> dict[str, Any]:
    """探测 NVIDIA GPU。用 nvidia-smi，避免依赖 torch。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "name": "", "memory": 0, "driver": ""}
    result = run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                 timeout=10, check=False)
    if result.returncode != 0:
        return {"available": False, "name": "", "memory": 0, "driver": ""}
    line = decode(result.stdout).strip().splitlines()
    if not line:
        return {"available": False, "name": "", "memory": 0, "driver": ""}
    parts = [p.strip() for p in line[0].split(",")]
    memory = 0
    try:
        memory = int(parts[1].split()[0])
    except (IndexError, ValueError):
        pass
    return {
        "available": True,
        "name": parts[0] if parts else "",
        "memory": memory,
        "driver": parts[2] if len(parts) > 2 else "",
    }


def probe() -> dict[str, Any]:
    """运行时自检报告。"""
    tools = paths.tool_status()
    missing_modules = [mod for mod in REQUIRED_MODULES if not _importable(mod)]
    optional_missing = [mod for mod in OPTIONAL_MODULES if not _importable(mod)]

    problems: list[str] = []
    if not tools["ffmpeg"]["ok"]:
        problems.append("缺少 ffmpeg")
    if not tools["ffprobe"]["ok"]:
        problems.append("缺少 ffprobe")
    if missing_modules:
        problems.append("缺少 Python 依赖：" + "、".join(missing_modules))

    from . import config as _cfg
    return {
        "ok": not problems,
        "problems": problems,
        "root": str(paths.ROOT),
        "python": sys.executable,
        "tools": tools,
        "modules": {
            "required": {name: _importable(name) for name in REQUIRED_MODULES},
            "optional": {name: _importable(name) for name in OPTIONAL_MODULES},
            "missing": missing_modules,
            "optional_missing": optional_missing,
        },
        "cuda": _cuda_report(),
        "config_note": _cfg.corrupt_note(),
    }


# ---------------------------------------------------------------- AI 资产清单

LLAMA_TAG = "b12412"
LLAMA_DIRNAME = f"llama-{LLAMA_TAG}-bin-win-cuda-13.5-x64"
VISION_REPO = "HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive"
VISION_FILE = "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
VISION_MMPROJ = "mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
ASR_REPO = "Qwen/Qwen3-ASR-0.6B"
ALIGNER_REPO = "Qwen/Qwen3-ForcedAligner-0.6B"

_GH = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"

# ffmpeg：gyan.dev 是 ffmpeg.org 官方推荐的 Windows 构建来源（每日更新，体积会变）；
# BtbN 的 GitHub 发布作为备用。两个包的内部结构不同，所以下面靠「找文件名」定位，不猜路径。
FFMPEG_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]

# exiftool：13.63 是写这段代码时的最新版；官网把下载挂在 SourceForge 上。
# exiftool.org/exiftool-<版本>_64.zip 这条老路径已经 404 了，别再改回去。
EXIFTOOL_FILE = "exiftool-13.63_64.zip"
EXIFTOOL_URLS = [
    f"https://sourceforge.net/projects/exiftool/files/{EXIFTOOL_FILE}/download",
    f"https://downloads.sourceforge.net/project/exiftool/{EXIFTOOL_FILE}",
]


def ai_assets() -> list[dict[str, Any]]:
    """资产清单。每项都说明「在哪、缺了会怎样、要下多少」。

    前两项是外部小工具（几十 MB），放最前面：它们在 run.py 自检里是硬门槛，
    放最后会让「一键补齐」的人先下 10 GB 才发现 ffmpeg 还是缺的。
    """
    return [
        {
            "id": "ffmpeg",
            "label": "ffmpeg / ffprobe（抽帧与视频探测）",
            "detail": "抽关键帧、读时长分辨率都靠它，缺了任务无法开始",
            "kind": "zip",
            "urls": FFMPEG_URLS,
            "target": "ffmpeg",
            "check": "ffmpeg/ffmpeg.exe",
            # 压缩包里靠这个文件名反查可执行文件所在目录
            "payload": "ffmpeg.exe",
            # 官方每日构建，大小每次都不同：只用于显示，不用于校验下载完整性
            "size": 115 * 1024 * 1024,
            "volatile_size": True,
            "required": True,
        },
        {
            "id": "exiftool",
            "label": "exiftool（写入视频元数据）",
            "detail": "把标题/标签/日期写进文件；缺了只是少写元数据，不影响改名",
            "kind": "zip",
            "urls": EXIFTOOL_URLS,
            "target": "exiftool",
            "check": "exiftool/exiftool.exe",
            "payload": "exiftool(-k).exe",       # 官方包里带 (-k)，装完必须改名
            "rename": {"exiftool(-k).exe": "exiftool.exe"},
            "size": 11 * 1024 * 1024,
            "volatile_size": True,
            "required": False,
        },
        {
            "id": "llama_bin",
            "label": "llama.cpp 运行时（Windows CUDA）",
            "detail": "视觉推理引擎，缺了就没法分析画面",
            "kind": "zip",
            "url": f"{_GH}/{LLAMA_DIRNAME}.zip",
            "target": f"AI/{LLAMA_DIRNAME}",
            "check": f"AI/{LLAMA_DIRNAME}/llama-server.exe",
            "size": 240 * 1024 * 1024,
            "required": True,
        },
        {
            "id": "llama_cudart",
            "label": "CUDA 运行库（llama.cpp）",
            "detail": "cublas / cudart，缺了 GPU 加速跑不起来",
            "kind": "zip",
            "url": f"{_GH}/cudart-llama-bin-win-cuda-13.5-x64.zip",
            "target": f"AI/{LLAMA_DIRNAME}",
            "check": f"AI/{LLAMA_DIRNAME}/cudart64_13.dll",
            "size": 540 * 1024 * 1024,
            "required": False,
        },
        {
            "id": "vision_model",
            "label": "视觉大模型 Qwen3.5-4B（Q4_K_M）",
            "detail": "核心推理模型，约 2.5 GB，缺了无法命名",
            "kind": "hf_file",
            "repo": VISION_REPO,
            "filename": VISION_FILE,
            "target": f"AI/models/Qwen3.5-4B/{VISION_FILE}",
            "check": f"AI/models/Qwen3.5-4B/{VISION_FILE}",
            "size": 2_707_513_696,
            "required": True,
        },
        {
            "id": "vision_mmproj",
            "label": "视觉投影层 mmproj",
            "detail": "让模型能「看图」，缺了只能靠文字猜",
            "kind": "hf_file",
            "repo": VISION_REPO,
            "filename": VISION_MMPROJ,
            "target": f"AI/models/Qwen3.5-4B/{VISION_MMPROJ}",
            "check": f"AI/models/Qwen3.5-4B/{VISION_MMPROJ}",
            "size": 675_568_768,
            "required": True,
        },
        {
            "id": "asr_model",
            "label": "Qwen3-ASR-0.6B（语音识别）",
            "detail": "把语音转成文字，缺了只能纯画面分析",
            "kind": "hf_repo",
            "repo": ASR_REPO,
            "target": "AI/models/Qwen/Qwen3-ASR-0.6B",
            "check": "AI/models/Qwen/Qwen3-ASR-0.6B/model.safetensors",
            "size": 1_876_091_704,
            "required": False,
        },
        {
            "id": "aligner_model",
            "label": "Qwen3-ForcedAligner-0.6B（词级对齐）",
            "detail": "生成精确时间轴与说话人分段",
            "kind": "hf_repo",
            "repo": ALIGNER_REPO,
            "target": "AI/models/Qwen/Qwen3-ForcedAligner-0.6B",
            "check": "AI/models/Qwen/Qwen3-ForcedAligner-0.6B/model.safetensors",
            "size": 1_876_091_704,
            "required": False,
        },
    ]


def _asset_state(asset: dict[str, Any]) -> dict[str, Any]:
    """判定单项状态：present / partial / missing。"""
    check = paths.ROOT / asset["check"]
    present = check.is_file()
    current = 0
    if asset["kind"] == "hf_repo":
        directory = paths.ROOT / asset["target"]
        if directory.is_dir():
            current = sum(f.stat().st_size for f in directory.rglob("*")
                          if f.is_file() and not f.name.endswith(".incomplete"))
        present = check.is_file() and current > asset["size"] * 0.5
    else:
        if check.is_file():
            try:
                current = check.stat().st_size
            except OSError:
                current = 0

    status = "present" if present else ("partial" if current > 0 else "missing")
    return {
        "id": asset["id"],
        "label": asset["label"],
        "detail": asset["detail"],
        "required": asset["required"],
        "status": status,
        "size": asset["size"],
        "present_bytes": current,
        "target": asset["target"],
        "url": (_asset_urls(asset) or [""])[0],
        "repo": asset.get("repo", ""),
    }


def check_assets() -> dict[str, Any]:
    items = [_asset_state(asset) for asset in ai_assets()]
    missing = [i for i in items if i["status"] != "present"]
    return {
        "items": items,
        "missing": len(missing),
        "required_missing": [i["id"] for i in missing if i["required"]],
        "total_missing_bytes": sum(max(0, i["size"] - i["present_bytes"]) for i in missing),
    }


# ---------------------------------------------------------------- 下载

def _hf_url(repo: str, filename: str) -> str:
    endpoint = (os.environ.get("HF_ENDPOINT") or HF_PRESETS["hf-mirror"]).rstrip("/")
    return f"{endpoint}/{repo}/resolve/main/{filename}"


def _hf_files(repo: str) -> list[tuple[str, int]]:
    """列出 HF 仓库里要下的文件及大小。"""
    import httpx

    endpoint = (os.environ.get("HF_ENDPOINT") or HF_PRESETS["hf-mirror"]).rstrip("/")
    resp = httpx.get(f"{endpoint}/api/models/{repo}", timeout=30)
    resp.raise_for_status()
    body = resp.json()

    skip = {".gitattributes", "README.md", ".gitignore"}
    files: list[tuple[str, int]] = []
    for sibling in body.get("siblings") or []:
        name = sibling.get("rfilename") or ""
        if not name or name in skip:
            continue
        files.append((name, int(sibling.get("size") or 0)))
    return files


ProgressFn = Callable[[dict[str, Any]], None]

# 分片并发下载。4 路对 hf-mirror、华为云、gyan.dev 都还站得住，再多容易被 CDN
# 限速甚至直接掐连接（表现就是「下着下着卡住」）。要退回单线程序排查问题，
# 设环境变量 VAIR_DOWNLOAD_CONNECTIONS=1。
PARALLEL_CONNECTIONS = 4
PARALLEL_MIN_SIZE = 8 * 1024 * 1024             # 小于 8 MB 的文件分片反而更慢


class _RangeUnsupported(Exception):
    """服务端忽略 Range 回了整个文件 —— 分片没意义，退回单线程。"""


def _connections() -> int:
    raw = (os.environ.get("VAIR_DOWNLOAD_CONNECTIONS") or "").strip()
    try:
        return max(1, min(16, int(raw))) if raw else PARALLEL_CONNECTIONS
    except ValueError:
        return PARALLEL_CONNECTIONS


def _remote_size(url: str, timeout: float) -> tuple[int, bool]:
    """问一句「这个文件多大、支不支持从中间接着下」。

    用 `Range: bytes=0-0` 而不是 HEAD：不少 CDN 的 HEAD 不给 Content-Length，
    但只要你带了 Range，它一定把总长放在 Content-Range 里。
    返回 (总字节数, 是否支持分片)。
    """
    import httpx

    try:
        with httpx.stream("GET", url, headers={"Range": "bytes=0-0"},
                          timeout=timeout, follow_redirects=True) as resp:
            if resp.status_code == 206:
                content_range = resp.headers.get("Content-Range", "")
                tail = content_range.rsplit("/", 1)[-1].strip() if "/" in content_range else ""
                return (int(tail) if tail.isdigit() else 0), True
            length = resp.headers.get("Content-Length") or ""
            return (int(length) if length.isdigit() else 0), False
    except Exception as exc:                        # noqa: BLE001
        hub.debug(f"探测 {url} 大小失败（{exc}），按单线程下载")
        return 0, False


def download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int = 0,
    on_progress: ProgressFn | None = None,
    timeout: float = 60.0,
    connections: int = 0,
) -> tuple[bool, str]:
    """带断点续传的单文件下载；够大又支持 Range 时自动分片并发。

    断流从已有字节接着下；`416` 视为已完成；`401/403/404` 立即失败（重试没意义）；
    `429/5xx` 退避重试。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".incomplete")
    sidecar = part.with_name(part.name + ".parts")

    # dest 只在整份文件到位后才存在（中途都是 part），所以它在了就是下完了
    if dest.is_file() and (not expected_size or dest.stat().st_size >= expected_size):
        return True, "已存在"

    lanes = connections or _connections()
    total, ranged = _remote_size(url, timeout) if lanes > 1 else (0, False)
    total = total or expected_size

    if lanes > 1 and ranged and total >= PARALLEL_MIN_SIZE:
        try:
            return _download_parts(url, part, dest, sidecar, total, lanes, on_progress, timeout)
        except _RangeUnsupported:
            hub.debug(f"{dest.name}：服务端不支持分片，改回单线程")
            part.unlink(missing_ok=True)
        except Exception as exc:                    # noqa: BLE001
            return False, f"下载失败：{exc}"

    # 单线程路径。分片记录在这里没有意义，必须清掉 —— 分片下载会把 part 撑到
    # 完整大小（各段按偏移写，中间还是空洞），留着它会被当成「已经下完了」。
    if sidecar.is_file():
        sidecar.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return _download_stream(url, part, dest, expected_size, on_progress, timeout)


def _download_parts(url: str, part: Path, dest: Path, sidecar: Path, total: int,
                    lanes: int, on_progress: ProgressFn | None,
                    timeout: float) -> tuple[bool, str]:
    """把文件切成几段，每段一路连接同时下，各自从上次断的地方接着写。"""
    lanes = max(1, min(lanes, total // PARALLEL_MIN_SIZE))
    chunk = math.ceil(total / lanes)
    bounds = [(i * chunk, min(total, (i + 1) * chunk) - 1) for i in range(lanes)]

    done = _read_parts(sidecar, total, lanes)
    part.touch(exist_ok=True)                       # 各段都往这一个文件里按偏移写

    started = time.time()
    base_done = sum(done)
    lock = threading.Lock()
    failures: list[str] = []
    unsupported = threading.Event()
    last = {"report": 0.0, "save": 0.0}

    def bump(lane: int, count: int) -> None:
        now = time.time()
        with lock:
            done[lane] += count
            current = sum(done)
            if on_progress and now - last["report"] > 0.4:
                last["report"] = now
                on_progress(_progress(dest.name, current, total, base_done, started))
            if now - last["save"] > 3 and current < total:
                last["save"] = now
                _write_parts(sidecar, total, list(done))

    def worker(lane: int, start: int, end: int) -> None:
        try:
            _download_range(url, part, lane, start + done[lane], end, timeout, bump, unsupported)
        except _RangeUnsupported:
            unsupported.set()
        except Exception as exc:                    # noqa: BLE001
            failures.append(f"第 {lane + 1} 段：{exc}")

    hub.debug(f"{dest.name} 分 {lanes} 段并发下载（每段约 {_human_size(chunk)}）")
    threads = [threading.Thread(target=worker, args=(i, start, end),
                                name=f"vair-dl-{i}", daemon=True)
               for i, (start, end) in enumerate(bounds)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if unsupported.is_set():
        raise _RangeUnsupported()
    if failures:
        raise RuntimeError("；".join(failures[:3]))

    pending = [i for i, (start, end) in enumerate(bounds) if done[i] < end - start + 1]
    if pending:
        raise RuntimeError(f"还有 {len(pending)} 段没下完")

    if on_progress:
        on_progress(_progress(dest.name, total, total, base_done, started))
    _promote(part, dest)
    sidecar.unlink(missing_ok=True)
    return True, "完成"


def _download_range(url: str, part: Path, lane: int, position: int, end: int,
                    timeout: float, bump: Callable[[int, int], None],
                    unsupported: threading.Event) -> None:
    """下 [position, end] 这一段；断了从断点接着来，最多重试 8 次。"""
    import httpx

    attempt = 0
    while position <= end:
        attempt += 1
        try:
            with httpx.stream("GET", url, headers={"Range": f"bytes={position}-{end}"},
                              timeout=timeout, follow_redirects=True) as resp:
                if resp.status_code == 200:
                    unsupported.set()               # Range 被忽略，回的是整个文件
                    raise _RangeUnsupported()
                if resp.status_code == 416:
                    return                          # 越界：这一段其实已经齐了
                if resp.status_code in (401, 403, 404):
                    raise RuntimeError(f"下载被拒绝（HTTP {resp.status_code}）")
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                with open(long_path(part), "r+b") as fh:
                    fh.seek(position)
                    for block in resp.iter_bytes(1024 * 256):
                        if not block:
                            continue
                        fh.write(block)
                        position += len(block)
                        bump(lane, len(block))
        except _RangeUnsupported:
            raise
        except Exception as exc:                    # noqa: BLE001 - 统一走重试
            if attempt >= 8:
                raise RuntimeError(f"下载失败（已重试 {attempt} 次）：{exc}") from None
            time.sleep(min(2 ** attempt, 20))


def _read_parts(sidecar: Path, total: int, lanes: int) -> list[int]:
    """读分片续传记录；对不上（换了文件、片数变了）就当没有，从头下。"""
    zeros = [0] * lanes
    try:
        data = json.loads(sidecar.read_text("utf-8"))
    except (OSError, ValueError):
        return zeros
    done = data.get("done")
    if int(data.get("total") or 0) != total or not isinstance(done, list) or len(done) != lanes:
        return zeros
    try:
        return [max(0, min(total, int(value))) for value in done]
    except (TypeError, ValueError):
        return zeros


def _write_parts(sidecar: Path, total: int, done: list[int]) -> None:
    try:
        sidecar.write_text(json.dumps({"total": total, "done": done}), "utf-8")
    except OSError:
        pass                                        # 记录丢了顶多重下，不该让下载失败


def _promote(part: Path, dest: Path) -> None:
    """part → dest。刚写完的大文件在 Windows 上可能被杀软/索引器短暂占用，
    直接 os.replace 会撞 WinError 32，所以退避重试几次。"""
    for attempt in range(10):
        try:
            os.replace(long_path(part), long_path(dest))
            return
        except OSError:
            if attempt == 9:
                raise
            time.sleep(0.3 * (attempt + 1))


def _download_stream(url: str, part: Path, dest: Path, expected_size: int,
                     on_progress: ProgressFn | None, timeout: float) -> tuple[bool, str]:
    """单线程下载：断流从已有字节接着下（老路子，服务端不支持分片时走这里）。"""
    import httpx

    done = part.stat().st_size if part.is_file() else 0
    if done == 0 and part.is_file():
        part.unlink(missing_ok=True)
    if expected_size and done >= expected_size:
        _promote(part, dest) if part.is_file() else None
        return True, "已存在"

    started = time.time()
    base_done = done
    attempt = 0

    while True:
        attempt += 1
        headers = {"Range": f"bytes={done}-"} if done else {}
        try:
            with httpx.stream("GET", url, headers=headers, timeout=timeout,
                              follow_redirects=True) as resp:
                if resp.status_code == 416:
                    # 请求范围超出文件长度 → 已经下完了
                    if part.is_file():
                        _promote(part, dest)
                    return True, "已完成（续传越界）"
                if resp.status_code in (401, 403, 404):
                    return False, f"下载被拒绝（HTTP {resp.status_code}）：{url}"
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                total = expected_size
                if not total:
                    length = resp.headers.get("Content-Length")
                    total = (int(length) + done) if length else 0

                mode = "ab" if done else "wb"
                last_report = 0.0
                with open(long_path(part), mode) as fh:
                    for chunk in resp.iter_bytes(1024 * 256):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if on_progress and now - last_report > 0.4:
                            last_report = now
                            on_progress(_progress(dest.name, done, total, base_done, started))

                if total and done < total:
                    raise RuntimeError(f"连接中断（{done}/{total} 字节）")

        except Exception as exc:                    # noqa: BLE001 - 统一走重试
            if attempt >= 8:
                return False, f"下载失败（已重试 {attempt} 次）：{exc}"
            delay = min(2 ** attempt, 20)
            hub.debug(f"{dest.name} 下载中断（{exc}），{delay}s 后继续，已下 {done} 字节")
            time.sleep(delay)
            continue

        _promote(part, dest)
        if on_progress:
            on_progress(_progress(dest.name, done, expected_size or done, base_done, started))
        return True, "完成"


def _progress(name: str, done: int, total: int, base_done: int, started: float) -> dict[str, Any]:
    elapsed = max(0.001, time.time() - started)
    speed = (done - base_done) / elapsed
    remaining = int((total - done) / speed) if speed > 0 and total > done else 0
    return {
        "file": name,
        "done": done,
        "total": total,
        "percent": round(done / total * 100, 1) if total else 0.0,
        "speed": int(speed),
        "eta": remaining,
    }


def _safe_extract(archive: Path, target: Path) -> None:
    """解压 zip 并防目录穿越（zip slip）。"""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        _safe_extract_to(zf, target)


def fetch_assets(ids: list[str] | None = None, echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    """执行补齐。返回逐项结果。

    `echo` 非空时把里程碑与下载进度也打到控制台 —— 启动阶段还没有前端页面，
    不然用户会盯着一个几分钟不动的窗口。
    """
    wanted = set(ids) if ids else None
    results: list[dict[str, Any]] = []
    manifest = {a["id"]: a for a in ai_assets()}

    for asset_id, asset in manifest.items():
        if wanted is not None and asset_id not in wanted:
            continue

        state = _asset_state(asset)
        if state["status"] == "present":
            results.append({"id": asset_id, "label": asset["label"], "ok": True,
                            "message": "已存在，跳过"})
            continue

        hub.publish("progress", {"phase": "start", "id": asset_id, "label": asset["label"]})
        message = f"开始下载：{asset['label']}（约 {_human_size(asset['size'])}）"
        hub.info(message)
        if echo:
            echo(message)

        try:
            if asset["kind"] == "zip":
                ok, message = _fetch_zip(asset, asset_id, echo)
            elif asset["kind"] == "hf_file":
                ok, message = _fetch_hf_file(asset, asset_id)
            else:
                ok, message = _fetch_hf_repo(asset, asset_id)
        except Exception as exc:                    # noqa: BLE001
            ok, message = False, f"失败：{exc}"

        line = f"{asset['label']} · {'完成' if ok else message}"
        hub.log("info" if ok else "error", line)
        if echo:
            echo(line)
        results.append({"id": asset_id, "label": asset["label"], "ok": ok, "message": message})
        hub.publish("progress", {"phase": "done", "id": asset_id, "ok": ok, "message": message})

    return {"results": results, "ok": all(r["ok"] for r in results)}


def provision_tools(ids: list[str] | None = None) -> tuple[bool, str]:
    """补齐外部工具（ffmpeg / exiftool）。给 run.py 启动自检用。

    ffmpeg 是硬门槛：没有它 run.py 会拒绝启动，而那时用户还进不去页面点「一键补齐」，
    所以这里直接把缺的拉下来。exiftool 缺了只影响写元数据，拉不到也不拦启动。
    """
    wanted = ["ffmpeg", "exiftool"] if ids is None else list(ids)
    missing = [a for a in ai_assets()
               if a["id"] in wanted and a["kind"] == "zip"
               and _asset_state(a)["status"] != "present"]
    if not missing:
        return True, "外部工具齐备"

    report = fetch_assets([a["id"] for a in missing], echo=print)
    failed = [r for r in report["results"] if not r["ok"]]
    if not failed:
        return True, "外部工具已就绪：" + "、".join(a["label"] for a in missing)

    detail = "；".join(f"{r['label']} {r['message']}" for r in failed)
    still_needed = [r for r in failed if any(a["id"] == r["id"] and a["required"] for a in missing)]
    if still_needed:
        return False, f"外部工具补齐失败：{detail}"
    return True, f"部分工具没下成（不影响启动）：{detail}"


def _asset_urls(asset: dict[str, Any]) -> list[str]:
    """一个资产可能有多个镜像，按顺序试。"""
    urls = [u for u in (asset.get("urls") or []) if u]
    if asset.get("url"):
        urls.insert(0, asset["url"])
    return urls


def _fetch_zip(asset: dict[str, Any], asset_id: str, echo: Callable[[str], None] | None = None) -> tuple[bool, str]:
    target = paths.ROOT / asset["target"]
    archive = paths.CACHE_DIR / f"{asset_id}.zip"
    # 每日构建的体积每次都变，拿它校验会把正常下载误判成「连接中断」，
    # 这种资产交给响应头的 Content-Length 去算总长。
    expected = 0 if asset.get("volatile_size") else asset["size"]

    last_line = [0.0]

    def on_progress(p: dict[str, Any]) -> None:
        hub.publish("progress", {"phase": "download", "id": asset_id, **p})
        if not echo or not sys.stdout.isatty():
            return
        now = time.time()
        if now - last_line[0] < 1.0:                # 每秒刷一行，别把窗口刷爆
            return
        last_line[0] = now
        print(f"\r  {p['percent']:5.1f}%  {_human_size(p['done'])} / {_human_size(p['total'])}"
              f"  {_human_size(p['speed'])}/s      ", end="", flush=True)

    ok, message = _download_any(asset, archive, expected, on_progress)
    if echo and last_line[0]:
        print()                                     # 收尾换行，别让后面的日志接在进度条后面
    if not ok:
        return False, message

    # 解压到临时目录再搬过去：解压中途失败时，ffmpeg/ 里不会留下半份文件
    stage = paths.CACHE_DIR / f"unzip-{asset_id}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        _safe_extract(archive, stage)
        payload_name = asset.get("payload") or Path(asset["check"]).name
        source = _locate_payload(stage, payload_name)
        if source is None:
            return False, f"压缩包里没找到 {payload_name}（可能是上游改了打包方式）"
        _install_payload(source, target, asset.get("rename") or {})
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return False, f"解压失败：{exc}"
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)

    paths.reset_tool_cache()                        # 刚放进去的工具要能被立刻找到
    return True, f"已解压到 {asset['target']}"


def _download_any(asset: dict[str, Any], dest: Path, expected: int,
                  on_progress: ProgressFn | None) -> tuple[bool, str]:
    """按镜像顺序下载，第一个成功的就收手。"""
    errors: list[str] = []
    for url in _asset_urls(asset):
        ok, message = download_file(url, dest, expected_size=expected, on_progress=on_progress)
        if ok:
            return True, message
        host = url.split("//", 1)[-1].split("/", 1)[0]
        errors.append(f"{host}：{message}")
        if len(_asset_urls(asset)) > 1:
            hub.warn(f"{asset['label']} 从 {host} 下载没成功，换下一个镜像")
    return False, "；".join(errors)


def _locate_payload(stage: Path, filename: str) -> Path | None:
    """在解压结果里找出可执行文件所在的目录（层级最浅的那个）。

    官方压缩包的层级各家不同：gyan.dev 是 `ffmpeg-x.y-essentials_build/bin/`，
    BtbN 是 `ffmpeg-master-latest-win64-gpl/bin/`，exiftool 直接把 exe 摊在根目录。
    所以不猜路径，按文件名找。
    """
    wanted = filename.lower()
    queue = [stage]
    while queue:
        directory = queue.pop(0)                    # 先宽后深 → 第一个命中的就是最浅的
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        if any(e.is_file() and e.name.lower() == wanted for e in entries):
            return directory
        queue.extend(e for e in entries if e.is_dir())
    return None


def _install_payload(source: Path, target: Path, rename: dict[str, str]) -> None:
    """把 source 目录里的东西搬进 target（同一分区，等于改名，很快）。"""
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        dest = target / rename.get(item.name, item.name)
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fetch_hf_file(asset: dict[str, Any], asset_id: str) -> tuple[bool, str]:
    dest = paths.ROOT / asset["target"]
    url = _hf_url(asset["repo"], asset["filename"])
    return download_file(
        url, dest, expected_size=asset["size"],
        on_progress=lambda p: hub.publish("progress", {"phase": "download", "id": asset_id, **p}),
    )


def _fetch_hf_repo(asset: dict[str, Any], asset_id: str) -> tuple[bool, str]:
    target = paths.ROOT / asset["target"]
    try:
        files = _hf_files(asset["repo"])
    except Exception as exc:                        # noqa: BLE001
        return False, f"获取文件清单失败：{exc}"

    if not files:
        return False, "仓库里没有找到可下载的文件"

    for index, (name, size) in enumerate(files, 1):
        url = _hf_url(asset["repo"], name)
        dest = target / name
        label = f"[{index}/{len(files)}] {name}"
        ok, message = download_file(
            url, dest, expected_size=size,
            on_progress=lambda p, lbl=label: hub.publish(
                "progress", {"phase": "download", "id": asset_id, "file": lbl, **{
                    k: v for k, v in p.items() if k != "file"}}),
        )
        if not ok:
            return False, f"{name} {message}"
    return True, f"已下载 {len(files)} 个文件到 {asset['target']}"


# ---------------------------------------------------------------- 依赖安装

def pip_available() -> bool:
    return _importable("pip")


def install_pip() -> tuple[bool, str]:
    """给当前解释器补 pip，返回 (是否可用, 说明)。

    便携版 Python（embeddable）不带 pip。原来只在启动脚本里补，用户直接
    `python run.py` 时会一路卡到「No module named pip」。这里兜一层：
    先试官方 get-pip.py，再退到国内镜像的 pip wheel（校验 SHA-256 后解压）。

    不能退到阿里云的 get-pip.py —— 那份是旧版，里面捆的 pip 还要 distutils，
    而 3.12 已经删掉 distutils，跑起来只会报 ModuleNotFoundError。
    """
    if pip_available():
        return True, "pip 已就绪"

    import urllib.request

    def fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=60) as resp:   # noqa: S310
            return resp.read()

    script = paths.CACHE_DIR / "get-pip.py"
    try:
        script.write_bytes(fetch("https://bootstrap.pypa.io/get-pip.py"))
        result = run([sys.executable, str(script), "--no-warn-script-location",
                      "--disable-pip-version-check"], timeout=900, check=False)
        if result.returncode == 0 and pip_available():
            return True, "pip 安装完成"
    except Exception as exc:                        # noqa: BLE001
        hub.debug(f"get-pip.py 引导失败：{exc}")
    finally:
        script.unlink(missing_ok=True)

    ok, message = _install_pip_wheel(fetch)
    if ok:
        return True, message
    return False, f"pip 安装失败（get-pip.py 与镜像 wheel 都没成）：{message}"


def _install_pip_wheel(fetch: Callable[[str], bytes]) -> tuple[bool, str]:
    """从国内 simple 索引取一个兼容的 pip wheel，校验后解压进 site-packages。"""
    import hashlib
    import io
    import re
    from urllib.parse import urljoin

    pattern = re.compile(
        r"""href=["']([^"']*pip-(\d+)\.(\d+)(?:\.(\d+))?-py3-none-any\.whl"""
        r"""#sha256=([0-9a-f]{64}))["']""", re.IGNORECASE)

    errors: list[str] = []
    for index in ("https://pypi.tuna.tsinghua.edu.cn/simple/pip/",
                  "https://mirrors.aliyun.com/pypi/simple/pip/"):
        try:
            page = fetch(index).decode("utf-8", "replace")
            best: tuple[tuple[int, int, int], str, str] | None = None
            for match in pattern.finditer(page):
                version = (int(match.group(2)), int(match.group(3)),
                           int(match.group(4) or 0))
                if version[0] >= 24 and (best is None or version > best[0]):
                    best = (version, match.group(1), match.group(5))
            if best is None:
                raise RuntimeError("索引里没有兼容的 pip wheel")

            version, href, digest = best
            url = urljoin(index, href)
            data = fetch(url)
            if hashlib.sha256(data).hexdigest() != digest.lower():
                raise RuntimeError("wheel 校验失败（SHA-256 不符）")

            site = Path(sys.prefix) / "Lib" / "site-packages"
            site.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                _safe_extract_to(zf, site)

            import importlib
            importlib.invalidate_caches()
            if pip_available():
                return True, f"pip {'.'.join(map(str, version))} 安装完成（{index.split('/')[2]}）"
            raise RuntimeError("解压后仍无法导入 pip")
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"{index.split('/')[2]}：{exc}")
    return False, "；".join(errors)


def _safe_extract_to(zf: zipfile.ZipFile, target: Path) -> None:
    """防 zip slip 地解压已打开的压缩包。"""
    target_resolved = target.resolve()
    for member in zf.infolist():
        if not member.filename or member.filename.endswith("/"):
            continue
        candidate = (target / member.filename).resolve()
        try:
            candidate.relative_to(target_resolved)
        except ValueError:
            raise RuntimeError(f"压缩包内含非法路径：{member.filename}") from None
        zf.extract(member, target)


def install_requirements(extra: list[str] | None = None) -> tuple[bool, str]:
    """把依赖装进 libs/（零污染，不动系统环境）。"""
    packages = list(extra or [])
    if not packages:
        packages = missing_packages()
    if not packages:
        return True, "依赖齐备，无需安装"

    if not pip_available():
        ok, message = install_pip()
        hub.log("info" if ok else "warn", message)
        if not ok:
            return False, message

    paths.LIBS_DIR.mkdir(parents=True, exist_ok=True)
    index = os.environ.get("PIP_INDEX_URL") or PIP_PRESETS["tsinghua"]

    args = [sys.executable, "-m", "pip", "install",
            "--target", str(paths.LIBS_DIR), "--upgrade",
            "--timeout", "120", "--retries", "10",
            "-i", index]
    host = index.split("//", 1)[-1].split("/", 1)[0]
    if host:
        args += ["--trusted-host", host]
    args += packages

    hub.info(f"安装依赖到 libs/：{' '.join(packages)}")
    result = run(args, timeout=3600, check=False)
    if result.returncode != 0:
        return False, f"依赖安装失败：{decode(result.stderr)[-500:]}"

    import importlib
    importlib.invalidate_caches()
    libs = str(paths.LIBS_DIR)
    if libs not in sys.path:
        sys.path.insert(0, libs)
    return True, "依赖安装完成"


def missing_packages() -> list[str]:
    """返回缺失项的 **pip 包名**（可直接喂给 pip install）。"""
    out: list[str] = []
    for module, package in REQUIRED_MODULES.items():
        if not _importable(module):
            out.append(package)
    for module, package in OPTIONAL_MODULES.items():
        if not _importable(module):
            out.append(package)
    return out


# ---------------------------------------------------------------- 内置转写运行环境
# Qwen3-ASR 需要 torch（CUDA 版）、qwen-asr、pyannote.audio。
# 这些包体积大、和主后端无关，单独装到 AI/asr-python，不混进 后端/libs。

ASR_TORCH_FIND_LINKS = "https://mirrors.aliyun.com/pytorch-wheels/cu128/"
ASR_WHEELS_DIR = paths.CACHE_DIR / "asr-wheels"
_ASR_IMPORT_PROBE = "import torch, torchaudio, qwen_asr, pyannote.audio"
_ASR_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
ASR_STATUS_TTL = 60.0

# 同时下几个 wheel。每个 wheel 内部再开 4 条分片连接，总连接数控制在 12 左右。
ASR_WHEEL_WORKERS = 3


def _asr_requirements() -> Path:
    return paths.ASR_SERVICE_DIR / "requirements.txt"


def asr_runtime_status(force: bool = False) -> dict[str, Any]:
    """检查内置转写运行环境是否可用。

    `import torch` 要好几秒，所以结果缓存 60 秒；安装完会主动失效。
    """
    import time

    cached = _ASR_STATUS_CACHE.get("data")
    if cached is not None and not force \
            and time.time() - float(_ASR_STATUS_CACHE.get("at") or 0) < ASR_STATUS_TTL:
        return dict(cached)

    target = paths.ASR_RUNTIME_DIR
    installed = target.is_dir() and any(target.iterdir())
    ok = False
    if installed:
        result = run([sys.executable, "-c", _ASR_IMPORT_PROBE],
                     timeout=180, check=False)
        ok = result.returncode == 0
    data = {
        "ok": ok,
        "installed": installed,
        "path": str(target),
        "python": sys.executable,
        "message": "运行环境已就绪" if ok else ("已安装但不完整，可重新安装" if installed else "尚未安装"),
    }
    _ASR_STATUS_CACHE.update({"at": time.time(), "data": data})
    return dict(data)


def _write_asr_pth() -> str:
    """把 AI/asr-python 挂进解释器的 sys.path。

    便携版 Python 带 python3xx._pth 时处于 isolated 模式，PYTHONPATH 会被忽略，
    .pth 是子进程找到这些包的唯一可靠方式。
    """
    import sysconfig

    purelib = Path(sysconfig.get_paths()["purelib"])
    pth = purelib / "vair-asr.pth"
    try:
        purelib.mkdir(parents=True, exist_ok=True)
        pth.write_text(str(paths.ASR_RUNTIME_DIR) + "\n", encoding="utf-8")
    except OSError as exc:
        return f"写入 {pth} 失败：{exc}"
    return ""


def _pip_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _pip_stream(args: list[str], timeout: float = 1800,
                on_line: Callable[[str], None] | None = None) -> int:
    """跑 pip 并把输出逐行转到运行日志（之前是整段吞掉，用户看不到任何进度）。"""
    try:
        child = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_pip_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        hub.warn(f"启动 pip 失败：{exc}")
        return 1

    started = time.time()
    watchdog = threading.Timer(timeout, child.kill)
    watchdog.daemon = True
    watchdog.start()
    assert child.stdout is not None
    try:
        for line in child.stdout:
            line = line.strip()
            if line:
                hub.debug(f"[pip] {line}")
                if on_line:
                    try:
                        on_line(line)
                    except Exception:               # noqa: BLE001 - 回调不能影响安装
                        pass
        return child.wait()
    finally:
        watchdog.cancel()
        if time.time() - started > timeout:
            hub.warn("pip 执行超时，已终止")


def _pinned_versions(requirements: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pins
    for line in lines:
        line = line.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([^\s;,]+)$", line)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def _torch_wheel_items(requirements: Path) -> list[dict[str, str]]:
    """从 PyTorch 镜像目录里找出 torch / torchaudio 的 CUDA wheel 地址。

    pip 在 `--find-links` 下解析版本时，必须先把整个 wheel 下载下来才能读元数据；
    torch 有 2.4 GB，这一步会把「快速并发下载」变成一次单连接慢速下载。所以这里
    直接按解释器标签挑文件名，自己下载。
    """
    pins = _pinned_versions(requirements)
    needed = {name: pins[name] for name in ("torch", "torchaudio") if name in pins}
    if not needed:
        return []

    import httpx

    try:
        resp = httpx.get(ASR_TORCH_FIND_LINKS, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        page = resp.text
    except Exception as exc:                        # noqa: BLE001
        hub.debug(f"读取 PyTorch 镜像目录失败：{exc}")
        return []

    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    hrefs = re.findall(r'href="([^"]+\.whl)"', page)
    items: list[dict[str, str]] = []
    for name, version in needed.items():
        base = version.split("+", 1)[0]
        candidates = []
        for href in hrefs:
            filename = html_mod.unescape(href).rsplit("/", 1)[-1]
            low = filename.lower()
            if not low.startswith(f"{name}-{base}"):
                continue
            if "cu128" not in low or "win_amd64" not in low:
                continue
            # 优先当前解释器标签（cp312-cp312），其次 abi3
            if f"-{py_tag}-{py_tag}-" in low:
                rank = 2
            elif "abi3" in low:
                rank = 1
            else:
                continue
            candidates.append((rank, filename))
        if candidates:
            candidates.sort(key=lambda pair: -pair[0])
            filename = candidates[0][1]
            url = urllib.parse.urljoin(ASR_TORCH_FIND_LINKS,
                                       urllib.parse.quote(filename))
            items.append({"name": name, "version": version,
                          "file": filename, "url": url})
    return items


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _pip_download_rest(requirements: Path, index: str, host: str) -> int:
    """把剩下的小依赖（qwen-asr / pyannote / fastapi 等）下到本地 wheel 目录。

    torch 已经在本地，pip 会直接复用，不会再下 2.4 GB。下载期间按磁盘增长量
    推送进度（这些包通常只有几百 MB）。
    """
    ASR_WHEELS_DIR.mkdir(parents=True, exist_ok=True)
    baseline = _dir_size(ASR_WHEELS_DIR)
    expected = {"size": 0}
    size_re = re.compile(r"\((\d+(?:\.\d+)?)\s*(kB|KB|MB|GB)\)")

    def on_line(line: str) -> None:
        found = size_re.search(line)
        if not found:
            return
        value = float(found.group(1))
        unit = found.group(2).lower()
        factor = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}[unit]
        expected["size"] += int(value * factor)

    args = [
        sys.executable, "-m", "pip", "download",
        "--dest", str(ASR_WHEELS_DIR), "--only-binary=:all:",
        "--find-links", str(ASR_WHEELS_DIR),
        "--find-links", ASR_TORCH_FIND_LINKS,
        "-i", index, "--timeout", "120", "--retries", "10",
        "-r", str(requirements),
    ]
    if host:
        args += ["--trusted-host", host]

    holder: dict[str, int] = {}

    def work() -> None:
        holder["code"] = _pip_stream(args, timeout=3600, on_line=on_line)

    worker = threading.Thread(target=work, daemon=True, name="vair-asr-pip-download")
    worker.start()
    at = time.time()
    last = baseline
    while worker.is_alive():
        time.sleep(1.0)
        size = _dir_size(ASR_WHEELS_DIR)
        now = time.time()
        speed = (size - last) / max(0.001, now - at)
        at, last = now, size
        total = baseline + max(expected["size"], size - baseline, 1)
        percent = min(99.0, (size - baseline) / max(1, total - baseline) * 100)
        eta = int((total - size) / speed) if speed > 0 and total > size else 0
        hub.publish("progress", {
            "phase": "download", "id": "asr-runtime",
            "file": "其余依赖", "done": size, "total": total,
            "percent": round(percent, 1), "speed": int(speed), "eta": eta,
        })
    worker.join()
    return holder.get("code", 1)


def _download_asr_wheels(items: list[dict[str, str]]) -> tuple[bool, str]:
    """并发下载所有 wheel，聚合进度推给前端（百分比 / 速度 / 剩余时间）。"""
    ASR_WHEELS_DIR.mkdir(parents=True, exist_ok=True)
    if not items:
        return False, "没有解析到需要下载的包"

    sizes: dict[str, int] = {}
    for item in items:
        dest = ASR_WHEELS_DIR / item["file"]
        item["dest"] = str(dest)
        if dest.is_file():
            sizes[item["file"]] = dest.stat().st_size
            continue
        size, _ = _remote_size(item["url"], 30)
        sizes[item["file"]] = size

    total_all = sum(sizes.values())
    hub.info(f"待下载 {len(items)} 个组件，共约 {_human_size(total_all)}，"
             f"{ASR_WHEEL_WORKERS} 个文件并发 + 每个文件 4 路分片…")
    done_map: dict[str, int] = {key: 0 for key in sizes}
    for item in items:
        dest = Path(item["dest"])
        if dest.is_file():
            done_map[item["file"]] = dest.stat().st_size

    lock = threading.Lock()
    state = {"at": time.time(), "done": sum(done_map.values()), "last": 0.0}
    seen_total = max(total_all, 1)
    hub.publish("progress", {
        "phase": "download", "id": "asr-runtime", "file": "准备下载",
        "done": state["done"], "total": seen_total,
        "percent": round(state["done"] / seen_total * 100, 1),
        "speed": 0, "eta": 0,
    })

    def emit(file_label: str, force: bool = False) -> None:
        now = time.time()
        with lock:
            done = sum(done_map.values())
            if not force and now - state["at"] < 0.7:
                return
            elapsed = max(0.001, now - state["at"])
            speed = (done - state["done"]) / elapsed
            state["at"] = now
            state["done"] = done
        total = max(total_all, done)
        percent = round(done / total * 100, 1) if total else 0.0
        eta = int((total - done) / speed) if speed > 0 and total > done else 0
        hub.publish("progress", {
            "phase": "download", "id": "asr-runtime",
            "file": file_label, "done": done, "total": total,
            "percent": percent, "speed": int(speed), "eta": eta,
        })

    def fetch(item: dict[str, str]) -> tuple[bool, str]:
        key = item["file"]

        def on_progress(p: dict[str, Any]) -> None:
            with lock:
                done_map[key] = min(int(p.get("done") or 0), sizes[key] or int(p.get("done") or 0))
            emit(key)

        ok, message = download_file(
            item["url"], Path(item["dest"]),
            expected_size=sizes.get(key, 0), on_progress=on_progress,
            timeout=120.0,
            # 单个过 GB 的大 wheel 多开几条连接；小文件保持默认
            connections=8 if sizes.get(key, 0) > (1 << 30) else 0,
        )
        if ok:
            with lock:
                done_map[key] = sizes.get(key) or Path(item["dest"]).stat().st_size
            emit(key, force=True)
        return ok, message

    # 大的先下：torch 占绝大部分体积，早点开始下载总时长更短
    ordered = sorted(items, key=lambda it: -sizes.get(it["file"], 0))
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=ASR_WHEEL_WORKERS) as pool:
        for item, (ok, message) in zip(ordered, pool.map(fetch, ordered)):
            if not ok:
                errors.append(f"{item['name'] or item['file']}：{message}")
    if errors:
        return False, "；".join(errors[:3])
    return True, f"已下载 {len(items)} 个 wheel"


def _install_asr_from_wheels(requirements: Path) -> int:
    args = [
        sys.executable, "-m", "pip", "install",
        "--target", str(paths.ASR_RUNTIME_DIR), "--upgrade",
        "--no-index", "--only-binary=:all:",
        "--find-links", str(ASR_WHEELS_DIR),
        "-r", str(requirements),
    ]
    hub.info("正在把已下载的组件安装到 AI/asr-python…")
    return _pip_stream(args, timeout=3600)


def install_asr_runtime() -> tuple[bool, str]:
    """安装内置转写运行环境（下载量大，调用方应放到后台线程）。

    流程：pip 解析出每个 wheel 的地址 → 自己的多连接下载器并发下载（带进度和
    速度）→ pip 从本地 wheel 离线安装。比直接 `pip install` 单连接快，而且
    用户能看见到底下到哪了。
    """
    if not pip_available():
        ok, message = install_pip()
        hub.log("info" if ok else "warn", message)
        if not ok:
            return False, message

    requirements = _asr_requirements()
    if not requirements.is_file():
        return False, f"找不到依赖清单：{requirements}"

    index = os.environ.get("PIP_INDEX_URL") or PIP_PRESETS["tsinghua"]
    host = index.split("//", 1)[-1].split("/", 1)[0]

    hub.publish("progress", {"phase": "start", "id": "asr-runtime",
                             "label": "转写运行环境"})

    # 大头（torch / torchaudio，约 3 GB）自己用多连接下载；其余小依赖交给 pip，
    # 但把本地 wheel 目录同时作为 --find-links，pip 就不会重复下载 torch。
    torch_items = _torch_wheel_items(requirements)
    if torch_items:
        hub.info(f"从 PyTorch 镜像并发下载 {len(torch_items)} 个 CUDA 组件…")
        ok, message = _download_asr_wheels(torch_items)
        if not ok:
            hub.publish("progress", {"phase": "done", "id": "asr-runtime",
                                     "ok": False, "message": message})
            return False, f"CUDA 组件下载失败：{message}"
    else:
        hub.warn("没能从镜像目录定位 torch wheel，改由 pip 解析下载（可能较慢）")

    code = _pip_download_rest(requirements, index, host)
    if code != 0:
        hub.publish("progress", {"phase": "done", "id": "asr-runtime",
                                 "ok": False, "message": "依赖下载失败"})
        return False, "其余依赖下载失败，详见运行日志里的 pip 输出"

    hub.publish("progress", {"phase": "download", "id": "asr-runtime",
                             "file": "正在安装", "percent": 99.5, "speed": 0, "eta": 0})
    code = _install_asr_from_wheels(requirements)
    hub.publish("progress", {"phase": "done", "id": "asr-runtime",
                             "ok": code == 0,
                             "message": "安装完成" if code == 0 else "安装失败"})

    if code != 0:
        return False, "转写运行环境安装失败，详见运行日志里的 pip 输出"

    note = _write_asr_pth()
    if note:
        return False, note

    # 之前探测过「这个解释器跑不了 ASR」的缓存要作废，否则服务仍然报找不到 Python。
    try:
        from .services import _ASR_PY_CACHE
        _ASR_PY_CACHE.clear()
    except Exception:                               # noqa: BLE001
        pass

    status = asr_runtime_status(force=True)
    if not status["ok"]:
        return False, "依赖装完了，但 import 仍然失败；请查看上一条日志里的错误"
    return True, f"转写运行环境已就绪（{paths.ASR_RUNTIME_DIR}）"
