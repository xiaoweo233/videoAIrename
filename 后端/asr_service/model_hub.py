"""模型加载：Qwen3-ASR（+ 强制对齐）与 pyannote 说话人分离。

来源：Qwen3-ASR Pro 增强离线版 `app/model_hub.py`（Apache-2.0）。
改动：
- 说话人模型路径不存在时给出明确提示而不是抛栈；
- `_fix_pyannote_config` 只在路径真的改变时写回，避免只读目录报错。
"""

from __future__ import annotations

import os
import warnings

import torch
import yaml

warnings.filterwarnings("ignore", category=UserWarning, module="pyannote.audio.core.io")
warnings.filterwarnings("ignore", category=UserWarning)
try:
    from pyannote.audio.utils.reproducibility import ReproducibilityWarning
    warnings.filterwarnings("ignore", category=ReproducibilityWarning)
except Exception:                                   # noqa: BLE001
    pass

from pyannote.audio import Pipeline
from qwen_asr import Qwen3ASRModel


class ModelHub:
    def __init__(self) -> None:
        self.asr_model = None
        self.sd_pipeline = None

    def load_asr(self, ckpt, backend, kwargs, aligner_ckpt=None, aligner_kwargs=None):
        load_fn = Qwen3ASRModel.from_pretrained if backend == "transformers" \
            else Qwen3ASRModel.LLM
        self.asr_model = load_fn(
            ckpt,
            forced_aligner=aligner_ckpt,
            forced_aligner_kwargs=aligner_kwargs,
            **kwargs,
        )
        return self.asr_model

    def _fix_pyannote_config(self, model_path: str) -> None:
        config_path = os.path.join(model_path, "config.yaml")
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}

        params = ((cfg.get("pipeline") or {}).get("params") or {})
        changed = False
        for key in ("embedding", "segmentation"):
            if key in params:
                wanted = os.path.join(model_path, key)
                if params[key] != wanted:
                    params[key] = wanted
                    changed = True
        if not changed:
            return
        try:
            with open(config_path, "w", encoding="utf-8") as fh:
                yaml.dump(cfg, fh)
        except OSError as exc:
            print(f"[warn] 无法改写 pyannote 配置（{exc}），将按原样加载", flush=True)

    def load_diarization(self, model_path: str):
        """加载 pyannote 离线流水线（首次调用后缓存）。"""
        if self.sd_pipeline is not None:
            return self.sd_pipeline
        if not os.path.isdir(model_path):
            raise FileNotFoundError(
                f"未找到说话人分离模型：{model_path}\n"
                "请把 speaker-diarization-community-1 放到 AI/models/ 下，"
                "或在设置里关掉「说话人分离」。")

        print("--- pyannote 离线模型加载 ---", flush=True)
        self._fix_pyannote_config(model_path)
        self.sd_pipeline = Pipeline.from_pretrained(
            os.path.join(model_path, "config.yaml"))
        if torch.cuda.is_available():
            self.sd_pipeline.to(torch.device("cuda"))
            print("pyannote 已加载至 GPU", flush=True)
        return self.sd_pipeline
