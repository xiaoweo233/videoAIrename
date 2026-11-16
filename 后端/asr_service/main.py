"""内置 Qwen3-ASR 服务启动入口。

由主后端 `app/services.py` 拉起，不需要用户手动运行。命令行参数与旧版
Qwen3-ASR 项目保持一致（`--asr-checkpoint` 等），这样已有的配置和脚本还能用。

示例：
    python main.py --asr-checkpoint AI/models/Qwen/Qwen3-ASR-0.6B \
        --aligner-checkpoint AI/models/Qwen/Qwen3-ForcedAligner-0.6B \
        --sd-model AI/models/speaker-diarization-community-1 \
        --ip 127.0.0.1 --port 7867
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _parse_json_dict(value: str | None) -> dict:
    try:
        return json.loads(value) if value else {}
    except json.JSONDecodeError:
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VAIR 内置 Qwen3-ASR 转写服务")
    parser.add_argument("--asr-checkpoint", required=True)
    parser.add_argument("--aligner-checkpoint", default=None)
    parser.add_argument("--backend", default="transformers")
    parser.add_argument("--cuda-visible-devices", default="0")
    parser.add_argument("--backend-kwargs", default=None)
    parser.add_argument("--aligner-kwargs", default=None)
    parser.add_argument("--sd-model", default="", help="说话人分离模型目录")
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7867)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices

    import torch

    # 这些模块只在服务进程里导入；主后端没有 torch 也能正常工作。
    from model_hub import ModelHub
    from engine import TranscribeEngine
    from server import create_app

    backend_kwargs = _parse_json_dict(args.backend_kwargs)
    backend_kwargs.setdefault("dtype", torch.bfloat16)
    backend_kwargs.setdefault("device_map", "cuda:0")
    aligner_kwargs = _parse_json_dict(args.aligner_kwargs)
    aligner_kwargs.setdefault("dtype", torch.bfloat16)
    aligner_kwargs.setdefault("device_map", "cuda:0")

    print(f"正在加载 ASR 模型：{args.asr_checkpoint}", flush=True)
    hub = ModelHub()
    hub.load_asr(args.asr_checkpoint, args.backend, backend_kwargs,
                 args.aligner_checkpoint, aligner_kwargs)
    print("ASR 模型已加载，开始监听", flush=True)

    engine = TranscribeEngine(hub, sd_model_path=args.sd_model)
    app = create_app(engine)

    import uvicorn
    uvicorn.run(app, host=args.ip, port=args.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception as exc:                        # noqa: BLE001
        print(f"转写服务启动失败：{exc}", file=sys.stderr, flush=True)
        raise
