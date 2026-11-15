# -*- coding: utf-8 -*-
"""Web UI 后端：本地 HTTP 服务 + 设备状态 API + 用 Edge 应用模式开窗口。

不依赖任何第三方库：http.server 提供静态文件与 JSON，adb 逻辑复用 toolbox。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STATIC = BASE / "static"
INDEX = STATIC / "index.html"
LOG_DIR = ROOT / "logs"
APP_ID = "mi6toolbox-webui"
VERSION = "1.2"


def log(msg: str) -> None:
    """写一行日志到 logs/webui.log；用 pythonw 启动时没有控制台，全靠它。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "webui.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _fatal(msg: str) -> None:
    """启动就挂了的话，用弹窗告诉用户 —— pythonw 下没有控制台可看。"""
    log("FATAL " + msg)
    print(msg)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, msg, "小米6 工具箱 · 启动失败", 0x10)
    except Exception:
        pass


sys.path.insert(0, str(ROOT))
try:
    from toolbox.adb import Adb  # noqa: E402
    from toolbox.device import probe  # noqa: E402
except Exception:  # noqa: BLE001
    _fatal("加载 toolbox 失败：\n" + traceback.format_exc())
    raise

PORT = int(os.environ.get("MI6_WEB_PORT", "8765"))
# 心跳断了这么久就认为窗口已关闭。浏览器把后台标签页的定时器限流到 1 分钟一次，
# 所以这个值必须明显大于 60 秒，否则最小化几分钟就会把服务误杀。
IDLE_LIMIT = 90
_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()
_probe_lock = threading.Lock()
# 最近一次活动（请求 / 心跳）的时间、正在处理的请求数、"页面要关了"的时间点
_activity = {"last": 0.0, "inflight": 0, "bye": 0.0}


def _build() -> str:
    """前端文件的版本号（mtime），页面靠它判断要不要自动刷新。"""
    try:
        return str(INDEX.stat().st_mtime_ns)
    except OSError:
        return "0"


EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _battery(adb: Adb) -> dict:
    txt = adb.sh("dumpsys battery", timeout=20)
    out = {}
    for line in txt.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    try:
        level = int(out.get("level", "0"))
    except ValueError:
        level = 0
    try:
        temp = int(out.get("temperature", "0")) / 10
    except ValueError:
        temp = 0.0
    return {"level": level, "temp": round(temp, 1), "status": out.get("status", ""),
            "voltage": out.get("voltage", ""), "technology": out.get("technology", ""),
            "health": out.get("health", ""), "current": out.get("current now", "")}


_RES_RE = re.compile(r"(\d{3,5})\s*[xX]\s*(\d{3,5})")
_NUM_RE = re.compile(r"(\d+)")


def _extra(adb: Adb) -> dict:
    """仪表盘要的杂项：安全补丁 / 内核 / 平台 / 分辨率 / 内存 / 三方应用数 / 运营商。

    全部塞进一条 shell 命令，只花一次 adb 往返。
    """
    script = (
        "echo SEC=$(getprop ro.build.version.security_patch);"
        "echo KER=$(uname -r);"
        "echo SOC=$(getprop ro.board.platform);"
        "echo ABI=$(getprop ro.product.cpu.abi);"
        "echo RES=$(wm size 2>/dev/null | tail -n1);"
        "echo DEN=$(wm density 2>/dev/null | tail -n1);"
        "echo MEMT=$(grep MemTotal /proc/meminfo);"
        "echo MEMA=$(grep MemAvailable /proc/meminfo);"
        "echo APPS=$(pm list packages -3 2>/dev/null | wc -l);"
        "echo SIM=$(getprop gsm.operator.alpha)"
    )
    txt = adb.sh(script, timeout=45)
    raw = {}
    for line in txt.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            raw[k.strip()] = v.strip()

    def num(text: str) -> int:
        m = _NUM_RE.search(text or "")
        return int(m.group(1)) if m else 0

    res = _RES_RE.search(raw.get("RES", ""))
    return {
        "securityPatch": raw.get("SEC", ""),
        "kernel": raw.get("KER", "")[:48],
        "soc": raw.get("SOC", ""),
        "abi": raw.get("ABI", ""),
        "screen": f"{res.group(1)}x{res.group(2)}" if res else "",
        "density": num(raw.get("DEN", "")),
        "memTotal": num(raw.get("MEMT", "")) * 1024,
        "memAvail": num(raw.get("MEMA", "")) * 1024,
        "apps": num(raw.get("APPS", "")),
        "sim": raw.get("SIM", "")[:40],
    }


def _storage(adb: Adb) -> dict:
    txt = adb.sh("df -h /data", timeout=20)
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-1] == "/data":
            try:
                pct = float(parts[4].rstrip("%"))
            except ValueError:
                pct = 0
            return {"size": parts[1], "used": parts[2], "avail": parts[3], "pct": pct}
    return {}


def _uptime(adb: Adb) -> float:
    try:
        return float(adb.sh("cat /proc/uptime", timeout=15).split()[0])
    except Exception:
        return 0.0


def _probe_and_store() -> dict:
    info = probe()
    data = {
        "app": APP_ID,
        "version": VERSION,
        "build": _build(),
        "connected": info.connected,
        "mode": info.mode,
        "modeLabel": info.mode_label,
        "model": info.model,
        "codename": info.codename,
        "rom": info.rom_name,
        "android": info.android,
        "unlocked": info.unlocked,
        "root": info.root,
        "magisk": info.magisk,
        "twrp": info.twrp,
        "serial": info.serial,
        "partitions": len(info.partitions),
        "note": info.note,
        "battery": {},
        "storage": {},
        "extra": {},
        "uptime": 0,
    }
    if info.connected and info.mode in ("adb", "twrp"):
        adb = Adb(info.serial or None)
        try:
            data["battery"] = _battery(adb)
            data["storage"] = _storage(adb)
            data["uptime"] = _uptime(adb)
            data["extra"] = _extra(adb)
        except Exception:
            pass
    with _lock:
        _cache["data"] = data
        _cache["t"] = time.time()
    return data


def _refresh_in_background() -> None:
    """旧数据先顶上，后台补探一次；同一时刻只允许一个探测在跑。"""
    if not _probe_lock.acquire(blocking=False):
        return

    def work():
        try:
            _probe_and_store()
        except Exception:
            log("后台刷新失败\n" + traceback.format_exc())
        finally:
            _probe_lock.release()

    threading.Thread(target=work, daemon=True).start()


def state(force: bool = False) -> dict:
    """返回设备状态。

    三级策略，都是为了让页面永远不会卡在一次慢 adb 上：
      1. 3 秒内刚探过 → 直接给缓存；
      2. 缓存过期但不算太旧 → 先返回旧的，后台刷新；
      3. 完全没数据 → 老老实实探一次（并发请求会排队，不会重复探）。
    """
    with _lock:
        data = _cache["data"]
        age = time.time() - _cache["t"]
    if data is not None and age < 3 and not force:
        return data
    if data is not None and age < 120 and not force:
        _refresh_in_background()
        return data

    with _probe_lock:
        with _lock:
            data = _cache["data"]
            age = time.time() - _cache["t"]
        if data is not None and age < 3 and not force:
            return data
        return _probe_and_store()


PULLED = ROOT / "downloads" / "pulled"


def _clean_name(name: str, default: str) -> str:
    """只留文件名本身，挡住 ../ 和空名字。"""
    n = Path(str(name or "").replace("\\", "/")).name
    return default if n in ("", ".", "..") else n


def _clean_part(part: str) -> str:
    """分区名只允许字母数字和 _ - .，避免拼进 shell / 路径时出事。"""
    p = (part or "").strip()
    return p if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", p) else ""


def list_dir(path: str) -> dict:
    adb = Adb()
    devs = adb.devices() if adb.available else []
    if not devs:
        return {"ok": False, "error": "未连接设备", "entries": []}
    adb.serial = devs[0][0]
    path = (path or "/sdcard").rstrip("/") or "/"
    r = adb.shell(f"ls -l '{path if path == '/' else path + '/'}'", timeout=60)
    txt = r.text
    if r.rc != 0 and not txt.strip():
        # adb 把 "No such file" 之类写进 stderr，text 已经合并了
        return {"ok": False, "error": "读取失败（目录不存在或没有权限）", "entries": []}
    entries = []
    for line in txt.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or line.startswith("total"):
            continue
        perms, _n, _o, _g, size, date, clock, name = parts
        if name in (".", ".."):
            continue
        try:
            size_i = int(size)
        except ValueError:
            size_i = 0
        entries.append(
            {"name": name, "dir": perms.startswith("d"), "size": size_i, "mtime": f"{date} {clock}"}
        )
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = "/" if path.count("/") <= 1 else path.rstrip("/").rsplit("/", 1)[0]
    return {"ok": True, "path": path, "parent": parent, "entries": entries}


