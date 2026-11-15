"""原生文件 / 文件夹选择器。

浏览器出于沙箱限制拿不到「文件夹的真实路径」，所以桌面场景必须走原生对话框。
实现用 PowerShell + WinForms：不依赖 tkinter（便携 Python 常常没编进去），
且脚本用 `-EncodedCommand` 传，彻底绕开中文与引号转义问题。

对话框会阻塞到用户点选为止，所以 HTTP 端点声明为同步 `def`，交给 FastAPI 的线程池。
"""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from pathlib import Path

from . import media, paths
from .hub import hub
from .proc import CREATE_NO_WINDOW, _child_env, decode

# 同时只允许一个对话框，避免用户狂点导致弹一堆
_ACTIVE: dict = {}
_FLAG_LOCK = threading.Lock()
_STALE_GRACE = 20.0


class DialogBusyError(RuntimeError):
    pass


def is_supported() -> bool:
    return paths.IS_WINDOWS


_VIDEO_FILTER = "|".join(f"*{ext}" for ext in sorted(media.VIDEO_EXTS))

_PS_COMMON = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Size = New-Object System.Drawing.Size(1,1)
$owner.StartPosition = 'CenterScreen'
$owner.Show()
"""


def _run_powershell(script: str, timeout: float) -> dict:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"ok": False, "cancelled": False, "paths": [], "error": "对话框等待超时"}
    text = decode(out).strip()
    if not text:
        return {"ok": False, "cancelled": False, "paths": [],
                "error": decode(err).strip()[-300:] or "没有拿到选择结果"}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": True, "cancelled": True, "paths": [],
                "error": ""}


def _claim() -> None:
    with _FLAG_LOCK:
        deadline = _ACTIVE.get("deadline", 0)
        if _ACTIVE and time.time() < deadline + _STALE_GRACE:
            raise DialogBusyError("已经有一个选择窗口开着，请先完成或取消它")
        _ACTIVE.clear()


def _release() -> None:
    with _FLAG_LOCK:
        _ACTIVE.clear()


def pick_files(initial_dir: str = "", timeout: float = 900) -> dict:
    if not is_supported():
        return {"ok": False, "cancelled": False, "paths": [],
                "error": "原生文件选择器仅在 Windows 上可用，请手动输入路径"}

    _claim()
    with _FLAG_LOCK:
        _ACTIVE["deadline"] = time.time() + timeout
    try:
        start = initial_dir if initial_dir and Path(initial_dir).is_dir() else str(paths.ROOT)
        script = _PS_COMMON + f"""
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = '选择视频文件'
$dlg.InitialDirectory = '{_ps_escape(start)}'
$dlg.Filter = '视频文件 ({_VIDEO_FILTER})|{_VIDEO_FILTER}|所有文件 (*.*)|*.*'
$dlg.Multiselect = $true
$result = $dlg.ShowDialog($owner)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
  $payload = @{{ ok = $true; cancelled = $false; paths = @($dlg.FileNames); error = '' }}
}} else {{
  $payload = @{{ ok = $true; cancelled = $true; paths = @(); error = '' }}
}}
$payload | ConvertTo-Json -Compress
"""
        result = _run_powershell(script, timeout)
        if result.get("paths"):
            hub.debug(f"用户选择了 {len(result['paths'])} 个文件")
        return result
    finally:
        _release()


def pick_folder(initial_dir: str = "", timeout: float = 900) -> dict:
    if not is_supported():
        return {"ok": False, "cancelled": False, "paths": [],
                "error": "原生文件夹选择器仅在 Windows 上可用，请手动输入路径"}

    _claim()
    with _FLAG_LOCK:
        _ACTIVE["deadline"] = time.time() + timeout
    try:
        start = initial_dir if initial_dir and Path(initial_dir).is_dir() else str(paths.ROOT)
        script = _PS_COMMON + f"""
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = '选择素材文件夹'
$dlg.SelectedPath = '{_ps_escape(start)}'
$dlg.ShowNewFolderButton = $true
$result = $dlg.ShowDialog($owner)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
  $payload = @{{ ok = $true; cancelled = $false; paths = @($dlg.SelectedPath); error = '' }}
}} else {{
  $payload = @{{ ok = $true; cancelled = $true; paths = @(); error = '' }}
}}
$payload | ConvertTo-Json -Compress
"""
        return _run_powershell(script, timeout)
    finally:
        _release()


def cancel() -> dict:
    """强制回收：把占位清掉，让下一次调用能弹出来。"""
    with _FLAG_LOCK:
        had = bool(_ACTIVE)
        _ACTIVE.clear()
    if had:
        hub.warn("已取消挂起的文件选择窗口")
    return {"ok": True, "message": "已取消" if had else "当前没有挂起的对话框"}


def status() -> dict:
    with _FLAG_LOCK:
        active = bool(_ACTIVE) and time.time() < _ACTIVE.get("deadline", 0) + _STALE_GRACE
    return {"supported": is_supported(), "active": active}


def _ps_escape(text: str) -> str:
    """PowerShell 单引号字符串里，单引号要双写。"""
    return str(text).replace("'", "''")
