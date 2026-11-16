"""FastAPI 路由。

约定：
- 前端是纯视图，权威状态都在后端，所以 `GET /api/state` 必须是完整快照。
- 长任务（下载、安装、转写测试）不在请求里同步等完，而是丢后台线程 + SSE 报进度。
- 静态前端挂在最后，`/{path}` 兜底做 SPA 回退，但 `/api/*` 永远优先。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import bootstrap, config as config_mod, dialogs, media, naming, paths, tts
from ..engine import engine
from ..hub import hub
from ..proc import decode
from ..services import services
from ..transcribe import transcriber
from ..vision import vision
from . import sse

router = APIRouter()
router.include_router(sse.router)

# 由 run.py 注入实际监听信息
_SERVER: dict[str, Any] = {"host": "127.0.0.1", "port": 8000, "lan": False}


def set_server_info(host: str, port: int) -> None:
    _SERVER.update({
        "host": host,
        "port": port,
        "lan": host not in ("127.0.0.1", "localhost", "::1"),
    })


# ---------------------------------------------------------------- 基础

@router.get("/api/health")
def health() -> dict[str, Any]:
    from .. import __version__
    return {
        "ok": True,
        "version": __version__,
        "root": str(paths.ROOT),
        "lan": _SERVER["lan"],
        "uptime": round(__import__("time").time() - hub.started_at, 1),
    }


@router.get("/api/state")
def state() -> dict[str, Any]:
    services.refresh()
    return {**engine.snapshot(), "services": services.status()}


@router.get("/api/logs")
def logs(limit: int = 300, level: str = "all") -> dict[str, Any]:
    return {"logs": hub.history(limit=max(1, min(limit, 1000)), level=level)}


@router.post("/api/logs/clear")
def clear_logs() -> dict[str, Any]:
    hub.reset_history()
    return {"ok": True}


# ---------------------------------------------------------------- 配置

@router.get("/api/config")
def get_config() -> dict[str, Any]:
    return {"config": config_mod.as_json(), "defaults": config_mod.DEFAULT_CONFIG}


@router.put("/api/config")
def put_config(payload: dict = Body(...)) -> dict[str, Any]:
    before = config_mod.as_json()
    patch = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="配置格式错误")

    after = config_mod.update(patch)

    # 镜像改了要立刻生效，不然下次装依赖还在走旧源
    bootstrap.apply_mirrors(after)

    changed = config_mod.diff_keys(before, after)
    restart = config_mod.restart_required(before, after)
    hub.info(f"配置已保存 · 变更 {len(changed)} 项" + (f"，需重启：{'、'.join(restart)}" if restart else ""))

    return {
        "ok": True,
        "config": after,
        "changed": changed,
        "restart_required": restart,
        "message": "已保存" + ("；部分项需重启后端才生效" if restart else ""),
    }


@router.post("/api/config/reset")
def reset_config() -> dict[str, Any]:
    cfg = config_mod.reset()
    bootstrap.apply_mirrors(cfg)
    hub.warn("配置已恢复默认值")
    return {"ok": True, "config": cfg}


@router.post("/api/config/open")
def open_config() -> dict[str, Any]:
    """用系统默认程序打开 config.json，第一次点就先落盘一份。"""
    if not paths.CONFIG_PATH.is_file():
        try:
            config_mod.save(config_mod.as_json())
        except OSError as exc:
            return {"ok": False, "message": f"无法生成配置文件：{exc}"}

    target = paths.CONFIG_PATH
    try:
        if os.name == "nt":
            os.startfile(str(target))                       # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except OSError as exc:
        return {"ok": False, "message": f"无法打开配置文件：{exc}", "path": str(target)}
    return {"ok": True, "message": "已用系统默认程序打开 config.json", "path": str(target)}


# ---------------------------------------------------------------- 运行时与网络

@router.get("/api/runtime")
def runtime() -> dict[str, Any]:
    return bootstrap.probe()


@router.get("/api/mirrors")
def mirrors() -> dict[str, Any]:
    return {"pip": bootstrap.PIP_PRESETS, "hf": bootstrap.HF_PRESETS}


def _lan_ips(limit: int = 6) -> list[str]:
    """枚举本机局域网地址。

    注意不要用 `connect("8.8.8.8")` 探测：挂了代理 TUN 的机器会拿到 198.18.x.x 这类假地址。
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen and not _is_fake_ip(value):
            seen.add(value)
            found.append(value)

    for probe in ("192.168.1.1", "10.0.0.1", "172.16.0.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 80))
            add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    def rank(ip: str) -> tuple[int, str]:
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10."):
            return (1, ip)
        return (2, ip)

    return sorted(found, key=rank)[:limit]


def _is_fake_ip(ip: str) -> bool:
    if ip.startswith(("127.", "169.254.", "0.0.0.0")):
        return True
    # RFC2544 保留段，常被代理软件拿来占坑
    if ip.startswith(("198.18.", "198.19.")):
        return True
    try:
        return int(ip.split(".")[0]) >= 224
    except (ValueError, IndexError):
        return True


@router.get("/api/server")
def server_info() -> dict[str, Any]:
    cfg_lan = bool(config_mod.load()["runtime"].get("lan", False))
    host, port = _SERVER["host"], _SERVER["port"]
    lan_urls = [f"http://{ip}:{port}/" for ip in _lan_ips()]
    return {
        "host": host,
        "port": port,
        "lan": _SERVER["lan"],
        "config_lan": cfg_lan,
        # 配置说开、实际只监听本机（或反之）→ 提示需要重启
        "restart_required": _SERVER["lan"] != cfg_lan,
        "local_url": f"http://127.0.0.1:{port}/",
        "lan_urls": lan_urls if _SERVER["lan"] else [],
        "platform": os.name,
    }


# ---------------------------------------------------------------- 原生对话框

@router.post("/api/dialog/files")
def dialog_files(payload: dict = Body(default={})) -> dict[str, Any]:
    try:
        return dialogs.pick_files(str(payload.get("dir") or ""))
    except dialogs.DialogBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/dialog/folder")
def dialog_folder(payload: dict = Body(default={})) -> dict[str, Any]:
    try:
        return dialogs.pick_folder(str(payload.get("dir") or ""))
    except dialogs.DialogBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/dialog/cancel")
def dialog_cancel() -> dict[str, Any]:
    return dialogs.cancel()


@router.get("/api/dialog/status")
def dialog_status() -> dict[str, Any]:
    return dialogs.status()


# ---------------------------------------------------------------- AI 连接测试

@router.get("/api/ai/models")
def ai_models() -> dict[str, Any]:
    return vision.list_models()


@router.post("/api/ai/test")
def ai_test(payload: dict = Body(default={})) -> dict[str, Any]:
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else None
    return vision.test(cfg)


@router.post("/api/whisper/test")
def whisper_test(payload: dict = Body(default={})) -> dict[str, Any]:
    """真实转写测试：现场合成一句话，走一遍完整转写链路。"""
    mode = str(payload.get("mode") or "real")
    local = transcriber.probe_local()
    remote = transcriber.probe_remote()

    if mode == "probe":
        parts = [f"内置：{local.get('message')}",
                 f"远程：{remote.get('message')}"]
        ok = bool(local.get("ok")) or bool(remote.get("ok") and remote.get("configured"))
        return {"ok": ok, "message": " · ".join(parts),
                "local": local, "remote": remote}

    audio, note = tts.synthesize()
    if audio is None:
        return {"ok": False, "message": note, "local": local, "remote": remote}

    try:
        result = transcriber.run(audio)
    finally:
        try:
            audio.unlink(missing_ok=True)
        except OSError:
            pass

    if result.error:
        return {"ok": False, "message": f"转写失败：{result.error}",
                "local": local, "remote": remote, "source": result.source}
    if not (result.text or "").strip():
        return {"ok": False, "message": "服务返回了空文本，模型可能还在加载",
                "local": local, "remote": remote, "source": result.source}

    return {
        "ok": True,
        "message": f"转写成功（{result.source}）· {len(result.text)} 字 · "
                   f"{result.elapsed_ms / 1000:.1f}s",
        "text": result.text[:300],
        "source": result.source,
        "elapsed_ms": result.elapsed_ms,
        "local": local,
        "remote": remote,
    }


# ---------------------------------------------------------------- 内置转写运行环境

_asr_runtime_lock = threading.Lock()
_asr_runtime_running = False
_asr_runtime_message = ""


@router.get("/api/asr/runtime")
def asr_runtime() -> dict[str, Any]:
    with _asr_runtime_lock:
        running, message = _asr_runtime_running, _asr_runtime_message
    return {**bootstrap.asr_runtime_status(), "running": running, "message_override": message}


@router.post("/api/asr/runtime/install")
def asr_runtime_install() -> dict[str, Any]:
    global _asr_runtime_running, _asr_runtime_message
    with _asr_runtime_lock:
        if _asr_runtime_running:
            return {"ok": True, "message": "正在安装，请稍候"}
        _asr_runtime_running = True
        _asr_runtime_message = ""

    def worker() -> None:
        global _asr_runtime_running, _asr_runtime_message
        try:
            ok, message = bootstrap.install_asr_runtime()
            _asr_runtime_message = message
            hub.log("info" if ok else "warn", f"转写运行环境：{message}")
        except Exception as exc:                    # noqa: BLE001
            _asr_runtime_message = f"安装异常：{exc}"
            hub.warn(_asr_runtime_message)
        finally:
            with _asr_runtime_lock:
                _asr_runtime_running = False

    threading.Thread(target=worker, daemon=True, name="vair-asr-runtime").start()
    return {"ok": True, "message": "已开始安装内置转写运行环境（约 3 GB，请留意日志）"}


# ---------------------------------------------------------------- AI 服务

@router.get("/api/services")
def list_services() -> dict[str, Any]:
    services.refresh()
    return services.status()


@router.post("/api/services/{name}/{action}")
def control_service(name: str, action: str) -> dict[str, Any]:
    if name not in ("llama", "asr"):
        raise HTTPException(status_code=404, detail=f"未知服务：{name}")
    if action == "start":
        result = services.start(name)
    elif action == "stop":
        result = services.stop(name)
    elif action == "restart":
        result = services.restart(name)
    else:
        raise HTTPException(status_code=400, detail=f"未知操作：{action}")
    services.refresh()
    return {**result, "services": services.status()}


# ---------------------------------------------------------------- 补齐 AI 依赖与模型

@router.get("/api/bootstrap/check")
def bootstrap_check() -> dict[str, Any]:
    return {**bootstrap.check_assets(), "runtime": bootstrap.probe()}


@router.post("/api/bootstrap/fetch")
def bootstrap_fetch(payload: dict = Body(default={})) -> dict[str, Any]:
    """下载缺失的 AI 资产。后台跑，进度走 SSE。"""
    ids = payload.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids 必须是数组")

    def worker() -> None:
        try:
            bootstrap.fetch_assets(ids)
        except Exception as exc:                    # noqa: BLE001
            hub.error(f"补齐过程出错：{exc}")
        finally:
            hub.publish("progress", {"phase": "all-done"})

    threading.Thread(target=worker, daemon=True, name="vair-fetch").start()
    return {"ok": True, "message": "已开始下载，进度会在页面上实时显示"}


@router.post("/api/bootstrap/install")
def bootstrap_install(payload: dict = Body(default={})) -> dict[str, Any]:
    packages = payload.get("packages")
    if packages is not None and not isinstance(packages, list):
        raise HTTPException(status_code=400, detail="packages 必须是数组")

    def worker() -> None:
        ok, message = bootstrap.install_requirements(packages)
        hub.log("info" if ok else "error", message)
        hub.publish("progress", {"phase": "install-done", "ok": ok, "message": message})

    threading.Thread(target=worker, daemon=True, name="vair-install").start()
    return {"ok": True, "message": "已开始安装依赖"}


# ---------------------------------------------------------------- 素材与任务

@router.post("/api/items")
def add_items(payload: dict = Body(...)) -> dict[str, Any]:
    raw = payload.get("paths")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="请提供 paths 数组")

    recursive = payload.get("recursive")
    result = engine.add_paths([str(p) for p in raw],
                              recursive=bool(recursive) if recursive is not None else None)
    for message in result["errors"]:
        hub.warn(message)
    if result["added"]:
        hub.info(f"已加入 {result['added']} 个素材"
                 + (f"，跳过重复 {result['skipped']} 个" if result["skipped"] else ""))
    return result


