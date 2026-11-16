# AI 服务模式与内置转写 · 技术设计

## 1. 配置模型

### 视觉

```jsonc
"ai": {
  "provider": "openai",
  "backend": "auto",          // auto | api | local
  "base_url": "",             // 远程接口；空 = 未配置
  "api_key": "",
  "remote_model": "",         // 远程模型名
  "model": "Qwen3.5-4B",      // 本地模型别名（llama-server --alias）
  "timeout": 60, "retry_times": 2, "max_tokens": 5000,
  "temperature": 0.6, "top_p": 0.8, "enforce_json_mode": true,
  "system_prompt": "...", "prompt": "..."
}
"services.llama": {
  "model": "AI/models/Qwen3.5-4B/Qwen3.5-4B-...-Q4_K_M.gguf",
  "mmproj": "AI/models/Qwen3.5-4B/mmproj-...-BF16.gguf"
}
```

- 本地地址不再配置：`http://{services.llama.host}:{services.llama.port}/v1`。避免「改端口」和「改地址」两处不同步。
- `services.llama` 启动时追加 `--alias <ai.model>`，保证 `/v1/models` 返回的 id 与请求里的 `model` 一致。llama.cpp 默认用文件名当 id，换模型后会导致 404。

### 语音

```jsonc
"whisper": {
  "enable": true,
  "backend": "auto",          // auto | api | local
  "language": "zh",
  "timeout": 600,
  "diarize": false, "max_chars": 20, "punc": true, "hotwords": "",
  "api": {
    "protocol": "gradio",     // gradio | openai
    "base_url": "",           // 远程接口；空 = 未配置
    "api_key": "", "model": "Qwen3-ASR", "prompt": ""
  }
}
```

- 删除 `prefer_api / allow_download / model / device / compute_type / workers / vad_filter / use_gpu`。
- `auto` 的语义就是「远程优先」，不再需要 `prefer_api`。

## 2. 调度语义（两端一致）

```
backend == local  → 只用本地
backend == api    → 只用远程；未配置地址直接报错，不静默回退
backend == auto   → 配了远程且本任务失败次数 < 2 → 远程
                    失败 → 计数 +1，回退本地重试
                    未配置远程 / 已连续失败 2 次 → 本地
```

- `vision.VisionClient.reset_task_state()` 与 `transcriber.reset_task_state()` 在每批任务开始时由 `engine.start()` 调用。
- 远程失败计数是任务级的，不跨批次累积。

## 3. 内置 ASR 服务

### 目录

```
后端/asr_service/
├── main.py            # argparse + uvicorn
├── server.py          # FastAPI：/health、/transcribe
├── engine.py          # 转写引擎（物理切片、时间轴、说话人）
├── model_hub.py       # Qwen3ASRModel + pyannote Pipeline 懒加载
├── audio_utils.py     # ffmpeg 规范化与切片
├── requirements.txt   # torch/qwen-asr/pyannote 等
└── README.md          # 来源与许可说明
```

代码来源：本地 Qwen3-ASR 增强版的 `app/`（Apache-2.0 / 模型 Apache-2.0）。为嵌入本项目做了三处改动：

1. 去掉 Gradio WebUI，改为 JSON HTTP 服务（本项目不需要再展示一套界面）。
2. `--sd-model`、`--outputs-dir` 显式传参，不再依赖「app 的上一级目录」猜路径。
3. 启动时不再 `inbrowser=True`。

### 接口

- `GET /health` → `{"ok": true, "ready": bool, "model": "..."}`；模型加载完成前 `ready=false`。
- `POST /transcribe`（multipart：`file`、`lang`、`diarize`、`hotwords`、`max_chars`、`punc`）
  → `{"text", "words":[{"start","end","text","speaker"}], "language", "elapsed_ms"}`。

主后端把 `words` 丢给现有的 `group_words_into_segments()`，与远程 Gradio 返回的词级 JSON 走同一条重排/断行路径。结果里 `source="builtin"`。

### 运行环境

- 目标目录：`AI/asr-python`（不入库）。
- 安装：`python -m pip install --target AI/asr-python -r 后端/asr_service/requirements.txt`
  - 主索引：清华 PyPI；
  - `--find-links https://mirrors.aliyun.com/pytorch-wheels/cu128/`（Windows 的 PyPI torch 是 CPU 版，CUDA 必须走这个镜像）；
  - 版本与已验证的离线包一致：`torch==2.8.0+cu128`、`torchaudio==2.8.0+cu128`、`qwen-asr==0.0.6`、`pyannote.audio==4.0.7`。
- 生效方式：安装完成后在解释器 `purelib` 下写 `vair-asr.pth`，把 `AI/asr-python` 加入 `sys.path`。便携版 Python 的 `_pth` 会忽略 `PYTHONPATH`，`.pth` 是唯一可靠通道。
- 解释器探测顺序：`services.asr.python` → 项目 `python/python.exe`（能 import torch 才算）→ 旧版外部 `../Qwen3_ASR/WPy64-*/python/python.exe`（兼容）→ 报错并提示安装。

### 服务生命周期

- `services.ensure_running()` 按后端模式决定拉起哪些服务：
  - `ai.backend == api` → 不启动 llama；
  - `whisper.backend == api` → 不启动 asr；
  - 其余情况（`auto` 需要本地兜底）照旧串行拉起，先 llama 后 asr。
- ASR 健康检查：先 `GET /health`，404 时回退旧 Gradio 的 `GET /config`。

## 4. 模型列表与切换

- `vision.list_models()` 返回：
  ```json
  {"ok": true, "message": "...", "models": ["Qwen3.5-4B", "..."],
   "local_entries": [{"name","model","mmproj","current"}], "remote": ["..."]}
  ```
- 本地扫描规则：`AI/models/*/*.gguf`，排除 `mmproj-*`；同目录下取第一个 `mmproj-*.gguf` 作为投影层。
- 前端点本地条目：
  1. `PUT /api/config` 写入 `ai.model`、`services.llama.model/mmproj`；
  2. `POST /api/services/llama/restart`；
  3. 等服务重新就绪后刷新服务卡片。
- 远程模型仍走 `/v1/models`，只填 `ai.remote_model`。

## 5. 并发视频数

- 绑定 `runtime.ai_workers`（1–8）。`engine._run()` 在任务开始时读取，因此改动对下一批任务生效。
- 保留 `frames.workers` 作为内部抽帧并发，不在工作台暴露。

## 6. 兼容与迁移

- 老配置里显式写过的 `ai.base_url = http://127.0.0.1:8084/v1` 在 `auto` 下会被当成远程接口：先请求它，失败再走内置服务 —— 行为仍然正确。
- `whisper.api.base_url = http://127.0.0.1:7867` 的老配置同理：远程指向的其实就是内置服务，`auto` 首选它，失败回退内置，不会报错。
- `services.asr.dir` 默认从 `../Qwen3_ASR` 改为 `后端/asr_service`；字段仍保留，允许指向旧项目。
- `faster-whisper` 从 `requirements.txt` 与可选模块清单移除；已安装的旧包不会被主动卸载。

## 7. 测试

- 后端单测脚本（仓库外 `D:/qita/ai/.vair-test/`）：
  - 视觉三档分派与回退计数；
  - 转写三档分派；
  - 本地模型扫描；
  - 内置 ASR `/health` 与 `/transcribe`（无模型时只验证错误路径）。
- 前端 jsdom 四套（`run.mjs` / `slider.mjs` / `ui2.mjs` / `autosave.mjs`）回归，`ui2.mjs` 的模型列表接口保持 `models` 字段兼容。