def pull_file(remote: str) -> dict:
    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "error": "未连接设备"}
    adb.serial = devs[0][0]
    PULLED.mkdir(parents=True, exist_ok=True)
    name = remote.rstrip("/").rsplit("/", 1)[-1]
    dest = PULLED / name
    r = adb.pull(remote, dest, timeout=3600)
    if not dest.exists():
        return {"ok": False, "error": (r.text or "拉取失败")[-200:]}
    try:
        subprocess.Popen(["explorer", "/select,", str(dest)])
    except Exception:
        pass
    return {"ok": True, "path": str(dest), "size": dest.stat().st_size}


def upload(file_path: Path, target_dir: str) -> dict:
    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "error": "未连接设备"}
    adb.serial = devs[0][0]
    remote = f"{target_dir.rstrip('/')}/{file_path.name}"
    r = adb.push(file_path, remote, timeout=3600)
    ok = r.rc == 0
    return {"ok": ok, "name": file_path.name, "remote": remote, "log": (r.text or "")[-300:] if not ok else ""}


APK_DIR = ROOT / "apk"


def list_apks() -> dict:
    """列出 apk/ 里能一键安装的包。"""
    APK_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for f in sorted(APK_DIR.iterdir(), key=lambda p: p.name.lower()):
        if f.suffix.lower() != ".apk" or not f.is_file():
            continue
        st = f.stat()
        items.append({"name": f.name, "size": st.st_size,
                      "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return {"ok": True, "dir": str(APK_DIR), "items": items}


def install_apk(src: Path) -> dict:
    """adb install -r -g 装一个本地 apk。"""
    if not src.is_file():
        return {"ok": False, "error": "安装包不存在"}
    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "error": "未连接设备：装应用要手机开机、开着 USB 调试（TWRP 里装不了）"}
    adb.serial = devs[0][0]
    if adb.prop("ro.twrp.version"):
        return {"ok": False, "error": "现在是 TWRP 模式，没有包管理器。先重启到系统再装"}
    r = adb.install(src, reinstall=True, grant=True, timeout=900)
    txt = r.text.strip()
    ok = r.rc == 0 and "success" in txt.lower()
    return {"ok": ok, "name": src.name, "size": src.stat().st_size, "log": txt[-500:]}


_start_time = [time.time()]


def _watchdog():
    """窗口关掉后自己退出。

    原来心跳一断 10 秒就退，但浏览器会把后台标签页的定时器限流到一分钟一次，
    窗口最小化几分钟服务就被误杀，之后再点任何按钮都是"连接被掐断"。
    现在：连续 90 秒没动静才退；正在处理请求时一律不退；页面主动 /api/bye 才快速退。
    """
    while True:
        time.sleep(2)
        now = time.time()
        if _activity["inflight"] > 0:
            continue
        bye = _activity["bye"]
        if bye and now - bye > 3:
            log("页面已关闭，服务退出")
            os._exit(0)
        base = _activity["last"] or _start_time[0]
        if now - base > IDLE_LIMIT:
            log(f"{IDLE_LIMIT}s 没有心跳，服务退出")
            os._exit(0)


def extract_partition(part: str) -> dict:
    """把分区 dump 出来存到电脑（需要 TWRP 或 root）。"""
    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "error": "手机没连上（提分区需要 adb，不能是 Fastboot）"}
    adb.serial = devs[0][0]
    part = _clean_part(part)
    if not part:
        return {"ok": False, "error": "分区名不合法"}
    out = ROOT / "backups"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{part}_{time.strftime('%Y%m%d_%H%M%S')}.img"
    remote = f"/dev/block/bootdevice/by-name/{part}"
    r = adb.pull(remote, dest, timeout=1800)
    if not dest.exists() or dest.stat().st_size < 512:
        return {"ok": False, "error": (r.text or "读取失败，需要 TWRP 或 root")[-200:]}
    try:
        subprocess.Popen(["explorer", "/select,", str(dest)])
    except Exception:
        pass
    return {"ok": True, "path": str(dest), "name": dest.name, "size": dest.stat().st_size,
            "dir": str(out)}


def reboot_to(target: str) -> dict:
    """按当前模式选通道：fastboot 用 fastboot，adb/TWRP 用 adb。"""
    from toolbox.fastboot import Fastboot

    target = (target or "system").lower()
    fb = Fastboot()
    fdevs = fb.devices()
    if fdevs:
        fb.serial = fdevs[0]
        arg = {"system": "", "recovery": "recovery", "bootloader": "bootloader", "poweroff": "poweroff"}.get(target, "")
        r = fb.run(["reboot"] + ([arg] if arg else []), timeout=60)
        txt = (r.text or "").strip()
        if r.rc != 0 and target == "poweroff":
            # 有些机型不支持 fastboot reboot poweroff，退回重启
            r = fb.run(["reboot"], timeout=60)
            txt = (r.text or "").strip() + "\n（该机型不支持 fastboot 关机，已改为重启）"
        return {"ok": r.rc == 0, "via": "fastboot", "out": txt[-300:]}

    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "via": "none", "out": "手机没连上：插上线，或进 Fastboot"}
    adb.serial = devs[0][0]
    twrp = bool(adb.prop("ro.twrp.version"))
    if target == "poweroff":
        cmd = "reboot -p"
    elif target == "recovery":
        cmd = "reboot recovery"
    elif target == "bootloader":
        cmd = "reboot bootloader"
    else:
        cmd = "reboot"
    r = adb.shell(cmd, timeout=60)
    return {"ok": True, "via": "twrp" if twrp else "adb", "out": (r.text or f"已发送 {cmd}").strip()[-300:]}