@router.delete("/api/items/{item_id}")
def remove_item(item_id: str) -> dict[str, Any]:
    if not engine.remove_item(item_id):
        raise HTTPException(status_code=404, detail="队列里没有这个素材")
    return {"ok": True, "queue": [i.as_dict() for i in engine.queue]}


@router.post("/api/items/clear")
def clear_items() -> dict[str, Any]:
    try:
        engine.clear_queue()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "queue": []}


@router.post("/api/tasks")
def start_task() -> dict[str, Any]:
    result = engine.start()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message"))
    return {**result, "task": engine.task_dict()}


@router.post("/api/tasks/stop")
def stop_task() -> dict[str, Any]:
    return engine.stop()


@router.get("/api/tasks")
def task_state() -> dict[str, Any]:
    return engine.snapshot()


# ---------------------------------------------------------------- 审批

@router.get("/api/reviews")
def list_reviews() -> dict[str, Any]:
    return {
        "reviews": engine.list_reviews(),
        "pending": sum(1 for r in engine.list_reviews() if r.get("status") == "pending"),
    }


@router.post("/api/reviews/{review_id}")
def resolve_review(review_id: str, payload: dict = Body(...)) -> dict[str, Any]:
    action = str(payload.get("action") or "approve")
    if action not in ("approve", "reject", "pending"):
        raise HTTPException(status_code=400, detail=f"未知操作：{action}")
    result = engine.resolve_review(review_id, action, payload.get("name"))
    return {**result, "reviews": engine.list_reviews()}


