"""运行时引导与「一键补齐 AI 依赖与模型」。

两件事：

1. `early_init()` —— 必须在任何第三方 import 之前跑。把临时目录/HF 缓存重定向到仓库内、
   把 libs/ 注入 sys.path、应用国内镜像、把 stdio 切成 UTF-8。
2. AI 资产清单与下载 —— 模型体积巨大不入库，别人拿到代码后需要能一键拉回来。
   清单里每一项都带来源与校验方式，下载支持断点续传，进度实时推给前端。
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
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
}

OPTIONAL_MODULES: dict[str, str] = {
    "faster_whisper": "faster-whisper",
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
VISION_REPO = "Noobito45/Qwen3.8-9B-heretic-uncensored-NVFP4-GGUF"
VISION_FILE = "Qwen3.8-9B-distill-heretic_nvfp4_q4_k_m.gguf"
VISION_MMPROJ = "mmproj-BF16.gguf"
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
            "label": "视觉大模型 Qwen3.8-9B（Q4_K_M）",
            "detail": "核心推理模型，约 4.8 GB，缺了无法命名",
            "kind": "hf_file",
            "repo": VISION_REPO,
            "filename": VISION_FILE,
            "target": f"AI/models/Qwen3.8-9B/{VISION_FILE}",
            "check": f"AI/models/Qwen3.8-9B/{VISION_FILE}",
            "size": 5_178_305_056,
            "required": True,
        },
        {
            "id": "vision_mmproj",
            "label": "视觉投影层 mmproj",
            "detail": "让模型能「看图」，缺了只能靠文字猜",
            "kind": "hf_file",
            "repo": VISION_REPO,
            "filename": VISION_MMPROJ,
            "target": f"AI/models/Qwen3.8-9B/{VISION_MMPROJ}",
            "check": f"AI/models/Qwen3.8-9B/{VISION_MMPROJ}",
            "size": 921_705_024,
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
    target_resolved = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            candidate = (target / name).resolve()
            try:
                candidate.relative_to(target_resolved)
            except ValueError:
                raise RuntimeError(f"压缩包内含非法路径，已中止：{name}") from None
            zf.extract(member, target)


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

def install_requirements(extra: list[str] | None = None) -> tuple[bool, str]:
    """把依赖装进 libs/（零污染，不动系统环境）。"""
    packages = list(extra or [])
    if not packages:
        packages = missing_packages()
    if not packages:
        return True, "依赖齐备，无需安装"
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
