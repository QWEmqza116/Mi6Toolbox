# -*- coding: utf-8 -*-
"""全量包（线刷包 / 卡刷包）的扫描、检查、解包、刷入与 sideload。

只依赖标准库。所有公开函数都不抛异常：失败时返回带中文 ``error`` 的字典。
包分三种：

* ``fastboot``：官方 .tgz/.tar.gz/.tar/.zip 线刷包，内部是 ``images/*.img``；
* ``sideload``：LineageOS 之类的卡刷 .zip，必须进 TWRP 用 ``adb sideload``；
* ``unknown``：能打开但找不到镜像 / 卡刷标志，多半不是刷机包。
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path

PKG_SUFFIXES = (".tgz", ".tar.gz", ".tar", ".zip", ".img.gz", ".gz")
FLASH_ORDER = [
    "boot",
    "recovery",
    "system",
    "vendor",
    "cust",
    "modem",
    "modemst1",
    "modemst2",
    "fsg",
    "splash",
    "persist",
    "misc",
    "cache",
    "userdata",
]

# 默认不无脑刷的分区：理由会原样回给前端。
_DEFAULT_SKIP = {
    "misc": "misc 存开机模式标志，刷错会反复进 recovery，默认不刷",
    "persist": "persist 存传感器校准 / 指纹 / WiFi MAC，刷错会丢校准，默认不刷",
    "modemst1": "modemst1 存 IMEI，刷错会没信号甚至丢 IMEI，默认不刷",
    "modemst2": "modemst2 存 IMEI 备份，刷错会丢备份，默认不刷",
    "fsg": "fsg 是基带备份分区，默认不刷",
    "cache": "cache 是临时缓存，刷了也会被系统重建，默认不刷",
}

_PART_BAD = re.compile(r"[^a-z0-9_-]+")
_ORDER_INDEX = {name: i for i, name in enumerate(FLASH_ORDER)}


class _UnsafeArchive(Exception):
    """压缩包里有绝对路径、``..``、符号链接等危险成员。"""


# ================================================================ 小工具
def _suffix(name: str) -> str:
    """按 PKG_SUFFIXES 的顺序匹配后缀（.tar.gz 必须先于 .gz）。"""
    low = str(name or "").lower()
    for suf in PKG_SUFFIXES:
        if low.endswith(suf):
            return suf
    return ""


def _base_name(name: str) -> str:
    """取成员路径里的文件名部分，同时兼容 / 和 \\。"""
    norm = str(name or "").replace("\\", "/").strip()
    if not norm or norm.endswith("/"):
        return ""
    return norm.rsplit("/", 1)[-1]


def _norm_part(value) -> str:
    """分区名归一化：去掉目录、.img/.gz 后缀，只留安全的 a-z0-9_-。"""
    s = _base_name(str(value or "")).lower()
    for suf in (".gz", ".img"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = _PART_BAD.sub("_", s).strip("._-")
    return s[:64]


def _part_from_name(name: str) -> str:
    """images/system.img -> system；boot.img.gz -> boot。"""
    return _norm_part(_base_name(name))


def _is_image_name(name: str) -> bool:
    """判断成员是不是镜像；META-INF 里的东西一律不算。"""
    norm = str(name or "").replace("\\", "/")
    if not norm or norm.endswith("/"):
        return False
    low = norm.lower().lstrip("/")
    if low.startswith(("meta-inf/", "__macosx/")) or "/meta-inf/" in low:
        return False
    base = low.rsplit("/", 1)[-1]
    if base in (".img", ".img.gz") or base.startswith("._"):
        return False
    return base.endswith(".img") or base.endswith(".img.gz")


def _order_key(part: str) -> tuple[int, str]:
    return (_ORDER_INDEX.get(part, len(FLASH_ORDER)), part)


def _tail(text: str, limit: int) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    return "…" + s[-limit:]


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _first_line_matching(text: str, needles: tuple[str, ...]) -> str:
    """从日志里挑最后一条像报错的行，挑不到就退回最后一行。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        low = line.lower()
        if "no error" in low:
            continue
        if any(n in low for n in needles):
            return line
    return lines[-1] if lines else ""


