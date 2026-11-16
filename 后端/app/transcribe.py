"""语音转写：内置 Qwen3-ASR 服务与远程接口（Gradio / OpenAI 协议）。

调度语义（配置 `whisper.backend`）：

- `local`：只用内置 Qwen3-ASR 服务（本项目自己拉起，127.0.0.1:7867）。
- `api`  ：只用远程接口，失败直接报错，**不回退**（用户明确要求走远程时别自作主张）。
- `auto` ：配了远程接口就优先远程；单条失败回退内置服务重试。远程连续失败 2 次后，
           本任务内固定走内置，避免每个文件都先卡一次超时。
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import proc
from .hub import hub

REMOTE_FAIL_CAP = 2
BUILTIN_TIMEOUT = 1800          # 内置服务按视频长度算，长视频要留足时间

GRADIO_LANG_MAP = {
    "auto": "自动识别",
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "ja": "Japanese",
    "ko": "Korean",
}

_SENTENCE_END = "。！？!?…；;"
_SPEAKER_TAG_RE = re.compile(r"^\s*\[[^\]]*SPEAKER_[^\]]*\]\s*")
_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


@dataclass
class TranscriptResult:
    text: str = ""
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = ""
    source: str = ""            # builtin | gradio | openai | skipped
    device: str = ""
    model: str = ""
    elapsed_ms: int = 0
    skipped: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "chars": len(self.text),
            "segments": len(self.segments),
            "language": self.language,
            "source": self.source,
            "device": self.device,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "skipped": self.skipped,
            "error": self.error,
        }

    def to_srt(self) -> str:
        """转 SRT。一条 segment 一行，保留说话人标签。"""
        lines: list[str] = []
        index = 0
        for seg in self.segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            index += 1
            lines.append(str(index))
            lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)


def _fmt_ts(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d},{ms:03d}"


def _display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _join_text(parts: list[str]) -> str:
    """CJK 之间不加空格，西文之间补一个。"""
    out = ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        if out and not _is_cjk(out[-1]) and not _is_cjk(part[0]):
            out += " "
        out += part
    return out


def _is_cjk(ch: str) -> bool:
    return ord(ch) > 0x2E80


def parse_srt(text: str) -> list[TranscriptSegment]:
    """解析 SRT。容忍 BOM、CRLF、序号错乱。"""
    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\r?\n\r?\n+", (text or "").lstrip("﻿"))
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        time_index = next((i for i, ln in enumerate(lines) if _SRT_TIME_RE.search(ln)), None)
        if time_index is None:
            continue
        match = _SRT_TIME_RE.search(lines[time_index])
        assert match is not None
        start = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
        end = int(match.group(5)) * 3600 + int(match.group(6)) * 60 + int(match.group(7)) + int(match.group(8)) / 1000
        body = " ".join(ln.strip() for ln in lines[time_index + 1:]).strip()
        if body:
            segments.append(TranscriptSegment(start, end, body))
    return segments


def _needs_reflow(segments: list[TranscriptSegment]) -> bool:
    """服务端没按字数断行时（行太长）需要客户端自己切。"""
    if not segments:
        return True
    widths = [_display_width(_SPEAKER_TAG_RE.sub("", s.text)) for s in segments if s.text.strip()]
    if not widths:
        return True
    return (sum(widths) / len(widths)) < 10


def _word_text(word: dict[str, Any]) -> str:
    """兼容两种词级格式：Gradio 结果包用 `word`，内置服务用 `text`。"""
    return str(word.get("word") or word.get("text") or "")


def group_words_into_segments(
    words: list[dict[str, Any]], max_chars: int = 20
) -> list[TranscriptSegment]:
    """按字数上限 / 句末标点 / 说话人切换 / 静音间隔切行。"""
    segments: list[TranscriptSegment] = []
    buf: list[dict[str, Any]] = []
    max_chars = max(5, int(max_chars or 20))

    def flush() -> None:
        if not buf:
            return
        text = _join_text([_word_text(w) for w in buf])
        speaker = buf[0].get("speaker")
        if text and speaker:
            text = f"[{speaker}] {text}"
        if text:
            segments.append(TranscriptSegment(
                float(buf[0].get("start") or 0.0),
                float(buf[-1].get("end") or buf[-1].get("start") or 0.0),
                text,
            ))
        buf.clear()

    for word in words:
        token = _word_text(word)
        if buf:
            prev = buf[-1]
            gap = float(word.get("start") or 0) - float(prev.get("end") or prev.get("start") or 0)
            speaker_changed = word.get("speaker") != prev.get("speaker")
            current_width = sum(_display_width(_word_text(w)) for w in buf)
            if gap > 0.8 or speaker_changed or current_width + _display_width(token) > max_chars:
                flush()
        buf.append(word)
        if token and token[-1] in _SENTENCE_END:
            flush()
    flush()
    return segments


def _normalize_speakers(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """`[角色 SPEAKER_00]` → `[SPEAKER_00]`。"""
    out: list[TranscriptSegment] = []
    for seg in segments:
        text = re.sub(r"\[\s*角色\s+([^\]]*SPEAKER_[^\]]*)\]", r"[\1]", seg.text or "")
        out.append(TranscriptSegment(seg.start, seg.end, text))
    return out


class Transcriber:
    """转写引擎。内置服务与远程接口共用同一套调度。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._remote_fails = 0

    # ---------- 任务级状态 ----------

    def reset_task_state(self) -> None:
        with self._lock:
            self._remote_fails = 0

    # ---------- 端点 ----------

    @staticmethod
    def builtin_url(cfg: dict | None = None) -> str:
        cfg = cfg or config_mod.load()
        asr = (cfg.get("services") or {}).get("asr") or {}
        host = str(asr.get("host") or "127.0.0.1")
        port = int(asr.get("port") or 7867)
        return f"http://{host}:{port}"

    @staticmethod
    def _params(cfg: dict) -> dict[str, Any]:
        """内置服务与远程接口共用的转写参数。"""
        return {
            "diarize": bool(cfg.get("diarize", False)),
            "max_chars": max(5, int(cfg.get("max_chars") or 20)),
            "punc": bool(cfg.get("punc", True)),
            "hotwords": str(cfg.get("hotwords") or ""),
        }

    # ---------- 对外入口 ----------

    def run(self, audio_path: str | os.PathLike[str], *, language: str | None = None) -> TranscriptResult:
        cfg = config_mod.load()["whisper"]
        if not cfg.get("enable", True):
            return TranscriptResult(skipped=True, source="skipped", text="", error="")

        backend = (cfg.get("backend") or "auto").lower()
        started = time.perf_counter()

        if backend == "local":
            result = self._run_builtin(audio_path, cfg, language)
        elif backend == "api":
            result = self._run_remote(audio_path, cfg, language)
        else:
            remote_ok = self._remote_configured(cfg)
            with self._lock:
                remote_ok = remote_ok and self._remote_fails < REMOTE_FAIL_CAP
            result = self._run_remote(audio_path, cfg, language) if remote_ok \
                else self._run_builtin(audio_path, cfg, language)
            if result.error and remote_ok:
                # 单条失败即回退内置服务重试
                with self._lock:
                    self._remote_fails += 1
                hub.debug(f"远程转写失败，回退内置服务：{result.error}")
                local = self._run_builtin(audio_path, cfg, language)
                if local.ok:
                    result = local

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _remote_configured(cfg: dict) -> bool:
        return bool(str((cfg.get("api") or {}).get("base_url") or "").strip())

    # ---------- 内置服务 ----------

    def _run_builtin(self, audio_path, cfg: dict, language: str | None) -> TranscriptResult:
        import httpx

        base = self.builtin_url()
        params = self._params(cfg)
        lang = _normalize_language(language if language is not None else cfg.get("language"))
        data = {
            "lang": lang or "auto",
            "diarize": "true" if params["diarize"] else "false",
            "max_chars": str(params["max_chars"]),
            "punc": "true" if params["punc"] else "false",
            "hotwords": params["hotwords"],
        }
        timeout = max(30, int(cfg.get("timeout") or BUILTIN_TIMEOUT))

        try:
            with open(proc.long_path(audio_path), "rb") as fh:
                resp = httpx.post(
                    base + "/transcribe",
                    data=data,
                    files={"file": (Path(audio_path).name, fh, "audio/wav")},
                    timeout=timeout,
                )
        except Exception as exc:                    # noqa: BLE001
            return TranscriptResult(source="builtin", device="内置 Qwen3-ASR",
                                    error=_explain_connect_error(str(exc), base))

        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                detail = str(resp.json().get("detail") or detail)
            except ValueError:
                pass
            return TranscriptResult(source="builtin", device="内置 Qwen3-ASR",
                                    error=f"内置转写服务报错：{detail}")

        try:
            body = resp.json()
        except ValueError:
            return TranscriptResult(source="builtin", device="内置 Qwen3-ASR",
                                    error="内置转写服务返回了非 JSON 内容")

        words = [w for w in (body.get("words") or []) if isinstance(w, dict)]
        segments = group_words_into_segments(words, params["max_chars"]) if words else []
        segments = _normalize_speakers(segments)

        text = str(body.get("text") or "").strip()
        if not text and not segments:
            empty_error = body.get("error") or "内置转写服务返回空结果（模型可能仍在加载）"
            return TranscriptResult(source="builtin", device="内置 Qwen3-ASR",
                                    error=str(empty_error))

        return TranscriptResult(
            text=text or _join_text([s.text for s in segments]),
            segments=segments,
            language=str(body.get("language") or lang or ""),
            source="builtin",
            device="内置 Qwen3-ASR",
            model=str(body.get("model") or "Qwen3-ASR"),
        )

    def probe_local(self) -> dict[str, Any]:
        """探测内置服务 /health。"""
        import httpx

        base = self.builtin_url()
        try:
            resp = httpx.get(base + "/health", timeout=5)
        except Exception as exc:                    # noqa: BLE001
            return {"ok": False, "message": f"内置服务未运行（{base}）：{exc}"}
        if resp.status_code >= 400:
            return {"ok": False, "message": f"内置服务返回 {resp.status_code}"}
        try:
            body = resp.json()
        except ValueError:
            return {"ok": True, "message": "内置服务可达"}
        if body.get("ready") is False:
            return {"ok": False, "message": "内置服务正在加载模型，请稍候"}
        return {"ok": True, "message": f"内置服务就绪 · {body.get('model') or 'Qwen3-ASR'}"}

    # ---------- 远程 ----------

    def _run_remote(self, audio_path, cfg: dict, language: str | None) -> TranscriptResult:
        api = cfg.get("api") or {}
        protocol = (api.get("protocol") or "gradio").lower()
        base = (api.get("base_url") or "").rstrip("/")
        if not base:
            return TranscriptResult(source=protocol, error="未配置远程转写接口地址")

        try:
            if protocol == "gradio":
                return self._transcribe_gradio(audio_path, cfg, base, language)
            return self._transcribe_openai(audio_path, cfg, base, language)
        except Exception as exc:                    # noqa: BLE001
            return TranscriptResult(source=protocol, model=str(api.get("model") or ""),
                                    error=_explain_connect_error(str(exc), base))

    def _transcribe_gradio(self, audio_path, cfg: dict, base: str,
                           language: str | None) -> TranscriptResult:
        from gradio_client import Client, handle_file

        api = cfg.get("api") or {}
        params = self._params(cfg)
        lang = _normalize_language(language if language is not None else cfg.get("language") or "auto")
        lang_arg = GRADIO_LANG_MAP.get(lang or "auto", "自动识别")

        client = Client(base, verbose=False)
        try:
            out = client.predict(
                handle_file(str(audio_path)),
                lang_arg,
                params["diarize"],
                params["max_chars"],
                params["punc"],
                params["hotwords"],
                api_name="/single_handler",
            )
        except Exception as exc:                    # noqa: BLE001
            raise RuntimeError(_explain_gradio_error(str(exc))) from exc

        parts = list(out) if isinstance(out, (list, tuple)) else [out]
        parts += [None] * (4 - len(parts))
        _status, full_text, srt_preview, zip_path = parts[0], parts[1], parts[2], parts[3]

        words: list[dict[str, Any]] = []
        srt_text = ""
        archive = self._read_gradio_zip(zip_path, base)
        if archive is not None:
            srt_text = _pick_srt(archive)
            words = _read_words_json(archive)

        segments: list[TranscriptSegment] = []
        if srt_text:
            segments = parse_srt(srt_text)
        if (not segments or _needs_reflow(segments)) and words:
            segments = group_words_into_segments(words, params["max_chars"])
        segments = _normalize_speakers(segments)

        text = str(full_text or "").strip() or _join_text([s.text for s in segments]) \
            or (srt_preview or "").strip()
        if not text and not segments:
            raise RuntimeError("远程转写返回空结果（服务端可能仍在加载模型）")

        return TranscriptResult(
            text=text,
            segments=segments,
            language=lang or "",
            source="gradio",
            model=str(api.get("model") or "Qwen3-ASR"),
        )

    @staticmethod
    def _read_gradio_zip(zip_path: Any, base: str) -> zipfile.ZipFile | None:
        """Gradio 返回的结果包可能是本机路径，也可能是服务端路径需要走 HTTP 取。"""
        if not zip_path:
            return None
        candidate = str(zip_path)
        raw: bytes | None = None
        if os.path.isfile(candidate):
            try:
                return zipfile.ZipFile(candidate)
            except (OSError, zipfile.BadZipFile):
                return None
        try:
            import httpx
            resp = httpx.get(f"{base}/gradio_api/file={candidate}", timeout=120)
            if resp.status_code == 200:
                raw = resp.content
        except Exception:                           # noqa: BLE001
            return None
        if not raw:
            return None
        try:
            return zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            return None

    def _transcribe_openai(self, audio_path, cfg: dict, base: str,
                           language: str | None) -> TranscriptResult:
        import httpx

        api = cfg.get("api") or {}
        lang = _normalize_language(language if language is not None else cfg.get("language"))
        data: dict[str, str] = {
            "model": str(api.get("model") or "Qwen3-ASR"),
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if lang:
            data["language"] = lang
        if api.get("prompt"):
            data["prompt"] = str(api["prompt"])

        headers = {}
        if api.get("api_key"):
            headers["Authorization"] = f"Bearer {api['api_key']}"

        with open(proc.long_path(audio_path), "rb") as fh:
            payload = httpx.post(
                f"{base}/audio/transcriptions",
                data=data,
                files={"file": (Path(audio_path).name, fh, "audio/wav")},
                headers=headers,
                timeout=max(10, int(cfg.get("timeout") or 600)),
            )
        if payload.status_code >= 400:
            raise RuntimeError(f"转写 API 返回 {payload.status_code}：{payload.text[:300]}")

        try:
            body = payload.json()
        except ValueError as exc:
            raise RuntimeError(f"转写 API 返回非 JSON：{payload.text[:200]}") from exc

        segments: list[TranscriptSegment] = []
        for item in body.get("segments") or []:
            text = str(item.get("text") or "").strip()
            if text:
                segments.append(TranscriptSegment(
                    float(item.get("start") or 0.0), float(item.get("end") or 0.0), text))

        text = str(body.get("text") or "").strip()
        if not segments and text:
            segments = [TranscriptSegment(0.0, 0.0, text)]
        if not text and not segments:
            raise RuntimeError("转写 API 返回空文本")

        return TranscriptResult(
            text=text or _join_text([s.text for s in segments]),
            segments=_normalize_speakers(segments),
            language=lang or str(body.get("language") or ""),
            source="openai",
            model=str(api.get("model") or "Qwen3-ASR"),
        )

    # ---------- 连通性探测 ----------

    def probe_remote(self, api: dict | None = None) -> dict[str, Any]:
        """探测远程转写服务是否可达。设置页「测试连接」用。"""
        api = api or (config_mod.load()["whisper"].get("api") or {})
        base = (api.get("base_url") or "").rstrip("/")
        protocol = (api.get("protocol") or "gradio").lower()
        if not base:
            return {"ok": True, "configured": False,
                    "message": "未配置远程接口（自动模式只走内置服务）"}

        try:
            import httpx
            if protocol == "gradio":
                resp = httpx.get(f"{base}/config", timeout=10)
                if resp.status_code != 200:
                    return {"ok": False, "configured": True,
                            "message": f"服务返回 {resp.status_code}"}
                names = [
                    str(dep.get("api_name") or "")
                    for dep in (resp.json().get("dependencies") or [])
                ]
                if not any("single_handler" in n for n in names):
                    return {"ok": False, "configured": True,
                            "message": "服务可达，但没找到 single_handler 接口（可能不是 Qwen3-ASR 服务）"}
                return {"ok": True, "configured": True,
                        "message": "Gradio 服务可达，single_handler 接口已就绪"}

            resp = httpx.get(f"{base}/models", timeout=10)
            # 404 也算可达：有些服务不实现 /models
            if resp.status_code in (200, 404):
                return {"ok": True, "configured": True,
                        "message": f"OpenAI 兼容服务可达（HTTP {resp.status_code}）"}
            return {"ok": False, "configured": True, "message": f"服务返回 {resp.status_code}"}
        except Exception as exc:                    # noqa: BLE001
            return {"ok": False, "configured": True, "message": f"连接失败：{exc}"}


def _pick_srt(archive: zipfile.ZipFile) -> str:
    names = [n for n in archive.namelist() if n.lower().endswith(".srt")]
    if not names:
        return ""
    preferred = next((n for n in names if "全员" in n), names[0])
    try:
        return archive.read(preferred).decode("utf-8", errors="replace")
    except (KeyError, OSError):
        return ""


def _read_words_json(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    """读服务端产出的词级时间轴，用于客户端重排。"""
    candidates = [n for n in archive.namelist() if n.lower().endswith(".json")]
    for name in candidates:
        try:
            data = json.loads(archive.read(name).decode("utf-8", errors="replace"))
        except (KeyError, OSError, json.JSONDecodeError):
            continue
        words = data.get("words") if isinstance(data, dict) else None
        if isinstance(words, list) and words:
            return [w for w in words if isinstance(w, dict)]
    return []


def _normalize_language(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in ("", "auto", "none", "自动", "自动识别"):
        return None
    return text


def _explain_gradio_error(message: str) -> str:
    """把 Gradio 的通用报错翻译成人能看懂的原因。

    Gradio 默认不把服务端异常透出来，只给一句「raised an exception but has not
    enabled verbose error reporting」，用户完全不知道是显存不够还是模型没加载。
    这里针对本项目最常见的原因给出可操作的提示。
    """
    text = message or ""
    low = text.lower()

    if "out of memory" in low or "outofmemory" in low:
        return ("显存不足：语音转写服务与视觉模型同时占卡。"
                "请先把「同时处理的视频数」设为 1、「每个视频最多抽几帧」降到 3–4，"
                "或在跑转写前先停止视觉服务（llama.cpp）")
    if "no such file" in low or "not found" in low:
        return f"服务端找不到文件：{text[:200]}"
    if "timed out" in low or "timeout" in low:
        return "转写请求超时：服务可能还在加载模型，稍后重试或调大「请求超时」"

    hint = ""
    if "has not enabled verbose error reporting" in low:
        hint = "（服务端未开启详细错误，具体原因需看转写服务的启动日志）"
    return f"远程转写服务报错{hint}：{text[:300]}"


def _explain_connect_error(message: str, base: str) -> str:
    """连接层的报错（服务没开、端口填错）跟转写本身无关，单独说清楚。"""
    low = (message or "").lower()
    if any(token in low for token in ("10061", "connection refused", "actively refused",
                                      "connect call failed", "connection refused")):
        return (f"转写服务没在运行（{base} 拒绝连接）。"
                "如果刚跑完一批任务，服务可能已按「处理完成后关闭 AI 服务」释放；"
                "开始任务时后端会自动重新拉起，也可以在设置 → AI 服务里手动启动。")
    if any(token in low for token in ("10060", "timed out", "timeout")):
        return f"连接转写服务超时（{base}）：服务可能还在加载模型，稍后重试"
    return _explain_gradio_error(message)


transcriber = Transcriber()
