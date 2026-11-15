# -*- coding: utf-8 -*-
"""设备探测：型号、系统、解锁与 root 状态、分区表。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .adb import Adb
from .fastboot import Fastboot
from .util import BUS, find_first

MIUI_KEYS = ("ro.miui.ui.version.name", "ro.miui.ui.version.code")

PROPS = [
    "ro.product.device",
    "ro.product.model",
    "ro.product.brand",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.display.id",
    "ro.build.version.incremental",
    "ro.miui.ui.version.name",
    "ro.boot.flash.locked",
    "ro.boot.verifiedbootstate",
    "ro.boot.veritymode",
    "ro.twrp.version",
    "ro.bootmode",
    "ro.build.type",
    "ro.build.tags",
    "ro.serialno",
    "persist.sys.locale",
]


@dataclass
class DeviceInfo:
    connected: bool = False
    mode: str = "none"          # adb / fastboot / sideload / twrp / none
    serial: str = ""
    codename: str = ""
    model: str = ""
    brand: str = ""
    android: str = ""
    sdk: str = ""
    build: str = ""
    rom_name: str = ""
    miui: str = ""
    unlocked: bool | None = None
    root: bool = False
    magisk: str = ""
    twrp: str = ""
    props: dict = field(default_factory=dict)
    partitions: list = field(default_factory=list)
    note: str = ""

    @property
    def is_sagit(self) -> bool:
        c = (self.codename or "").lower()
        return c == "sagit" or "mi 6" in (self.model or "").lower() or "mi6" in c

    @property
    def mode_label(self) -> str:
        return {
            "adb": "系统运行中（ADB）",
            "twrp": "TWRP 恢复模式",
            "fastboot": "Fastboot 模式",
            "sideload": "Sideload 模式",
            "unauthorized": "未授权（需在手机上点允许）",
            "offline": "离线",
            "none": "未连接",
        }.get(self.mode, self.mode)

    @property
    def unlock_label(self) -> str:
        if self.mode == "fastboot":
            return "已解锁" if self.unlocked else ("未解锁/未知" if self.unlocked is False else "未知")
        if self.unlocked is None:
            return "未知"
        return "已解锁" if self.unlocked else "未解锁"


def list_partitions(adb: Adb) -> list[str]:
    for path in ("/dev/block/bootdevice/by-name", "/dev/block/by-name"):
        out = adb.sh(f"ls {path} 2>/dev/null", timeout=25)
        names = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("ls:")]
        if names:
            return sorted(set(names))
    return []


def _read_root_state(adb: Adb) -> tuple[bool, str]:
    """返回 (是否已 root, Magisk 版本)。"""
    r = adb.shell("su -v", timeout=12)
    txt = r.text.strip()
    if r.ok and txt and "not found" not in txt.lower() and "denied" not in txt.lower():
        magisk = ""
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", txt)
        if m:
            magisk = m.group(1)
        # su -v 在 Magisk 上输出形如 "27.0:MAGISK"
        if "magisk" in txt.upper():
            return True, txt.split(":")[0] if ":" in txt else (magisk or txt)
        return True, magisk
    # 有些 ROM 用 kernelsu / supersu
    r2 = adb.shell("which su", timeout=10)
    if r2.ok and "/su" in r2.text:
        return True, "未知"
    return False, ""


def probe() -> DeviceInfo:
    """探测当前连接的设备。"""
    info = DeviceInfo()
    adb = Adb()
    fb = Fastboot()

    if adb.available and adb.ensure_server():
        devices = adb.devices()
        if devices:
            serial, state = devices[0]
            info.serial = serial
            if state == "unauthorized":
                info.connected, info.mode = True, "unauthorized"
                info.note = "手机上会弹「允许 USB 调试」，勾选后点允许"
                return info
            if state == "offline":
                info.connected, info.mode = True, "offline"
                return info
            if state == "sideload":
                info.connected, info.mode = True, "sideload"
                return info

            adb.serial = serial
            props = adb.props(PROPS)
            info.props = props
            info.codename = props.get("ro.product.device", "")
            info.model = props.get("ro.product.model", "")
            info.brand = props.get("ro.product.brand", "")
            info.android = props.get("ro.build.version.release", "")
            info.sdk = props.get("ro.build.version.sdk", "")
            info.build = props.get("ro.build.display.id", "") or props.get("ro.build.version.incremental", "")
            info.miui = props.get("ro.miui.ui.version.name", "")
            info.twrp = props.get("ro.twrp.version", "")
            info.connected = True
            info.mode = "twrp" if info.twrp else "adb"

            if info.miui:
                info.rom_name = f"MIUI {info.miui}"
            elif info.twrp:
                info.rom_name = "Recovery（TWRP）"
            elif info.build:
                info.rom_name = info.build
            else:
                info.rom_name = "第三方 / AOSP"

            # 判定 BL 解锁：Android 7+ 看 ro.boot.flash.locked；
            # 有些老 ROM（比如这台 8.1）没这个属性，就退回 verifiedbootstate：
            # orange = 已解锁（自签名 boot），green = 官方签名未解锁。
            locked = props.get("ro.boot.flash.locked", "").strip()
            vb = props.get("ro.boot.verifiedbootstate", "").strip().lower()
            if locked in ("0", "1"):
                info.unlocked = locked == "0"
            elif vb in ("orange", "yellow"):
                info.unlocked = True
            elif vb == "green":
                info.unlocked = False
            else:
                info.unlocked = None

            if info.mode != "twrp":
                info.root, info.magisk = _read_root_state(adb)
            if info.mode in ("twrp", "adb"):
                info.partitions = list_partitions(adb)
            return info

    if fb.available:
        fdevs = fb.devices()
        if fdevs:
            fb.serial = fdevs[0]
            info.connected, info.mode, info.serial = True, "fastboot", fdevs[0]
            info.codename = fb.product()
            info.model = "Xiaomi Mi 6" if info.codename == "sagit" else info.codename
            info.unlocked = fb.unlocked()
            try:
                info.props = {"battery-voltage": fb.getvar("battery-voltage", 10)}
            except Exception:
                pass
            return info

    info.note = "手机没连上：确认数据线、USB 调试已开、或已进入 Fastboot"
    return info


def status_line(info: DeviceInfo) -> str:
    if not info.connected:
        return "未连接设备"
    bits = [info.mode_label]
    if info.model:
        bits.append(f"{info.model} ({info.codename})")
    if info.mode == "adb" or info.mode == "twrp":
        bits.append(f"BL {info.unlock_label}")
        bits.append("已 Root" if info.root else "未 Root")
    elif info.mode == "fastboot":
        bits.append(f"BL {info.unlock_label}")
    return " · ".join(bits)


SHORT_MODE = {
    "adb": "已连接",
    "twrp": "TWRP",
    "fastboot": "Fastboot",
    "unauthorized": "待授权",
    "offline": "离线",
    "sideload": "Sideload",
    "none": "未连接",
}


def short_status(info: DeviceInfo) -> str:
    """顶栏用的短状态：Mi 6 · 已连接。"""
    if not info.connected:
        return "未连接设备"
    mode = SHORT_MODE.get(info.mode, info.mode)
    return f"{info.model} · {mode}" if info.model else mode
