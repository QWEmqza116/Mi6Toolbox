# -*- coding: utf-8 -*-
"""全局配置 + Material 3（深色）设计令牌。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "小米6 刷机工具箱"
APP_NAME_EN = "Mi6 Toolbox"
VERSION = "2.0.0"
CODENAME = "sagit"
DEVICE_LABEL = "Xiaomi Mi 6"


def app_root() -> Path:
    """程序根目录（源码运行 / 打包 exe 都适用）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


ROOT = app_root()
DIR_TOOLS = ROOT / "tools"
DIR_DOWNLOAD = ROOT / "downloads"
DIR_BACKUP = ROOT / "backups"
DIR_LOG = ROOT / "logs"
DIR_IMG = ROOT / "images"          # 用户自备的 img/zip 放这里（可选）
DIR_DATA = ROOT / "data"           # adb 密钥等运行时数据

for _d in (DIR_TOOLS, DIR_DOWNLOAD, DIR_BACKUP, DIR_LOG, DIR_IMG, DIR_DATA):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

CONFIG_FILE = ROOT / "settings.json"

# ---------------------------------------------------------------- 下载源
URL_PLATFORM_TOOLS = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
URL_PLATFORM_TOOLS_MIRRORS = [
    "https://dl-ssl.google.com/android/repository/platform-tools-latest-windows.zip",
    "https://googledownloads.cn/android/repository/platform-tools-latest-windows.zip",
]
URL_USB_DRIVER = "https://dl.google.com/android/repository/usb_driver_r13-windows.zip"
URL_MAGISK_API = "https://api.github.com/repos/topjohnwu/Magisk/releases/latest"
URL_MAGISK_PAGE = "https://github.com/topjohnwu/Magisk/releases"
URL_TWRP_PAGE = "https://twrp.me/xiaomi/xiaomimi6.html"
URL_TWRP_LIST = "https://dl.twrp.me/sagit/"
# 核实日期 2026-11：sagit 官方最新就是 3.7.0_9-0，没有再往后更新
URL_TWRP_IMG = "https://dl.twrp.me/sagit/twrp-3.7.0_9-0-sagit.img"
URL_TWRP_IMG_FALLBACKS = [
    "https://dl.twrp.me/sagit/twrp-3.6.2_9-0-sagit.img",
]
URL_MI_UNLOCK_PAGE = "https://www.miui.com/unlock/index.html"
# GitHub API 挂了时的兜底版本（(tag, apk 直链)）；新版资产名是 Magisk-v<版本>.apk
MAGISK_FALLBACK = (
    "31.1",
    "https://github.com/topjohnwu/Magisk/releases/download/v31.1/Magisk-v31.1.apk",
)
URL_LINEAGE_SAGIT = "https://download.lineageos.org/devices/sagit"
URL_LINEAGE_WIKI = "https://wiki.lineageos.org/devices/sagit/install"
URL_HOSTS = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
URL_HOSTS_MIRRORS = [
    "https://cdn.jsdelivr.net/gh/StevenBlack/hosts@master/hosts",
    "https://ghproxy.net/https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
]
GH_MIRRORS = [
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "https://gh-proxy.com/",
]

# ---------------------------------------------------------------- 已知分区
CRITICAL_PARTITIONS = [
    ("boot", "内核 + ramdisk，root 的核心，刷坏最常见"),
    ("recovery", "恢复分区，TWRP 装这里"),
    ("modem", "基带，刷坏会没信号（TWRP 里挂成 /firmware）"),
    ("modemst1", "IMEI / 网络参数（TWRP 里显示为 efs1）"),
    ("modemst2", "IMEI 备份（TWRP 里显示为 efs2）"),
    ("persist", "传感器校准、指纹、WiFi MAC 等，丢了很麻烦"),
    ("fsg", "基带备份"),
    ("misc", "开机模式标志，卡开机可清"),
    ("cust", "运营商定制分区（可选）"),
]
PARTITION_ALIASES = {
    "modemst1": ["modemst1", "efs1"],
    "modemst2": ["modemst2", "efs2"],
}

