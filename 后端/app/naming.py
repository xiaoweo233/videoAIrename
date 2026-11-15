"""命名规则：模板渲染、非法字符清洗、撞名处理、跳过已处理判定。"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from . import media, proc

_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WS = re.compile(r"\s+")
_MULTI_UNDERSCORE = re.compile(r"_+")

# Windows 保留设备名，直接用作文件名会创建失败
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

TITLE_MAX_CHARS = 50
STEM_MAX_CHARS = 120

# 支持的时间格式，按常见程度排列
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d",
)

PLACEHOLDERS = ("{date}", "{title}", "{original}", "{marker}")


def extract_datetime(creation_time: str, path: str | os.PathLike[str]) -> datetime:
    """取录制时间：容器 creation_time → 文件时间 → 当前时间。"""
    text = (creation_time or "").strip()
    if text:
        normalized = text.replace("Z", "+0000")
        for fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue
        # 带时区的 ISO 串交给 fromisoformat 再试一次
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            pass

    try:
        stat = os.stat(proc.long_path(path))
        stamp = min(stat.st_mtime, stat.st_ctime)
        if stamp > 1e8:                      # 过滤掉明显异常的 0 / 1970 附近的值
            return datetime.fromtimestamp(stamp)
    except OSError:
        pass

    return datetime.now()


def extract_date_str(creation_time: str, path: str | os.PathLike[str], date_format: str) -> str:
    """按配置格式渲染日期。格式串非法时退回内置格式，不让整条任务失败。"""
    moment = extract_datetime(creation_time, path)
    try:
        return moment.strftime(date_format or "%Y%m%d_%H%M")
    except (ValueError, TypeError):
        return moment.strftime("%Y%m%d_%H%M")


def render_template(template: str, values: dict[str, str]) -> str:
    """替换占位符。不认识的占位符原样保留，方便用户看出写错了。"""
    text = template or "{date}_{title}"
    for key, value in values.items():
        text = text.replace("{" + key + "}", value or "")
    return text


def sanitize_filename(text: str, max_chars: int = TITLE_MAX_CHARS) -> str:
    """清洗成合法文件名片段。"""
    cleaned = _WIN_ILLEGAL.sub("_", text or "")
    cleaned = _WS.sub("_", cleaned)
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned)
    cleaned = cleaned.strip(". _-")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].strip(". _-")
    if not cleaned:
        return "untitled"
    if cleaned.upper() in _RESERVED or cleaned.upper().split(".")[0] in _RESERVED:
        cleaned += "_"
    return cleaned


def build_stem(
    *,
    title: str,
    original_name: str,
    creation_time: str,
    path: str | os.PathLike[str],
    cfg: dict,
) -> str:
    """按模板拼出新文件名（不含扩展名）。"""
    marker = str(cfg.get("marker") or "AI_RENAMED")
    values = {
        "date": extract_date_str(creation_time, path, str(cfg.get("date_format") or "")),
        "title": sanitize_filename(title, TITLE_MAX_CHARS),
        "original": sanitize_filename(Path(original_name or path).stem, 60),
        "marker": sanitize_filename(marker, 30),
    }

    template = str(cfg.get("template") or "{date}_{title}")
    stem = render_template(template, values)

    # 模板里没写、但开关打开的，补到尾部（保持与旧版行为一致，用户不用改模板）
    suffix: list[str] = []
    if cfg.get("include_original") and "{original}" not in template and values["original"]:
        suffix.append(values["original"])
    if cfg.get("enable_marker") and "{marker}" not in template and values["marker"]:
        suffix.append(values["marker"])

    stem = sanitize_filename(stem, STEM_MAX_CHARS)
    if suffix:
        stem = sanitize_filename(f"{stem}_{'_'.join(suffix)}", STEM_MAX_CHARS)
    return stem or "untitled"


def marker_pattern(marker: str) -> re.Pattern[str]:
    """标记必须是词边界，避免 `XAI_RENAMEDY` 这类子串误判。"""
    return re.compile(r"(?:^|_)" + re.escape(marker or "AI_RENAMED") + r"(?:_|$)")


def has_name_marker(path: str | os.PathLike[str], marker: str) -> bool:
    return bool(marker_pattern(marker).search(Path(path).stem))


def unique_target(target: Path, source: str | os.PathLike[str] | None = None) -> Path:
    """撞名时追加 _1/_2…；上限 100 次后放弃。"""
    if not _exists(target):
        return target
    if source and _same_file(target, source):
        return target

    stem, suffix, parent = target.stem, target.suffix, target.parent
    for index in range(1, 101):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not _exists(candidate):
            return candidate
    raise OSError(f"目标文件名冲突过多，放弃：{target.name}")


def _exists(path: Path) -> bool:
    try:
        return os.path.exists(proc.long_path(path))
    except OSError:
        return False


def _same_file(a: Path, b: str | os.PathLike[str]) -> bool:
    try:
        return os.path.abspath(proc.long_path(a)) == os.path.abspath(proc.long_path(b))
    except OSError:
        return False


def rename_file(
    source: str | os.PathLike[str],
    target: Path,
    *,
    preserve_times: bool = True,
    dry_run: bool = False,
) -> str:
    """执行重命名。返回 `ok` / `skipped` / `error:…`。

    `skipped` 表示新名与原名相同（比如跳过标记已经在了），不算失败。
    """
    src = Path(source)
    if _same_file(target, src):
        return "skipped"

    if dry_run:
        return "ok"

    try:
        times = None
        if preserve_times:
            stat = os.stat(proc.long_path(src))
            times = (stat.st_atime, stat.st_mtime)
        os.replace(proc.long_path(src), proc.long_path(target))
        if times:
            os.utime(proc.long_path(target), times)
    except OSError as exc:
        return f"error:{exc}"
    return "ok"


def move_to_failed(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """把失败文件移进同目录的 `_failed/`，便于复查。"""
    src = Path(path)
    try:
        bucket = src.parent / "_failed"
        bucket.mkdir(exist_ok=True)
        target = unique_target(bucket / src.name)
        os.replace(proc.long_path(src), proc.long_path(target))
        return True, str(target)
    except OSError as exc:
        return False, str(exc)


def sibling(path: Path, suffix: str) -> Path:
    """把扩展名换成别的（.mp4 → .nfo / .srt）。"""
    return path.with_suffix(suffix)


def scan_folder(
    folder: str | os.PathLike[str],
    recursive: bool = True,
    skip_marker: str | None = None,
) -> list[str]:
    """扫描文件夹里的视频。可选跳过带处理标记的文件。"""
    root = Path(folder)
    if not root.is_dir():
        return []

    pattern = marker_pattern(skip_marker) if skip_marker else None
    found: list[str] = []
    iterator = root.rglob("*") if recursive else root.glob("*")
    for entry in iterator:
        try:
            if not entry.is_file() or not media.is_video(entry):
                continue
        except OSError:
            continue
        if pattern and pattern.search(entry.stem):
            continue
        found.append(str(entry))
    found.sort()
    return found
