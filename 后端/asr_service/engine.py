"""转写引擎：物理切片 + Qwen3-ASR + pyannote 说话人分离。

代码来源：Qwen3-ASR Pro 增强离线版的 `app/transcribe_engine.py`
（Apache-2.0，模型由阿里 Qwen 团队开源）。为嵌入本项目做了一处结构调整：
输出改为纯数据（文本 + 词级时间轴 + 说话人），不再写 SRT/ZIP 文件，
由主后端统一走与远程接口相同的重排与落盘逻辑。
"""

from __future__ import annotations

import difflib
import os
import re

import torch

from audio_utils import AudioUtils


# qwen-asr 只认完整语言名（"Chinese"），主后端传的是 ISO 码（"zh"）。
# 这里把界面/配置里可能出现的形式统一转过去，未知值直接透传，
# 让 qwen-asr 自己报错，避免悄悄按自动识别处理。
_ASR_LANG_NAMES = {
    "chinese", "english", "cantonese", "arabic", "german", "french", "spanish",
    "portuguese", "indonesian", "italian", "korean", "russian", "thai",
    "vietnamese", "japanese", "turkish", "hindi", "malay", "dutch", "swedish",
    "danish", "finnish", "polish", "czech", "filipino", "persian", "greek",
    "romanian", "hungarian", "macedonian",
}
_LANG_ALIASES = {
    # 中文
    "zh": "Chinese", "zh-cn": "Chinese", "zh-hans": "Chinese", "cmn": "Chinese",
    "中文": "Chinese", "汉语": "Chinese", "普通话": "Chinese",
    "zh-tw": "Chinese", "zh-hant": "Chinese", "zh-hk": "Cantonese",
    # 粤语
    "yue": "Cantonese", "zh-yue": "Cantonese", "粤语": "Cantonese", "广东话": "Cantonese",
    # 其他常用
    "en": "English", "en-us": "English", "en-gb": "English", "英语": "English",
    "ja": "Japanese", "jp": "Japanese", "日语": "Japanese", "日本語": "Japanese",
    "ko": "Korean", "kr": "Korean", "韩语": "Korean", "한국어": "Korean",
    "fr": "French", "法语": "French",
    "de": "German", "德语": "German",
    "es": "Spanish", "西班牙语": "Spanish",
    "ru": "Russian", "俄语": "Russian",
    "pt": "Portuguese", "葡萄牙语": "Portuguese",
    "ar": "Arabic", "阿拉伯语": "Arabic",
    "th": "Thai", "泰语": "Thai",
    "vi": "Vietnamese", "越南语": "Vietnamese",
    "it": "Italian", "意大利语": "Italian",
    "id": "Indonesian", "印尼语": "Indonesian",
    "ms": "Malay", "马来语": "Malay",
    "hi": "Hindi", "印地语": "Hindi",
    "tr": "Turkish", "土耳其语": "Turkish",
    "nl": "Dutch", "荷兰语": "Dutch",
    "sv": "Swedish", "瑞典语": "Swedish",
    "da": "Danish", "丹麦语": "Danish",
    "fi": "Finnish", "芬兰语": "Finnish",
    "pl": "Polish", "波兰语": "Polish",
    "cs": "Czech", "捷克语": "Czech",
    "fil": "Filipino", "tl": "Filipino", "菲律宾语": "Filipino",
    "fa": "Persian", "波斯语": "Persian",
    "el": "Greek", "希腊语": "Greek",
    "ro": "Romanian", "罗马尼亚语": "Romanian",
    "hu": "Hungarian", "匈牙利语": "Hungarian",
    "mk": "Macedonian", "马其顿语": "Macedonian",
}


def _qwen_language(value: str) -> str | None:
    """把 ISO 码 / 中文名 / 完整名都归一成 qwen-asr 要的语言名。"""
    text = str(value or "").strip()
    if text.lower() in ("", "auto", "none", "自动", "自动识别"):
        return None
    name = _LANG_ALIASES.get(text.lower()) or _LANG_ALIASES.get(text)
    if name:
        return name
    if text.lower() in _ASR_LANG_NAMES:
        return text[:1].upper() + text[1:].lower()
    return text


