"""视觉大模型调用：把关键帧 + 转写文本 + 元信息交给多模态模型，拿回结构化命名数据。

本地走内置 llama.cpp 的 OpenAI 兼容接口，远程走任何 OpenAI 兼容服务。

调度语义（配置 `ai.backend`）：

- `local`：只用内置服务。
- `api`  ：只用远程接口，失败直接报错，**不回退**（用户明确选远程时别自作主张）。
- `auto` ：配了远程接口就优先远程；单条失败回退本地重试。远程连续失败 2 次后，
           本任务内固定走本地，避免每个文件都先卡一次超时。
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
from . import paths
from .hub import hub

TRANSCRIPT_BUDGET = 1200          # 转写文本塞给模型的字符上限
RETRY_BACKOFF_CAP = 8
REMOTE_FAIL_CAP = 2               # 远程连续失败这么多次后，本任务固定走本地

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
    source: str = ""             # local | remote

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
            "source": self.source,
        }


def is_context_error(message: str) -> bool:
    low = (message or "").lower()
    return any(hint in low for hint in _CONTEXT_HINTS)


class VisionClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._remote_fails = 0

    # ---------- 任务级状态 ----------

    def reset_task_state(self) -> None:
        with self._lock:
            self._remote_fails = 0

    # ---------- 端点解析 ----------

    @staticmethod
    def local_base_url(cfg: dict | None = None) -> str:
        """内置 llama.cpp 的地址由服务端口推出，不单独配置，避免两处不同步。"""
        cfg = cfg or config_mod.load()
        llama = (cfg.get("services") or {}).get("llama") or {}
        host = str(llama.get("host") or "127.0.0.1")
        port = int(llama.get("port") or 8084)
        return f"http://{host}:{port}/v1"

    @classmethod
    def local_endpoint(cls, cfg: dict) -> dict[str, Any]:
        endpoint = dict(cfg)
        endpoint.update({"base_url": cls.local_base_url(), "api_key": "",
                         "model": str(cfg.get("model") or "")})
        return endpoint

    @staticmethod
    def remote_endpoint(cfg: dict) -> dict[str, Any] | None:
        base = str(cfg.get("base_url") or "").strip()
        if not base:
            return None
        endpoint = dict(cfg)
        endpoint.update({"base_url": base, "api_key": str(cfg.get("api_key") or ""),
                         "model": str(cfg.get("remote_model") or cfg.get("model") or "")})
        return endpoint

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
        backend = str(cfg.get("backend") or "auto").lower()

        if not frames:
            return AIResult(model=str(cfg.get("model") or ""), error="没有可用关键帧，无法进行视觉分析")
        if cfg.get("provider", "openai") != "openai":
            # gemini 走不同的 SDK；本项目默认全本地，先只支持 OpenAI 兼容协议。
            return AIResult(model=str(cfg.get("model") or ""),
                            error=f"暂不支持的 provider：{cfg.get('provider')}")

        messages = self._build_messages(cfg, frames, transcript, original_name, duration)

        if backend == "api":
            endpoint = self.remote_endpoint(cfg)
            if endpoint is None:
                return AIResult(error="未配置远程接口地址（设置 → AI → 远程接口地址）")
            raw, retries, error = self._chat(endpoint, messages, stop)
            source = "remote"
        elif backend == "local":
            endpoint = self.local_endpoint(cfg)
            raw, retries, error = self._chat(endpoint, messages, stop)
            source = "local"
        else:
            endpoint = self.remote_endpoint(cfg)
            with self._lock:
                remote_ok = endpoint is not None and self._remote_fails < REMOTE_FAIL_CAP
            if remote_ok:
                raw, retries, error = self._chat(endpoint, messages, stop)  # type: ignore[arg-type]
                source = "remote"
                if error:
                    with self._lock:
                        self._remote_fails += 1
                    hub.debug(f"远程视觉调用失败，回退本地：{error}")
                    endpoint = self.local_endpoint(cfg)
                    raw, retries, error = self._chat(endpoint, messages, stop)
                    source = "local"
            else:
                endpoint = self.local_endpoint(cfg)
                raw, retries, error = self._chat(endpoint, messages, stop)
                source = "local"

        model = str((endpoint or {}).get("model") or "")
        elapsed = int((time.perf_counter() - started) * 1000)

        if error:
            return AIResult(model=model, raw=raw, error=error, retries=retries,
                            elapsed_ms=elapsed, source=source)

        try:
            title, plot, tags = self._parse(raw)
        except ValueError as exc:
            return AIResult(model=model, raw=raw, error=str(exc), retries=retries,
                            elapsed_ms=elapsed, source=source)

        return AIResult(
            title=title, plot=plot, tags=tags, raw=raw, retries=retries, model=model,
            elapsed_ms=elapsed, source=source,
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
        """列出可用模型：本地 GGUF + 远程 /v1/models。设置页「拉取模型」用。"""
        cfg = cfg or config_mod.load()["ai"]
        local_entries = scan_local_models()
        models = [entry["name"] for entry in local_entries]

        remote: list[str] = []
        remote_message = ""
        endpoint = self.remote_endpoint(cfg)
        if endpoint is not None:
            ok, message, remote = self._remote_models(endpoint)
            remote_message = message
            models += [name for name in remote if name and name not in models]

        if not models:
            return {"ok": False,
                    "message": remote_message or "没找到本地模型，也未配置远程接口",
                    "models": [], "local_entries": [], "remote": []}

        parts = [f"本地 {len(local_entries)} 个"] if local_entries else []
        if remote:
            parts.append(f"远程 {len(remote)} 个")
        return {
            "ok": True,
            "message": "、".join(parts) or remote_message or "服务可达",
            "models": models,
            "local_entries": local_entries,
            "remote": remote,
        }

    @staticmethod
    def _remote_models(endpoint: dict) -> tuple[bool, str, list[str]]:
        import httpx

        base = str(endpoint.get("base_url") or "").rstrip("/")
        headers = {}
        if endpoint.get("api_key"):
            headers["Authorization"] = f"Bearer {endpoint['api_key']}"
        try:
            resp = httpx.get(base + "/models", headers=headers, timeout=10)
        except Exception as exc:                    # noqa: BLE001
            return False, f"远程连接失败：{exc}", []
        if resp.status_code >= 400:
            return False, f"远程服务返回 {resp.status_code}", []
        try:
            body = resp.json()
        except ValueError:
            return False, "远程服务返回非 JSON", []
        models = [
            str(item.get("id") or "")
            for item in (body.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return True, (f"远程 {len(models)} 个模型" if models else "远程服务可达"), models

    def test(self, cfg: dict | None = None) -> dict[str, Any]:
        """端到端连接测试：发一句话，看模型是否真的能回。"""
        cfg = cfg or config_mod.load()["ai"]
        backend = str(cfg.get("backend") or "auto").lower()

        if backend == "api":
            endpoint = self.remote_endpoint(cfg)
            if endpoint is None:
                return {"ok": False, "message": "未配置远程接口地址"}
            return self._ping(endpoint, "远程")

        if backend == "local":
            return self._ping(self.local_endpoint(cfg), "本地")

        endpoint = self.remote_endpoint(cfg)
        if endpoint is not None:
            result = self._ping(endpoint, "远程")
            if result.get("ok"):
                return result
            local = self._ping(self.local_endpoint(cfg), "本地")
            if local.get("ok"):
                return {**local, "message": f"远程失败（{result.get('message')}），本地可用："
                                            + str(local.get("message") or "")}
            return {**local, "message": f"远程失败（{result.get('message')}）；本地也失败："
                                        + str(local.get("message") or "")}
        return self._ping(self.local_endpoint(cfg), "本地")

    def _ping(self, cfg: dict, label: str) -> dict[str, Any]:
        import httpx

        base = str(cfg.get("base_url") or "").rstrip("/")
        if not base:
            return {"ok": False, "message": f"未配置{label}服务地址"}
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
            return {"ok": False, "message": f"{label}请求失败：{exc}"}
        if resp.status_code >= 400:
            return {"ok": False, "message": f"{label}服务返回 {resp.status_code}：{resp.text[:200]}"}
        content = self._extract_content(resp)
        if not content:
            return {"ok": False, "message": f"{label}服务可达但返回空内容"}
        return {"ok": True, "message": f"{label}模型已响应：{content[:40]}",
                "source": "local" if label == "本地" else "remote"}


def scan_local_models() -> list[dict[str, Any]]:
    """扫描 AI/models/*/*.gguf（排除 mmproj），找出可切换的本地视觉模型。"""
    cfg = config_mod.load()
    current = paths.abs_path(str(((cfg.get("services") or {}).get("llama") or {}).get("model") or ""))
    alias = str((cfg.get("ai") or {}).get("model") or "")

    entries: list[dict[str, Any]] = []
    root = paths.AI_DIR / "models"
    if not root.is_dir():
        return entries
    for gguf in sorted(root.glob("*/*.gguf")):
        if gguf.name.lower().startswith("mmproj"):
            continue
        try:
            is_current = gguf.resolve() == current.resolve()
        except OSError:
            is_current = False
        if is_current and alias:
            name = alias
        else:
            # 去掉常见的量化后缀，标签短一点
            name = re.sub(r"-(?:Q\d(?:_[A-Z0-9_]+)?|IQ\d_[A-Z0-9_]+|f16|fp16|bf16)$",
                          "", gguf.stem, flags=re.IGNORECASE) or gguf.stem
        mmproj = next(iter(sorted(gguf.parent.glob("mmproj*.gguf"))), None)
        entries.append({
            "name": name,
            "model": paths.rel_path(gguf),
            "mmproj": paths.rel_path(mmproj) if mmproj else "",
            "current": is_current,
        })
    return entries


vision = VisionClient()
