"""子进程封装。

Windows 上跑 ffmpeg / exiftool 有三个必须处理的坑，收口在这里：
1. 不加 `CREATE_NO_WINDOW` 会弹黑框；
2. 不设 `PYTHONIOENCODING` / 不按 UTF-8 解码，中文路径与日志会乱码；
3. perl 会因 locale 缺失刷警告，`LC_ALL=C` 消掉。

所有外部命令都必须走 `run()`，不要在别处直接 `subprocess.run`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

_SUBPROC_FLAGS: dict[str, int] = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 消除 perl / gettext 的 locale 警告（否则 stderr 里全是噪声）
    env.pop("LC_ALL", None)
    env.pop("LANG", None)
    env["LC_ALL"] = "C"
    if extra:
        env.update(extra)
    return env


class ToolError(RuntimeError):
    """外部命令失败。带 stderr 尾部，便于定位。"""

    def __init__(self, message: str, *, returncode: int | None = None,
                 stderr: str = "", args: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
        self.args_used = list(args or [])


def decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def run(
    args: Sequence[str],
    *,
    timeout: float = 60,
    input_bytes: bytes | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env_extra: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """同步执行外部命令。

    `check=False` 时返回原始结果（调用方自己判断 returncode）；
    `check=True` 时非 0 退出抛 `ToolError`。
    """
    try:
        proc = subprocess.run(
            list(args),
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=_child_env(env_extra),
            **_SUBPROC_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(
            f"命令超时（{timeout:g}s）：{_cmdline(args)}",
            stderr=decode(exc.stderr),
            args=args,
        ) from exc
    except OSError as exc:
        raise ToolError(f"命令无法执行：{exc}（{_cmdline(args)}）", args=args) from exc

    if check and proc.returncode != 0:
        raise ToolError(
            f"命令返回 {proc.returncode}：{_cmdline(args)}",
            returncode=proc.returncode,
            stderr=decode(proc.stderr)[-2000:],
            args=args,
        )
    return proc


def _cmdline(args: Sequence[str]) -> str:
    text = " ".join(str(a) for a in args)
    return text if len(text) <= 300 else text[:300] + " …"


def which(name: str) -> str | None:
    return shutil.which(name)


def long_path(path: str | os.PathLike[str]) -> str:
    """Windows 长路径前缀，绕过 260 字符限制。

    非 Windows 或已经是 `\\\\?\\` 前缀时原样返回。
    """
    text = str(Path(path).absolute())
    if not IS_WINDOWS or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):     # UNC
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def pythonw_safe_stdio() -> None:
    """pythonw 下 `sys.stdout` 可能是 None，写日志会炸。补个黑洞。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文日志会乱码甚至抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
