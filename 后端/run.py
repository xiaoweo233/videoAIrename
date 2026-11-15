"""VAIR 后端启动入口。

顺序有硬性依赖，不要随意调整：

    1. sys.path 处理 + stdio 兜底（pythonw 下 stdout 可能是 None）
    2. bootstrap.early_init()      ← 必须在任何第三方 import 之前
    3. 读取配置并应用镜像
    4. 自检并尝试补齐依赖 / 外部工具
    5. 核心依赖缺失时只启动本机诊断服务；齐备时才导入 uvicorn 与 api

用法：
    python run.py                     # 默认 127.0.0.1:8000
    python run.py --lan               # 监听 0.0.0.0，手机可访问
    python run.py --port 8080 --reload
    python run.py --selfcheck         # 只打印自检报告后退出
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让 `python 后端/run.py` 与 `python run.py`（在 后端/ 下）都能跑
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import bootstrap  # noqa: E402  —— 只依赖标准库，安全


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VAIR · 视频 AI 重命名助手 后端")
    parser.add_argument("--host", default="", help="监听地址（默认取配置，配置缺失则 127.0.0.1）")
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认取配置，配置缺失则 8000）")
    parser.add_argument("--lan", action="store_true", help="监听 0.0.0.0，允许局域网设备访问")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重启")
    parser.add_argument("--selfcheck", action="store_true", help="只打印自检报告后退出")
    parser.add_argument("--no-install", action="store_true", help="跳过自动安装；核心依赖缺失时进入只读诊断页")
    parser.add_argument("--no-autostart", action="store_true", help="不自动拉起内置 AI 服务")
    return parser.parse_args()


def _print_banner(host: str, port: int, lan_urls: list[str]) -> None:
    print()
    print("  VAIR · 视频 AI 重命名助手")
    print("  " + "-" * 46)
    print(f"  监听地址 : {host}:{port}")
    print(f"  本机访问 : http://127.0.0.1:{port}/")
    for url in lan_urls:
        print(f"  局域网   : {url}   （手机连同一 Wi-Fi 可用）")
    print(f"  接口文档 : http://127.0.0.1:{port}/api/docs")
    print("  按 Ctrl+C 停止")
    print()


def _resolve_listen(args: argparse.Namespace, cfg: dict) -> tuple[str, int]:
    runtime = cfg.get("runtime") or {}
    want_lan = args.lan or bool(runtime.get("lan", False))

    if args.host:
        host = args.host
    elif want_lan:
        host = "0.0.0.0"
    else:
        host = str(runtime.get("host") or "127.0.0.1")

    port = args.port or int(runtime.get("port") or 8000)
    return host, port


def _lan_urls(port: int) -> list[str]:
    try:
        from app.web.api import _lan_ips
        return [f"http://{ip}:{port}/" for ip in _lan_ips()]
    except Exception:                               # noqa: BLE001
        return []


def main() -> int:
    args = parse_args()

    # ---- 1. 引导（必须最先）----
    bootstrap.early_init()

    from app import config as config_mod
    from app import proc

    proc.pythonw_safe_stdio()
    proc.force_utf8_stdio()

    cfg = config_mod.as_json()
    bootstrap.apply_mirrors(cfg)

    if note := config_mod.corrupt_note():
        print(f"  ⚠ {note}")

    # ---- 2. 自检 ----
    report = bootstrap.probe()
    if args.selfcheck:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print(f"  Python   : {report['python']}")
    print(f"  仓库根   : {report['root']}")
    for name, info in report["tools"].items():
        mark = "OK  " if info["ok"] else "缺失"
        print(f"  {name:<9}: {mark} {info['path'] or '（未找到，请放进仓库目录或加入 PATH）'}")

    if report["cuda"]["available"]:
        print(f"  GPU      : {report['cuda']['name']} · "
              f"{report['cuda']['memory'] // 1024} GB")
    else:
        print("  GPU      : 未检测到 NVIDIA 显卡（将使用 CPU，速度会慢很多）")

    if not report["ok"]:
        print()
        for problem in report["problems"]:
            print(f"  ✗ {problem}")

        missing_modules = report["modules"]["missing"]
        if missing_modules and not args.no_install:
            print()
            print("  正在尝试把缺失依赖安装到 后端/libs/（不污染系统环境）…")
            try:
                ok, message = bootstrap.install_requirements(
                    [bootstrap.REQUIRED_MODULES[m] for m in missing_modules])
            except Exception as exc:               # noqa: BLE001
                ok, message = False, f"依赖安装异常：{exc}"
            print(f"  {'✓' if ok else '✗'} {message}")
            report = bootstrap.probe()

        # ffmpeg / exiftool 体积大、不入库，缺了在启动时就补一次 —— 否则用户连
        # 页面都进不去（ffmpeg 缺失会直接拦下启动），也就点不到「一键补齐」。
        # 必须排在依赖安装之后：这一步要用 httpx。
        tools_wanted: list[str] = []
        if not (report["tools"]["ffmpeg"]["ok"] and report["tools"]["ffprobe"]["ok"]):
            tools_wanted.append("ffmpeg")
        if not report["tools"]["exiftool"]["ok"]:
            tools_wanted.append("exiftool")

        if tools_wanted and not args.no_install and not report["modules"]["missing"]:
            print()
            print(f"  缺外部工具：{'、'.join(tools_wanted)} —— 正在从网络补齐"
                  "（只下一次，之后就在仓库目录里了）…")
            try:
                ok, message = bootstrap.provision_tools(tools_wanted)
            except Exception as exc:               # noqa: BLE001
                ok, message = False, f"外部工具下载异常：{exc}"
            print(f"  {'✓' if ok else '✗'} {message}")
            report = bootstrap.probe()

    missing = report["modules"]["missing"]
    if missing:
        # FastAPI/Uvicorn 本身不存在就不能承诺启动完整后端；只开放本机诊断页，
        # 让用户看到缺什么并能重试，而不是窗口一闪而过或显示假工作台。
        host, port = _resolve_listen(args, cfg)
        if host not in ("127.0.0.1", "localhost"):
            print("  ⚠ 核心依赖缺失，诊断页只允许本机访问（忽略局域网监听配置）")
        print("  ⚠ 核心依赖缺失，进入诊断模式：工作台和视频处理暂不可用")
        from app.diagnostics import serve
        return serve(port)

    if not report["ok"]:
        print("  ⚠ ffmpeg/ffprobe 缺失：先打开界面，视频处理会提示缺少外部工具。")
        print(f"    也可手动将 ffmpeg.exe、ffprobe.exe 放到 {bootstrap.paths.FFMPEG_DIR}")

    # ---- 3. 监听参数 ----
    host, port = _resolve_listen(args, cfg)
    api_mod = None

    if args.no_autostart:
        os.environ["VAIR_NO_AUTOSTART"] = "1"

    # ---- 4. 此时才导入 uvicorn 与 api ----
    try:
        import uvicorn
        from app.web import api as api_mod
        from app.main import app as application
    except ImportError as exc:
        print(f"  ✗ 导入失败：{exc}")
        print("  请先安装依赖：python -m pip install -r 后端/requirements.txt")
        return 1

    api_mod.set_server_info(host, port)
    os.environ["VAIR_HOST"] = host
    os.environ["VAIR_PORT"] = str(port)

    _print_banner(host, port, _lan_urls(port) if host not in ("127.0.0.1", "localhost") else [])

    try:
        uvicorn.run(
            "app.main:app" if args.reload else application,
            host=host,
            port=port,
            reload=args.reload,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