def run_adb(cmd: str) -> dict:
    """执行一条 adb shell 命令，返回输出与退出码。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "out": "", "code": -1, "error": "空命令"}
    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "out": "", "code": -1, "error": "未连接设备"}
    adb.serial = devs[0][0]
    # 以 adb 开头的命令按整条 adb 参数跑，其余当 shell 命令
    low = cmd.lower()
    if low == "adb" or low.startswith(("adb ", "adb\t")):
        tail = cmd[3:].strip()
        try:
            args = shlex.split(tail)
        except ValueError:
            args = tail.split()
        if args and args[0] == "shell":
            # shell 后面整段原样交给手机，保留引号 / 管道 / 重定向
            r = adb.shell(tail[len("shell"):].strip(), timeout=180)
        else:
            r = adb.run(args, timeout=180)
    else:
        r = adb.shell(cmd, timeout=180)
    out = (r.out or "") + (("\n" + r.err) if (r.err or "").strip() else "")
    return {"ok": r.rc == 0, "code": r.rc, "out": out.strip()[:20000]}


def partitions() -> dict:
    """列出手机上所有 by-name 分区。"""
    adb = Adb()
    devs = adb.devices()
    if not devs:
        return {"ok": False, "parts": [], "error": "未连接设备"}
    adb.serial = devs[0][0]
    txt = adb.sh("ls /dev/block/bootdevice/by-name 2>/dev/null || ls /dev/block/by-name 2>/dev/null", timeout=40)
    parts = sorted({p.strip() for p in (txt or "").split() if p.strip() and not p.startswith("ls:")})
    return {"ok": bool(parts), "parts": parts, "count": len(parts)}


def flash_partition(part: str, img: Path) -> dict:
    """刷入分区：Fastboot 走 fastboot flash；TWRP/root 下走 dd。"""
    from toolbox.fastboot import Fastboot

    if not part:
        return {"ok": False, "error": "没指定分区"}
    part = _clean_part(part)
    if not part:
        return {"ok": False, "error": "分区名不合法"}

    fb = Fastboot()
    devs = fb.devices()
    if devs:
        fb.serial = devs[0]
        r = fb.flash(part, img, timeout=1800)
        txt = (r.text or "").strip()
        return {"ok": r.rc == 0 and "FAILED" not in txt.upper(), "mode": "fastboot",
                "log": txt[-400:], "size": img.stat().st_size}

    adb = Adb()
    ad = adb.devices()
    if not ad:
        return {"ok": False, "error": "手机既不在 Fastboot，也没连 adb。要么进 Fastboot，要么进 TWRP"}
    adb.serial = ad[0][0]
    twrp = bool(adb.prop("ro.twrp.version"))
    rooted = bool(adb.shell("su -c id", timeout=10).ok)
    if not twrp and not rooted:
        return {"ok": False, "error": "当前是普通系统模式，没有写分区权限：需要进 Fastboot，或进 TWRP，或先 root"}
    remote = f"/sdcard/_flash_{part}.img"
    if adb.push(img, remote, timeout=3600).rc != 0:
        return {"ok": False, "error": "推送到手机失败，检查存储空间"}
    dev = f"/dev/block/bootdevice/by-name/{part}"
    cmd = f"dd if={remote} of={dev}" if twrp else f"su -c 'dd if={remote} of={dev}'"
    r2 = adb.shell(cmd, timeout=1800)
    adb.shell(f"rm -f {remote}", timeout=60)
    txt = (r2.text or "").strip()
    low = txt.lower()
    bad = r2.rc != 0 or "denied" in low or "no such" in low or "not found" in low
    return {"ok": not bad, "mode": "twrp" if twrp else "adb-root", "log": txt[-400:],
            "size": img.stat().st_size}


# ---------------------------------------------------------------- 全量包
DOWNLOAD_DIR = ROOT / "downloads"
IMAGE_DIR = ROOT / "images"
BACKUP_DIR = ROOT / "backups"


def _rom():
    """懒加载 toolbox.rom —— 模块没就位时服务照样能起。"""
    from toolbox import rom

    return rom


def safe_local(path_text: str) -> Path | None:
    """只允许操作工具箱目录里的文件，挡住 ../../Windows 这种路径。"""
    try:
        p = Path(str(path_text or "")).expanduser()
        p = (p if p.is_absolute() else ROOT / p).resolve()
    except (OSError, ValueError):
        return None
    root = ROOT.resolve()
    return p if (p == root or root in p.parents) else None


def _open_in_explorer(path: Path) -> None:
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["explorer", str(path)])
    except Exception:
        pass


_rom_scan_cache = {"t": 0.0, "key": "", "data": None}


def rom_scan(which: str = "") -> dict:
    """列出 downloads / images / backups 里的全量包（结果缓存 20 秒，大包扫描很慢）。"""
    rom = _rom()
    key = which or ""
    with _lock:
        if (_rom_scan_cache["data"] is not None and _rom_scan_cache["key"] == key
                and time.time() - _rom_scan_cache["t"] < 20):
            return _rom_scan_cache["data"]
    places = [("downloads", DOWNLOAD_DIR), ("images", IMAGE_DIR), ("backups", BACKUP_DIR)]
    if which:
        places = [p for p in places if p[0] == which] or places[:1]
    items = []
    for label, d in places:
        try:
            d.mkdir(parents=True, exist_ok=True)
            for it in rom.scan_packages(d):
                it["where"] = label
                items.append(it)
        except Exception as exc:  # noqa: BLE001
            log(f"扫描 {d} 失败：{exc}")
    items.sort(key=lambda x: str(x.get("mtime") or ""), reverse=True)
    res = {"ok": True, "items": items, "dirs": [str(d) for _, d in places]}
    with _lock:
        _rom_scan_cache.update({"t": time.time(), "key": key, "data": res})
    return res


def rom_inspect(path_text: str) -> dict:
    p = safe_local(path_text)
    if not p or not p.exists():
        return {"ok": False, "error": "找不到这个文件（只认工具箱文件夹里的包）"}
    return _rom().inspect(p)


def rom_unpack(path_text: str, parts=None) -> dict:
    p = safe_local(path_text)
    if not p or not p.exists():
        return {"ok": False, "error": "找不到这个文件（只认工具箱文件夹里的包）"}
    if parts is not None and not isinstance(parts, list):
        parts = [x for x in str(parts).split(",") if x]
    dest = BACKUP_DIR / f"full_{p.name.split('.')[0]}_{time.strftime('%Y%m%d_%H%M%S')}"
    res = _rom().unpack(p, dest, parts or None)
    if res.get("ok"):
        res["dir"] = str(dest)
        _open_in_explorer(dest)
    return res


def rom_flash(path_text: str, parts=None) -> dict:
    """解包 + 整包刷入；TWRP 卡刷包自动走 sideload。"""
    rom = _rom()
    p = safe_local(path_text)
    if not p or not p.exists():
        return {"ok": False, "error": "找不到这个文件（只认工具箱文件夹里的包）"}
    kind = rom.detect_kind(p)
    if kind == "sideload":
        return rom.sideload(p)
    if kind != "fastboot":
        return {"ok": False, "error": "这个包不像刷机包：里面既没有 .img，也没有 META-INF / payload.bin"}
    # 先确认手机在 Fastboot，别解完几个 G 才告诉你没连
    from toolbox.fastboot import Fastboot

    if not Fastboot().devices():
        return {"ok": False, "error": "手机不在 Fastboot 模式：先 `adb reboot bootloader` 再来整包刷"}
    if parts is not None and not isinstance(parts, list):
        parts = [x for x in str(parts).split(",") if x]
    dest = BACKUP_DIR / f"flash_{time.strftime('%Y%m%d_%H%M%S')}"
    un = rom.unpack(p, dest, parts or None)
    if not un.get("ok"):
        return un
    plan = rom.flash_plan(un.get("images") or [], include=parts or None)
    res = rom.flash_images(plan.get("steps") or [])
    res["unpacked"] = un.get("images")
    res["skipped"] = plan.get("skipped")
    return res


def flash_local(part: str, path_text: str) -> dict:
    """刷本机已有的 img（不走浏览器上传）。"""
    p = safe_local(path_text)
    if not p or not p.is_file():
        return {"ok": False, "error": "找不到镜像文件"}
    if p.suffix.lower() != ".img":
        return {"ok": False, "error": "只认 .img 镜像"}
    return flash_partition(part, p)


def roman_images(dir_text: str) -> dict:
    """列一个目录里已解压好的 img（手动解压的情况）。"""
    p = safe_local(dir_text)
    if not p or not p.is_dir():
        return {"ok": False, "error": "目录不存在", "images": []}
    imgs = _rom().image_files(p)
    return {"ok": True, "dir": str(p), "images": imgs}


def rom_plan(dir_text: str, parts=None) -> dict:
    """给已解包的目录排一个刷入顺序（默认跳过危险分区）。"""
    p = safe_local(dir_text)
    if not p or not p.is_dir():
        return {"ok": False, "error": "目录不存在"}
    rom = _rom()
    imgs = rom.image_files(p)
    if not imgs:
        return {"ok": False, "error": "这个目录里没有 .img"}
    if parts is not None and not isinstance(parts, list):
        parts = [x for x in str(parts).split(",") if x]
    plan = rom.flash_plan(imgs, include=parts or None)
    plan.setdefault("ok", True)
    plan["dir"] = str(p)
    return plan


# ---------------------------------------------------------------- IMG 解包/重打包
IMG_WORK = DOWNLOAD_DIR / "img_work"


def _bootimg():
    """懒加载 toolbox.bootimg。"""
    from toolbox import bootimg

    return bootimg


def img_scan() -> dict:
    """列出可以直接拿来解包的 .img，并标出它到底是什么镜像。"""
    bootimg = _bootimg()
    items = []
    for label, d in (("downloads", DOWNLOAD_DIR), ("images", IMAGE_DIR), ("backups", BACKUP_DIR)):
        try:
            if not d.is_dir():
                continue
            for f in d.rglob("*.img"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                try:
                    kind = bootimg.sniff(f)
                except Exception:  # noqa: BLE001
                    kind = {}
                items.append({"name": f.name, "path": str(f), "size": st.st_size,
                              "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                              "where": label, "kind": kind.get("kind") or "unknown",
                              "note": kind.get("note") or ""})
        except Exception as exc:  # noqa: BLE001
            log(f"扫描 {d} 失败：{exc}")
    # 能解包的 boot 镜像排前面，组内按时间倒序（稳定排序）
    items.sort(key=lambda x: x["mtime"], reverse=True)
    items.sort(key=lambda x: 0 if x["kind"] == "boot" else 1)
    return {"ok": True, "items": items[:80]}


def img_inspect(path_text: str) -> dict:
    p = safe_local(path_text)
    if not p or not p.is_file():
        return {"ok": False, "error": "找不到这个镜像（只认工具箱文件夹里的文件）"}
    res = _bootimg().inspect(p)
    if not res.get("ok"):
        try:  # 打不开时把"它到底是什么"补进错误里
            s = _bootimg().sniff(p)
            if s.get("ok"):
                res["kind"] = s.get("kind")
                res["note"] = s.get("note")
                res["error"] = f"{res.get('error') or '打不开'}（{s.get('note')}）"
        except Exception:
            pass
    return res


def img_unpack(path_text: str) -> dict:
    p = safe_local(path_text)
    if not p or not p.is_file():
        return {"ok": False, "error": "找不到这个镜像（只认工具箱文件夹里的文件）"}
    dest = BACKUP_DIR / f"img_{p.stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    res = _bootimg().unpack(p, dest)
    if res.get("ok"):
        res["dir"] = str(dest)
    return res


def img_tree(dir_text: str) -> dict:
    p = safe_local(dir_text)
    if not p or not p.is_dir():
        return {"ok": False, "error": "工作目录不存在（先解包）", "entries": []}
    try:
        return {"ok": True, "dir": str(p), "entries": _bootimg().list_ramdisk(p)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "entries": []}


def img_read(dir_text: str, name: str) -> dict:
    p = safe_local(dir_text)
    if not p or not p.is_dir():
        return {"ok": False, "error": "工作目录不存在"}
    try:
        return _bootimg().read_ramdisk_file(p, name)
    except Exception as exc:  # noqa: BLE001  模块对坏工作目录会抛 BootImageError
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def img_write(dir_text: str, name: str, text: str) -> dict:
    p = safe_local(dir_text)
    if not p or not p.is_dir():
        return {"ok": False, "error": "工作目录不存在"}
    try:
        return _bootimg().write_ramdisk_file(p, name, text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def img_repack(dir_text: str, name: str) -> dict:
    p = safe_local(dir_text)
    if not p or not p.is_dir():
        return {"ok": False, "error": "工作目录不存在（先解包）"}
    out_name = _clean_name(name, "") or (p.name.replace("img_", "", 1) + "_repack.img")
    if not out_name.lower().endswith(".img"):
        out_name += ".img"
    dest = DOWNLOAD_DIR / out_name
    res = _bootimg().repack(p, dest)
    if res.get("ok"):
        res["dir"] = str(DOWNLOAD_DIR)
    return res


# ---------------------------------------------------------------- AI 助手
_ai_lock = threading.Lock()
_ai_stop = threading.Event()   # 用户点了「停止」就置位，正在跑的 agent 会尽快收手
_ai = {
    "messages": [],   # 发给 DeepSeek 的原始上下文
    "log": [],        # 给页面渲染的对话记录
    "pending": [],    # 等用户点头的危险操作
}


def _ai_mod():
    from toolbox import ai

    return ai


# 最近一次会让设备断线的操作（重启/刷机），用来提醒 AI 别把"重启中"当成"拔线了"
_last_action = {"what": "", "expect": "", "ts": 0.0}


def _note_action(what: str, expect: str = "any") -> None:
    _last_action.update({"what": what, "expect": expect or "any", "ts": time.time()})


def _recent_action(within: float = 180.0):
    ts = _last_action["ts"]
    if not ts or time.time() - ts > within:
        return None
    return {"what": _last_action["what"], "expect": _last_action["expect"],
            "secondsAgo": int(time.time() - ts)}


def _mode_now() -> str:
    """轻量探当前模式（不读属性表），给 wait_for_device 轮询用。"""
    from toolbox.fastboot import Fastboot

    adb = Adb()
    devs = adb.devices() if adb.available else []
    if devs:
        st = devs[0][1]
        if st in ("sideload", "unauthorized", "offline", "recovery"):
            return st
        adb.serial = devs[0][0]
        return "twrp" if adb.prop("ro.twrp.version") else "adb"
    fb = Fastboot()
    if fb.available and fb.devices():
        return "fastboot"
    return "none"


def wait_for_device(expect: str = "any", timeout: int = 90) -> dict:
    """等设备以指定模式回来 —— 重启之后 AI 必须用它，而不是断定拔线。"""
    alias = {"recovery": "twrp", "recover": "twrp", "系统": "adb", "正常": "adb", "线刷": "fastboot"}
    want = str(expect or "any").strip().lower()
    want = alias.get(want, want)
    if want not in ("any", "adb", "twrp", "fastboot", "sideload", "unauthorized", "offline"):
        want = "any"
    try:
        limit = max(5, min(int(timeout or 90), 300))
    except (TypeError, ValueError):
        limit = 90
    t0 = time.time()
    seen = "none"
    while time.time() - t0 < limit:
        if _ai_stop.is_set():  # 用户点了停止，别再傻等
            return {"ok": False, "stopped": True, "mode": seen,
                    "waited": round(time.time() - t0, 1), "error": "用户停止了等待"}
        try:
            seen = _mode_now()
        except Exception:  # noqa: BLE001
            seen = "none"
        if seen != "none" and (want == "any" or seen == want):
            waited = round(time.time() - t0, 1)
            d = state(force=True)
            return {"ok": True, "mode": seen, "waited": waited, "state": d,
                    "note": f"设备已以 {seen} 模式连上（等了 {waited} 秒），不是拔线，是重启过程"}
        time.sleep(2)
    waited = round(time.time() - t0, 1)
    return {"ok": False, "mode": seen, "waited": waited,
            "error": (f"等了 {limit} 秒，设备还没以 {want} 模式出现（现在：{seen}）。"
                      f"如果刚才重启到了 recovery/TWRP，可能还停在开机动画或 recovery 主界面；"
                      f"再调用一次 wait_for_device 多等一会儿，仍不行再让用户检查数据线。")}


def _ai_context() -> str:
    """给模型的现场信息：设备状态 + 本机有哪些包。"""
    try:
        d = state()
    except Exception:
        d = {}
    if d.get("connected"):
        head = (f"当前设备：{d.get('model')}（{d.get('codename')}），{d.get('modeLabel')}，"
                f"Android {d.get('android') or '?'}，"
                f"BL {'已解锁' if d.get('unlocked') else '未解锁/未知'}，"
                f"Magisk {d.get('magisk') or '无'}，分区 {d.get('partitions')} 个")
    else:
        head = f"当前没有设备连接（{d.get('note') or '未连接'}）"
    lines = [head, "工具箱根目录：" + str(ROOT)]
    act = _recent_action()
    if act:
        lines.append(f"最近操作：{act['what']}（{act['secondsAgo']} 秒前）。"
                     f"如果现在显示未连接，多半是重启过程中 ADB 断开，不是拔线；先 wait_for_device 等它。")
    for label, folder in (("downloads", DOWNLOAD_DIR), ("images", IMAGE_DIR), ("apk", APK_DIR)):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            names = [f.name for f in sorted(folder.iterdir()) if f.is_file()][:15]
        except Exception:
            names = []
        lines.append(f"{label}/ 里的文件：{'、'.join(names) if names else '（空）'}")
    return "\n".join(lines)


def _step_text(name: str, args: dict, res: dict) -> str:
    if not isinstance(res, dict):
        return str(res)[:200]
    if res.get("ok") is False:
        return "失败：" + str(res.get("error") or res.get("log") or "未知原因")[:200]
    for k in ("log", "out", "note", "reply", "path", "dir"):
        v = res.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[-1][:200]
    return "完成"


def _fmt_steps(steps) -> list:
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        res = s.get("result") if isinstance(s.get("result"), dict) else {}
        out.append({
            "name": s.get("name"),
            "args": s.get("args") or {},
            "ok": res.get("ok", True) is not False,
            "text": _step_text(s.get("name"), s.get("args") or {}, res),
        })
    return out


def _ai_executor(name: str, args: dict) -> dict:
    """AI 能调用的全部动作都走这里，逐个对应到已有函数。"""
    args = args or {}
    try:
        if name == "get_device_state":
            d = state(force=True)
            act = _recent_action()
            if act:
                d["recentAction"] = act
                if not d.get("connected") and act["secondsAgo"] < 150:
                    d["hint"] = (f"{act['secondsAgo']} 秒前刚执行过「{act['what']}」，设备重启时 ADB 会断开 "
                                 f"10~60 秒，这是正常的，不是拔线。下一步调用 wait_for_device"
                                 f"(expect={act['expect'] or 'any'}) 等它回来，别让用户检查数据线。")
            return d
        if name == "wait_for_device":
            return wait_for_device(args.get("expect") or "any", args.get("timeout") or 90)
        if name == "list_partitions":
            return partitions()
        if name == "list_local_packages":
            which = str(args.get("dir") or "").strip().lower()
            return rom_scan(which if which in ("downloads", "images", "backups") else "")
        if name == "inspect_package":
            return rom_inspect(args.get("path", ""))
        if name == "extract_package":
            return rom_unpack(args.get("path", ""), args.get("parts"))
        if name == "flash_local_image":
            res = flash_local(args.get("part", ""), args.get("path", ""))
            if res.get("ok"):
                res["hint"] = ("刷好了。要回系统就 reboot_device(target=system)，"
                               "然后 wait_for_device(expect='adb') 确认能开机。")
            return res
        if name == "flash_package":
            res = rom_flash(args.get("path", ""), args.get("parts"))
            if res.get("ok"):
                res["hint"] = ("整包刷完。一般接着 reboot_device(target=system)，"
                               "再 wait_for_device(expect='adb')；第一次开机可能要几分钟。")
            return res
        if name == "sideload_package":
            p = safe_local(args.get("path", ""))
            if not p or not p.exists():
                return {"ok": False, "error": "找不到这个包"}
            res = _rom().sideload(p)
            if res.get("ok"):
                _note_action("sideload 卡刷", "twrp")
                res["hint"] = ("sideload 结束，设备通常会自己重启或停在 TWRP。"
                               "用 wait_for_device 等它，别急着判断没连上。")
            return res
        if name == "extract_partition":
            return extract_partition(args.get("part", ""))
        if name == "reboot_device":
            target = (args.get("target") or "system").strip().lower()
            res = reboot_to(target)
            if res.get("ok"):
                if target == "poweroff":
                    _note_action("关机", "poweroff")
                    res["hint"] = ("设备正在关机，它不会自己回来。提示用户长按电源键开机，"
                                   "然后用 wait_for_device(expect='adb') 等它重新连上。")
                else:
                    expect = {"recovery": "twrp", "bootloader": "fastboot", "system": "adb"}.get(target, "any")
                    _note_action(f"重启到 {target}", expect)
                    res["hint"] = (f"重启命令已发出，设备会断开 10~60 秒再以 {expect} 模式回来。"
                                   f"这期间 get_device_state 显示未连接是正常的，不是拔线："
                                   f"下一步调用 wait_for_device(expect='{expect}') 等它。")
            return res
        if name == "install_apk":
            nm = _clean_name(args.get("name", ""), "")
            if not nm:
                return {"ok": False, "error": "没给安装包名字"}
            return install_apk(APK_DIR / nm)
        if name == "adb_shell":
            return run_adb(args.get("cmd", ""))
        return {"ok": False, "error": f"不认识的工具：{name}"}
    except Exception as exc:  # noqa: BLE001
        log(f"AI 工具 {name} 出错\n{traceback.format_exc()}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _trim_messages(msgs: list, keep: int = 40) -> list:
    """只留 system + 最近若干条，防止上下文无限膨胀。"""
    if len(msgs) <= keep:
        return msgs
    head = [m for m in msgs[:1] if m.get("role") == "system"]
    tail = msgs[-keep:]
    while tail and tail[0].get("role") == "tool":  # tool 消息不能脱离它的 assistant
        tail = tail[1:]
    return head + tail


def _ai_run(allow_risky: bool) -> dict:
    ai = _ai_mod()
    key = ai.key_load()
    if not key:
        return {"ok": False, "error": "还没设置 DeepSeek API Key"}
    _ai_stop.clear()
    try:
        res = ai.run_agent(_ai["messages"], _ai_executor, allow_risky=allow_risky, key=key,
                           should_stop=_ai_stop.is_set)
    except Exception as exc:  # noqa: BLE001
        log("AI 运行出错\n" + traceback.format_exc())
        entry = {"role": "bot", "text": "", "steps": [], "pending": [],
                 "error": f"AI 调用出错：{type(exc).__name__}: {exc}"}
        _ai["log"].append(entry)
        return {"ok": False, "error": entry["error"], "entry": entry, "pending": [], "log": list(_ai["log"])}
    entry = {
        "role": "bot",
        "text": res.get("reply") or "",
        "steps": _fmt_steps(res.get("steps")),
        "pending": [{"name": p.get("name"), "args": p.get("args")} for p in (res.get("pending") or [])],
        "error": res.get("error") or "",
    }
    _ai["pending"] = res.get("pending") or []
    _ai["log"].append(entry)
    if len(_ai["log"]) > 60:
        del _ai["log"][:-60]
    _ai["messages"] = _trim_messages(_ai["messages"])
    return {"ok": bool(res.get("ok")), "error": res.get("error") or "",
            "entry": entry, "pending": entry["pending"], "log": list(_ai["log"])}


def ai_chat(text: str, allow_risky: bool = False) -> dict:
    ai = _ai_mod()
    if not ai.key_has():
        return {"ok": False, "error": "还没设置 DeepSeek API Key，点右上角「设置 Key」"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "先说一句你想干什么"}
    if not _ai_lock.acquire(timeout=3.0):
        return {"ok": False, "error": "上一条还在处理，等它跑完"}
    try:
        if not _ai["messages"]:
            _ai["messages"] = [{"role": "system", "content": ai.system_prompt(_ai_context())}]
        _ai["messages"].append({"role": "user", "content": text})
        _ai["log"].append({"role": "user", "text": text})
        if len(_ai["log"]) > 60:
            del _ai["log"][:-60]
        return _ai_run(allow_risky)
    finally:
        _ai_lock.release()


def ai_approve() -> dict:
    ai = _ai_mod()
    pending = _ai["pending"]
    if not pending:
        return {"ok": False, "error": "没有待确认的操作"}
    if not _ai_lock.acquire(timeout=3.0):
        return {"ok": False, "error": "上一条还在处理，等它跑完"}
    try:
        step1 = ai.approve_pending(_ai["messages"], pending, _ai_executor)
        _ai["pending"] = []
        _ai["log"].append({
            "role": "bot", "text": "已执行你确认的操作",
            "steps": _fmt_steps(step1.get("steps")), "pending": [],
            "error": "" if step1.get("ok") else str(step1.get("error") or ""),
        })
        # 继续让模型总结；后面再想要危险操作还得再确认一次
        return _ai_run(False)
    finally:
        _ai_lock.release()


def ai_reset() -> dict:
    _ai["messages"] = []
    _ai["log"] = []
    _ai["pending"] = []
    try:
        _ai_mod().reset_session()
    except Exception:
        pass
    return {"ok": True}


def ai_cancel() -> dict:
    """丢弃还没执行的危险操作；把那条 assistant(tool_calls) 弹掉保持一致。"""
    if not _ai["pending"]:
        return {"ok": False, "error": "没有待确认的操作"}
    _ai["pending"] = []
    while _ai["messages"] and _ai["messages"][-1].get("role") == "assistant" and _ai["messages"][-1].get("tool_calls"):
        _ai["messages"].pop()
    _ai["log"].append({"role": "bot", "text": "已取消，没有执行任何操作", "steps": [], "pending": [], "error": ""})
    return {"ok": True, "log": list(_ai["log"])}


def _ai_model(ai) -> str:
    """当前实际在用的模型（模型兜底成功后会变）。"""
    return str(getattr(ai, "_active_model", "") or getattr(ai, "MODEL", ""))


def ai_state() -> dict:
    ai = _ai_mod()
    return {"ok": True, "hasKey": ai.key_has(), "mask": ai.key_mask(),
            "model": _ai_model(ai), "base": ai.BASE_URL,
            "models": list(getattr(ai, "MODELS", [getattr(ai, "MODEL", "")])),
            "pending": [{"name": p.get("name"), "args": p.get("args")} for p in _ai["pending"]],
            "log": _ai["log"]}


def ai_test() -> dict:
    ai = _ai_mod()
    key = ai.key_load()
    if not key:
        return {"ok": False, "error": "还没设置 Key"}
    r = ai.chat([{"role": "user", "content": "只回复两个字：可用"}], key=key, timeout=40)
    if r.get("error"):
        return {"ok": False, "error": r["error"]}
    try:
        txt = r["choices"][0]["message"]["content"]
    except Exception:
        txt = ""
    return {"ok": True, "reply": (txt or "").strip()[:120]}


# ---------------------------------------------------------------- 终端补全
SHELL_CMDS = [
    "getprop", "setprop", "pm", "am", "dumpsys", "logcat", "wm", "df", "du", "ls", "cd",
    "cat", "reboot", "input", "settings", "screencap", "screenrecord", "uname", "uptime",
    "su", "top", "ps", "kill", "mount", "id", "which", "echo", "rm", "mkdir", "cp", "mv",
    "svc", "cmd", "monkey", "getevent", "ifconfig", "ip", "netstat", "ping", "date", "stat",
]
ADB_SUBCMDS = [
    "devices", "shell", "push", "pull", "install", "uninstall", "reboot", "logcat", "get-state",
    "get-serialno", "wait-for-device", "forward", "reverse", "root", "unroot", "remount",
    "disable-verity", "sideload", "exec-out", "bugreport", "kill-server", "start-server",
    "version", "connect",
]
EXTRA = {
    "dumpsys": ["battery", "meminfo", "cpuinfo", "wifi", "power", "window", "activity", "display",
                "package", "notification", "alarm", "connectivity", "bluetooth_manager"],
    "pm": ["list packages", "list packages -3", "list packages -s", "uninstall", "disable-user",
           "enable", "clear", "install", "path", "dump"],
    "am": ["start", "force-stop", "broadcast", "startservice", "kill"],
    "settings": ["get", "put", "list", "delete", "list global", "list secure", "list system",
                 "get global private_dns_mode", "put global private_dns_mode hostname"],
    "wm": ["size", "density"],
    "input": ["keyevent 26", "keyevent 3", "keyevent 4", "tap 500 900",
              "swipe 500 1600 500 400", "text hello"],
    "logcat": ["-d", "-d -t 200", "-c", "-v time", "| grep -i error"],
    "svc": ["wifi enable", "wifi disable", "data enable", "data disable", "power stayon true"],
}
PHONE_LISTS: dict[str, tuple[float, list]] = {}
PATH_CMDS = {"ls", "cd", "cat", "du", "rm", "mkdir", "cp", "mv", "stat", "screencap"}


_serial_cache = {"t": 0.0, "serial": ""}


def _one_adb():
    """拿一台已连上的设备，没有就回 None（序列号缓存 10 秒，别每次 Tab 都执行 adb）。"""
    adb = Adb()
    if not adb.available:
        return None
    now = time.time()
    if now - _serial_cache["t"] < 10:
        if not _serial_cache["serial"]:
            return None
        adb.serial = _serial_cache["serial"]
        return adb
    devs = adb.devices()
    _serial_cache["t"] = now
    _serial_cache["serial"] = devs[0][0] if devs else ""
    if not devs:
        return None
    adb.serial = _serial_cache["serial"]
    return adb


def _phone_list(kind: str, fetcher, ttl: int = 45) -> list[str]:
    """手机上的属性/服务/包名列表，缓存一会儿，别每按一次 Tab 都去问 adb。"""
    stamp, val = PHONE_LISTS.get(kind, (0.0, []))
    if val and time.time() - stamp < ttl:
        return val
    try:
        val = fetcher() or []
    except Exception:  # noqa: BLE001
        val = []
    PHONE_LISTS[kind] = (time.time(), val)
    return val


def _phone_props() -> list[str]:
    adb = _one_adb()
    if not adb:
        return []
    out = adb.sh("getprop", timeout=30)
    return [m.group(1) for m in (re.match(r"\[([^\]]+)\]", ln.strip()) for ln in out.splitlines()) if m]


def _phone_services() -> list[str]:
    adb = _one_adb()
    if not adb:
        return []
    out = adb.sh("dumpsys -l", timeout=30)
    return [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.strip().endswith(":")]


def _phone_packages() -> list[str]:
    adb = _one_adb()
    if not adb:
        return []
    return adb.installed_packages(third_party=False)[:500]


def _local_apks() -> list[str]:
    try:
        APK_DIR.mkdir(parents=True, exist_ok=True)
        return [str(f) for f in sorted(APK_DIR.glob("*.apk"))]
    except Exception:  # noqa: BLE001
        return []


def _remote_paths(typed: str, limit: int = 60) -> list[str]:
    """手机上补路径：/sdc 这种补成 /sdcard/…，没有斜杠就默认在 /sdcard 下找。"""
    adb = _one_adb()
    if not adb:
        return []
    typed = typed or ""
    if "/" in typed:
        dirname, _, base = typed.rpartition("/")
        dirname = (dirname + "/") if dirname else "/"
    else:
        dirname, base = "/sdcard/", typed
    out = adb.sh(
        f"for p in '{dirname}'*; do [ -e \"$p\" ] || continue; "
        f"if [ -d \"$p\" ]; then echo \"$p/\"; else echo \"$p\"; fi; done 2>/dev/null",
        timeout=20,
    )
    items = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("ls:"):
            continue
        is_dir = ln.endswith("/")
        path = ln.rstrip("/")
        if not path.startswith(dirname) or path == dirname.rstrip("/"):
            continue
        name = path.rsplit("/", 1)[-1]
        if base and not name.startswith(base):
            continue
        items.append(path + ("/" if is_dir else ""))
    items.sort()
    return items[:limit]


def _filter(items, prefix: str, limit: int = 60) -> list[str]:
    p = (prefix or "").lower()
    return [x for x in items if x.lower().startswith(p)][:limit]


def complete_line(line: str) -> dict:
    """给终端做 Tab 补全：输入到光标为止的内容，返回候选的完整命令行。"""
    raw = (line or "").replace("\r", " ").replace("\n", " ")
    body = raw.rstrip()
    ends_space = bool(raw) and raw.endswith(" ")
    toks = body.split() if body else []
    device = _one_adb() is not None

    def finish(kind: str, prefix_toks: list[str], values: list[str], space: bool = True) -> dict:
        items = []
        head = " ".join(prefix_toks)
        for v in values:
            item = (head + " " + v).strip()
            if space and not item.endswith("/"):
                item += " "
            items.append(item)
        return {"ok": True, "kind": kind, "items": items[:80], "device": device}

    # 空行 / 正在敲第一个词
    if not toks:
        return finish("cmd", [], SHELL_CMDS)
    if len(toks) == 1 and not ends_space:
        return finish("cmd", [], _filter(SHELL_CMDS, toks[0]))

    # ---- adb 开头的完整命令
    if toks[0] == "adb":
        sub = toks[1] if len(toks) > 1 else ""
        if len(toks) == 1:  # 只打了 "adb" / "adb "
            return finish("subcmd", ["adb"], ADB_SUBCMDS)
        if len(toks) == 2 and not ends_space:
            return finish("subcmd", ["adb"], _filter(ADB_SUBCMDS, sub))
        if sub == "shell":
            inner = toks[2:]
            inner_ends = ends_space
            if not inner:
                return finish("cmd", ["adb", "shell"], SHELL_CMDS)
            if len(inner) == 1 and not inner_ends:
                return finish("cmd", ["adb", "shell"], _filter(SHELL_CMDS, inner[0]))
            return _complete_shell(inner, inner_ends, device, ["adb", "shell"])
        if sub in ("pull", "push") and len(toks) >= 3:
            cur = "" if ends_space else toks[-1]
            vals = _remote_paths(cur) if sub == "pull" else []
            return finish("path", toks[:-1] if not ends_space else toks, vals)
        if sub in ("install", "uninstall") and (len(toks) >= 3 or ends_space):
            cur = "" if ends_space else toks[-1]
            return finish("file", toks[:-1] if not ends_space else toks, _filter(_local_apks(), cur))
        if sub in ("reboot",) and len(toks) == 2:
            return finish("static", ["adb", "reboot"], ["", "bootloader", "recovery", "sideload"])
        return {"ok": True, "kind": "subcmd", "items": [], "device": device}

    return _complete_shell(toks, ends_space, device, [])


def _complete_shell(toks: list[str], ends_space: bool, device: bool, prefix_toks: list[str]) -> dict:
    """补 shell 命令的参数（属性名 / 服务名 / 包名 / 路径 / 常用参数）。"""
    def finish(kind: str, prefix: list[str], values: list[str], space: bool = True) -> dict:
        head = " ".join(prefix)
        items = []
        for v in values:
            item = (head + " " + v).strip()
            if space and not item.endswith("/"):
                item += " "
            items.append(item)
        return {"ok": True, "kind": kind, "items": items[:80], "device": device}

    cmd = toks[0] if toks else ""
    if not cmd:
        return finish("cmd", prefix_toks, SHELL_CMDS)
    cur = "" if ends_space or len(toks) <= 1 else toks[-1]
    args = toks[1:]
    base = prefix_toks + [cmd]

    if cmd == "getprop":
        vals = _filter(_phone_list("props", _phone_props), cur)
        return finish("prop", base if not args else base, vals)
    if cmd == "setprop":
        if len(args) < 2 or (len(args) == 2 and ends_space):
            return finish("static", base, EXTRA["settings"][:0])
        vals = _filter(_phone_list("props", _phone_props), cur)
        return finish("prop", base + args[:-1], vals)
    if cmd == "dumpsys":
        vals = _filter(_phone_list("services", _phone_services) or EXTRA["dumpsys"], cur)
        return finish("service", base, vals)
    if cmd == "pm":
        pool = EXTRA["pm"] + _filter(_phone_list("pkgs", _phone_packages), cur)
        return finish("pkg", base, _filter(pool, cur) if not args else _filter(pool, cur))
    if cmd in EXTRA and cmd not in PATH_CMDS:
        return finish("static", base, _filter(EXTRA[cmd], cur))
    if cmd == "screencap" and cur.startswith("-"):
        return finish("static", base, _filter(["-p", "-d /sdcard/shot.png"], cur))
    if cmd in PATH_CMDS:
        vals = _remote_paths(cur)
        return finish("path", base + (args[:-1] if args and not ends_space else args), vals, space=False)
    return {"ok": True, "kind": "cmd", "items": [], "device": device}


# ---------------------------------------------------------------- 退出
def _quit_soon():
    time.sleep(0.4)
    log("用户点了关闭按钮，服务退出")
    os._exit(0)


class Handler(BaseHTTPRequestHandler):
    server_version = "Mi6Toolbox/" + VERSION

    def log_message(self, fmt, *args):  # 安静点：访问日志不刷，出事了看 _fail
        pass

    # ---------------------------------------------------------------- 基础
    def _send(self, code: int, body: bytes, ctype: str):
        if getattr(self, "_sent", False):
            return
        self._sent = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass  # 页面已经关掉或刷新了，写失败很正常

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, exc: Exception):
        """兜底：没预料到的异常也回一条 JSON，绝不把连接直接掐掉。"""
        log(f"{getattr(self, 'command', '?')} {self.path} 出错\n{traceback.format_exc()}")
        if getattr(self, "_streaming", False):  # 已经开了流，只能补一个 error 事件
            self._sse({"type": "error", "error": f"后端出错：{type(exc).__name__}: {exc}"})
            self._sse({"type": "done"})
            return
        try:
            self._json({"ok": False, "out": "", "code": -1,
                        "error": f"后端出错：{type(exc).__name__}: {exc}"}, 500)
        except Exception:
            pass

    # ---------------------------------------------------------------- SSE
    def _sse_start(self):
        """开一条 text/event-stream。HTTP/1.0 没有 chunked，靠关连接表示结束。"""
        self._streaming = True
        self._sent = True
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            self._streaming = False

    def _sse(self, obj: dict):
        if not getattr(self, "_streaming", False):
            return
        try:
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            self._streaming = False  # 页面已经走了

    def _ai_chat_stream(self, text: str, allow_risky: bool, deep: bool):
        """边想边把思考过程和工具调用推给页面。"""
        ai = _ai_mod()
        self._sse_start()

        def fail(msg: str):
            self._sse({"type": "error", "error": msg})
            self._sse({"type": "done"})

        if not ai.key_has():
            fail("还没设置 DeepSeek API Key，点右上角「设置 Key」")
            return
        text = (text or "").strip()
        if not text:
            fail("先说一句你想干什么")
            return
        if not _ai_lock.acquire(timeout=3.0):  # 停止后上一轮还要收尾，给它几秒
            fail("上一条还在处理，等它跑完")
            return
        try:
            if not _ai["messages"]:
                _ai["messages"] = [{"role": "system", "content": ai.system_prompt(_ai_context())}]
            _ai["messages"].append({"role": "user", "content": text})
            _ai["log"].append({"role": "user", "text": text})
            if len(_ai["log"]) > 60:
                del _ai["log"][:-60]

            key = ai.key_load()
            _ai_stop.clear()
            self._sse({"type": "start", "model": _ai_model(ai), "deep": bool(deep)})

            think_parts: list[str] = []

            def on_event(ev):
                if not isinstance(ev, dict):
                    return
                if not self._streaming:  # 页面已经断开了，让 agent 收手，别白烧 token
                    _ai_stop.set()
                    return
                kind = ev.get("type")
                if kind == "reasoning" and ev.get("delta"):
                    think_parts.append(str(ev["delta"]))
                if kind in ("round", "reasoning", "content", "tool_call", "tool_result", "pending"):
                    self._sse(ev)

            runner = getattr(ai, "run_agent_stream", None)
            try:
                if runner is not None:
                    res = runner(_ai["messages"], _ai_executor, allow_risky=allow_risky, key=key,
                                 thinking=bool(deep), on_event=on_event,
                                 should_stop=_ai_stop.is_set)
                else:  # 模块还没升级：退回非流式，前端仍能拿到最终结果
                    res = ai.run_agent(_ai["messages"], _ai_executor, allow_risky=allow_risky, key=key)
            except Exception as exc:  # noqa: BLE001
                log("AI 流式出错\n" + traceback.format_exc())
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "reply": "", "steps": [], "pending": []}

            entry = {
                "role": "bot",
                "text": res.get("reply") or "",
                "steps": _fmt_steps(res.get("steps")),
                "pending": [{"name": p.get("name"), "args": p.get("args")}
                            for p in (res.get("pending") or [])],
                "error": res.get("error") or "",
                "reasoning": "".join(think_parts).strip()[:20000],
            }
            _ai["pending"] = res.get("pending") or []
            _ai["log"].append(entry)
            if len(_ai["log"]) > 60:
                del _ai["log"][:-60]
            _ai["messages"] = _trim_messages(_ai["messages"])
            self._sse({"type": "final", "ok": bool(res.get("ok")), "text": entry["text"],
                       "steps": entry["steps"], "pending": entry["pending"],
                       "error": entry["error"], "log": list(_ai["log"])})
            self._sse({"type": "done"})
        finally:
            _ai_stop.clear()
            _ai_lock.release()

    def _body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return b""
        try:
            return self.rfile.read(n)
        except Exception:
            return b""

    def _recv_file(self, subdir: str, default_name: str) -> Path:
        """把请求体整个存成临时文件（刷机 / 上传共用）。"""
        raw = urllib.parse.unquote(self.headers.get("X-Filename") or default_name)
        name = _clean_name(raw, default_name)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        tmp = ROOT / "downloads" / subdir / name
        tmp.parent.mkdir(parents=True, exist_ok=True)
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(262144, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        return tmp

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        _activity["inflight"] += 1
        _activity["last"] = time.time()
        _activity["bye"] = 0.0  # 还有请求进来就说明页面活着，取消退出
        self._sent = False
        try:
            self._post()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
        finally:
            _activity["inflight"] -= 1

    def _post(self):
        """刷入镜像 / 接收拖进来的文件 / 心跳。"""
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/heartbeat":  # 页面关闭时的 sendBeacon 也走这里
            _activity["bye"] = 0.0
            self._json({"ok": True})
            return
        if parsed.path == "/api/bye":
            _activity["bye"] = time.time()
            self._json({"ok": True})
            return
        if parsed.path == "/api/flash":
            part = urllib.parse.parse_qs(parsed.query).get("part", [""])[0]
            tmp = self._recv_file("_flash_tmp", "image.img")
            try:
                res = flash_partition(part, tmp)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self._json(res)
            return
        if parsed.path == "/api/adb":
            cmd = self._body().decode("utf-8", "replace")
            self._json(run_adb(cmd))
            return
        if parsed.path == "/api/upload":
            q = urllib.parse.parse_qs(parsed.query)
            target = q.get("dir", ["/sdcard/Download"])[0]
            tmp = self._recv_file("_upload_tmp", "upload.bin")
            try:
                res = upload(tmp, target)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self._json(res)
            return
        if parsed.path == "/api/install":
            q = urllib.parse.parse_qs(parsed.query)
            name = Path(q.get("name", [""])[0]).name
            if name:  # 装 apk/ 里现成的包
                self._json(install_apk(APK_DIR / name))
                return
            tmp = self._recv_file("_apk_tmp", "upload.apk")  # 拖进来的包
            try:
                self._json(install_apk(tmp))
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return
        if parsed.path == "/api/rom/unpack":
            qs = urllib.parse.parse_qs(parsed.query)
            self._json(rom_unpack(qs.get("path", [""])[0], qs.get("parts", [None])[0]))
            return
        if parsed.path == "/api/rom/flash":
            qs = urllib.parse.parse_qs(parsed.query)
            self._json(rom_flash(qs.get("path", [""])[0], qs.get("parts", [None])[0]))
            return
        if parsed.path == "/api/rom/sideload":
            qs = urllib.parse.parse_qs(parsed.query)
            p = safe_local(qs.get("path", [""])[0])
            if not p or not p.exists():
                self._json({"ok": False, "error": "找不到这个包"})
            else:
                self._json(_rom().sideload(p))
            return
        if parsed.path == "/api/rom/save":
            saved = self._recv_file("", "package.tgz")
            self._json({"ok": True, "path": str(saved), "size": saved.stat().st_size})
            return
        if parsed.path == "/api/img/save":
            IMG_WORK.mkdir(parents=True, exist_ok=True)
            saved = self._recv_file("img_work", "upload.img")
            self._json({"ok": True, "path": str(saved), "size": saved.stat().st_size})
            return
        if parsed.path == "/api/img/unpack":
            qs = urllib.parse.parse_qs(parsed.query)
            self._json(img_unpack(qs.get("path", [""])[0]))
            return
        if parsed.path == "/api/img/file":
            qs = urllib.parse.parse_qs(parsed.query)
            text = self._body().decode("utf-8", "replace")
            self._json(img_write(qs.get("dir", [""])[0], qs.get("name", [""])[0], text))
            return
        if parsed.path == "/api/img/repack":
            qs = urllib.parse.parse_qs(parsed.query)
            self._json(img_repack(qs.get("dir", [""])[0], qs.get("name", [""])[0]))
            return
        if parsed.path == "/api/flash-local":
            qs = urllib.parse.parse_qs(parsed.query)
            self._json(flash_local(qs.get("part", [""])[0], qs.get("path", [""])[0]))
            return
        if parsed.path == "/api/ai/key":
            if urllib.parse.parse_qs(parsed.query).get("clear", ["0"])[0] == "1":
                self._json(_ai_mod().key_clear())
            else:
                self._json(_ai_mod().key_save(self._body().decode("utf-8", "replace")))
            return
        if parsed.path == "/api/ai/chat":
            raw = self._body().decode("utf-8", "replace").strip()
            if raw.startswith("{"):
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {"text": raw}
            else:
                data = {"text": raw}
            self._json(ai_chat(str(data.get("text") or ""), bool(data.get("allowRisky"))))
            return
        if parsed.path == "/api/ai/stream":
            raw = self._body().decode("utf-8", "replace").strip()
            if raw.startswith("{"):
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {"text": raw}
            else:
                data = {"text": raw}
            self._ai_chat_stream(str(data.get("text") or ""), bool(data.get("allowRisky")),
                                 bool(data.get("deep")))
            return
        if parsed.path == "/api/ai/approve":
            self._json(ai_approve())
            return
        if parsed.path == "/api/ai/cancel":
            self._json(ai_cancel())
            return
        if parsed.path == "/api/ai/stop":
            _ai_stop.set()
            self._json({"ok": True})
            return
        if parsed.path == "/api/ai/reset":
            self._json(ai_reset())
            return
        if parsed.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=_quit_soon, daemon=True).start()
            return
        self._send(404, b"not found", "text/plain")

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        _activity["inflight"] += 1
        _activity["last"] = time.time()
        _activity["bye"] = 0.0  # 页面刷新时会先发 bye，新页面的第一个请求就把退出取消
        self._sent = False
        try:
            self._get()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
        finally:
            _activity["inflight"] -= 1

    def _get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        if path == "/api/version":
            self._json({"app": APP_ID, "version": VERSION, "build": _build()})
            return
        if path == "/api/state":
            self._json(state(force=q.get("force", ["0"])[0] == "1"))
            return
        if path == "/api/list":
            self._json(list_dir(q.get("path", ["/sdcard"])[0]))
            return
        if path == "/api/pull":
            self._json(pull_file(q.get("path", [""])[0]))
            return
        if path == "/api/reboot":
            self._json(reboot_to(q.get("to", ["system"])[0]))
            return
        if path == "/api/open":
            which = q.get("dir", ["backups"])[0]
            target = ROOT / which if which in ("backups", "downloads", "apk") else ROOT / "backups"
            target.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.Popen(["explorer", str(target)])
                ok = True
            except Exception:
                ok = False
            self._json({"ok": ok, "dir": str(target)})
            return
        if path == "/api/extract":
            self._json(extract_partition(q.get("part", [""])[0]))
            return
        if path == "/api/partitions":
            self._json(partitions())
            return
        if path == "/api/apks":
            self._json(list_apks())
            return
        if path == "/api/rom/scan":
            self._json(rom_scan(q.get("dir", [""])[0]))
            return
        if path == "/api/rom/inspect":
            self._json(rom_inspect(q.get("path", [""])[0]))
            return
        if path == "/api/rom/images":
            self._json(roman_images(q.get("dir", [""])[0]))
            return
        if path == "/api/rom/plan":
            self._json(rom_plan(q.get("dir", [""])[0], q.get("parts", [None])[0]))
            return
        if path == "/api/img/scan":
            self._json(img_scan())
            return
        if path == "/api/img/inspect":
            self._json(img_inspect(q.get("path", [""])[0]))
            return
        if path == "/api/img/tree":
            self._json(img_tree(q.get("dir", [""])[0]))
            return
        if path == "/api/img/file":
            self._json(img_read(q.get("dir", [""])[0], q.get("name", [""])[0]))
            return
        if path == "/api/ai/state":
            self._json(ai_state())
            return
        if path == "/api/ai/test":
            self._json(ai_test())
            return
        if path == "/api/complete":
            try:
                self._json(complete_line(q.get("line", [""])[0]))
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []})
            return
        if path == "/api/heartbeat":
            self._json({"ok": True})
            return
        if path == "/api/bye":
            _activity["bye"] = time.time()
            self._json({"ok": True})
            return
        self._static(path)

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        root = STATIC.resolve()
        try:
            f = (root / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            self._send(400, b"bad path", "text/plain")
            return
        if f != root and root not in f.parents:
            self._send(404, b"not found", "text/plain")
            return
        if not f.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(f.suffix.lower(), "application/octet-stream")
        self._send(200, f.read_bytes(), ctype)


def open_edge(url: str) -> bool:
    # 窗口居中：按屏幕尺寸算位置
    win_w, win_h = 1120, 780
    try:
        import ctypes

        user32 = ctypes.windll.user32
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        x = max(0, (sw - win_w) // 2)
        y = max(0, (sh - win_h) // 2)
        pos = f"--window-position={x},{y}"
    except Exception:
        pos = ""
    for p in EDGE_PATHS:
        if Path(p).exists():
            args = [p, f"--app={url}", f"--window-size={win_w},{win_h}"]
            if pos:
                args.append(pos)
            subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
    import webbrowser

    webbrowser.open(url)
    return False


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """单个连接炸了不要闷声吞掉，记进日志。"""
        log(f"处理 {client_address} 时出错\n{traceback.format_exc()}")


def instance_alive(port: int) -> bool:
    """那个端口上是不是已经有一个本程序在跑。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("app") == APP_ID
    except Exception:
        return False


