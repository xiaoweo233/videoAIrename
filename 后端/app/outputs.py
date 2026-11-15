"""附属产物：NFO、SRT、ExifTool 元数据。

ExifTool 中文写元数据是本项目最容易踩坑的地方，规则如下（实测结论）：

- 文件路径**必须作为独立 argv 参数**，不能塞进 ArgFile（`"-@" + path` 这种拼法会失效）。
- `-charset exiftool=` 只认 Latin / UTF8，**不认 cp936**，所以编码靠「ArgFile 用什么字节写」控制。
- 写要分两遍：`ItemList:*` / `XMP-*` 用 UTF-8；`Keys:*`（QuickTime 盒）用 **GBK**，
  因为 Windows 资源管理器的「属性 → 详细信息」按系统 ANSI 解码 Keys 盒。
- 读与写相反：读的 ArgFile 用 UTF-8，且**要带** `-charset filename=UTF8`。
- 优先用 `exiftool_files/perl.exe exiftool.pl`，规避 `exiftool(-k).exe` 的
  `-- press ENTER --` 交互挂起。
"""

from __future__ import annotations

import os
import tempfile
import xml.sax.saxutils as saxutils
from pathlib import Path

from . import config as config_mod
from . import paths, proc
from .hub import hub

WATERMARK = "AIVideoRenameV1"          # 软水印：写进视频内部属性，用于「跳过已处理」判定
EXIFTOOL_TIMEOUT = 120


class MetadataError(RuntimeError):
    pass


def _exiftool_base() -> list[str]:
    """返回 exiftool 的调用前缀（可能是 perl + .pl，也可能是裸 exe）。"""
    found = paths.tool("exiftool")
    if not found:
        raise MetadataError("未找到 exiftool")

    exe = Path(found)
    for candidate in (exe.parent / "exiftool_files" / "exiftool.pl",
                      exe.parent / "exiftool.pl"):
        if candidate.is_file():
            perl = candidate.parent / "perl.exe"
            if perl.is_file():
                return [str(perl), str(candidate)]
    return [str(exe)]


def _run_exiftool(groups: list[tuple[list[str], str]],
                  argv_prefix: tuple[str, ...] = ()) -> proc.CompletedProcess:
    """把参数分组写进若干 ArgFile 再执行。

    每个分组是 (参数行, 编码)。用多个 `-@` 是为了**同时满足两个互相冲突的需求**：

    - 标签值（Keys 盒）必须以 GBK 字节落盘，Windows 属性面板才不显示乱码；
    - 文件路径必须按 UTF-8 交给 exiftool，否则中文文件名会被判成「文件不存在」。

    这两者的编码要求不同，所以拆成两个 ArgFile。路径走 ArgFile 而不是 argv，
    还能顺手摆脱对系统 ANSI 代码页的依赖（英文 Windows 上中文路径也不会坏）。
    """
    paths.ensure_dirs()
    temp_files: list[Path] = []
    try:
        # `-b` 这类裸开关放 argv 更可靠：写进 ArgFile 时 ExifTool 会把它后面的
        # 标签名当成文件名（实测 "File not found - Keys:Title"）。
        args = _exiftool_base() + list(argv_prefix)
        for lines, encoding in groups:
            if not lines:
                continue
            handle, name = tempfile.mkstemp(suffix=".args", dir=str(paths.CACHE_DIR))
            argfile = Path(name)
            temp_files.append(argfile)
            with os.fdopen(handle, "wb") as fh:
                fh.write(("\n".join(lines) + "\n").encode(encoding, errors="replace"))
            # `-@` 与 ArgFile 路径必须拆成两个独立参数
            args += ["-@", str(argfile)]
        return proc.run(args, timeout=EXIFTOOL_TIMEOUT)
    finally:
        for argfile in temp_files:
            argfile.unlink(missing_ok=True)


# ---------- 写入 ----------

