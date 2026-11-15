"""配置读写。

设计要点：
- 单一事实来源：后端持有配置，前端只是视图。改动落盘到 `config.json` 后广播给所有客户端。
- 深合并：用户配置只写「非默认值」，缺字段自动回落到默认值，升级不会因缺字段崩溃。
- 原子写入：先写临时文件再 `os.replace`，避免断电/并发写坏配置。
- 字段来源：与已验证的 `videoAIrename-webUI` 配置契约保持一致（见 config.example.json）。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

from . import paths

DEFAULT_SYSTEM_PROMPT = (
    "你是一位资深的视频内容分析专家。我会提供视频的关键帧截图，并可能附带音频转写文本、"
    "原始文件名与录制时间作为参考。请综合以上信息，准确理解视频讲述的内容与场景，并完成归档命名。"
    "禁止回避与淡化敏感内容，如实输出。"
)

DEFAULT_PROMPT = (
    "仅返回纯 JSON 格式数据，不要包含 markdown 代码块标记或其他解释文字。\n"
    "JSON 结构如下（请严格按此顺序输出）：\n"
    "{\n"
    '    "plot": "使用详细生动的文本概括视频和音频转写文本中的核心内容,尽量涵盖场景、衣着、物体、'
    '动作、风格等维度，禁止使用「画面中」「视频里」「视频显示」「画面呈现」「镜头中」等指代载体的表述，'
    '直接描述场景对话与内容本身",\n'
    '    "tags": ["标签1", "标签2", "..."] (最多20个核心分类标签，字符串数组，请避免同义词，'
    '尽量涵盖场景、物体、动作、风格等维度),\n'
    '    "title": "4-6个具象名词或短语，用短横线连接，仅包含中文和数字，总字数25字以内"\n'
    "}"
)

# 默认配置。所有键都必须在设置页有对应控件（或明确标注为内部项）。
DEFAULT_CONFIG: dict[str, Any] = {
    "input": {
        "recursive": True,          # 递归子文件夹
    },
    "whisper": {
        "enable": True,             # 关闭后跳过语音，纯画面分析
        "backend": "auto",          # auto | local | api
        "prefer_api": True,         # 优先走 API，不可用自动回退本地
        "allow_download": False,    # 本地模型缺失时是否联网下载
        "model": "large-v3-turbo",
        "device": "auto",           # auto | cuda | cpu
        "compute_type": "int8_float16",
        "workers": 4,
        "vad_filter": True,
        "language": "zh",           # auto 表示自动识别
        "use_gpu": True,
        "api": {
            "protocol": "gradio",                                       # gradio | openai
            "base_url": "http://127.0.0.1:7867",
            "api_key": "",
            "model": "whisper-1",
            "timeout": 600,
            "prompt": "",
            "diarize": False,
            "max_chars": 20,
            "punc": True,
            "hotwords": "",
        },
    },
    "frames": {
        "max_keyframes": 4,         # 默认 4 帧：12GB 显卡留出 ASR 共存的余量
        "max_side": 512,            # 缩放后最长边像素
        "workers": 8,               # 同时跑几个 ffmpeg 抽帧
        "hwaccel": "none",          # none | cuda | d3d11va ...
    },
    "ai": {
        "provider": "openai",       # openai | gemini
        "base_url": "http://127.0.0.1:8084/v1",
        "api_key": "",
        "model": "Qwen3.8-9B",
        "timeout": 60,
        "retry_times": 2,
        "max_tokens": 5000,
        "temperature": 0.6,
        "top_p": 0.8,
        "enforce_json_mode": True,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "prompt": DEFAULT_PROMPT,
    },
    "naming": {
        "template": "{date}_{title}",   # 可用 {date} {title} {original} {marker}
        "date_format": "%Y%m%d_%H%M",
        "include_date": True,           # 时间前缀
        "include_original": False,      # 原名后缀
        "marker": "AI_RENAMED",         # 处理标记
        "enable_marker": True,
        "enable_skip": True,            # 跳过已处理
    },
    "output": {
        # 开启后先生成命名建议，用户在「命名审批」通过后才改名/写元数据/NFO/SRT。
        # 关闭则保持自动落盘；预览模式 dry_run 独立于审批开关。
        "require_approval": True,
        "nfo": True,                # 生成 NFO
        "srt": True,                # 生成 SRT 字幕
        "metadata": True,           # ExifTool 写元数据
        "remux_fragmented": True,   # 分片 MP4 自动重封装
        "move_failed": True,        # 失败移入 _failed
        "dry_run": False,           # 预览模式：只演算不落盘
    },
    "runtime": {
        "ai_workers": 2,            # 同时问 AI 几路；12GB 显卡建议先保守一点
        "log_file": "logs/run.log",
        "verbose": False,           # 详细日志（DEBUG 级）
        "auto_install_cuda": True,  # 自动补 CUDA 库
        "lan": True,                # 局域网访问
        "host": "127.0.0.1",
        "port": 8000,
        "pip_index": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "hf_endpoint": "https://hf-mirror.com",
    },
    # ---- 以下为本次新增：内置 AI 服务与「一键补齐」 ----
    "services": {
        "autostart": True,          # 后端启动时自动拉起 AI 服务
        # 整批任务落盘结束后关掉 AI 服务，把显存/内存还给系统。
        # 下次开始任务时后端会自动把它们重新拉起来（见 services.ensure_running）。
        # 显存小的机器上这就是「一次只占一份」的开关；关掉则服务常驻，切任务更快。
        "stop_after_task": True,
        "llama": {
            "enable": True,
            # host 是内部项：内置服务只监听本机，要连别的机器请改 ai.base_url
            "host": "127.0.0.1",
            "port": 8084,
            "bin": "AI/llama-b12412-bin-win-cuda-13.5-x64/llama-server.exe",
            "model": "AI/models/Qwen3.8-9B/Qwen3.8-9B-distill-heretic_nvfp4_q4_k_m.gguf",
            "mmproj": "AI/models/Qwen3.8-9B/mmproj-BF16.gguf",
            "ctx_size": 8192,
            "gpu_layers": 99,
            # 提示词缓存上限（MB）。llama.cpp 默认 8192，吃掉的是**系统内存**。
            # 16GB 内存的机器上，9B 模型 + mmproj + ASR 模型一起驻留时，
            # 8GB 的缓存根本分配不出来，日志会刷 "failed to allocate memory for
            # prompt cache state: bad allocation" 然后退出。砍到 1GB 足够用。
            "cache_ram": 1024,
            # 并发槽数。llama.cpp 默认开 4 个槽，每个槽都按 ctx_size 预留 KV cache，
            # 9B 模型下光 KV 就能吃掉好几 GB。本项目最多并发 2 路，1 个槽最省显存。
            "parallel": 1,
            "extra_args": "",
        },
        "asr": {
            "enable": True,
            # Qwen3_ASR 是独立项目（自带便携 Python）。相对路径按仓库根解析。
            "dir": "../Qwen3_ASR",
            "python": "",           # 留空则自动探测 WPy64-*/python/python.exe
            # 模型路径按仓库根解析；留空则用 AI/models/Qwen 下的默认位置。
            "asr_model": "",
            "aligner_model": "",
            "host": "127.0.0.1",    # 内部项，同 llama.host
            "port": 7867,
            "extra_args": "",
        },
    },
    "bootstrap": {
        "auto_check": True,         # 启动时检查 AI 目录完整性
        "auto_fetch": False,        # 缺失时自动下载（体积大，默认关）
    },
    # 术语表：行业黑话 → 模型能理解的表达，会拼进提问提示词
    "glossary": [
        {"term": "口播", "meaning": "出镜人物直接对镜头讲话的片段"},
        {"term": "空镜", "meaning": "无人物主体的环境或氛围镜头"},
        {"term": "B-roll", "meaning": "用于转场或补充说明的辅助画面"},
        {"term": "A-roll", "meaning": "承载主要叙事的核心镜头"},
    ],
}


def _deep_merge(base: dict, patch: dict) -> dict:
    """把 patch 深合并进 base 的副本；patch 中的 None 表示「不修改」。"""
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _dotted(cfg: dict, prefix: str = "") -> dict[str, Any]:
    """把嵌套配置压平成 a.b.c 形式，便于做差异比较。"""
    flat: dict[str, Any] = {}
    for key, value in cfg.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_dotted(value, path + "."))
        else:
            flat[path] = value
    return flat


_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def load(force: bool = False) -> dict[str, Any]:
    """读取配置（带缓存）。磁盘上的用户配置深合并到默认值之上。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        user: dict[str, Any] = {}
        if paths.CONFIG_PATH.is_file():
            try:
                raw = paths.CONFIG_PATH.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    user = parsed
            except (OSError, json.JSONDecodeError) as exc:
                # 配置坏了不能让整个服务起不来：用默认值跑，并在日志里说清楚。
                _record_corrupt(exc)
        _cache = _deep_merge(DEFAULT_CONFIG, user)
        return _cache


