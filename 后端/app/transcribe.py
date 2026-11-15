"""语音转写：本地 faster-whisper 与远程 API（Gradio / OpenAI 协议）。

调度语义（与配置 `whisper.backend` 对应）：

- `local`：只用本地。
- `api`  ：只用远程，失败直接报错，**不回退**（用户明确要求走远程时别自作主张）。
- `auto` ：优先远程；单条失败即回退本地重试。远程连续失败 2 次后，本任务内固定走本地，
           避免每个文件都先卡一次超时。
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
LOCAL_WATCHDOG_SECONDS = 300

GRADIO_LANG_MAP = {
    "auto": "自动识别",
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
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
    source: str = ""            # local | gradio | openai | skipped
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
        """转 SRT。本地路径一条 segment 一行，不做字数切分。"""
        lines: list[str] = []
        index = 0
        for seg in self.segments:
            text = _SPEAKER_TAG_RE.sub("", seg.text or "").strip()
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
        text = _join_text([str(w.get("word", "")) for w in buf])
        if text:
            segments.append(TranscriptSegment(
                float(buf[0].get("start") or 0.0),
                float(buf[-1].get("end") or buf[-1].get("start") or 0.0),
                text,
            ))
        buf.clear()

    for word in words:
        token = str(word.get("word", ""))
        if buf:
            prev = buf[-1]
            gap = float(word.get("start") or 0) - float(prev.get("end") or prev.get("start") or 0)
            speaker_changed = word.get("speaker") != prev.get("speaker")
            current_width = sum(_display_width(str(w.get("word", ""))) for w in buf)
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
    """转写引擎。懒加载本地模型，并记录远程可用性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local_model: Any = None
        self._local_key: tuple | None = None
        self._local_failed = False
        self._remote_fails = 0
        self._infer_lock = threading.Lock()   # 本地推理串行：GPU 独占

    # ---------- 任务级状态 ----------

    def reset_task_state(self) -> None:
        with self._lock:
            self._remote_fails = 0

    # ---------- 对外入口 ----------

    def run(self, audio_path: str | os.PathLike[str], *, language: str | None = None) -> TranscriptResult:
        cfg = config_mod.load()["whisper"]
        if not cfg.get("enable", True):
            return TranscriptResult(skipped=True, source="skipped", text="", error="")

        backend = (cfg.get("backend") or "auto").lower()
        started = time.perf_counter()

        if backend == "local":
            result = self._run_local(audio_path, cfg, language)
        elif backend == "api":
            result = self._run_remote(audio_path, cfg, language)
        else:
            prefer_api = bool(cfg.get("prefer_api", True))
            remote_ok = prefer_api and self._remote_fails < REMOTE_FAIL_CAP
            result = self._run_remote(audio_path, cfg, language) if remote_ok \
                else self._run_local(audio_path, cfg, language)
            if result.error and remote_ok:
                # 单条失败即回退本地重试
                with self._lock:
                    self._remote_fails += 1
                hub.debug(f"远程转写失败，回退本地：{result.error}")
                local = self._run_local(audio_path, cfg, language)
                if local.ok:
                    result = local

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ---------- 本地 ----------

    def _run_local(self, audio_path, cfg: dict, language: str | None) -> TranscriptResult:
        if self._local_failed:
            return TranscriptResult(source="local", error="本地转写模型此前加载失败")

        try:
            model = self._ensure_local_model(cfg)
        except Exception as exc:                    # noqa: BLE001 - 要把原因带回给用户
            self._local_failed = True
            return TranscriptResult(source="local", error=f"加载本地模型失败：{exc}")

        device = self.device_label(cfg)
        lang = _normalize_language(language if language is not None else cfg.get("language"))

        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                with self._infer_lock:
                    segments_iter, info = model.transcribe(
                        str(audio_path),
                        beam_size=5,
                        vad_filter=bool(cfg.get("vad_filter", True)),
                        language=lang,
                        word_timestamps=False,
                    )
                    segments: list[TranscriptSegment] = []
                    parts: list[str] = []
                    for seg in segments_iter:
                        text = (getattr(seg, "text", "") or "").strip()
                        if not text:
                            continue
                        segments.append(TranscriptSegment(
                            float(getattr(seg, "start", 0.0)),
                            float(getattr(seg, "end", 0.0)),
                            text,
                        ))
                        parts.append(text)
                    outcome["segments"] = segments
                    outcome["text"] = _join_text(parts)
                    outcome["language"] = getattr(info, "language", "") or ""
            except Exception as exc:                # noqa: BLE001
                outcome["error"] = str(exc)

        thread = threading.Thread(target=worker, daemon=True, name="vair-whisper")
        thread.start()
        thread.join(timeout=LOCAL_WATCHDOG_SECONDS)

        if thread.is_alive():
            # CTranslate2 缺 CUDA 库时会挂起而不是报错，只能靠看门狗兜住。
            return TranscriptResult(
                source="local", device=device, model=str(cfg.get("model")),
                error=f"本地转写超时（>{LOCAL_WATCHDOG_SECONDS}s），可能是 CUDA 库缺失导致挂起",
            )
        if outcome.get("error"):
            return TranscriptResult(source="local", device=device, error=outcome["error"])

        segments = outcome.get("segments") or []
        return TranscriptResult(
            text=outcome.get("text") or "",
            segments=_normalize_speakers(segments),
            language=outcome.get("language") or "",
            source="local",
            device=device,
            model=str(cfg.get("model")),
        )

    def _ensure_local_model(self, cfg: dict) -> Any:
        key = (str(cfg.get("model")), str(cfg.get("device")), str(cfg.get("compute_type")),
               bool(cfg.get("use_gpu", True)))
        with self._lock:
            if self._local_model is not None and self._local_key == key:
                return self._local_model

        from faster_whisper import WhisperModel  # 延迟导入：没装也不影响启动

        device, compute_type = self._resolve_device(cfg)
        source = self._resolve_model_source(str(cfg.get("model") or "large-v3-turbo"),
                                            bool(cfg.get("allow_download", False)))

        hub.info(f"加载本地 Whisper 模型 · {source} · {device}/{compute_type}")
        try:
            model = WhisperModel(source, device=device, compute_type=compute_type)
        except Exception as exc:                    # noqa: BLE001
            # 只有设备相关错误才值得用 CPU 重试；模型来源错误重试也没用。
            message = str(exc).lower()
            device_problem = any(k in message for k in ("cuda", "cublas", "cudnn", "device", "gpu"))
            if device != "cpu" and device_problem:
                hub.warn(f"GPU 加载失败，回退 CPU/int8：{exc}")
                model = WhisperModel(source, device="cpu", compute_type="int8")
                device, compute_type = "cpu", "int8"
            else:
                raise

        with self._lock:
            self._local_model = model
            self._local_key = key
            self._local_device = f"{device}/{compute_type}"
        hub.info(f"本地 Whisper 就绪 · {device}/{compute_type}")
        return model

    def _resolve_device(self, cfg: dict) -> tuple[str, str]:
        compute_type = str(cfg.get("compute_type") or "int8_float16")
        if not cfg.get("use_gpu", True) or str(cfg.get("device") or "auto") == "cpu":
            return "cpu", "int8"

        wanted = str(cfg.get("device") or "auto")
        try:
            import ctranslate2
            has_gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:                           # noqa: BLE001
            has_gpu = False

        if not has_gpu or not _cuda_libs_present():
            return "cpu", "int8"
        if wanted not in ("auto", "cuda"):
            return "cpu", "int8"
        return "cuda", ("int8_float16" if compute_type == "int8" else compute_type)

    @staticmethod
    def _resolve_model_source(name: str, allow_download: bool) -> str:
        """本地目录优先（传目录会跳过一切网络请求）。"""
        local = paths_whisper_models() / name
        if local.is_dir() and any(local.iterdir()):
            return str(local)
        if allow_download:
            return name
        # 不允许下载又找不到本地目录：给出明确错误，而不是静默联网拉 1.6GB
        raise FileNotFoundError(
            f"未找到本地模型 {name}（{local}）。可开启「本地模型缺失时自动联网下载」或先下载模型。"
        )

    def device_label(self, cfg: dict | None = None) -> str:
        cached = getattr(self, "_local_device", None)
        if cached:
            return str(cached)
        cfg = cfg or config_mod.load()["whisper"]
        device, compute_type = self._resolve_device(cfg)
        return f"{device}/{compute_type}"

    # ---------- 远程 ----------

    def _run_remote(self, audio_path, cfg: dict, language: str | None) -> TranscriptResult:
        api = cfg.get("api") or {}
        protocol = (api.get("protocol") or "gradio").lower()
        base = (api.get("base_url") or "").rstrip("/")
        if not base:
            return TranscriptResult(source=protocol, error="未配置转写 API 地址")

        try:
            if protocol == "gradio":
                return self._transcribe_gradio(audio_path, api, base, cfg, language)
            return self._transcribe_openai(audio_path, api, base, cfg, language)
        except Exception as exc:                    # noqa: BLE001
            return TranscriptResult(source=protocol, model=str(api.get("model") or ""), error=str(exc))

    def _transcribe_gradio(self, audio_path, api: dict, base: str, cfg: dict,
                           language: str | None) -> TranscriptResult:
        from gradio_client import Client, handle_file

        lang = _normalize_language(language if language is not None else cfg.get("language") or "auto")
        lang_arg = GRADIO_LANG_MAP.get(lang or "auto", "自动识别")

        client = Client(base, verbose=False)
        out = client.predict(
            handle_file(str(audio_path)),
            lang_arg,
            bool(api.get("diarize", False)),
            max(5, int(api.get("max_chars") or 20)),
            bool(api.get("punc", True)),
            str(api.get("hotwords") or ""),
            api_name="/single_handler",
        )

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
            segments = group_words_into_segments(words, int(api.get("max_chars") or 20))
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

    def _transcribe_openai(self, audio_path, api: dict, base: str, cfg: dict,
                           language: str | None) -> TranscriptResult:
        import httpx

        lang = _normalize_language(language if language is not None else cfg.get("language"))
        data: dict[str, str] = {
            "model": str(api.get("model") or "whisper-1"),
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
                timeout=max(10, int(api.get("timeout") or 600)),
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
            model=str(api.get("model") or "whisper-1"),
        )

    # ---------- 连通性探测 ----------

    def probe_remote(self, api: dict | None = None) -> dict[str, Any]:
        """探测远程转写服务是否可达。设置页「测试连接」用。"""
        api = api or (config_mod.load()["whisper"].get("api") or {})
        base = (api.get("base_url") or "").rstrip("/")
        protocol = (api.get("protocol") or "gradio").lower()
        if not base:
            return {"ok": False, "message": "未配置服务地址"}

        try:
            import httpx
            if protocol == "gradio":
                resp = httpx.get(f"{base}/config", timeout=10)
                if resp.status_code != 200:
                    return {"ok": False, "message": f"服务返回 {resp.status_code}"}
                names = [
                    str(dep.get("api_name") or "")
                    for dep in (resp.json().get("dependencies") or [])
                ]
                if not any("single_handler" in n for n in names):
                    return {"ok": False,
                            "message": "服务可达，但没找到 single_handler 接口（可能不是 Qwen3-ASR 服务）"}
                return {"ok": True, "message": "Gradio 服务可达，single_handler 接口已就绪"}

            resp = httpx.get(f"{base}/models", timeout=10)
            # 404 也算可达：有些服务不实现 /models
            if resp.status_code in (200, 404):
                return {"ok": True, "message": f"OpenAI 兼容服务可达（HTTP {resp.status_code}）"}
            return {"ok": False, "message": f"服务返回 {resp.status_code}"}
        except Exception as exc:                    # noqa: BLE001
            return {"ok": False, "message": f"连接失败：{exc}"}


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


def _cuda_libs_present() -> bool:
    """faster-whisper 缺 cublas 时不报错而是挂起，所以先探一下。"""
    import ctypes
    import sys

    if sys.platform != "win32":
        return True
    for dll in ("cublas64_12.dll", "cublas64_13.dll"):
        try:
            ctypes.WinDLL(dll)
            return True
        except OSError:
            continue
    return False


def paths_whisper_models() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "AI" / "models" / "whisper"


transcriber = Transcriber()