def _build_tags(title: str, plot: str, tags: list[str], original_name: str) -> tuple[list[str], list[str]]:
    """返回 (UTF-8 遍的标签, GBK 遍的标签)。"""
    utf8_tags: list[str] = []
    gbk_tags: list[str] = []

    if title:
        utf8_tags += [f"-ItemList:Title={title}", f"-XMP-dc:Title={title}"]
        gbk_tags.append(f"-Keys:Title={title}")
    if plot:
        utf8_tags += [
            f"-ItemList:Description={plot}",
            f"-ItemList:Comment={plot}",
            f"-XMP-dc:Description={plot}",
        ]
        gbk_tags += [f"-Keys:Description={plot}", f"-Keys:Comment={plot}"]
    if tags:
        joined = "，".join(tags)
        utf8_tags.append(f"-XMP-dc:Subject={joined}")
        gbk_tags.append(f"-Keys:Keywords={joined}")
    if original_name:
        utf8_tags.append(f"-XMP-getty:OriginalFileName={original_name}")
        gbk_tags.append(f"-Keys:OriginalFileName={original_name}")

    # 软水印：读回来判「已处理」
    utf8_tags.append(f"-XMP-tiff:Software={WATERMARK}")
    gbk_tags.append(f"-Keys:Software={WATERMARK}")

    return utf8_tags, gbk_tags


def write_metadata(
    video: str | os.PathLike[str],
    *,
    title: str,
    plot: str = "",
    tags: list[str] | None = None,
    original_name: str = "",
    dry_run: bool = False,
) -> tuple[bool, str]:
    """写视频内部元数据。返回 (是否成功, 说明)。"""
    if dry_run:
        return True, "预览模式，跳过元数据写入"
    if not paths.tool("exiftool"):
        return False, "未找到 exiftool，跳过元数据写入"

    utf8_tags, gbk_tags = _build_tags(title, plot, tags or [], original_name)
    if not utf8_tags and not gbk_tags:
        return True, "没有可写入的元数据"

    problems: list[str] = []
    target = str(video)

    # 第 1 遍：UTF-8（ItemList / XMP）。参数与路径同处一个 UTF-8 ArgFile。
    if utf8_tags:
        try:
            result = _run_exiftool([
                (["-overwrite_original", "-charset", "exiftool=UTF8", *utf8_tags], "utf-8"),
                ([target], "utf-8"),
            ])
            if result.returncode != 0:
                problems.append(f"UTF-8 遍失败：{proc.decode(result.stderr)[-200:]}")
        except proc.ToolError as exc:
            problems.append(f"UTF-8 遍异常：{exc}")

    # 第 2 遍：GBK（Keys 盒，给 Windows 属性面板读）。标签必须 GBK，路径必须 UTF-8，故拆两个 ArgFile。
    if gbk_tags:
        try:
            result = _run_exiftool([
                (["-overwrite_original", *gbk_tags], "gbk"),
                ([target], "utf-8"),
            ])
            if result.returncode != 0:
                problems.append(f"GBK 遍失败：{proc.decode(result.stderr)[-200:]}")
        except proc.ToolError as exc:
            problems.append(f"GBK 遍异常：{exc}")

    if problems:
        return False, "；".join(problems)
    return True, "元数据已写入"


def _repair_encoding(value: str) -> str:
    """把读回来的值修回正确文本。

    一次读取里会同时出现 UTF-8（ItemList/XMP）与 GBK（Keys 盒）两种字节，而 stdout
    只能按一种编码解码。所以先按 latin-1 原样收字节，再逐个值试 UTF-8 / GBK。
    """
    if not value:
        return value
    try:
        raw = value.encode("latin-1", errors="strict")
    except UnicodeEncodeError:
        return value                     # 已经是正常 Unicode 了
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value


# Windows 属性面板关心的 QuickTime Keys 标签
KEY_TAGS = ("Title", "Description", "Comment", "Keywords", "Software", "OriginalFileName")


def read_key_tag(video: str | os.PathLike[str], tag: str) -> str:
    """按 GBK 正确读回一个 QuickTime Keys 标签。

    必须走 `-b`（原始字节直出）：ExifTool 的 `-j` / 文本输出会对值做 UTF-8 校验，
    GBK 字节序列里非法的那部分会被替换成 `?`，信息就永久丢了。实测 `-b` 是唯一
    能拿到原始字节的出口。
    """
    if not paths.tool("exiftool"):
        return ""
    # 标签名必须带 `-` 前缀，否则 ExifTool 会把它当成文件名
    name = tag if tag.startswith("-") else f"-{tag if ':' in tag else f'Keys:{tag}'}"
    try:
        result = _run_exiftool([([name, str(video)], "utf-8")], argv_prefix=("-b",))
    except proc.ToolError:
        return ""
    if result.returncode != 0:
        return ""
    raw = (result.stdout or b"").strip()
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_key_tags(video: str | os.PathLike[str]) -> dict[str, str]:
    """读全部关心的 Keys 标签。每个标签一次调用 —— 只在用户主动查看时用。"""
    out: dict[str, str] = {}
    for tag in KEY_TAGS:
        value = read_key_tag(video, tag)
        if value:
            out[f"Keys:{tag}"] = value
    return out


