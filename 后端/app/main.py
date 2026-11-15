"""FastAPI 应用装配。

单独放在这里，是为了让 `uvicorn app.main:app --reload` 也能直接跑起来（开发时方便）。
正式入口是 `后端/run.py`，它会在导入本模块之前先完成环境引导。
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import bootstrap, config as config_mod, paths
from .engine import engine
from .hub import hub
from .services import services
from .web import api as api_mod


def _ensure_config_file() -> None:
    """首次启动时把默认配置落盘，用户才找得到 config.json。"""
    if paths.CONFIG_PATH.is_file():
        return
    try:
        config_mod.save(config_mod.as_json())
        hub.info(f"已生成默认配置：{paths.CONFIG_PATH}")
    except OSError as exc:
        hub.warn(f"无法写入默认配置：{exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.bind_loop(asyncio.get_running_loop())
    _ensure_config_file()

    report = bootstrap.probe()
    tools_ok = report["tools"]
    hub.info(f"后端就绪 · 仓库根 {report['root']}")
    hub.debug(f"ffmpeg={tools_ok['ffmpeg']['path']} · exiftool={tools_ok['exiftool']['path']}")
    if report["cuda"]["available"]:
        hub.info(f"GPU · {report['cuda']['name']} · "
                 f"{report['cuda']['memory'] // 1024} GB · 驱动 {report['cuda']['driver']}")
    else:
        hub.warn("未检测到 NVIDIA GPU，转写与视觉推理会走 CPU（会慢很多）")
    for problem in report["problems"]:
        hub.error(problem)

    # 恢复队列状态：重命名过的文件可能已不在原位，清理掉失效条目
    _prune_queue()

    # AI 服务自动拉起放到后台：模型加载几十秒，不能卡住服务启动
    if os.environ.get("VAIR_NO_AUTOSTART") == "1":
        hub.info("已按 --no-autostart 跳过 AI 服务自动拉起")
    else:
        threading.Thread(target=services.autostart, daemon=True, name="vair-autostart").start()

    # 自动自检（只读，很快）
    if (config_mod.load().get("bootstrap") or {}).get("auto_check", True):
        threading.Thread(target=_auto_check, daemon=True, name="vair-autocheck").start()

    try:
        yield
    finally:
        hub.info("正在关闭，停止内置 AI 服务…")
        services.stop_all()


def _auto_check() -> None:
    try:
        report = bootstrap.check_assets()
        if report["missing"]:
            names = [i["label"] for i in report["items"] if i["status"] != "present"]
            hub.warn(f"AI 资产缺失 {report['missing']} 项：{'、'.join(names)}")
            hub.info("可在「设置 → 系统与依赖」里点击「补齐 AI 依赖与模型」自动下载")
        else:
            hub.info("AI 资产齐备")
    except Exception as exc:                        # noqa: BLE001
        hub.debug(f"AI 资产自检跳过：{exc}")


def _prune_queue() -> None:
    """丢掉源文件已消失的队列条目（上次运行后被移动/删除的）。"""
    gone = [item for item in engine.queue if not Path(item.path).is_file()]
    for item in gone:
        engine.queue.remove(item)
    if gone:
        hub.debug(f"已从队列移除 {len(gone)} 个不存在的文件")


def create_app() -> FastAPI:
    app = FastAPI(
        title="VAIR · 视频 AI 重命名助手",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    api_mod.add_middleware(app)
    # 先注册所有 /api 路由，再挂静态与 SPA 兜底，否则兜底路由会把接口吃掉
    app.include_router(api_mod.router)
    api_mod.mount_static(app)
    return app


app = create_app()
