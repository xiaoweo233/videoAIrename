"""用 Windows 内置语音合成造一段测试音频。

用途：设置页的「真实转写测试」需要一段**真的有人在说话**的音频才能证明
语音识别链路是通的。用系统 TTS 现场合成一小段，比让用户自己找素材方便得多。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

from . import paths
from .hub import hub
from .proc import CREATE_NO_WINDOW, _child_env, decode

DEFAULT_TEXT = "这是一段用于验证语音识别链路的测试音频，请确认能否正确转写出这句话。"

_SCRIPT = """
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 0
$synth.SetOutputToWaveFile('{out}')
$synth.Speak('{text}')
$synth.Dispose()
Write-Output 'ok'
"""


def available() -> bool:
    return paths.IS_WINDOWS


def synthesize(text: str = DEFAULT_TEXT, out_name: str = "asr_test.wav") -> tuple[Path | None, str]:
    """合成一段 wav。返回 (路径, 说明)。"""
    if not available():
        return None, "语音合成测试仅支持 Windows；请改用「测试连接」验证服务可达性"

    paths.ensure_dirs()
    out = paths.CACHE_DIR / out_name
    script = _SCRIPT.replace("{out}", _ps_escape(str(out))).replace("{text}", _ps_escape(text))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=120, env=_child_env(), creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"语音合成失败：{exc}"

    if proc.returncode != 0 or not out.is_file():
        return None, f"语音合成失败：{decode(proc.stderr)[-300:] or '未知错误'}"
    hub.debug(f"已合成测试音频 {out.name}（{out.stat().st_size} 字节）")
    return out, "已生成测试音频"


def _ps_escape(text: str) -> str:
    return str(text).replace("'", "''")