def read_metadata(video: str | os.PathLike[str], *, with_keys: bool = False) -> dict:
    """读全部元数据（-G1 -s 带分组名）。

    `with_keys=True` 时会额外用 `-b` 把 QuickTime Keys 标签读准（多几次调用，
    只在用户主动查看元数据时才开）。
    """
    if not paths.tool("exiftool"):
        return {}
    try:
        result = _run_exiftool([
            (["-j", "-G1", "-s", "-charset", "exiftool=UTF8", str(video)], "utf-8"),
        ])
    except proc.ToolError:
        return {}
    if result.returncode != 0:
        return {}

    import json
    try:
        # latin-1 保证字节 1:1 保留，再按值逐个修复编码
        parsed = json.loads(result.stdout.decode("latin-1"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not parsed:
        return {}
    row = parsed[0]
    out = {
        key: _repair_encoding(value) if isinstance(value, str) else value
        for key, value in row.items()
    }
    if with_keys:
        # JSON 那一路的 Keys 值可能已被替换成 ?，用原始字节覆盖回来
        out.update(read_key_tags(video))
    return out


def has_watermark(video: str | os.PathLike[str]) -> bool:
    """判定视频是否已带本工具的软水印。"""
    for key, value in read_metadata(video).items():
        if key.lower().endswith("software") and WATERMARK in str(value):
            return True
    return False


# ---------- NFO ----------

def write_nfo(
    video: str | os.PathLike[str],
    *,
    title: str,
    plot: str = "",
    tags: list[str] | None = None,
    original_name: str = "",
    duration: float = 0.0,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """生成 Jellyfin / Kodi 认的 movie.nfo。"""
    target = Path(video).with_suffix(".nfo")
    if dry_run:
        return True, f"预览模式，跳过写入 {target.name}"

    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<movie>"]
    lines.append(f"    <title>{saxutils.escape(title or '')}</title>")
    if plot:
        lines.append(f"    <plot>{saxutils.escape(plot)}</plot>")
    if duration:
        lines.append(f"    <runtime>{int(duration / 60)}</runtime>")
    if original_name:
        lines.append(f"    <originaltitle>{saxutils.escape(original_name)}</originaltitle>")
    for tag in tags or []:
        lines.append(f"    <tag>{saxutils.escape(tag)}</tag>")
    lines.append("</movie>")
    lines.append("")

    try:
        target.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return False, f"NFO 写入失败：{exc}"
    return True, f"已生成 {target.name}"


# ---------- SRT ----------

def write_srt(
    video: str | os.PathLike[str],
    content: str,
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    target = Path(video).with_suffix(".srt")
    if dry_run:
        return True, f"预览模式，跳过写入 {target.name}"
    if not (content or "").strip():
        return False, "字幕内容为空，跳过写入"
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return False, f"SRT 写入失败：{exc}"
    return True, f"已生成 {target.name}"


# ---------- 汇总 ----------

def write_all(
    video: str | os.PathLike[str],
    *,
    title: str,
    plot: str = "",
    tags: list[str] | None = None,
    original_name: str = "",
    duration: float = 0.0,
    srt_content: str = "",
    dry_run: bool = False,
) -> list[str]:
    """按配置写全部附属产物，返回人类可读的结果列表。"""
    cfg = config_mod.load()["output"]
    notes: list[str] = []

    if cfg.get("metadata", True):
        ok, message = write_metadata(
            video, title=title, plot=plot, tags=tags,
            original_name=original_name, dry_run=dry_run,
        )
        notes.append(message)
        if not ok:
            hub.warn(f"元数据写入未完全成功：{message}")

    if cfg.get("nfo", True):
        ok, message = write_nfo(
            video, title=title, plot=plot, tags=tags,
            original_name=original_name, duration=duration, dry_run=dry_run,
        )
        notes.append(message)

    if cfg.get("srt", True) and srt_content.strip():
        ok, message = write_srt(video, srt_content, dry_run=dry_run)
        notes.append(message)

    return notes