def _decode_line(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ================================================================ 包结构探测
def _member_name_error(name: str) -> str:
    """安全校验：返回空串表示安全，否则是中文原因。"""
    raw = str(name or "")
    if not raw.strip():
        return "是空路径"
    if "\x00" in raw:
        return "含 NUL 字符"
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        return "是绝对路径"
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return "含 .. 路径"
    return ""


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _kind_from_names(names: list[str], has_image: bool) -> str:
    for raw in names:
        n = str(raw or "").replace("\\", "/").lower()
        if n.endswith("meta-inf/com/google/android/updater-script"):
            return "sideload"
        if n.rsplit("/", 1)[-1] == "payload.bin":
            return "sideload"
    return "fastboot" if has_image else "unknown"


def _probe_zip(path: Path, validate: bool) -> dict:
    # infolist() 和 namelist() 读的是同一份中央目录，这里顺手把 size 也拿到，
    # 省得 inspect 再开一次压缩包。
    images: list[dict] = []
    names: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename
            names.append(name)
            if validate:
                reason = _member_name_error(name)
                if reason:
                    raise _UnsafeArchive(f"压缩包不安全：成员 {name!r} {reason}")
                if _zip_is_symlink(info):
                    raise _UnsafeArchive(f"压缩包里有符号链接（{name}），已拒绝")
            if info.is_dir():
                continue
            if not _is_image_name(name):
                continue
            part = _part_from_name(name)
            if not part:
                continue
            images.append({"part": part, "member": name, "size": int(info.file_size)})
    has_image = any(_is_image_name(n) for n in names)
    return {"kind": _kind_from_names(names, has_image), "images": images,
            "names": names, "container": "zip"}


def _probe_tar(path: Path, validate: bool) -> dict:
    # getmembers() 和 getnames() 内部走同一遍 tar 索引，这里顺便留下 size。
    images: list[dict] = []
    names: list[str] = []
    with tarfile.open(path, "r:*") as tf:
        for member in tf.getmembers():
            name = member.name
            names.append(name)
            if validate:
                reason = _member_name_error(name)
                if reason:
                    raise _UnsafeArchive(f"压缩包不安全：成员 {name!r} {reason}")
                if not member.isdir() and not member.isreg():
                    raise _UnsafeArchive(f"压缩包里有符号链接或特殊文件（{name}），已拒绝")
            if member.isdir() or not _is_image_name(name):
                continue
            if not member.isreg():
                continue
            part = _part_from_name(name)
            if not part:
                continue
            images.append({"part": part, "member": name, "size": int(member.size)})
    has_image = any(_is_image_name(n) for n in names)
    return {"kind": _kind_from_names(names, has_image), "images": images,
            "names": names, "container": "tar"}


def _is_gzip(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _gzip_inner_name(path: Path) -> str:
    """从 gzip 头里读原始文件名；读不到就用外层文件名去掉 .gz。"""
    fallback = path.name[:-3] if path.name.lower().endswith(".gz") else path.name
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if len(head) < 10 or head[:2] != b"\x1f\x8b":
                return fallback
            flg = head[3]
            if flg & 0x04:  # FEXTRA
                raw_len = f.read(2)
                if len(raw_len) != 2:
                    return fallback
                f.seek(int.from_bytes(raw_len, "little"), 1)
            if flg & 0x08:  # FNAME
                buf = bytearray()
                while len(buf) < 1024:
                    ch = f.read(1)
                    if not ch or ch == b"\x00":
                        break
                    buf += ch
                if buf:
                    for enc in ("utf-8", "gbk", "latin-1"):
                        try:
                            return buf.decode(enc)
                        except UnicodeDecodeError:
                            continue
    except (OSError, ValueError):
        return fallback
    return fallback


def _gzip_isize(path: Path) -> int:
    """gzip 尾部的 4 字节 ISIZE（原文件大小）。"""
    try:
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            raw = f.read(4)
        return int.from_bytes(raw, "little") if len(raw) == 4 else 0
    except (OSError, ValueError):
        return 0


def _probe_single_gz(path: Path) -> dict:
    if not _is_gzip(path):
        raise ValueError("不是有效的 gzip 文件")
    inner = _gzip_inner_name(path)
    base = _base_name(inner)
    if not _is_image_name(base):
        fallback = path.name[:-3] if path.name.lower().endswith(".gz") else ""
        base = _base_name(fallback)
    names = [base] if base else []
    if not base or not _is_image_name(base):
        return {"kind": "unknown", "images": [], "names": names, "container": "gzip"}
    part = _part_from_name(base)
    if not part:
        return {"kind": "unknown", "images": [], "names": names, "container": "gzip"}
    images = [{"part": part, "member": base, "size": _gzip_isize(path)}]
    return {"kind": "fastboot", "images": images, "names": names, "container": "gzip"}


def _probe_package(path: Path, validate: bool = True) -> dict:
    """识别包结构并列出镜像；``validate=False`` 时跳过安全性检查（给 detect_kind 用）。"""
    suf = _suffix(path.name)
    if suf == ".zip":
        return _probe_zip(path, validate)
    if suf in (".tgz", ".tar.gz", ".tar"):
        return _probe_tar(path, validate)
    if suf == ".img.gz":
        return _probe_single_gz(path)
    if suf == ".gz":
        try:
            return _probe_tar(path, validate)
        except _UnsafeArchive:
            raise
        except Exception:  # 不是 tar，按单文件 gzip 处理
            return _probe_single_gz(path)
    return {"kind": "unknown", "images": [], "names": [], "container": "none"}


# ================================================================ 公开 API
def detect_kind(path: Path) -> str:
    """快速判断包类型：fastboot / sideload / unknown，任何异常都算 unknown。"""
    try:
        p = Path(path)
        if not p.is_file():
            return "unknown"
        probe = _probe_package(p, validate=False)
        return str(probe.get("kind") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def scan_packages(root: Path) -> list[dict]:
    """扫一层目录，按 mtime 倒序列出全量包；单个 .img 不算。"""
    rows: list[tuple[float, dict]] = []
    try:
        base = Path(root)
        if not base.is_dir():
            return []
        for f in base.iterdir():
            try:
                if not f.is_file() or not _suffix(f.name):
                    continue
                st = f.stat()
            except (OSError, ValueError, OverflowError):
                continue
            rows.append((
                float(st.st_mtime),
                {
                    "name": f.name,
                    "path": str(f),
                    "size": int(st.st_size),
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    "kind": detect_kind(f),
                },
            ))
    except (OSError, TypeError, ValueError, OverflowError):
        return []
    rows.sort(key=lambda row: (-row[0], row[1]["name"].lower()))
    return [entry for _mtime, entry in rows]


def _dedupe_images(images: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in images or []:
        part = str(item.get("part") or "")
        if not part or part in seen:
            continue
        seen.add(part)
        out.append({
            "part": part,
            "member": str(item.get("member") or ""),
            "size": int(item.get("size") or 0),
        })
    out.sort(key=lambda x: _order_key(x["part"]))
    return out


def inspect(path: Path) -> dict:
    """不解包，列出包里的镜像并给一句中文说明。"""
    p = Path(path)
    try:
        if not p.exists():
            return {"ok": False, "error": "文件不存在"}
        if not p.is_file():
            return {"ok": False, "error": "不是文件"}
        st = p.stat()
        probe = _probe_package(p, validate=True)
        kind = str(probe.get("kind") or "unknown")
        result = {
            "ok": True,
            "kind": kind,
            "path": str(p),
            "size": int(st.st_size),
            "total_size": 0,
            "images": [],
            "note": "",
        }
        if kind == "sideload":
            result["note"] = "这是 TWRP 卡刷包，需要进 TWRP 用 adb sideload 刷"
            return result
        if kind != "fastboot":
            result["note"] = "包里没有 .img，可能不是刷机包"
            return result

        images = _dedupe_images(probe.get("images") or [])
        if not images:
            result["kind"] = "unknown"
            result["note"] = "包里没有 .img，可能不是刷机包"
            return result
        result["images"] = images
        result["total_size"] = sum(int(x["size"]) for x in images)
        count = len(images)
        if probe.get("container") == "gzip":
            result["note"] = "单个镜像包，1 个镜像，解压后可用 fastboot 刷入"
        elif _suffix(p.name) in (".tgz", ".tar.gz", ".tar"):
            result["note"] = f"官方线刷包，{count} 个镜像，可整包 fastboot 刷入"
        else:
            result["note"] = f"线刷包，{count} 个镜像，可整包 fastboot 刷入"
        return result
    except _UnsafeArchive as exc:
        return {"ok": False, "error": str(exc)}
    except (zipfile.BadZipFile, tarfile.TarError, gzip.BadGzipFile, OSError,
            EOFError, ValueError) as exc:
        return {"ok": False, "error": f"打不开这个包（文件损坏或格式不支持）：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"检查全量包失败：{type(exc).__name__}: {exc}"}


def _norm_parts(parts) -> set[str] | None:
    if parts is None:
        return None
    if isinstance(parts, (str, bytes)):
        parts = [parts]
    wanted: set[str] = set()
    for item in parts:
        part = _norm_part(item)
        if part:
            wanted.add(part)
    return wanted


def _select_images(images: list[dict], wanted: set[str] | None) -> tuple[list[dict], list[str]]:
    selected: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for item in images or []:
        part = str(item.get("part") or "")
        if not part:
            continue
        if part in seen:
            if part not in skipped:
                skipped.append(part)
            continue
        seen.add(part)
        if wanted is not None and part not in wanted:
            skipped.append(part)
            continue
        selected.append({
            "part": part,
            "member": str(item.get("member") or ""),
            "size": int(item.get("size") or 0),
        })
    selected.sort(key=lambda x: _order_key(x["part"]))
    return selected, skipped


def _gunzip_to(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as gz, open(dst, "wb") as out:
        shutil.copyfileobj(gz, out, 1024 * 1024)


def _tmp_path(dest: Path, part: str, tag: str, temps: list[Path]) -> Path:
    tmp = dest / f".{part}.{time.time_ns()}.{len(temps)}.{tag}.tmp"
    temps.append(tmp)
    return tmp


def _extract_zip(path: Path, dest: Path, targets: dict[str, str],
                 temps: list[Path]) -> list[tuple[str, Path, int]]:
    tasks: list[tuple[str, Path, int]] = []
    done: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not _is_image_name(info.filename):
                continue
            part = _part_from_name(info.filename)
            if part not in targets or part in done:
                continue
            if info.filename != targets[part]:
                continue
            done.add(part)
            tmp = _tmp_path(dest, part, "img", temps)
            if info.filename.lower().endswith(".gz"):
                gz_tmp = _tmp_path(dest, part, "gz", temps)
                with zf.open(info) as src, open(gz_tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                _gunzip_to(gz_tmp, tmp)
                _unlink(gz_tmp)
            else:
                with zf.open(info) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
            tasks.append((part, tmp, int(tmp.stat().st_size)))
    return tasks


def _extract_tar(path: Path, dest: Path, targets: dict[str, str],
                 temps: list[Path]) -> list[tuple[str, Path, int]]:
    tasks: list[tuple[str, Path, int]] = []
    done: set[str] = set()
    with tarfile.open(path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isreg() or not _is_image_name(member.name):
                continue
            part = _part_from_name(member.name)
            if part not in targets or part in done:
                continue
            if member.name != targets[part]:
                continue
            done.add(part)
            src = tf.extractfile(member)
            if src is None:
                raise OSError(f"读不出镜像成员：{member.name}")
            tmp = _tmp_path(dest, part, "img", temps)
            try:
                if member.name.lower().endswith(".gz"):
                    gz_tmp = _tmp_path(dest, part, "gz", temps)
                    with open(gz_tmp, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    _gunzip_to(gz_tmp, tmp)
                    _unlink(gz_tmp)
                else:
                    with open(tmp, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
            finally:
                try:
                    src.close()
                except OSError:
                    pass
            tasks.append((part, tmp, int(tmp.stat().st_size)))
    return tasks


def _extract_gzip(path: Path, dest: Path, targets: dict[str, str],
                  temps: list[Path]) -> list[tuple[str, Path, int]]:
    if len(targets) != 1:
        raise OSError("单文件 gzip 包里只有一个镜像")
    part = next(iter(targets))
    tmp = _tmp_path(dest, part, "img", temps)
    _gunzip_to(path, tmp)
    return [(part, tmp, int(tmp.stat().st_size))]


def unpack(path: Path, dest: Path, parts: list[str] | None = None) -> dict:
    """把镜像解到 dest（平铺成 ``<part>.img``）；只解 .img 和 .img.gz。"""
    p = Path(path)
    d = Path(dest)
    try:
        if not p.exists():
            return {"ok": False, "error": "文件不存在"}
        if not p.is_file():
            return {"ok": False, "error": "不是文件"}
        probe = _probe_package(p, validate=True)
        kind = str(probe.get("kind") or "unknown")
        if kind == "sideload":
            return {"ok": False,
                    "error": "这是 TWRP 卡刷包，没有可解出的镜像；请进 TWRP 用 adb sideload 刷"}
        if kind != "fastboot" or not probe.get("images"):
            return {"ok": False, "error": "包里没有 .img，可能不是刷机包"}

        wanted = _norm_parts(parts)
        selected, skipped = _select_images(probe.get("images") or [], wanted)
        if not selected:
            if wanted:
                names = "、".join(sorted(wanted))
                return {"ok": False, "error": f"没有匹配的镜像：包里没有 {names}"}
            if wanted is not None:
                return {"ok": False, "error": "没有选择要解出的分区"}
            return {"ok": False, "error": "包里没有 .img，可能不是刷机包"}

        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"创建目标目录失败：{exc}"}
        if not d.is_dir():
            return {"ok": False, "error": "目标路径不是文件夹"}

        targets = {item["part"]: item["member"] for item in selected}
        temps: list[Path] = []
        try:
            container = probe.get("container")
            if container == "zip":
                tasks = _extract_zip(p, d, targets, temps)
            elif container == "tar":
                tasks = _extract_tar(p, d, targets, temps)
            elif container == "gzip":
                tasks = _extract_gzip(p, d, targets, temps)
            else:
                return {"ok": False, "error": "不支持的包格式"}
            found = {item[0] for item in tasks}
            missing = [part for part in targets if part not in found]
            if missing:
                raise OSError("压缩包里找不到镜像：" + "、".join(missing))

            images: list[dict] = []
            for part, tmp, size in tasks:
                target = d / f"{part}.img"
                try:
                    os.replace(tmp, target)
                except OSError as exc:
                    raise OSError(f"写入 {target.name} 失败：{exc}") from exc
                images.append({"part": part, "path": str(target), "size": int(size)})
        except _UnsafeArchive:
            raise
        except Exception as exc:  # noqa: BLE001
            for tmp in temps:
                _unlink(tmp)
            if isinstance(exc, OSError) and str(exc).startswith(("压缩包里", "写入")):
                return {"ok": False, "error": str(exc)}
            return {"ok": False, "error": f"解包失败：{type(exc).__name__}: {exc}"}
        images.sort(key=lambda x: _order_key(x["part"]))
        skipped.sort(key=_order_key)
        return {"ok": True, "dir": str(d), "images": images, "kind": "fastboot",
                "skipped": skipped}
    except _UnsafeArchive as exc:
        return {"ok": False, "error": str(exc)}
    except (zipfile.BadZipFile, tarfile.TarError, gzip.BadGzipFile, OSError,
            EOFError, ValueError) as exc:
        return {"ok": False, "error": f"解包失败（文件损坏或格式不支持）：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"解包失败：{type(exc).__name__}: {exc}"}


def image_files(dir_: Path) -> list[dict]:
    """列一个目录里已有的 .img（不递归），part 取文件名去后缀。"""
    out: list[dict] = []
    try:
        d = Path(dir_)
        if not d.is_dir():
            return []
        for f in d.iterdir():
            try:
                if not f.is_file() or f.suffix.lower() != ".img":
                    continue
                part = _part_from_name(f.name)
                if not part:
                    continue
                out.append({"part": part, "path": str(f), "size": int(f.stat().st_size)})
            except OSError:
                continue
    except (OSError, TypeError, ValueError):
        return []
    out.sort(key=lambda x: _order_key(x["part"]))
    return out


def flash_plan(images: list[dict], include: list[str] | None = None) -> dict:
    """按 FLASH_ORDER 排序；默认跳过 misc/persist/modemst* 等高风险分区。"""
    try:
        if isinstance(include, (str, bytes)):
            include = [include]
        wanted = None if include is None else {
            _norm_part(x) for x in include if _norm_part(x)
        }
        steps: list[dict] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for item in images or []:
            if not isinstance(item, dict):
                continue
            part = _norm_part(item.get("part") or "")
            path = str(item.get("path") or "")
            if not part and path:
                part = _part_from_name(path)
            if not part:
                skipped.append({"part": str(item.get("part") or ""), "reason": "分区名不合法"})
                continue
            if part in seen:
                skipped.append({"part": part, "reason": "同一分区出现多次，只保留第一个"})
                continue
            seen.add(part)
            if wanted is not None and part not in wanted:
                skipped.append({"part": part, "reason": "不在本次选择的刷入分区里"})
                continue
            if wanted is None and part in _DEFAULT_SKIP:
                skipped.append({"part": part, "reason": _DEFAULT_SKIP[part]})
                continue
            if not path:
                skipped.append({"part": part, "reason": "缺少镜像文件路径（先解包再刷）"})
                continue
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            steps.append({"part": part, "path": path, "size": size})
        steps.sort(key=lambda x: _order_key(x["part"]))
        return {"ok": True, "steps": steps, "skipped": skipped}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "steps": [], "skipped": [],
                "error": f"生成刷入顺序失败：{type(exc).__name__}: {exc}"}


def _call_step(callback, index: int, total: int, data: dict) -> None:
    """进度回调出错不能把刷机打断。"""
    if callback is None:
        return
    try:
        callback(index, total, data)
    except Exception:  # noqa: BLE001
        pass


def _flash_bad(text: str) -> bool:
    low = str(text or "").lower()
    return "failed" in low or "error" in low


def flash_images(images: list[dict], on_step=None, include: list[str] | None = None) -> dict:
    """用 fastboot 逐个刷入；单步失败就中止。

    ``include=None`` 表示 images 已经是选好的步骤（通常来自 flash_plan），
    执行阶段不再套默认跳过，以免把用户显式勾选的 misc / persist 又滤掉。
    直接拿原始镜像列表来刷时，请先调用 :func:`flash_plan` 或显式传 include。

    ``on_step(i, total, step)`` 每步调两次：开始前 ``ok=None, done=False``，
    结束后 ``ok`` 为布尔、``done=True``；``i`` 从 0 开始。
    """
    done_steps: list[dict] = []
    try:
        try:
            from .fastboot import Fastboot
        except ImportError:
            from toolbox.fastboot import Fastboot

        try:
            images = list(images or [])
        except TypeError:
            images = []
        fb = Fastboot()
        devs = fb.devices()
        if not devs:
            return {"ok": False, "mode": "fastboot", "steps": [],
                    "error": "手机不在 Fastboot 模式（重启到 bootloader 再试）"}
        fb.serial = devs[0]

        if include is None:
            # 传进来的可能已经是 flash_plan 排好的步骤，不能再套一次默认跳过：
            # 否则用户显式勾选的 misc / persist 会在执行阶段被二次过滤掉。
            every: list[str] = []
            for item in images or []:
                if not isinstance(item, dict):
                    continue
                part = _norm_part(item.get("part") or "")
                if not part:
                    part = _part_from_name(str(item.get("path") or ""))
                if part and part not in every:
                    every.append(part)
            plan = flash_plan(images, include=every)
        else:
            plan = flash_plan(images, include=include)
        if not plan.get("ok"):
            return {"ok": False, "mode": "fastboot", "steps": [],
                    "error": str(plan.get("error") or "生成刷入顺序失败")}
        todo = plan.get("steps") or []
        if not todo:
            return {"ok": False, "mode": "fastboot", "steps": [],
                    "error": "没有可刷的镜像（可能都被默认跳过或没选）"}

        total = len(todo)
        for i, step in enumerate(todo):
            part = str(step.get("part") or "")
            path = str(step.get("path") or "")
            size = int(step.get("size") or 0)
            _call_step(on_step, i, total, {
                "part": part, "path": path, "size": size, "ok": None,
                "done": False, "log": f"正在刷入 {part}…",
            })
            log = ""
            ok = False
            try:
                if not path:
                    log = "缺少镜像文件路径"
                elif not Path(path).is_file():
                    log = f"镜像文件不存在：{path}"
                else:
                    result = fb.flash(part, path, timeout=1800)
                    log = str(getattr(result, "text", "") or "").strip()
                    try:
                        rc = int(getattr(result, "rc", -1))
                    except (TypeError, ValueError):
                        rc = -1
                    ok = rc == 0 and not _flash_bad(log)
                    if not ok and not log:
                        log = f"fastboot 退出码 {rc}"
            except Exception as exc:  # noqa: BLE001
                ok = False
                log = f"{type(exc).__name__}: {exc}"
            entry = {
                "part": part,
                "path": path,
                "size": size,
                "ok": bool(ok),
                "log": _tail(log, 4000),
            }
            done_steps.append(entry)
            _call_step(on_step, i, total, dict(entry, done=True))
            if not ok:
                reason = _first_line_matching(entry["log"], ("failed", "error"))
                if not reason:
                    reason = "fastboot 返回失败"
                return {"ok": False, "mode": "fastboot", "steps": done_steps,
                        "error": f"刷入 {part} 失败，已中止：{reason}"}
        return {"ok": True, "mode": "fastboot", "steps": done_steps, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "mode": "fastboot", "steps": done_steps,
                "error": f"刷机过程出错：{type(exc).__name__}: {exc}"}


def sideload(path: Path, on_line=None) -> dict:
    """在 TWRP 的 sideload 模式下执行 ``adb sideload``，输出逐行回调。"""
    p = Path(path)
    log_parts: list[str] = []
    try:
        if not p.exists():
            return {"ok": False, "log": "", "error": "卡刷包不存在"}
        if not p.is_file():
            return {"ok": False, "log": "", "error": "不是文件"}
        try:
            from .adb import Adb, android_env
            from .util import CREATE_NO_WINDOW
        except ImportError:
            from toolbox.adb import Adb, android_env
            from toolbox.util import CREATE_NO_WINDOW

        adb = Adb()
        if not adb.available:
            return {"ok": False, "log": "",
                    "error": "未找到 adb，请先在「设置 / 环境」里安装 platform-tools"}
        devs = adb.devices()
        if not devs:
            return {"ok": False, "log": "",
                    "error": "手机没连上：请先重启到 TWRP，打开 ADB Sideload"}
        serial = next((s for s, state in devs if state == "sideload"), None)
        if serial is None:
            state = devs[0][1]
            return {"ok": False, "log": "",
                    "error": f"手机当前是 {state} 模式，不是 sideload 模式："
                             "在 TWRP 里点 Advanced → ADB Sideload 再试"}
        adb.serial = serial
        if detect_kind(p) == "fastboot":
            return {"ok": False, "log": "",
                    "error": "这是 fastboot 线刷包，不能 sideload：请用「解包 + fastboot 刷入」"}

        cmd = [adb.exe or "adb"]
        if adb.serial:
            cmd += ["-s", adb.serial]
        cmd += ["sideload", str(p)]
        env = os.environ.copy()
        try:
            env.update({k: str(v) for k, v in android_env().items()})
        except Exception:  # noqa: BLE001
            pass
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        timed_out = {"value": False}

        def _kill() -> None:
            if proc.poll() is None:
                timed_out["value"] = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

        timer = threading.Timer(7200, _kill)
        timer.daemon = True
        timer.start()
        def _emit(line: str) -> None:
            log_parts.append(line)
            if on_line is not None and line.strip():
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001
                    pass

        rc = -1
        stream = proc.stdout
        try:
            if stream is not None:
                pending = b""
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    pending += chunk
                    while True:
                        cuts = [i for i in (pending.find(b"\n"), pending.find(b"\r")) if i >= 0]
                        if not cuts:
                            break
                        cut = min(cuts)
                        raw = pending[:cut]
                        delim = pending[cut:cut + 1]
                        pending = pending[cut + 1:]
                        if delim == b"\r" and pending.startswith(b"\n"):
                            pending = pending[1:]
                        _emit(_decode_line(raw))
                if pending:
                    _emit(_decode_line(pending))
            rc = proc.wait()
        except OSError:
            if not timed_out["value"]:
                raise
        finally:
            timer.cancel()
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        log = "\n".join(log_parts).strip()
        if timed_out["value"]:
            return {"ok": False, "log": _tail(log, 100000),
                    "error": "sideload 超时（超过 2 小时），已终止"}
        low = log.lower().replace("failed to read command: no error", "")
        bad = rc != 0 or "error:" in low or "failed" in low
        if not bad:
            return {"ok": True, "log": _tail(log, 100000), "error": ""}
        reason = _first_line_matching(log, ("failed", "error"))
        if not reason or "no error" in reason.lower():
            reason = f"adb 退出码 {rc}"
        return {"ok": False, "log": _tail(log, 100000),
                "error": f"sideload 失败：{reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "log": _tail("\n".join(log_parts), 100000),
                "error": f"sideload 出错：{type(exc).__name__}: {exc}"}


def format_size(n: int) -> str:
    """1023 -> 1023 B；1536 -> 1.5 KB；依次到 GB / TB。"""
    try:
        value = float(n)
    except (TypeError, ValueError, OverflowError):
        return "0 B"
    if value < 0:
        value = 0.0
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"
