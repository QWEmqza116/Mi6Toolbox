# -*- coding: utf-8 -*-
"""Android boot 镜像解包 / 编辑 / 重打包（小米6 sagit 等 A-only 机型）。

纯标准库实现，不依赖 mkbootimg / magiskboot / lz4 等外部工具。

支持范围
--------
* boot header v0 / v1 / v2（v3 / v4 会明确报“暂不支持”）
* 分区布局：header(page 对齐) | kernel | ramdisk | second | recovery_dtbo | dtb
* ramdisk：gzip / xz / 未压缩(raw)；lz4、zstd 能识别但明确报错
* cpio newc（070701 / 070702）：目录、普通文件、符号链接、TRAILER!!!、4 字节对齐

设计约定
--------
* 所有对外函数都不抛异常（`list_ramdisk` 例外：它按签名返回 list，出错时抛
  `BootImageError`，上层 serve.py 已经用 try/except 包住；其余函数返回
  ``{"ok": False, "error": "中文原因"}``）。
* Windows 默认没有创建符号链接的权限（WinError 1314）。解包时若 os.symlink
  失败，会在 ``ramdisk/`` 下写一个“内容为目标路径”的占位普通文件，并把
  symlink 的真实 mode / inode / 目标路径记录进 header.json，repack 时仍按
  符号链接写回 cpio。这样在没有开发者模式的机器上也能完整往返。
* repack 输出的是“干净的 boot 镜像”（各段按 page_size 对齐，到最后一个段
  结束），不会把分区 dump 尾部的 50 MB 填充再写进去；`extra.bin` 仅用于查看。
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import struct
import time
from pathlib import Path

__all__ = [
    "inspect",
    "unpack",
    "list_ramdisk",
    "read_ramdisk_file",
    "write_ramdisk_file",
    "repack",
    "format_size",
    "BootImageError",
]

# ------------------------------------------------------------------ 常量
MAGIC = b"ANDROID!"
SPARSE_MAGIC = b"\x3a\xff\x26\xed"
BOOT_NAME_SIZE = 16
BOOT_ARGS_SIZE = 512
BOOT_EXTRA_ARGS_SIZE = 1024

V0_HEADER_SIZE = 1632
V1_HEADER_SIZE = 1648
V2_HEADER_SIZE = 1660
HEADER_SIZES = {0: V0_HEADER_SIZE, 1: V1_HEADER_SIZE, 2: V2_HEADER_SIZE}

CPIO_MAGICS = (b"070701", b"070702")
CPIO_HEADER_SIZE = 110

MIN_PAGE_SIZE = 2048
MAX_PAGE_SIZE = 131072
MAX_RAMDISK_RAW = 512 * 1024 * 1024
MAX_CPIO_ENTRIES = 200000
MAX_CPIO_NAME = 1024 * 1024
MAX_CPIO_TAIL = 1024 * 1024

CPIO_IFMT = 0o170000
CPIO_IFDIR = 0o040000
CPIO_IFREG = 0o100000
CPIO_IFLNK = 0o120000


class BootImageError(RuntimeError):
    """可读的中文错误。"""


# ------------------------------------------------------------------ 基础工具
def format_size(n: int) -> str:
    """把字节数格式化成 ``1.5 MB`` 这样的字符串（1024 进制）。"""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "0 B"
    if value <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _align(value: int, page: int) -> int:
    if page <= 0:
        raise BootImageError(f"page_size 非法：{page}")
    return (value + page - 1) // page * page


def _is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def _validate_page_size(page: int, version: int) -> None:
    try:
        page = int(page)
    except (TypeError, ValueError):
        raise BootImageError(f"page_size 非法：{page!r}")
    if page < MIN_PAGE_SIZE or page > MAX_PAGE_SIZE or not _is_power_of_two(page):
        raise BootImageError(f"page_size 非法：{page}（应为 2048/4096 等 2 的幂）")
    need = HEADER_SIZES.get(version, V0_HEADER_SIZE)
    if page < need:
        raise BootImageError(f"page_size={page} 太小，装不下 v{version} header（{need} 字节）")


def _u32(value, field: str) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise BootImageError(f"header 字段 {field} 不是整数：{value!r}")
    if value < 0 or value > 0xFFFFFFFF:
        raise BootImageError(f"header 字段 {field} 超出 32 位范围：{value}")
    return value


def _u64(value, field: str) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise BootImageError(f"header 字段 {field} 不是整数：{value!r}")
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise BootImageError(f"header 字段 {field} 超出 64 位范围：{value}")
    return value


def _decode_text(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    return raw.decode("utf-8", "replace").rstrip("\x00")


def _os_version_parts(raw: int) -> tuple[str, str]:
    if raw == 0:
        return "", ""
    a = (raw >> 25) & 0x7F
    b = (raw >> 18) & 0x7F
    c = (raw >> 11) & 0x7F
    year = 2000 + ((raw >> 4) & 0x7F)
    month = raw & 0x0F
    version = f"{a}.{b}.{c}"
    patch = f"{year:04d}-{month:02d}" if 1 <= month <= 12 else ""
    return version, patch


def _cmdline_value(cmdline: str, keys: tuple[str, ...]) -> str:
    for token in cmdline.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key in keys:
            return value
    return ""


def _detect_compression(data: bytes) -> str:
    if data.startswith(b"\x1f\x8b"):
        return "gzip"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    # lz4 frame magic 04 22 4d 18；legacy magic 02 21 4c 18
    if data.startswith(b"\x04\x22\x4d\x18") or data.startswith(b"\x02\x21\x4c\x18"):
        return "lz4"
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    return "raw"


def _decompress_ramdisk(data: bytes, compression: str) -> bytes:
    compression = (compression or "raw").lower()
    if compression == "lz4":
        raise BootImageError("lz4 压缩的 ramdisk 暂不支持")
    if compression == "zstd":
        raise BootImageError("zstd 压缩的 ramdisk 暂不支持")
    if compression == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                out = gz.read(MAX_RAMDISK_RAW + 1)
        except (OSError, EOFError, ValueError) as exc:
            raise BootImageError(f"gzip 解压 ramdisk 失败：{exc}")
        if len(out) > MAX_RAMDISK_RAW:
            raise BootImageError("ramdisk 解压后超过 512 MB，拒绝处理")
        return out
    if compression == "xz":
        try:
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            out = dec.decompress(data, max_length=MAX_RAMDISK_RAW + 1)
        except lzma.LZMAError as exc:
            raise BootImageError(f"xz 解压 ramdisk 失败：{exc}")
        if len(out) > MAX_RAMDISK_RAW:
            raise BootImageError("ramdisk 解压后超过 512 MB，拒绝处理")
        if not dec.eof:
            raise BootImageError("xz 压缩的 ramdisk 不完整（缺少结束标记）")
        return out
    if len(data) > MAX_RAMDISK_RAW:
        raise BootImageError("ramdisk 超过 512 MB，拒绝处理")
    return data


def _compress_ramdisk(data: bytes, compression: str) -> bytes:
    compression = (compression or "raw").lower()
    if compression == "gzip":
        # mtime=0 让输出可复现；压缩级别与原厂一致用 9
        return gzip.compress(data, compresslevel=9, mtime=0)
    if compression == "xz":
        return lzma.compress(
            data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=6
        )
    if compression == "raw":
        return data
    if compression == "lz4":
        raise BootImageError("lz4 压缩的 ramdisk 暂不支持")
    if compression == "zstd":
        raise BootImageError("zstd 压缩的 ramdisk 暂不支持")
    raise BootImageError(f"未知的 ramdisk 压缩方式：{compression}")


# ------------------------------------------------------------------ cpio
def _parse_cpio(data: bytes) -> tuple[list[dict], str, bytes, list[int]]:
    """解析 newc cpio，返回 (entries, magic, trailer 后的尾部字节, trailer 头字段)。"""
    entries: list[dict] = []
    pos = 0
    magic_used = ""
    trailer_tail = b""
    trailer_fields: list[int] = []
    total = len(data)

    while True:
        if len(entries) >= MAX_CPIO_ENTRIES:
            raise BootImageError(f"cpio 条目超过 {MAX_CPIO_ENTRIES} 个，拒绝处理")
        if pos + CPIO_HEADER_SIZE > total:
            raise BootImageError("cpio 数据在 TRAILER!!! 之前就结束了（缺少结束标记）")
        header = data[pos : pos + CPIO_HEADER_SIZE]
        magic = header[:6]
        if magic not in CPIO_MAGICS:
            if not entries:
                raise BootImageError(
                    "ramdisk 不是 cpio newc 格式（文件头 magic="
                    f"{magic!r}，应为 b'070701'）"
                )
            raise BootImageError(f"cpio 第 {len(entries) + 1} 个条目 magic 损坏：{magic!r}")
        if not magic_used:
            magic_used = magic.decode("ascii")

        fields = []
        try:
            for i in range(13):
                fields.append(int(header[6 + i * 8 : 14 + i * 8], 16))
        except ValueError:
            raise BootImageError(f"cpio 第 {len(entries) + 1} 个条目的头字段不是十六进制")
        (
            ino,
            mode,
            uid,
            gid,
            nlink,
            mtime,
            file_size,
            devmajor,
            devminor,
            rdevmajor,
            rdevminor,
            name_size,
            check,
        ) = fields

        if name_size <= 0:
            raise BootImageError(f"cpio 第 {len(entries) + 1} 个条目 name_size=0")
        if name_size > MAX_CPIO_NAME:
            raise BootImageError(f"cpio 条目名过长（{name_size} 字节），拒绝处理")
        name_start = pos + CPIO_HEADER_SIZE
        name_end = name_start + name_size
        if name_end > total:
            raise BootImageError("cpio 条目名越界（镜像可能被截断）")
        raw_name = data[name_start:name_end]
        if raw_name.endswith(b"\x00"):
            raw_name = raw_name[:-1]
        try:
            name = raw_name.decode("utf-8", "surrogateescape")
        except UnicodeDecodeError:
            raise BootImageError("cpio 条目名不是合法的 UTF-8")
        name = name.replace("\x00", "")

        data_start = _align(name_end, 4)
        if data_start > total:
            raise BootImageError("cpio 条目名后的对齐越界（镜像可能被截断）")
        if file_size > 0xFFFFFFFF:
            raise BootImageError(f"cpio 条目 {name!r} 超过 4 GB，暂不支持")
        data_end = data_start + file_size
        if data_end > total:
            raise BootImageError(
                f"cpio 条目 {name!r} 声明 {file_size} 字节，但数据越界（镜像可能被截断）"
            )

        if name == "TRAILER!!!":
            trailer_fields = [int(x) for x in fields]
            trailer_tail = data[_align(data_end, 4) :]
            if len(trailer_tail) > MAX_CPIO_TAIL:
                # 防止恶意 cpio 在 TRAILER 后塞几百 MB 垃圾把 header.json 撑爆
                trailer_tail = b""
            break

        kind = mode & CPIO_IFMT
        entry = {
            "name": name,
            "rawName": raw_name.decode("utf-8", "surrogateescape"),
            "size": int(file_size),
            "modeRaw": int(mode),
            "mode": f"{mode:06o}",
            "uid": int(uid),
            "gid": int(gid),
            "nlink": int(nlink),
            "mtime": int(mtime),
            "ino": int(ino),
            "devmajor": int(devmajor),
            "devminor": int(devminor),
            "rdevmajor": int(rdevmajor),
            "rdevminor": int(rdevminor),
            "check": int(check),
            "dir": kind == CPIO_IFDIR,
            "symlink": kind == CPIO_IFLNK,
            "data": b"",
            "linkname": "",
        }
        if kind == CPIO_IFLNK:
            link_bytes = data[data_start:data_end]
            entry["data"] = link_bytes
            entry["linkname"] = link_bytes.decode("utf-8", "replace")
            if len(link_bytes) > 65536:
                raise BootImageError(f"cpio 符号链接 {name!r} 的目标路径过长")
        elif kind == CPIO_IFREG:
            entry["data"] = data[data_start:data_end]
        elif kind == CPIO_IFDIR:
            if file_size:
                raise BootImageError(f"cpio 目录条目 {name!r} 的 size 不为 0")
        else:
            raise BootImageError(
                f"ramdisk 含特殊文件 {name!r}（mode={mode:06o}），暂不支持"
            )
        entries.append(entry)
        pos = _align(data_end, 4)

    if not magic_used:
        raise BootImageError("ramdisk 不是 cpio newc 格式")
    return entries, magic_used, trailer_tail, trailer_fields


def _cpio_manifest(entries: list[dict]) -> list[dict]:
    out = []
    for entry in entries:
        item = {k: v for k, v in entry.items() if k not in ("data", "dir", "symlink", "linkname", "rawName")}
        item["dir"] = bool(entry.get("dir"))
        item["symlink"] = bool(entry.get("symlink"))
        if entry.get("symlink"):
            item["linkname"] = entry.get("linkname", "")
            item["linkB64"] = base64.b64encode(entry.get("data", b"")).decode("ascii")
        if entry.get("rawName") != entry.get("name"):
            item["rawName"] = entry.get("rawName")
        out.append(item)
    return out


def _public_entry(entry: dict) -> dict:
    item = {
        "name": entry.get("name", ""),
        "size": int(entry.get("size", 0)),
        "mode": entry.get("mode", ""),
        "dir": bool(entry.get("dir")),
    }
    if entry.get("symlink"):
        item["symlink"] = True
        item["target"] = entry.get("linkname", "")
    return item


def _norm_rel(name: str, where: str = "ramdisk 条目") -> tuple[str, list[str]]:
    """把 cpio 里的路径规范成 Windows 安全的相对路径。"""
    if not isinstance(name, str):
        raise BootImageError(f"{where}名不是字符串")
    if "\x00" in name:
        raise BootImageError(f"{where}名含 NUL 字符")
    if "\\" in name:
        raise BootImageError(f"{where}名含反斜杠，Windows 无法安全展开：{name!r}")
    text = name
    if text.startswith("/"):
        raise BootImageError(f"{where}是绝对路径，拒绝处理：{name!r}")
    while text.startswith("./"):
        text = text[2:]
    if text in ("", "."):
        return "", []
    if text.endswith("/"):
        text = text[:-1]
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BootImageError(f"{where}路径不安全（含空段、. 或 ..）：{name!r}")
    for part in parts:
        if ":" in part:
            raise BootImageError(f"{where}名含冒号，Windows 无法安全展开：{name!r}")
        if _windows_bad_name(part):
            raise BootImageError(f"{where}名是 Windows 保留名/非法名：{name!r}")
    return text, parts


_WIN_RESERVED = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE
)


def _windows_bad_name(part: str) -> bool:
    if part.endswith(" ") or part.endswith("."):
        return True
    return bool(_WIN_RESERVED.match(part))


def _check_parent_chain(root: Path, parts: list[str]) -> Path:
    """逐级检查父目录不是符号链接，返回目标 Path。"""
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            if current.is_symlink():
                raise BootImageError(
                    f"ramdisk 路径经过符号链接，拒绝写入：{current.relative_to(root)}"
                )
        except OSError as exc:
            raise BootImageError(f"检查 ramdisk 路径失败：{exc}")
    target = current / parts[-1] if parts else root
    try:
        root_res = root.resolve()
        parent_res = target.parent.resolve()
    except OSError as exc:
        raise BootImageError(f"解析 ramdisk 路径失败：{exc}")
    if parent_res != root_res and root_res not in parent_res.parents:
        raise BootImageError("ramdisk 路径越界，拒绝访问")
    return target


def _safe_workdir_file(workdir: Path, rel: str) -> tuple[Path, str, list[str]]:
    root = workdir / "ramdisk"
    if not root.is_dir():
        raise BootImageError("工作目录里没有 ramdisk/（请先解包）")
    if not isinstance(rel, str) or not rel.strip():
        raise BootImageError("文件名不能为空")
    if "\x00" in rel:
        raise BootImageError("文件名含 NUL 字符")
    # Windows 上反斜杠也是分隔符，先统一再检查，挡 ..\..\x
    unified = rel.replace("\\", "/")
    if unified.startswith("/") or (len(unified) >= 2 and unified[1] == ":"):
        raise BootImageError("只允许 ramdisk 内的相对路径")
    key, parts = _norm_rel(unified, "文件名")
    if not parts:
        raise BootImageError("文件名不能为空")
    target = _check_parent_chain(root, parts)
    return target, key, parts


# ------------------------------------------------------------------ header
def _read_page(path: Path, page_size: int) -> bytes:
    try:
        with path.open("rb") as f:
            data = f.read(page_size)
    except OSError as exc:
        raise BootImageError(f"读取镜像失败：{exc}")
    if len(data) != page_size:
        raise BootImageError(
            f"镜像被截断：不足一个 header page（需要 {page_size} 字节，实际 {len(data)}）"
        )
    return data


def _hash_segments(handle, segments, sha) -> None:
    for offset, size, label in segments:
        if size <= 0:
            continue
        try:
            handle.seek(offset)
        except OSError as exc:
            raise BootImageError(f"定位 {label} 失败：{exc}")
        remain = size
        while remain > 0:
            chunk = handle.read(min(1024 * 1024, remain))
            if not chunk:
                raise BootImageError(f"{label} 数据不完整（镜像可能被截断）")
            sha.update(chunk)
            remain -= len(chunk)


def _detect_at(handle, offset: int, size: int) -> str:
    if size <= 0:
        return "raw"
    try:
        handle.seek(offset)
        head = handle.read(min(16, size))
    except OSError as exc:
        raise BootImageError(f"读取压缩魔数失败：{exc}")
    return _detect_compression(head)


def _inspect_impl(path: Path) -> dict:
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        raise BootImageError(f"镜像不存在：{p}")
    except OSError as exc:
        raise BootImageError(f"读取镜像失败：{exc}")
    if not p.is_file():
        raise BootImageError(f"不是文件：{p}")
    file_size = int(st.st_size)
    if file_size < 48:
        raise BootImageError(f"文件太小（{file_size} 字节），不是 Android boot 镜像")

    try:
        with p.open("rb") as f:
            head = f.read(4096)
    except OSError as exc:
        raise BootImageError(f"读取镜像失败：{exc}")

    if head.startswith(SPARSE_MAGIC):
        raise BootImageError(
            "这是 Android 稀疏镜像（sparse），暂不支持；请先用 simg2img 转成 raw 镜像"
        )
    if head[:8] != MAGIC:
        shown = head[:8].hex() if head[:8] else "(空)"
        raise BootImageError(f"不是 Android boot 镜像：magic={shown}，应为 ANDROID!")

    version = struct.unpack_from("<I", head, 40)[0]
    if version not in (0, 1, 2):
        alt = struct.unpack_from("<I", head, 44)[0] if len(head) >= 48 else -1
        if alt in (3, 4):
            raise BootImageError(
                f"暂不支持 boot header v{alt}（小米6 是 v0/v1/v2）"
            )
        raise BootImageError(f"不支持的 boot header 版本：{version}")

    page_size = struct.unpack_from("<I", head, 36)[0]
    if (
        page_size < MIN_PAGE_SIZE
        or page_size > MAX_PAGE_SIZE
        or not _is_power_of_two(page_size)
    ):
        alt = struct.unpack_from("<I", head, 44)[0] if len(head) >= 48 else -1
        hs = struct.unpack_from("<I", head, 24)[0] if len(head) >= 28 else -1
        if alt in (3, 4) or hs in (1580, 1584):
            if alt in (3, 4):
                raise BootImageError(f"暂不支持 boot header v{alt}")
            raise BootImageError("暂不支持 boot header v3/v4")
        raise BootImageError(
            f"page_size 非法：{page_size}（应为 2048/4096 等 2 的幂）"
        )
    _validate_page_size(page_size, version)

    page = _read_page(p, page_size)

    kernel_size, kernel_addr = struct.unpack_from("<II", page, 8)
    ramdisk_size, ramdisk_addr = struct.unpack_from("<II", page, 16)
    second_size, second_addr = struct.unpack_from("<II", page, 24)
    tags_addr = struct.unpack_from("<I", page, 32)[0]
    os_version = struct.unpack_from("<I", page, 44)[0]
    name = _decode_text(page[48:64])
    cmdline = _decode_text(page[64:576])
    stored_id = page[576:596]
    extra_cmdline = _decode_text(page[608:1632])

    recovery_size = 0
    recovery_offset_field = 0
    header_size_field = 0
    dtb_size = 0
    dtb_addr = 0
    if version >= 1:
        recovery_size, recovery_offset_field, header_size_field = struct.unpack_from(
            "<IQI", page, 1632
        )
    if version >= 2:
        dtb_size, dtb_addr = struct.unpack_from("<IQ", page, 1648)

    canonical = HEADER_SIZES[version]
    header_size = canonical
    if version >= 1 and header_size_field:
        # 以盘上字段为准（兼容个别工具把 v2 header_size 写成 1648 的镜像）；
        # headerStructSize 始终给出 AOSP 的结构体大小（v2=1660）。
        header_size = int(header_size_field)

    kernel_offset = page_size
    kernel_end = kernel_offset + kernel_size
    ramdisk_offset = _align(kernel_end, page_size)
    ramdisk_end = ramdisk_offset + ramdisk_size
    second_offset = _align(ramdisk_end, page_size)
    second_end = second_offset + second_size

    if version >= 1:
        recovery_offset = _align(second_end, page_size)
        recovery_note = ""
        if (
            recovery_size > 0
            and recovery_offset_field
            and recovery_offset_field >= page_size
            and recovery_offset_field + recovery_size <= file_size
        ):
            if recovery_offset_field != recovery_offset:
                recovery_note = (
                    f"recovery_dtbo_offset 字段={recovery_offset_field}，"
                    f"按 page 对齐应为 {recovery_offset}，已按字段读取"
                )
            recovery_offset = int(recovery_offset_field)
        recovery_end = recovery_offset + recovery_size
        dtb_offset = _align(recovery_end, page_size)
    else:
        recovery_offset = 0
        recovery_size = 0
        recovery_offset_field = 0
        recovery_end = 0
        recovery_note = ""
        dtb_offset = 0

    if version == 2:
        dtb_end = dtb_offset + dtb_size
    else:
        dtb_size = 0
        dtb_addr = 0
        dtb_offset = 0
        dtb_end = 0

    for label, offset, size in (
        ("kernel", kernel_offset, kernel_size),
        ("ramdisk", ramdisk_offset, ramdisk_size),
        ("second", second_offset, second_size),
        ("recovery_dtbo", recovery_offset, recovery_size),
        ("dtb", dtb_offset, dtb_size),
    ):
        if size <= 0:
            continue
        if offset < page_size or offset + size > file_size:
            raise BootImageError(
                f"{label} 段越界：offset={offset} size={size} 文件大小={file_size}，"
                "镜像可能被截断或 header 损坏"
            )

    last_end = max(kernel_end, ramdisk_end, second_end, recovery_end, dtb_end)
    image_size = _align(last_end, page_size)

    with p.open("rb") as f:
        kernel_comp = _detect_at(f, kernel_offset, kernel_size)
        ramdisk_comp = _detect_at(f, ramdisk_offset, ramdisk_size)
        sha = hashlib.sha1()
        _hash_segments(
            f,
            [
                (kernel_offset, kernel_size, "kernel"),
                (ramdisk_offset, ramdisk_size, "ramdisk"),
                (second_offset, second_size, "second"),
                (recovery_offset, recovery_size, "recovery_dtbo"),
                (dtb_offset, dtb_size, "dtb"),
            ],
            sha,
        )
    sha1 = sha.hexdigest()
    id_hex = stored_id.hex()

    extra_offset = image_size
    if extra_offset <= file_size:
        extra_size = file_size - extra_offset
        padding_incomplete = False
    else:
        extra_offset = file_size
        extra_size = 0
        padding_incomplete = True

    os_ver, os_patch = _os_version_parts(os_version)
    hardware = _cmdline_value(cmdline + " " + extra_cmdline, ("androidboot.hardware",))
    board = _cmdline_value(
        cmdline + " " + extra_cmdline, ("androidboot.board", "board")
    ) or hardware
    id_matches = stored_id == sha.digest()

    notes: list[str] = []
    if version == 0:
        notes.append("boot header v0（无 recovery_dtbo/dtb/header_size 字段）")
    elif version == 1:
        notes.append("boot header v1（含 recovery_dtbo）")
    else:
        notes.append("boot header v2（含 recovery_dtbo + dtb）")
    if header_size_field and header_size_field != canonical:
        notes.append(
            f"header_size 字段={header_size_field}（v{version} 标准为 {canonical}）"
        )
    if recovery_note:
        notes.append(recovery_note)
    if ramdisk_comp == "lz4":
        notes.append("ramdisk 为 lz4 压缩；lz4 压缩的 ramdisk 暂不支持解包")
    elif ramdisk_comp == "zstd":
        notes.append("ramdisk 为 zstd 压缩；zstd 压缩的 ramdisk 暂不支持解包")
    elif ramdisk_comp == "raw":
        notes.append("ramdisk 未识别出 gzip/xz 魔数，按 raw 处理")
    if kernel_comp not in ("raw", "gzip", "xz", "lz4", "zstd"):
        notes.append(f"kernel 压缩：{kernel_comp}")
    elif kernel_comp != "raw":
        notes.append(f"kernel 压缩：{kernel_comp}")
    if extra_size > 0:
        notes.append(
            f"尾部有 {format_size(extra_size)} 多余字节（分区填充，repack 时会丢弃）"
        )
    if padding_incomplete:
        notes.append("镜像末尾的 page 对齐填充不完整")
    if not id_matches:
        if not any(stored_id):
            notes.append("header 的 id 字段全为 0（未写入 SHA1）")
        else:
            notes.append("header 的 id 与实测 SHA1 不一致（镜像可能被改过或厂商未更新 id）")

    info = {
        "ok": True,
        "format": "boot",
        "magic": "ANDROID!",
        "headerVersion": int(version),
        "pageSize": int(page_size),
        "headerSize": int(header_size),
        "headerSizeField": int(header_size_field),
        "headerStructSize": int(canonical),
        "kernel": {
            "offset": int(kernel_offset),
            "size": int(kernel_size),
            "addr": int(kernel_addr),
            "compression": kernel_comp,
            "present": kernel_size > 0,
        },
        "ramdisk": {
            "offset": int(ramdisk_offset),
            "size": int(ramdisk_size),
            "addr": int(ramdisk_addr),
            "compression": ramdisk_comp,
            "present": ramdisk_size > 0,
        },
        "second": {
            "offset": int(second_offset),
            "size": int(second_size),
            "addr": int(second_addr),
            "present": second_size > 0,
        },
        "recoveryDtbo": {
            "offset": int(recovery_offset),
            "size": int(recovery_size),
            "offsetField": int(recovery_offset_field),
            "present": recovery_size > 0,
        },
        "dtb": {
            "offset": int(dtb_offset),
            "size": int(dtb_size),
            "addr": int(dtb_addr),
            "present": dtb_size > 0,
        },
        "cmdline": cmdline,
        "extraCmdline": extra_cmdline,
        "board": board,
        "hardware": hardware,
        "osVersion": os_ver,
        "osPatchLevel": os_patch,
        "osVersionRaw": int(os_version),
        "name": name,
        "tagsAddr": int(tags_addr),
        "id": id_hex,
        "sha1": sha1,
        "idMatches": bool(id_matches),
        "totalSize": int(file_size),
        "imageSize": int(image_size),
        "extra": {
            "offset": int(extra_offset),
            "size": int(extra_size),
            "paddingIncomplete": bool(padding_incomplete),
        },
        "note": "；".join(notes),
    }
    return info


def inspect(path: Path) -> dict:
    """只读 header 与各段魔数，不解包 ramdisk。

    成功返回 dict（字段见模块说明）；失败返回 ``{"ok": False, "error": 中文}``。
    """
    try:
        return _inspect_impl(Path(path))
    except BootImageError as exc:
        return {"ok": False, "error": str(exc)}
    except FileNotFoundError:
        return {"ok": False, "error": f"镜像不存在：{path}"}
    except OSError as exc:
        return {"ok": False, "error": f"读取镜像失败：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"解析镜像失败：{type(exc).__name__}: {exc}"}


def sniff(path: Path) -> dict:
    """只看文件头，判断这个 .img 到底是什么。

    分区镜像不都是 boot 镜像：persist/misc/cust 常常是 ext4 或者全 0，
    modem 是 FAT，fsg/modemst 是高通私有格式。列表里先把类型标出来，
    用户就不会点进去才发现"不是 Android boot 镜像"。

    返回 ``{"ok": True, "kind": ..., "magic": "十六进制前 8 字节", "size": int, "note": 中文}``，
    kind ∈ boot / sparse / ext4 / fat / gzip / xz / lz4 / zero / empty / unknown。
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        return {"ok": False, "error": f"读不到文件：{exc}"}
    if size <= 0:
        return {"ok": True, "kind": "empty", "magic": "", "size": 0, "note": "空文件（0 字节）"}

    try:
        with p.open("rb") as f:
            head = f.read(4096)
            if size > 4096:  # 抽中段和尾部，判断"整块都是 0"更靠谱
                f.seek(max(0, size // 2 - 2048))
                mid = f.read(4096)
                f.seek(max(0, size - 4096))
                tail = f.read(4096)
            else:
                mid = tail = b""
    except OSError as exc:
        return {"ok": False, "error": f"读不到文件：{exc}"}

    magic = head[:8].hex().upper()
    base = {"ok": True, "magic": magic, "size": size}

    if head.startswith(MAGIC):
        return {**base, "kind": "boot", "note": "Android boot 镜像，可以解包"}
    if head.startswith(SPARSE_MAGIC):
        return {**base, "kind": "sparse", "note": "Android 稀疏镜像，要先 simg2img 转成 raw 才能解包"}
    if len(head) >= 1082 and head[1080:1082] == b"\x53\xef":
        return {**base, "kind": "ext4", "note": "ext4 文件系统分区镜像，不是 boot 镜像（没有 ramdisk 可解）"}
    if head[:1] == b"\xeb" and len(head) > 3 and head[2:3] == b"\x90":
        return {**base, "kind": "fat", "note": "FAT 文件系统分区镜像（常见于 modem 分区），不是 boot 镜像"}
    if head[:2] == b"\x1f\x8b":
        return {**base, "kind": "gzip", "note": "gzip 压缩过的镜像，不是 boot 镜像"}
    if head.startswith(b"\xfd7zXZ\x00"):
        return {**base, "kind": "xz", "note": "xz 压缩过的镜像，不是 boot 镜像"}
    if head[:4] in (b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18"):
        return {**base, "kind": "lz4", "note": "lz4 压缩过的镜像，不是 boot 镜像"}
    if head and not head.strip(b"\x00") and not mid.strip(b"\x00") and not tail.strip(b"\x00"):
        return {**base, "kind": "zero", "note": f"整片都是 0（{format_size(size)}），这个分区应该是空的"}
    return {**base, "kind": "unknown",
            "note": f"不是 Android boot 镜像（开头是 {head[:4].hex().upper() or '空'}）"}


# ------------------------------------------------------------------ 解包
def _copy_range(src: Path, offset: int, size: int, dst: Path) -> None:
    try:
        with src.open("rb") as f, dst.open("wb") as out:
            f.seek(offset)
            remain = size
            while remain > 0:
                chunk = f.read(min(1024 * 1024, remain))
                if not chunk:
                    raise BootImageError(f"读取 {src.name} 时数据提前结束（镜像被截断）")
                out.write(chunk)
                remain -= len(chunk)
    except BootImageError:
        raise
    except OSError as exc:
        raise BootImageError(f"写出 {dst.name} 失败：{exc}")


def _extract_cpio(root: Path, entries: list[dict]) -> tuple[int, list[str]]:
    """把 cpio 条目展开到 root，返回 (符号链接占位数量, 警告列表)。"""
    root.mkdir(parents=True, exist_ok=True)
    fallback = 0
    warnings: list[str] = []

    for entry in entries:
        name = entry.get("name", "")
        key, parts = _norm_rel(name)
        if not parts:
            continue
        target = _check_parent_chain(root, parts)
        parent = target.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BootImageError(f"创建 ramdisk 目录失败：{exc}")

        mode = int(entry.get("modeRaw", 0))
        mtime = int(entry.get("mtime", 0)) & 0xFFFFFFFF
        try:
            if entry.get("dir"):
                if target.exists() and not target.is_dir():
                    raise BootImageError(f"cpio 目录条目与已有文件冲突：{key}")
                target.mkdir(exist_ok=True)
            elif entry.get("symlink"):
                link_bytes = entry.get("data", b"")
                try:
                    link_text = link_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    link_text = link_bytes.decode("utf-8", "surrogateescape")
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        raise BootImageError(f"cpio 符号链接与已有目录冲突：{key}")
                    target.unlink()
                try:
                    os.symlink(link_text, target)
                except (OSError, NotImplementedError):
                    # Windows 默认没有符号链接权限：写占位文件，header.json 里
                    # 仍然保留 symlink 元数据，repack 会还原。
                    with target.open("wb") as fh:
                        fh.write(link_bytes)
                    fallback += 1
            else:
                if target.exists() and target.is_dir():
                    raise BootImageError(f"cpio 文件与已有目录冲突：{key}")
                if target.is_symlink():
                    target.unlink()
                with target.open("wb") as fh:
                    fh.write(entry.get("data", b""))
            if os.name != "nt" and not entry.get("symlink"):
                # Windows 的 chmod 只会改只读位，而且对符号链接会作用到目标，
                # 这里跳过；mode 已经写进 header.json，repack 时原样使用。
                try:
                    os.chmod(target, mode & 0o7777)
                except OSError:
                    pass
            try:
                os.utime(target, (mtime, mtime), follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
        except BootImageError:
            raise
        except OSError as exc:
            raise BootImageError(f"展开 cpio 条目 {key!r} 失败：{exc}")

    # 目录 mtime 会被随后创建的子文件改掉，最后再补一次（失败无所谓）
    for entry in reversed(entries):
        if not entry.get("dir"):
            continue
        key, parts = _norm_rel(entry.get("name", ""))
        if not parts:
            continue
        target = root.joinpath(*parts)
        if target.is_dir():
            try:
                mtime = int(entry.get("mtime", 0)) & 0xFFFFFFFF
                os.utime(target, (mtime, mtime))
            except OSError:
                pass

    if fallback:
        warnings.append(
            f"Windows 无符号链接权限，{fallback} 个符号链接以占位文件保存；"
            "header.json 已记录 linkname，repack 仍会写回符号链接"
        )
    return fallback, warnings


def _clean_unpack_dir(dest: Path) -> None:
    for name in (
        "header.json",
        "kernel",
        "ramdisk.cpio",
        "second",
        "dtb",
        "recovery_dtbo",
        "extra.bin",
    ):
        p = dest / name
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    ramdisk = dest / "ramdisk"
    if ramdisk.is_dir() and not ramdisk.is_symlink():
        shutil.rmtree(ramdisk)
    elif ramdisk.exists() or ramdisk.is_symlink():
        ramdisk.unlink()


def unpack(path: Path, dest: Path) -> dict:
    """解包 boot.img 到 ``dest``。

    产物：header.json、kernel、ramdisk.cpio、ramdisk/、second、dtb、
    recovery_dtbo、extra.bin（存在时）。失败返回中文 error。
    """
    try:
        src = Path(path)
        out_dir = Path(dest)
        info = _inspect_impl(src)
        compression = info["ramdisk"].get("compression", "raw")
        if compression in ("lz4", "zstd"):
            return {
                "ok": False,
                "error": f"{compression} 压缩的 ramdisk 暂不支持",
                "info": info,
            }
        if info["ramdisk"]["size"] <= 0:
            raise BootImageError("ramdisk 大小为 0，无法解包")

        with src.open("rb") as f:
            f.seek(info["ramdisk"]["offset"])
            ramdisk_compressed = f.read(info["ramdisk"]["size"])
            if len(ramdisk_compressed) != info["ramdisk"]["size"]:
                raise BootImageError("读取 ramdisk 失败：数据不完整")
            f.seek(0)
            header_page = f.read(info["pageSize"])

        raw_cpio = _decompress_ramdisk(ramdisk_compressed, compression)
        entries, cpio_format, cpio_tail, trailer_fields = _parse_cpio(raw_cpio)

        # 先全部解析成功，再动 dest，避免坏镜像留一堆半成品
        if out_dir.exists() and not out_dir.is_dir():
            raise BootImageError(f"解包目标不是目录：{out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
        _clean_unpack_dir(out_dir)

        _copy_range(
            src,
            info["kernel"]["offset"],
            info["kernel"]["size"],
            out_dir / "kernel",
        )
        (out_dir / "ramdisk.cpio").write_bytes(raw_cpio)

        written: list[str] = ["kernel", "ramdisk.cpio"]
        for key, filename in (
            ("second", "second"),
            ("recoveryDtbo", "recovery_dtbo"),
            ("dtb", "dtb"),
        ):
            seg = info[key]
            if seg.get("size", 0) > 0:
                _copy_range(src, seg["offset"], seg["size"], out_dir / filename)
                written.append(filename)

        extra_size = info["extra"]["size"]
        if extra_size > 0:
            _copy_range(
                src,
                info["extra"]["offset"],
                extra_size,
                out_dir / "extra.bin",
            )
            written.append("extra.bin")

        fallback, warnings = _extract_cpio(out_dir / "ramdisk", entries)
        header_note = info.get("note", "")
        if fallback and warnings:
            header_note = "；".join([x for x in (header_note, *warnings) if x])

        header_json = copy.deepcopy(info)
        header_json.pop("ok", None)
        header_json["headerB64"] = base64.b64encode(header_page).decode("ascii")
        header_json["headerPageSize"] = int(info["pageSize"])
        header_json["headerStructSize"] = int(HEADER_SIZES[info["headerVersion"]])
        header_json["ramdiskCompression"] = compression
        header_json["ramdisk"]["uncompressedSize"] = len(raw_cpio)
        header_json["cpioFormat"] = cpio_format
        header_json["cpioEntries"] = _cpio_manifest(entries)
        header_json["cpioTrailer"] = trailer_fields
        header_json["cpioTailB64"] = base64.b64encode(cpio_tail).decode("ascii")
        header_json["sourcePath"] = str(src)
        header_json["sourceSize"] = int(info["totalSize"])
        header_json["unpackedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        header_json["symlinkFallback"] = int(fallback)
        header_json["note"] = header_note
        (out_dir / "header.json").write_text(
            json.dumps(header_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        files = []
        for name in (
            "kernel",
            "ramdisk.cpio",
            "second",
            "dtb",
            "recovery_dtbo",
            "extra.bin",
            "header.json",
        ):
            p = out_dir / name
            if p.is_file():
                files.append({"name": name, "size": int(p.stat().st_size)})
        public_entries = [_public_entry(e) for e in entries]

        return {
            "ok": True,
            "dir": str(out_dir),
            "info": info,
            "files": files,
            "entries": public_entries,
        }
    except BootImageError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"解包失败（文件系统错误）：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"解包失败：{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ 工作目录 API
def _load_header(workdir: Path) -> dict:
    wd = Path(workdir)
    if not wd.is_dir():
        raise BootImageError(f"工作目录不存在：{wd}")
    path = wd / "header.json"
    if not path.is_file():
        raise BootImageError("工作目录缺少 header.json（请先解包）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BootImageError(f"读取 header.json 失败：{exc}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BootImageError(f"header.json 损坏：{exc}")
    if not isinstance(data, dict):
        raise BootImageError("header.json 内容不是对象")
    return data


def _manifest_map(header: dict) -> dict[str, dict]:
    items = header.get("cpioEntries")
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            key, _ = _norm_rel(str(item.get("name", "")), "header.json 条目")
        except BootImageError:
            continue
        if key and key not in out:
            out[key] = item
    return out


def _walk_tree(root: Path) -> list[dict]:
    """返回 ramdisk 下所有条目（不跟随符号链接）。"""
    result: list[dict] = []

    def walk(dirpath: Path, prefix: str) -> None:
        try:
            with os.scandir(dirpath) as it:
                items = sorted(it, key=lambda e: e.name.casefold())
        except OSError as exc:
            raise BootImageError(f"遍历 ramdisk 目录失败：{exc}")
        for item in items:
            rel = f"{prefix}{item.name}"
            try:
                if item.is_symlink():
                    result.append({"rel": rel, "path": Path(item.path), "type": "symlink"})
                elif item.is_dir(follow_symlinks=False):
                    result.append({"rel": rel, "path": Path(item.path), "type": "dir"})
                    walk(Path(item.path), rel + "/")
                elif item.is_file(follow_symlinks=False):
                    result.append({"rel": rel, "path": Path(item.path), "type": "file"})
                else:
                    result.append({"rel": rel, "path": Path(item.path), "type": "other"})
            except OSError as exc:
                raise BootImageError(f"读取 ramdisk 条目 {rel!r} 失败：{exc}")

    walk(root, "")
    return result


def list_ramdisk(workdir: Path) -> list[dict]:
    """列出 ``workdir/ramdisk/`` 下的全部条目（扁平列表）。

    每项：``{"name","size","dir","mode","mtime"}``；符号链接额外带
    ``symlink`` / ``target``。工作目录无效时抛 :class:`BootImageError`。
    """
    try:
        wd = Path(workdir)
        header = _load_header(wd)
        root = wd / "ramdisk"
        if not root.is_dir():
            raise BootImageError("工作目录里没有 ramdisk/（请先解包）")
        manifest = _manifest_map(header)
        entries: list[dict] = []
        for item in _walk_tree(root):
            rel = item["rel"]
            path = item["path"]
            meta = manifest.get(rel)
            is_link = item["type"] == "symlink" or bool(meta and meta.get("symlink"))
            if is_link:
                if item["type"] == "symlink":
                    try:
                        target = os.readlink(path)
                    except OSError:
                        target = str(meta.get("linkname", "")) if meta else ""
                else:
                    target = str(meta.get("linkname", "")) if meta else ""
                size = len(target.encode("utf-8", "surrogateescape"))
                mode = (
                    str(meta.get("mode"))
                    if meta and meta.get("mode")
                    else "120777"
                )
                mtime = (
                    int(meta.get("mtime", 0))
                    if meta and "mtime" in meta
                    else int(os.lstat(path).st_mtime)
                )
                entries.append(
                    {
                        "name": rel,
                        "size": size,
                        "dir": False,
                        "mode": mode,
                        "mtime": mtime,
                        "symlink": True,
                        "target": target,
                    }
                )
                continue
            try:
                st = path.stat()
            except OSError as exc:
                raise BootImageError(f"读取 ramdisk 条目 {rel!r} 失败：{exc}")
            is_dir = item["type"] == "dir"
            if is_dir:
                size = 0
                default_mode = "040755"
            else:
                size = int(st.st_size)
                default_mode = "100644"
            mode = str(meta.get("mode")) if meta and meta.get("mode") else default_mode
            mtime = (
                int(meta.get("mtime", 0))
                if meta and "mtime" in meta
                else int(st.st_mtime)
            )
            entries.append(
                {
                    "name": rel,
                    "size": size,
                    "dir": bool(is_dir),
                    "mode": mode,
                    "mtime": mtime,
                }
            )
        entries.sort(key=lambda e: e["name"].casefold())
        return entries
    except BootImageError:
        raise
    except OSError as exc:
        raise BootImageError(f"列出 ramdisk 失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        raise BootImageError(f"列出 ramdisk 失败：{type(exc).__name__}: {exc}")


def read_ramdisk_file(
    workdir: Path, rel: str, max_bytes: int = 262144
) -> dict:
    """读取 ramdisk 里的文件；过大或二进制时 ``binary=True`` 且 text 截断。"""
    try:
        wd = Path(workdir)
        header = _load_header(wd)
        manifest = _manifest_map(header)
        target, key, _ = _safe_workdir_file(wd, rel)
        meta = manifest.get(key)
        if meta and meta.get("symlink"):
            raise BootImageError(
                f"{key} 是符号链接（→ {meta.get('linkname', '')}），不支持直接读取/编辑"
            )
        if target.is_symlink():
            raise BootImageError(f"{key} 是符号链接，不支持直接读取")
        if target.is_dir():
            raise BootImageError(f"{key} 是目录，不是文件")
        if not target.is_file():
            raise BootImageError(f"文件不存在：{key}")
        try:
            size = int(target.stat().st_size)
            limit = max(0, int(max_bytes))
        except (OSError, TypeError, ValueError) as exc:
            raise BootImageError(f"读取文件信息失败：{exc}")
        try:
            with target.open("rb") as f:
                data = f.read(limit)
        except OSError as exc:
            raise BootImageError(f"读取文件失败：{exc}")
        binary = size > limit or b"\x00" in data
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            binary = True
            text = data.decode("utf-8", "replace")
        return {
            "ok": True,
            "text": text,
            "size": size,
            "binary": bool(binary),
            "truncated": bool(size > limit),
        }
    except BootImageError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"读取失败：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读取失败：{type(exc).__name__}: {exc}"}


def write_ramdisk_file(
    workdir: Path, rel: str, text: str, encoding: str = "utf-8"
) -> dict:
    """把文本写回 ``workdir/ramdisk/rel``，挡 ``../`` 逃逸与符号链接。"""
    try:
        wd = Path(workdir)
        header = _load_header(wd)
        manifest = _manifest_map(header)
        if not isinstance(text, str):
            raise BootImageError("text 必须是字符串")
        target, key, _ = _safe_workdir_file(wd, rel)
        meta = manifest.get(key)
        if meta and meta.get("symlink"):
            raise BootImageError(
                f"{key} 是符号链接（→ {meta.get('linkname', '')}），不能当普通文件写"
            )
        if target.is_symlink():
            raise BootImageError(f"{key} 是符号链接，不能当普通文件写")
        if target.exists() and target.is_dir():
            raise BootImageError(f"{key} 是目录，不能写文件")
        try:
            data = text.encode(encoding or "utf-8")
        except LookupError:
            raise BootImageError(f"不支持的编码：{encoding!r}")
        except UnicodeEncodeError as exc:
            raise BootImageError(f"文本无法用 {encoding} 编码：{exc}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            raise BootImageError(f"写入文件失败：{exc}")
        return {"ok": True, "size": len(data)}
    except BootImageError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ 重打包
def _cpio_pad4(data: bytes) -> bytes:
    rem = len(data) % 4
    return data if rem == 0 else data + b"\x00" * (4 - rem)


def _build_cpio(root: Path, header: dict) -> bytes:
    fs_items = _walk_tree(root)
    fs_map = {item["rel"]: item for item in fs_items}
    manifest = header.get("cpioEntries")
    if not isinstance(manifest, list):
        manifest = []

    ordered: list[tuple[str, dict | None, dict]] = []
    seen: set[str] = set()
    for raw in manifest:
        if not isinstance(raw, dict):
            continue
        try:
            key, _ = _norm_rel(str(raw.get("name", "")), "header.json 条目")
        except BootImageError as exc:
            raise BootImageError(f"header.json 损坏：{exc}")
        if not key or key in seen or key not in fs_map:
            continue
        ordered.append((key, raw, fs_map[key]))
        seen.add(key)
    for item in fs_items:
        if item["rel"] in seen:
            continue
        ordered.append((item["rel"], None, item))
        seen.add(item["rel"])

    if not ordered:
        raise BootImageError("ramdisk/ 目录是空的，拒绝生成空 cpio")

    out = bytearray()
    next_ino = 1
    for raw in manifest:
        if isinstance(raw, dict):
            try:
                next_ino = max(next_ino, int(raw.get("ino", 0)) + 1)
            except (TypeError, ValueError):
                pass
    for key, meta, fs_item in ordered:
        path: Path = fs_item["path"]
        kind = fs_item["type"]
        if meta and meta.get("symlink"):
            kind = "symlink"
        if meta and meta.get("dir") and kind == "dir":
            kind = "dir"

        if kind == "dir":
            data = b""
            default_mode = CPIO_IFDIR | 0o755
        elif kind == "symlink":
            if meta and meta.get("linkB64"):
                try:
                    data = base64.b64decode(str(meta.get("linkB64")), validate=True)
                except (ValueError, TypeError):
                    raise BootImageError(f"header.json 中符号链接 {key!r} 的目标数据损坏")
            elif meta and meta.get("linkname"):
                data = str(meta.get("linkname")).encode("utf-8")
            else:
                try:
                    data = os.fsencode(os.readlink(path))
                except OSError as exc:
                    raise BootImageError(f"读取符号链接 {key!r} 失败：{exc}")
            default_mode = CPIO_IFLNK | 0o777
        elif kind == "file":
            try:
                file_size = int(path.stat().st_size)
                if file_size > 0xFFFFFFFF:
                    raise BootImageError(f"ramdisk 文件 {key!r} 超过 4 GB，暂不支持")
                if file_size > MAX_RAMDISK_RAW:
                    raise BootImageError(
                        f"ramdisk 文件 {key!r} 超过 512 MB，拒绝处理"
                    )
                data = path.read_bytes()
            except BootImageError:
                raise
            except OSError as exc:
                raise BootImageError(f"读取 ramdisk 文件 {key!r} 失败：{exc}")
            default_mode = CPIO_IFREG | 0o644
        else:
            raise BootImageError(f"ramdisk 中有不支持的特殊文件：{key!r}")

        if len(data) > 0xFFFFFFFF:
            raise BootImageError(f"ramdisk 文件 {key!r} 超过 4 GB，暂不支持")

        base_mode = 0
        if meta and meta.get("modeRaw") is not None:
            try:
                base_mode = int(meta.get("modeRaw"))
            except (TypeError, ValueError):
                base_mode = 0
        if base_mode:
            mode = base_mode
            if (mode & CPIO_IFMT) != (default_mode & CPIO_IFMT):
                mode = (default_mode & CPIO_IFMT) | (mode & 0o7777)
        else:
            mode = default_mode

        if meta and "uid" in meta:
            uid = int(meta.get("uid", 0))
            gid = int(meta.get("gid", 0))
            mtime = int(meta.get("mtime", 0))
            ino = int(meta.get("ino", 0))
            nlink = int(meta.get("nlink", 1))
            devmajor = int(meta.get("devmajor", 0))
            devminor = int(meta.get("devminor", 0))
            rdevmajor = int(meta.get("rdevmajor", 0))
            rdevminor = int(meta.get("rdevminor", 0))
            check = int(meta.get("check", 0))
        else:
            ino = next_ino
            next_ino += 1
            uid, gid = 0, 0
            try:
                mtime = int(path.stat().st_mtime)
            except OSError:
                mtime = 0
            nlink = 2 if kind == "dir" else 1
            devmajor = devminor = rdevmajor = rdevminor = 0
            check = 0

        name = str(meta.get("rawName") or meta.get("name")) if meta else key
        if name != key:
            # 原始 name 可能带 "./" 前缀，保留它；但必须仍解析到同一路径
            try:
                nkey, _ = _norm_rel(name, "cpio 条目")
            except BootImageError:
                nkey = ""
            if nkey != key:
                name = key
        try:
            name_bytes = name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BootImageError(f"文件名无法编码为 UTF-8：{key!r}（{exc}）")
        if b"\x00" in name_bytes:
            raise BootImageError(f"文件名含 NUL：{key!r}")
        if len(name_bytes) > 0xFFFFFFFF - 1:
            raise BootImageError(f"文件名过长：{key!r}")

        fields = (
            ino & 0xFFFFFFFF,
            mode & 0xFFFFFFFF,
            uid & 0xFFFFFFFF,
            gid & 0xFFFFFFFF,
            max(1, nlink) & 0xFFFFFFFF,
            mtime & 0xFFFFFFFF,
            len(data),
            devmajor & 0xFFFFFFFF,
            devminor & 0xFFFFFFFF,
            rdevmajor & 0xFFFFFFFF,
            rdevminor & 0xFFFFFFFF,
            len(name_bytes) + 1,
            check & 0xFFFFFFFF,
        )
        out += b"070701"
        out += b"".join(f"{value:08x}".encode("ascii") for value in fields)
        out += name_bytes + b"\x00"
        out = bytearray(_cpio_pad4(bytes(out)))
        out += data
        out = bytearray(_cpio_pad4(bytes(out)))

    trailer = b"TRAILER!!!\x00"
    namesize = len(trailer)
    default_trailer = (0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, namesize, 0)
    saved_trailer = header.get("cpioTrailer")
    if isinstance(saved_trailer, list) and len(saved_trailer) == 13:
        try:
            vals = tuple(int(x) & 0xFFFFFFFF for x in saved_trailer)
            vals = vals[:11] + (namesize,) + vals[12:]
        except (TypeError, ValueError):
            vals = default_trailer
    else:
        vals = default_trailer
    out += b"070701"
    out += b"".join(f"{value:08x}".encode("ascii") for value in vals)
    out += trailer
    out = bytearray(_cpio_pad4(bytes(out)))

    tail_b64 = header.get("cpioTailB64")
    if isinstance(tail_b64, str) and tail_b64:
        try:
            tail = base64.b64decode(tail_b64, validate=True)
        except (ValueError, TypeError):
            tail = b""
        if len(tail) <= MAX_CPIO_TAIL:
            out += tail
    return bytes(out)


def _header_values(header: dict, version: int, page_size: int) -> dict:
    kernel = header.get("kernel") or {}
    ramdisk = header.get("ramdisk") or {}
    second = header.get("second") or {}
    recovery = header.get("recoveryDtbo") or {}
    dtb = header.get("dtb") or {}
    return {
        "kernel_addr": _u32(kernel.get("addr", 0), "kernel.addr"),
        "ramdisk_addr": _u32(ramdisk.get("addr", 0), "ramdisk.addr"),
        "second_addr": _u32(second.get("addr", 0), "second.addr"),
        "tags_addr": _u32(header.get("tagsAddr", 0), "tags_addr"),
        "os_version": _u32(header.get("osVersionRaw", 0), "os_version"),
        "name": str(header.get("name", "")),
        "cmdline": str(header.get("cmdline", "")),
        "extra_cmdline": str(header.get("extraCmdline", "")),
        "dtb_addr": _u64(dtb.get("addr", 0), "dtb.addr"),
        "recovery_size": _u32(recovery.get("size", 0), "recoveryDtbo.size"),
        "dtb_size": _u32(dtb.get("size", 0), "dtb.size"),
    }


def _build_header_page(
    header: dict,
    version: int,
    page_size: int,
    kernel_size: int,
    ramdisk_size: int,
    second_size: int,
    recovery_size: int,
    dtb_size: int,
    recovery_offset: int,
    id_bytes: bytes,
) -> bytes:
    values = _header_values(header, version, page_size)
    canonical = HEADER_SIZES[version]
    if page_size < canonical:
        raise BootImageError(f"page_size={page_size} 装不下 v{version} header")
    page = bytearray(page_size)
    struct.pack_into("<8s", page, 0, MAGIC)
    struct.pack_into("<I", page, 8, _u32(kernel_size, "kernel_size"))
    struct.pack_into("<I", page, 12, values["kernel_addr"])
    struct.pack_into("<I", page, 16, _u32(ramdisk_size, "ramdisk_size"))
    struct.pack_into("<I", page, 20, values["ramdisk_addr"])
    struct.pack_into("<I", page, 24, _u32(second_size, "second_size"))
    struct.pack_into("<I", page, 28, values["second_addr"])
    struct.pack_into("<I", page, 32, values["tags_addr"])
    struct.pack_into("<I", page, 36, _u32(page_size, "page_size"))
    struct.pack_into("<I", page, 40, _u32(version, "header_version"))
    struct.pack_into("<I", page, 44, values["os_version"])

    def put_text(offset: int, length: int, text: str) -> None:
        raw = text.encode("utf-8", "replace").split(b"\x00", 1)[0]
        raw = raw[: max(0, length - 1)]
        page[offset : offset + len(raw)] = raw

    put_text(48, BOOT_NAME_SIZE, values["name"])
    put_text(64, BOOT_ARGS_SIZE, values["cmdline"])
    page[576:596] = id_bytes[:20]
    put_text(608, BOOT_EXTRA_ARGS_SIZE, values["extra_cmdline"])

    if version >= 1:
        struct.pack_into("<I", page, 1632, _u32(recovery_size, "recovery_dtbo_size"))
        struct.pack_into("<Q", page, 1636, _u64(recovery_offset, "recovery_dtbo_offset"))
        struct.pack_into("<I", page, 1644, V1_HEADER_SIZE)
    if version >= 2:
        struct.pack_into("<I", page, 1648, _u32(dtb_size, "dtb_size"))
        struct.pack_into("<Q", page, 1652, values["dtb_addr"])
        struct.pack_into("<I", page, 1644, V2_HEADER_SIZE)
    return bytes(page)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _declared_size(header: dict, key: str) -> int:
    seg = header.get(key)
    if not isinstance(seg, dict):
        return 0
    try:
        return int(seg.get("size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def repack(workdir: Path, dest: Path) -> dict:
    """用 ``workdir/ramdisk/`` 重建 cpio 并重打包 boot.img。

    失败绝不会在 dest 留下半个镜像：先写 ``dest.with_suffix('.tmp')``，
    校验后再 ``os.replace``。成功返回 path/size/sha256/headerVersion。
    """
    tmp: Path | None = None
    replaced = False
    try:
        wd = Path(workdir)
        header = _load_header(wd)
        version = int(header.get("headerVersion", 0))
        if version not in (0, 1, 2):
            raise BootImageError(f"暂不支持 boot header v{version}")
        page_size = int(header.get("pageSize", 0))
        _validate_page_size(page_size, version)

        root = wd / "ramdisk"
        if not root.is_dir():
            raise BootImageError("工作目录里没有 ramdisk/（请先解包）")
        kernel_path = wd / "kernel"
        if not kernel_path.is_file():
            raise BootImageError("工作目录里没有 kernel 文件")
        kernel_size = int(kernel_path.stat().st_size)

        def segment(name: str, declared: int) -> tuple[int, Path | None]:
            path = wd / name
            if path.is_file():
                return int(path.stat().st_size), path
            if declared > 0:
                raise BootImageError(f"header 里 {name} 大小为 {declared}，但工作目录缺少该文件")
            return 0, None

        second_size, second_path = segment("second", _declared_size(header, "second"))
        if version >= 1:
            recovery_size, recovery_path = segment(
                "recovery_dtbo", _declared_size(header, "recoveryDtbo")
            )
        else:
            recovery_size, recovery_path = 0, None
        if version == 2:
            dtb_size, dtb_path = segment("dtb", _declared_size(header, "dtb"))
        else:
            dtb_size, dtb_path = 0, None

        compression = str(
            header.get("ramdiskCompression")
            or (header.get("ramdisk") or {}).get("compression")
            or "raw"
        ).lower()
        if compression == "lz4":
            raise BootImageError("lz4 压缩的 ramdisk 暂不支持")
        if compression == "zstd":
            raise BootImageError("zstd 压缩的 ramdisk 暂不支持")

        cpio = _build_cpio(root, header)
        ramdisk = _compress_ramdisk(cpio, compression)
        ramdisk_size = len(ramdisk)

        kernel_offset = page_size
        ramdisk_offset = _align(kernel_offset + kernel_size, page_size)
        second_offset = _align(ramdisk_offset + ramdisk_size, page_size)
        if version >= 1:
            recovery_offset = _align(second_offset + second_size, page_size)
            dtb_offset = _align(recovery_offset + recovery_size, page_size)
        else:
            recovery_offset = 0
            dtb_offset = 0

        sha1 = hashlib.sha1()
        try:
            with kernel_path.open("rb") as f:
                _hash_segments(
                    f, [(0, kernel_size, "kernel")], sha1
                )
        except OSError as exc:
            raise BootImageError(f"读取 kernel 失败：{exc}")
        sha1.update(ramdisk)
        for path, size, label in (
            (second_path, second_size, "second"),
            (recovery_path, recovery_size, "recovery_dtbo"),
            (dtb_path, dtb_size, "dtb"),
        ):
            if path is not None and size > 0:
                try:
                    with path.open("rb") as f:
                        _hash_segments(f, [(0, size, label)], sha1)
                except OSError as exc:
                    raise BootImageError(f"读取 {label} 失败：{exc}")
        id_bytes = sha1.digest()

        header_page = _build_header_page(
            header,
            version,
            page_size,
            kernel_size,
            ramdisk_size,
            second_size,
            recovery_size,
            dtb_size,
            recovery_offset,
            id_bytes,
        )

        dest = Path(dest)
        if dest.exists() and dest.is_dir():
            raise BootImageError(f"输出路径是目录：{dest}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BootImageError(f"创建输出目录失败：{exc}")
        tmp = dest.with_suffix(".tmp")
        if tmp == dest:
            raise BootImageError("输出文件名不能以 .tmp 结尾")
        if tmp.exists() and tmp.is_dir():
            raise BootImageError(f"临时路径是目录：{tmp}")

        written = 0

        def write_pad(out, current: int) -> int:
            pad = (-current) % page_size
            if pad:
                out.write(b"\x00" * pad)
            return current + pad

        try:
            with tmp.open("wb") as out:
                out.write(header_page)
                written = page_size
                with kernel_path.open("rb") as kf:
                    remain = kernel_size
                    while remain > 0:
                        chunk = kf.read(min(1024 * 1024, remain))
                        if not chunk:
                            raise BootImageError("kernel 文件读取不完整")
                        out.write(chunk)
                        remain -= len(chunk)
                written += kernel_size
                written = write_pad(out, written)
                out.write(ramdisk)
                written += ramdisk_size
                written = write_pad(out, written)
                for path, size, label in (
                    (second_path, second_size, "second"),
                    (recovery_path, recovery_size, "recovery_dtbo"),
                    (dtb_path, dtb_size, "dtb"),
                ):
                    if path is not None and size > 0:
                        with path.open("rb") as sf:
                            remain = size
                            while remain > 0:
                                chunk = sf.read(min(1024 * 1024, remain))
                                if not chunk:
                                    raise BootImageError(f"{label} 文件读取不完整")
                                out.write(chunk)
                                remain -= len(chunk)
                        written += size
                        written = write_pad(out, written)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise BootImageError(f"写入临时镜像失败：{exc}")

        size = tmp.stat().st_size
        if written != size:
            raise BootImageError(
                f"输出镜像大小校验失败（写入 {written} 字节，实际 {size} 字节）"
            )
        sha256 = _sha256_file(tmp)
        dropped_extra = _declared_size(header, "extra")
        try:
            os.replace(tmp, dest)
            replaced = True
        except OSError as exc:
            raise BootImageError(f"替换输出文件失败：{exc}")

        return {
            "ok": True,
            "path": str(dest),
            "size": int(size),
            "sha256": sha256,
            "headerVersion": int(version),
            "headerSize": int(HEADER_SIZES[version]),
            "pageSize": int(page_size),
            "kernelSize": int(kernel_size),
            "ramdiskSize": int(ramdisk_size),
            "ramdiskCompression": compression,
            "cpioSize": int(len(cpio)),
            "id": id_bytes.hex(),
            "droppedExtra": int(dropped_extra),
        }
    except BootImageError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"重打包失败（文件系统错误）：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"重打包失败：{type(exc).__name__}: {exc}"}
    finally:
        if tmp is not None and not replaced:
            try:
                if tmp.exists() and tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