class TranscribeEngine:
    def __init__(self, hub, sd_model_path: str = "") -> None:
        self.hub = hub
        self.sd_model_path = sd_model_path

    # ---------- 对外入口 ----------

    def run(self, file_path: str, *, lang: str = "auto", diarize: bool = False,
            hotwords: str = "", max_seconds: int = 1000) -> dict:
        """转写一个音频/视频文件，返回 {text, words, language}。"""
        std_wav = AudioUtils.convert_to_std_wav(file_path)
        chunks: list[dict] = []
        try:
            speakers = self._diarize(std_wav) if diarize else None
            chunks = AudioUtils.split_audio(std_wav, max_seconds)
            language = _qwen_language(lang)
            system_prompt = self._build_system_prompt(hotwords)

            all_text = ""
            words: list[dict] = []
            for chunk in chunks:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                result = self.hub.asr_model.transcribe(
                    audio=chunk["path"],
                    language=language,
                    context=system_prompt,
                    return_time_stamps=True,
                )[0]
                all_text += (getattr(result, "text", "") or "") + " "
                for ts in getattr(result, "time_stamps", []) or []:
                    start = float(ts.start_time) + chunk["offset"]
                    end = float(ts.end_time) + chunk["offset"]
                    words.append({
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "text": str(ts.text or ""),
                        "speaker": self._match_speaker(start, end, speakers),
                    })

            return {
                "text": all_text.strip(),
                "words": words,
                "language": lang if lang not in ("", "auto", "自动识别") else "",
            }
        finally:
            AudioUtils.cleanup(std_wav, chunks)

    def align(self, file_path: str, text: str, lang: str) -> tuple[str, list[dict]]:
        """把用户提供的文稿对齐到音频（保留给后续「精准对齐」功能使用）。"""
        asr_text, words = self._plain_run(file_path, lang)

        def pure_chars(value: str) -> str:
            return "".join(ch for ch in (value or "")
                           if re.match(r"[a-zA-Z0-9一-鿿]", ch))

        char_ts: list[dict] = []
        for word in words:
            chars = pure_chars(word["text"])
            if not chars:
                continue
            duration = (word["end"] - word["start"]) / len(chars)
            for index, char in enumerate(chars):
                char_ts.append({
                    "char": char,
                    "start": word["start"] + index * duration,
                    "end": word["start"] + (index + 1) * duration,
                })

        asr_pure = pure_chars(asr_text)
        user_pure = pure_chars(text)
        matcher = difflib.SequenceMatcher(None, asr_pure, user_pure)

        mapped: list[dict] = []
        last_time = 0.0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for idx in range(i1, i2):
                    if idx < len(char_ts):
                        item = char_ts[idx]
                        mapped.append({"start": item["start"], "end": item["end"],
                                       "text": item["char"]})
                        last_time = item["end"]
            elif tag in ("replace", "insert"):
                start_ref = char_ts[i1]["start"] if i1 < len(char_ts) else last_time
                end_ref = (char_ts[min(i2, len(char_ts) - 1)]["end"]
                           if i2 <= len(char_ts) and i2 > 0 else last_time + 0.5)
                count = j2 - j1
                if count > 0:
                    char_dur = (end_ref - start_ref) / count
                    for k in range(count):
                        start = start_ref + k * char_dur
                        end = start_ref + (k + 1) * char_dur
                        mapped.append({"start": start, "end": end,
                                       "text": user_pure[j1 + k]})
                        last_time = end
        return text, mapped

    # ---------- 内部 ----------

    def _plain_run(self, file_path: str, lang: str) -> tuple[str, list[dict]]:
        data = self.run(file_path, lang=lang, diarize=False)
        return str(data.get("text") or ""), list(data.get("words") or [])

    def _diarize(self, std_wav: str) -> list[list] | None:
        if not self.sd_model_path or not os.path.isdir(self.sd_model_path):
            print(f"[warn] 未找到说话人分离模型：{self.sd_model_path or '（未配置）'}"
                  "，本次不做角色识别", flush=True)
            return None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        try:
            print("正在运行 pyannote 角色分离…", flush=True)
            import torchaudio

            pipeline = self.hub.load_diarization(self.sd_model_path)
            waveform, sample_rate = torchaudio.load(std_wav)
            output = pipeline({"waveform": waveform, "sample_rate": sample_rate})
            diarization = getattr(output, "exclusive_speaker_diarization", None) \
                or output.speaker_diarization
            segments = [[float(seg.start), float(seg.end), str(label)]
                        for seg, _, label in diarization.itertracks(yield_label=True)]
            print(f"角色分离完成，识别到 {len({s[2] for s in segments})} 个说话人", flush=True)
            return segments
        except Exception as exc:                    # noqa: BLE001 - 角色分离失败不该拖垮转写
            print(f"[warn] pyannote 运行失败：{exc}", flush=True)
            import traceback
            traceback.print_exc()
            return None
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    @staticmethod
    def _build_system_prompt(hotwords: str) -> str | None:
        if not hotwords:
            return None
        words = hotwords.replace("，", ",").replace(" ", ",")
        return f"Vocabulary: {words}."

    @staticmethod
    def _match_speaker(start: float, end: float, segments: list[list] | None) -> str:
        """按时间重叠率把词归到说话人。服务端时间轴单位是秒。"""
        if not segments:
            return ""
        best_label, best_iou = "", -1.0
        for seg_start, seg_end, label in segments:
            overlap = max(0.0, min(end, seg_end) - max(start, seg_start))
            union = (end - start) + (seg_end - seg_start) - overlap
            iou = overlap / union if union > 0 else 0.0
            if iou > best_iou:
                best_iou, best_label = iou, label
        return best_label if best_iou > 0 else ""
