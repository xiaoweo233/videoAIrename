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
import os
import shutil
import subprocess
import sys
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


def ai_assets() -> list[dict[str, Any]]:
    """AI 资产清单。每项都说明「在哪、缺了会怎样、要下多少」。"""
    return [
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
        "url": asset.get("url") or f"{ASR_REPO if asset['id'] == 'asr_model' else ''}",
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


def download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int = 0,
    on_progress: ProgressFn | None = None,
    timeout: float = 60.0,
) -> tuple[bool, str]:
    """带断点续传的单文件下载。

    断流从已有字节接着下；`416` 视为已完成；`401/403/404` 立即失败（重试没意义）；
    `429/5xx` 退避重试。
    """
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".incomplete")

    # 正式文件与 .incomplete 取较大的那个继续
    done = 0
    for candidate in (dest, part):
        if candidate.is_file():
            done = max(done, candidate.stat().st_size)
    if done == 0 and part.is_file():
        part.unlink(missing_ok=True)
    if part.is_file() and part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        done = 0

    if expected_size and done >= expected_size:
        part.replace(dest) if part.is_file() else None
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
                        part.replace(dest)
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

        part.replace(dest)
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
            if not str(candidate).startswith(str(target_resolved)):
                raise RuntimeError(f"压缩包内含非法路径，已中止：{name}")
            zf.extract(member, target)


def fetch_assets(ids: list[str] | None = None) -> dict[str, Any]:
    """执行补齐。返回逐项结果。"""
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
        hub.info(f"开始下载：{asset['label']}（约 {asset['size'] / 1024**3:.1f} GB）")

        try:
            if asset["kind"] == "zip":
                ok, message = _fetch_zip(asset, asset_id)
            elif asset["kind"] == "hf_file":
                ok, message = _fetch_hf_file(asset, asset_id)
            else:
                ok, message = _fetch_hf_repo(asset, asset_id)
        except Exception as exc:                    # noqa: BLE001
            ok, message = False, f"失败：{exc}"

        hub.log("info" if ok else "error",
                f"{asset['label']} · {'完成' if ok else message}")
        results.append({"id": asset_id, "label": asset["label"], "ok": ok, "message": message})
        hub.publish("progress", {"phase": "done", "id": asset_id, "ok": ok, "message": message})

    return {"results": results, "ok": all(r["ok"] for r in results)}


def _fetch_zip(asset: dict[str, Any], asset_id: str) -> tuple[bool, str]:
    target = paths.ROOT / asset["target"]
    archive = paths.CACHE_DIR / f"{asset_id}.zip"
    ok, message = download_file(
        asset["url"], archive, expected_size=asset["size"],
        on_progress=lambda p: hub.publish("progress", {"phase": "download", "id": asset_id, **p}),
    )
    if not ok:
        return False, message
    try:
        _safe_extract(archive, target)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return False, f"解压失败：{exc}"
    finally:
        archive.unlink(missing_ok=True)
    return True, f"已解压到 {asset['target']}"


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
