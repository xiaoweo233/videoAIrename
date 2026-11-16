"""内置 Qwen3-ASR 的 HTTP 接口（JSON，不再附带 Gradio 界面）。

只服务本项目的后端：`/health` 用于服务状态探测，`/transcribe` 返回
文本 + 词级时间轴 + 说话人标签，由主后端统一断句与落盘。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

MODEL_NAME = "Qwen3-ASR"


def create_app(engine) -> FastAPI:
    app = FastAPI(title="VAIR 内置语音转写", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "ready": True, "model": MODEL_NAME}

    @app.post("/transcribe")
    async def transcribe(
        file: UploadFile = File(...),
        lang: str = Form("auto"),
        diarize: bool = Form(False),
        max_chars: int = Form(20),
        punc: bool = Form(True),
        hotwords: str = Form(""),
    ) -> dict:
        # max_chars / punc 由主后端断句时使用，这里接收但不重复实现，
        # 保持与远程 Gradio 接口的参数形状一致。
        del max_chars, punc
        suffix = Path(file.filename or "audio").suffix or ".wav"
        fd, audio_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        started = time.perf_counter()
        try:
            with open(audio_path, "wb") as out:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            result = engine.run(audio_path, lang=lang, diarize=diarize,
                                hotwords=hotwords)
            result["model"] = MODEL_NAME
            result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            return result
        except Exception as exc:                    # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            try:
                os.unlink(audio_path)
            except OSError:
                pass

    return app
