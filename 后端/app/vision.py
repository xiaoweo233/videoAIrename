"""视觉大模型调用：把关键帧 + 转写文本 + 元信息交给多模态模型，拿回结构化命名数据。

走 OpenAI 兼容的 `/chat/completions`（llama.cpp server、vLLM、Ollama、各家云服务都兼容）。
图片用 base64 data URL，多帧就是 content 数组里多个 image_url 对象。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config as config_mod
from .hub import hub

TRANSCRIPT_BUDGET = 1200          # 转写文本塞给模型的字符上限
RETRY_BACKOFF_CAP = 8

# 上下文溢出类的错误：重试没用，直接告诉用户调小帧数
_CONTEXT_HINTS = (
    "context_length_exceeded", "context length", "maximum context length",
    "max_length", "message too long", "too many tokens", "上下文",
)


@dataclass
class AIResult:
    title: str = ""
    plot: str = ""
    tags: list[str] = field(default_factory=list)
    raw: str = ""
    error: str = ""
    retries: int = 0
    elapsed_ms: int = 0
    model: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.title) and not self.error

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "plot": self.plot,
            "tags": self.tags,
            "error": self.error,
            "retries": self.retries,
            "elapsed_ms": self.elapsed_ms,
            "model": self.model,
        }


def is_context_error(message: str) -> bool:
    low = (message or "").lower()
    return any(hint in low for hint in _CONTEXT_HINTS)


class VisionClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ---------- 主入口 ----------

    def analyze(
        self,
        frames: list[bytes],
        *,
        transcript: str = "",
        original_name: str = "",
        duration: float = 0.0,
        stop: threading.Event | None = None,
    ) -> AIResult:
        cfg = config_mod.load()["ai"]
        started = time.perf_counter()
        model = str(cfg.get("model") or "")

        if not frames:
            return AIResult(model=model, error="没有可用关键帧，无法进行视觉分析")
        if cfg.get("provider", "openai") != "openai":
            # gemini 走不同的 SDK；本项目默认全本地，先只支持 OpenAI 兼容协议。
            return AIResult(model=model, error=f"暂不支持的 provider：{cfg.get('provider')}")

        messages = self._build_messages(cfg, frames, transcript, original_name, duration)
        raw, retries, error = self._chat(cfg, messages, stop)

        if error:
            return AIResult(model=model, raw=raw, error=error, retries=retries,
                            elapsed_ms=int((time.perf_counter() - started) * 1000))

        try:
            title, plot, tags = self._parse(raw)
        except ValueError as exc:
            return AIResult(model=model, raw=raw, error=str(exc), retries=retries,
                            elapsed_ms=int((time.perf_counter() - started) * 1000))

        return AIResult(
            title=title, plot=plot, tags=tags, raw=raw, retries=retries, model=model,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    # ---------- 请求构建 ----------

    def _build_messages(
        self, cfg: dict, frames: list[bytes], transcript: str,
        original_name: str, duration: float,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_prompt = str(cfg.get("system_prompt") or "").strip()
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        content: list[dict[str, Any]] = [
            {"type": "text", "text": str(cfg.get("prompt") or "")
             + self._glossary_suffix(cfg)
             + self._meta_suffix(transcript, original_name, duration)}
        ]
        for frame in frames:
            encoded = base64.b64encode(frame).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            })
        messages.append({"role": "user", "content": content})
        return messages

    @staticmethod
    def _glossary_suffix(cfg: dict) -> str:
        """把用户维护的术语表拼进提示词。

        行业黑话（「口播」「空镜」「B-roll」）模型未必按你的意思理解，明确喂给它，
        命名准确率会明显好于让它自由发挥。
        """
        rows = cfg.get("glossary") or []
        lines: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            term = str(row.get("term") or "").strip()
            meaning = str(row.get("meaning") or "").strip()
            if term and meaning:
                lines.append(f"{term} = {meaning}")
        if not lines:
            return ""
        return "\n\n术语参考（请按以下定义理解内容）：\n" + "\n".join(lines)

    @staticmethod
    def _meta_suffix(transcript: str, original_name: str, duration: float) -> str:
        bits: list[str] = []
        if original_name:
            bits.append(f"原始文件名：{original_name}")
        if duration:
            bits.append(f"时长：{int(duration)} 秒")
        if transcript:
            text = transcript.strip()
            if len(text) > TRANSCRIPT_BUDGET:
                text = text[:TRANSCRIPT_BUDGET] + "…"
            bits.append(f"音频转写内容：\n{text}")
        if not bits:
            return ""
        return "\n\n补充参考信息（可能不完整，请与画面互相印证）：\n" + "\n".join(bits)

    # ---------- 调用与重试 ----------

    def _chat(
        self, cfg: dict, messages: list[dict[str, Any]], stop: threading.Event | None
    ) -> tuple[str, int, str]:
        import httpx

        base = str(cfg.get("base_url") or "").rstrip("/")
        if not base:
            return "", 0, "未配置 AI 服务地址"

        url = base + "/chat/completions"
        payload: dict[str, Any] = {
            "model": str(cfg.get("model") or ""),
            "messages": messages,
            "max_tokens": int(cfg.get("max_tokens") or 2048),
            "temperature": float(cfg.get("temperature") or 0.6),
            "top_p": float(cfg.get("top_p") or 0.9),
        }
        if cfg.get("enforce_json_mode", True):
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        attempts = max(0, int(cfg.get("retry_times") or 0)) + 1
        timeout = float(cfg.get("timeout") or 60)
        last_error = ""
        raw = ""

        for attempt in range(attempts):
            if stop is not None and stop.is_set():
                return raw, attempt, "已取消"

            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            except httpx.TimeoutException:
                last_error = f"请求超时（{timeout:g}s）"
            except httpx.HTTPError as exc:
                last_error = f"连接失败：{exc}"
            else:
                if resp.status_code >= 500:
                    last_error = f"服务端错误 {resp.status_code}：{resp.text[:200]}"
                elif resp.status_code >= 400:
                    # 4xx 重试没意义，直接返回
                    return raw, attempt, f"AI 服务返回 {resp.status_code}：{resp.text[:300]}"
                else:
                    raw = self._extract_content(resp)
                    if raw:
                        return raw, attempt, ""
                    last_error = "模型返回了空内容"

            # 最后一条不用再等
            if attempt < attempts - 1:
                delay = min(2 * (attempt + 1), RETRY_BACKOFF_CAP)
                hub.debug(f"AI 调用失败（{last_error}），{delay}s 后重试 {attempt + 1}/{attempts - 1}")
                if stop is not None:
                    if stop.wait(delay):
                        return raw, attempt, "已取消"
                else:
                    time.sleep(delay)

        return raw, attempts - 1, last_error or "AI 调用失败"

    @staticmethod
    def _extract_content(resp: Any) -> str:
        try:
            body = resp.json()
        except ValueError:
            return ""
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # 少数服务返回 content 数组
            return "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            ).strip()
        return str(content or "").strip()

    # ---------- 解析 ----------

    @staticmethod
    def _parse(raw: str) -> tuple[str, str, list[str]]:
        if "{" not in raw or "}" not in raw:
            raise ValueError("模型返回中未检测到 JSON 结构")

        # 剥离 markdown 代码块与前后缀
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("模型返回中未检测到 JSON 结构")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败：{exc}") from exc

        title = str(data.get("title") or "").strip().strip("\"'").strip()
        plot = str(data.get("plot") or "").strip()

        tags: list[str] = []
        seen: set[str] = set()
        raw_tags = data.get("tags")
        if isinstance(raw_tags, list):
            for item in raw_tags:
                text = str(item or "").strip()
                key = text.lower()
                if text and key not in seen:
                    seen.add(key)
                    tags.append(text)

        if not title:
            raise ValueError("模型返回缺少 title 字段")

        return title, plot, tags[:20]

    # ---------- 连接测试 ----------

    def list_models(self, cfg: dict | None = None) -> dict[str, Any]:
        """探测 AI 服务可用模型。设置页「测试连接」用。"""
        import httpx

        cfg = cfg or config_mod.load()["ai"]
        base = str(cfg.get("base_url") or "").rstrip("/")
        if not base:
            return {"ok": False, "message": "未配置服务地址", "models": []}

        headers = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        try:
            resp = httpx.get(base + "/models", headers=headers, timeout=10)
        except Exception as exc:                    # noqa: BLE001
            return {"ok": False, "message": f"连接失败：{exc}", "models": []}

        if resp.status_code >= 400:
            return {"ok": False, "message": f"服务返回 {resp.status_code}", "models": []}

        try:
            body = resp.json()
        except ValueError:
            return {"ok": False, "message": "服务返回非 JSON", "models": []}

        models = [
            str(item.get("id") or "")
            for item in (body.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return {
            "ok": True,
            "message": f"服务可达，共 {len(models)} 个模型" if models else "服务可达",
            "models": models,
        }

    def test(self, cfg: dict | None = None) -> dict[str, Any]:
        """端到端连接测试：发一句话，看模型是否真的能回。"""
        import httpx

        cfg = cfg or config_mod.load()["ai"]
        base = str(cfg.get("base_url") or "").rstrip("/")
        if not base:
            return {"ok": False, "message": "未配置服务地址"}

        payload = {
            "model": str(cfg.get("model") or ""),
            "messages": [{"role": "user", "content": "只回复两个字：就绪"}],
            "max_tokens": 16,
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        try:
            resp = httpx.post(base + "/chat/completions", json=payload, headers=headers, timeout=30)
        except Exception as exc:                    # noqa: BLE001
            return {"ok": False, "message": f"请求失败：{exc}"}

        if resp.status_code >= 400:
            return {"ok": False, "message": f"服务返回 {resp.status_code}：{resp.text[:200]}"}
        content = self._extract_content(resp)
        if not content:
            return {"ok": False, "message": "服务可达但返回空内容"}
        return {"ok": True, "message": f"模型已响应：{content[:40]}"}


vision = VisionClient()