# ================================================================ Material 3 设计令牌
# 浅色基线（M3 light，紫调 primary）。所有界面颜色都从这里取。
M3 = {
    "surface": "#FEF7FF",
    "surface_dim": "#DED8E1",
    "surface_lowest": "#FFFFFF",
    "surface_low": "#F7F2FA",
    "surface_container": "#F3EDF7",
    "surface_high": "#ECE6F0",
    "surface_highest": "#E6E0E9",
    "on_surface": "#1D1B20",
    "on_surface_variant": "#49454F",
    "outline": "#79747E",
    "outline_variant": "#CAC4D0",
    "primary": "#6750A4",
    "on_primary": "#FFFFFF",
    "primary_hover": "#7F67BE",
    "primary_container": "#EADDFF",
    "on_primary_container": "#21005D",
    "secondary": "#625B71",
    "on_secondary": "#FFFFFF",
    "secondary_container": "#E8DEF8",
    "secondary_hover": "#DCCEF8",
    "on_secondary_container": "#1D192B",
    "tertiary": "#7D5260",
    "tertiary_container": "#FFD8E4",
    "on_tertiary_container": "#31111D",
    "error": "#B3261E",
    "on_error": "#FFFFFF",
    "error_hover": "#C6392F",
    "error_container": "#F9DEDC",
    "on_error_container": "#410E0B",
    "success": "#0F6B3F",
    "success_container": "#C6F0DA",
    "success_hover": "#B3EBCF",
    "warning": "#7A5200",
    "warning_container": "#FFE7B3",
    "info": "#0B4A8F",
    "info_container": "#D7E3FF",
    "disabled_bg": "#E7E1E9",
    "disabled_fg": "#8E8A90",
}

# 兼容旧代码的颜色字典（页面里大量用到 COL["xxx"]）
COL = {
    "bg": M3["surface_low"],
    "panel": M3["surface_lowest"],
    "card": M3["surface_lowest"],
    "card_hover": M3["surface_container"],
    "border": M3["outline_variant"],
    "border_hi": M3["outline"],
    "text": M3["on_surface"],
    "dim": M3["on_surface_variant"],
    "faint": M3["outline"],
    "accent": M3["primary"],
    "accent_hover": M3["primary_hover"],
    "accent_soft": M3["primary_container"],
    "blue": M3["info"],
    "green": M3["success"],
    "yellow": M3["warning"],
    "red": M3["error"],
    "log_bg": M3["surface_lowest"],
    "ok_bg": M3["success_container"],
    "warn_bg": M3["warning_container"],
    "err_bg": M3["error_container"],
    "info_bg": M3["info_container"],
    "primary_container": M3["primary_container"],
    "on_primary_container": M3["on_primary_container"],
    "surface_high": M3["surface_high"],
    "surface_highest": M3["surface_highest"],
    "outline": M3["outline"],
}

# 圆角（M3 shape scale）
R_SM = 10
R_MD = 14
R_LG = 20
R_XL = 28
R_FULL = 999

# 间距
GAP = 12
PAD = 20

FONT_FAMILY = "Microsoft YaHei UI"
FONT = (FONT_FAMILY, 10)               # body medium ≈ 14
FONT_SM = (FONT_FAMILY, 9)             # body small ≈ 12
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_LABEL = (FONT_FAMILY, 9, "bold")
FONT_TITLE = (FONT_FAMILY, 12, "bold")   # title large
FONT_H2 = (FONT_FAMILY, 13, "bold")      # headline small
FONT_H1 = (FONT_FAMILY, 19, "bold")      # headline medium
FONT_DISPLAY = (FONT_FAMILY, 26, "bold")  # display small
FONT_MONO = ("Consolas", 9)
