"""音频规范化与切片。

来源：Qwen3-ASR Pro 增强离线版 `app/audio_utils.py`（Apache-2.0）。
改动：临时文件改为 mkstemp（mktemp 有竞态），并集中提供 cleanup。
"""

from __future__ import annotations

import os
import subprocess
import tempfile


class AudioUtils:
    @staticmethod
    def get_duration(file_path: str) -> float:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", file_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            return float(result.stdout)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def convert_to_std_wav(input_path: str) -> str:
        """强制规范化：16k, mono, s16le。这是防漂移的物理基础。"""
        fd, temp_wav = tempfile.mkstemp(suffix="_std_16k.wav")
        os.close(fd)
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-acodec", "pcm_s16le",
               "-ar", "16000", "-ac", "1", temp_wav]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
        except FileNotFoundError as exc:
            os.unlink(temp_wav)
            raise RuntimeError(
                "找不到 ffmpeg。请把 ffmpeg.exe 放到项目的 ffmpeg/ 目录，"
                "或确认它已在 PATH 上。") from exc
        except subprocess.CalledProcessError:
            os.unlink(temp_wav)
            raise
        return temp_wav

    @staticmethod
    def split_audio(input_path: str, max_sec: int = 1000) -> list[dict]:
        """按固定时长物理切片。短音频直接返回原文件。"""
        duration = AudioUtils.get_duration(input_path)
        if duration <= max_sec:
            return [{"path": input_path, "offset": 0.0}]

        chunks: list[dict] = []
        for start in range(0, int(duration), max_sec):
            end = min(start + max_sec, int(duration))
            fd, chunk_path = tempfile.mkstemp(suffix=f"_chunk_{start}.wav")
            os.close(fd)
            cmd = ["ffmpeg", "-y", "-i", input_path, "-ss", str(start),
                   "-t", str(end - start), "-acodec", "pcm_s16le",
                   "-ar", "16000", "-ac", "1", chunk_path]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            chunks.append({"path": chunk_path, "offset": float(start)})
        return chunks

    @staticmethod
    def cleanup(std_wav: str, chunks: list[dict]) -> None:
        paths = {str(std_wav)} | {str(c.get("path")) for c in chunks or []}
        for path in paths:
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