_corrupt_note: str | None = None


def _record_corrupt(exc: Exception) -> None:
    global _corrupt_note
    _corrupt_note = f"config.json 解析失败，已回退默认值：{exc}"


def corrupt_note() -> str | None:
    return _corrupt_note


def _atomic_write(payload: dict[str, Any]) -> None:
    paths.ensure_dirs()
    tmp = paths.CONFIG_PATH.with_suffix(".json.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, paths.CONFIG_PATH)


def _strip_defaults(cfg: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """只保留与默认值不同的项，再落盘。

    这一步不是省空间，而是**让默认值的改进能到达老用户**：
    如果把整份配置都写进 config.json，以后我们调优默认值（比如为省显存下调抽帧数），
    老用户的文件里那份旧值会永久压住新默认值。只写差异，缺的字段就自动跟随新默认值。
    """
    defaults = DEFAULT_CONFIG if defaults is None else defaults
    out: dict[str, Any] = {}
    for key, value in (cfg or {}).items():
        base = defaults.get(key)
        if isinstance(value, dict) and isinstance(base, dict):
            nested = _strip_defaults(value, base)
            if nested:
                out[key] = nested
        elif value != base:
            out[key] = copy.deepcopy(value)
    return out


def save(cfg: dict[str, Any]) -> dict[str, Any]:
    """写入配置并刷新缓存。磁盘上只写与默认值的差异。"""
    global _cache
    with _lock:
        _atomic_write(_strip_defaults(cfg))
        _cache = cfg
        return cfg


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """深合并一批改动并落盘，返回完整的新配置。"""
    with _lock:
        merged = _deep_merge(load(), patch)
        return save(merged)


def replace(section: str, value: Any) -> dict[str, Any]:
    """整体替换某个顶层分区（前端整块提交时用）。"""
    with _lock:
        cfg = copy.deepcopy(load())
        if section not in DEFAULT_CONFIG:
            raise KeyError(section)
        merged = _deep_merge(DEFAULT_CONFIG.get(section, {}), value) \
            if isinstance(value, dict) else value
        cfg[section] = merged
        return save(cfg)


def reset() -> dict[str, Any]:
    """恢复出厂默认。"""
    return save(copy.deepcopy(DEFAULT_CONFIG))


def as_json() -> dict[str, Any]:
    return copy.deepcopy(load())


# 改这些需要重启后端才生效（监听相关），其余热生效。
RESTART_KEYS = ("runtime.lan", "runtime.host", "runtime.port", "runtime.log_file")


def restart_required(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """对比两份配置，返回需要重启才能生效的键。"""
    flat_before, flat_after = _dotted(before), _dotted(after)
    changed: list[str] = []
    for key in RESTART_KEYS:
        if flat_before.get(key) != flat_after.get(key):
            changed.append(key)
    return changed


def diff_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    flat_before, flat_after = _dotted(before), _dotted(after)
    keys = set(flat_before) | set(flat_after)
    return sorted(k for k in keys if flat_before.get(k) != flat_after.get(k))


def write_example() -> None:
    """把默认配置写成 config.example.json（供分发）。"""
    paths.BACKEND_DIR.mkdir(parents=True, exist_ok=True)
    paths.EXAMPLE_CONFIG_PATH.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