# ---------------------------------------------------------------- 静态前端

def mount_static(app) -> None:
    """挂载前端。必须在所有 /api 路由注册之后调用。"""
    frontend = paths.FRONTEND_DIR
    index = frontend / "index.html"

    if (frontend / "css").is_dir():
        app.mount("/css", StaticFiles(directory=str(frontend / "css")), name="css")
    if (frontend / "js").is_dir():
        app.mount("/js", StaticFiles(directory=str(frontend / "js")), name="js")

    @app.get("/")
    @app.get("/index.html")
    def root() -> Any:
        if not index.is_file():
            return JSONResponse(
                {"ok": False, "message": f"未找到前端：{index}"}, status_code=500)
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Any:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")

        # 防目录穿越：规范化之后必须仍在前端目录内
        try:
            candidate = (frontend / full_path).resolve()
            candidate.relative_to(frontend.resolve())
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="资源不存在") from None

        if candidate.is_file():
            return FileResponse(str(candidate))
        if index.is_file():
            return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
        raise HTTPException(status_code=404, detail="资源不存在")


def add_middleware(app) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(_locality_guard)


# 局域网访问时，这些操作只在「本机」可用。
# 理由：它们的作用域是**这台机器**，不是这个页面 —— 改监听地址、弹系统文件对话框、
# 下载几 GB 模型、装 pip 包、开关本机进程。远程访客应该只能看状态、跑任务、做审批。
_REMOTE_BLOCKED: tuple[tuple[str, str], ...] = (
    ("PUT", "/api/config"),
    ("POST", "/api/config/reset"),
    ("POST", "/api/config/open"),
    ("POST", "/api/dialog/"),
    ("POST", "/api/bootstrap/"),
    ("POST", "/api/services/"),
    ("POST", "/api/logs/clear"),
    ("POST", "/api/whisper/test"),
)


def _is_local(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


async def _locality_guard(request: Request, call_next):
    if request.method != "OPTIONS" and not _is_local(request):
        path = request.url.path
        for method, prefix in _REMOTE_BLOCKED:
            if request.method == method and path.startswith(prefix):
                hub.warn(f"已拒绝来自 {request.client.host} 的 {method} {path}"
                         "（该操作仅限本机）")
                return JSONResponse(
                    {"detail": "该操作需要在这台电脑上操作（局域网访客只能查看状态、运行任务与审批）"},
                    status_code=403,
                )
    return await call_next(request)
