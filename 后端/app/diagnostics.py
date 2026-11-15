"""核心依赖缺失时的本机诊断服务：仅使用 Python 标准库。

不能在这里引入 FastAPI/httpx —— 它们缺失正是这个模块会启动的原因。
本模式绝不挂真实工作台，也不读取/修改配置，避免被误认为应用已经正常可用。
"""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bootstrap

_retry_lock = threading.Lock()
_retry_running = False
_retry_message = ""


def _page() -> str:
    report = bootstrap.probe()
    missing = report["modules"]["missing"]
    problems = "".join(f"<li>{html.escape(str(p))}</li>" for p in report["problems"])
    with _retry_lock:
        status = _retry_message
        running = _retry_running
    note = "正在安装，完成后刷新此页。" if running else status
    if not missing:
        note = "核心依赖已齐备！请关闭此窗口并重新双击启动.bat，进入完整工作台。"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>VAIR 环境诊断</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:760px;margin:8vh auto;padding:0 24px;
background:#111827;color:#f3f4f6}}main{{background:#1f2937;border:1px solid #374151;border-radius:18px;
padding:32px}}h1{{font-size:26px}}p{{color:#d1d5db}}li{{padding:3px}}button{{background:#60a5fa;
border:0;border-radius:8px;padding:10px 18px;cursor:pointer;font-size:16px;color:#111827}}
small{{display:block;margin-top:16px;color:#9ca3af}}.warn{{color:#fbbf24}}</style></head>
<body><main><h1>VAIR · 环境诊断模式</h1><p class="warn">完整工作台未启动，暂时不能处理视频。</p>
<p>缺失：{html.escape('、'.join(missing) or '无')}。诊断服务只监听本机。</p>
<ul>{problems}</ul><p>{html.escape(note)}</p>
<form method="post" action="/api/retry"><button type="submit">重试安装核心依赖</button></form>
<small>如果网络无法下载，可将已安装依赖的 后端/libs/ 从另一台兼容的 Python 机器复制过来；
请确保 Python 主次版本和系统架构一致。然后重新启动。</small></main></body></html>"""


def _retry() -> None:
    global _retry_running, _retry_message
    try:
        missing = [bootstrap.REQUIRED_MODULES[m]
                   for m in bootstrap.probe()["modules"]["missing"]]
        ok, message = bootstrap.install_requirements(missing)
        _retry_message = message if ok else f"安装仍未成功：{message}"
    except Exception as exc:  # noqa: BLE001
        _retry_message = f"安装失败：{exc}"
    finally:
        with _retry_lock:
            _retry_running = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            payload = _page().encode("utf-8")
            self._send(200, "text/html; charset=utf-8", payload)
        elif self.path in ("/api/health", "/api/diagnostics"):
            report = bootstrap.probe()
            payload = json.dumps({"mode": "diagnostics", "full_app": False,
                                  "missing": report["modules"]["missing"],
                                  "problems": report["problems"]}, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload)
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:  # noqa: N802
        global _retry_running, _retry_message
        if self.path != "/api/retry":
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        # HTTP request body 无需传参，限制长度并丢弃；固定行为，不能传命令或包名。
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1024:
            self._send(413, "text/plain; charset=utf-8", b"Too large")
            return
        if length:
            self.rfile.read(length)
        with _retry_lock:
            if not _retry_running:
                _retry_running = True
                _retry_message = ""
                threading.Thread(target=_retry, daemon=True, name="vair-diagnostic-retry").start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(port: int) -> int:
    """仅绑定环回地址；端口失败时返回非零，绝不回退到外网监听。"""
    try:
        with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
            server.daemon_threads = True
            print(f"  环境诊断页：http://127.0.0.1:{server.server_port}/")
            print("  修复成功后请按 Ctrl+C 并重新启动，才会加载完整工作台。")
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"  ✗ 诊断服务无法监听端口 {port}：{exc}")
        return 1
    return 0