def main() -> int:
    port, srv = PORT, None
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        # 端口被占：多半是上次的窗口还开着。复用它，绝不再起第二个 ——
        # Windows 上两个进程能同时绑同一个端口，请求随机落到其中一个，
        # 那才是"连接被掐断"的真正原因。
        if instance_alive(port):
            url = f"http://127.0.0.1:{port}/"
            log(f"端口 {port} 上已有实例，直接复用 {url}")
            print(f"已在运行，直接开窗口 → {url}")
            open_edge(url)
            return 0
        log(f"端口 {port} 被别的程序占用：{exc}")
        for cand in range(port + 1, port + 21):
            try:
                srv = Server(("127.0.0.1", cand), Handler)
                port = cand
                break
            except OSError:
                continue
        if srv is None:
            _fatal(f"端口 {PORT}～{PORT + 20} 全被占用，服务起不来。\n"
                   f"先关掉占用这些端口的程序（大多是上次没退干净的本程序）再双击启动。")
            return 1

    url = f"http://127.0.0.1:{port}/"
    log(f"启动 {url}（v{VERSION}）")
    print(f"小米6 工具箱 Web UI → {url}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _start_time[0] = time.time()
    _activity["last"] = 0.0
    threading.Thread(target=_watchdog, daemon=True).start()
    if os.environ.get("MI6_WEB_NO_OPEN"):  # 调试用：只起服务不开窗
        print("已启动（MI6_WEB_NO_OPEN=1，不开窗口）")
    else:
        opened = open_edge(url)
        print("已用 Edge 应用模式打开" if opened else "未找到 Edge，已用默认浏览器打开")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("退出")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        _fatal("启动失败：\n" + traceback.format_exc())
        raise SystemExit(1)
