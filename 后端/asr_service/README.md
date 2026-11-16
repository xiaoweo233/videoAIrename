# 内置语音转写服务（Qwen3-ASR）

本项目自己拉起、自己管理的语音转写服务，不再依赖外部的 `Qwen3_ASR` 文件夹。

## 组成

| 文件 | 作用 |
| --- | --- |
| `main.py` | 参数解析 + 加载模型 + 启动 uvicorn |
| `server.py` | FastAPI：`GET /health`、`POST /transcribe` |
| `engine.py` | 物理切片、时间轴、说话人归属 |
| `model_hub.py` | Qwen3-ASR / 强制对齐 / pyannote 懒加载 |
| `audio_utils.py` | ffmpeg 规范化（16k mono）与切片 |
| `requirements.txt` | 运行环境（torch CUDA / qwen-asr / pyannote） |

## 接口

- `GET /health` → `{"ok": true, "ready": true, "model": "Qwen3-ASR"}`
- `POST /transcribe`（multipart：`file`、`lang`、`diarize`、`max_chars`、`punc`、`hotwords`）
  → `{"text", "words": [{"start","end","text","speaker"}], "language", "model", "elapsed_ms"}`

## 运行环境

由主程序安装：

```
python -m pip install --target AI/asr-python -r 后端/asr_service/requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --find-links https://mirrors.aliyun.com/pytorch-wheels/cu128/
```

安装完成后会在解释器的 `purelib` 写 `vair-asr.pth`，把 `AI/asr-python` 加入 `sys.path`
（便携版 Python 的 `_pth` 会忽略 `PYTHONPATH`）。

## 模型

- 识别：`AI/models/Qwen/Qwen3-ASR-0.6B`
- 对齐：`AI/models/Qwen/Qwen3-ForcedAligner-0.6B`
- 说话人：`AI/models/speaker-diarization-community-1`（可选，缺了只跳过角色识别）

## 来源与许可

代码改编自 **Qwen3-ASR Pro 增强离线版** 的 `app/` 目录（原作者提供的懒人包），
底层模型来自阿里 Qwen 团队的 Qwen3-ASR / Qwen3-ForcedAligner（Apache-2.0），
说话人分离使用 pyannote.audio（MIT）。
本项目只保留转写所需的部分，并去掉 Gradio 界面、改为 JSON 接口。
