# -*- coding: utf-8 -*-
"""Fastboot 封装。"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import DIR_TOOLS
from .util import ProcResult, run


def _find_exe(name: str) -> str | None:
    local = DIR_TOOLS / "platform-tools" / (name + (".exe" if __import__("os").name == "nt" else ""))
    if local.exists():
        return str(local)
    return shutil.which(name)


class Fastboot:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.exe = _find_exe("fastboot")

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def _cmd(self, args) -> list[str]:
        base = [self.exe or "fastboot"]
        if self.serial:
            base += ["-s", self.serial]
        return base + [str(a) for a in args]

    def run(self, args, timeout: int = 120) -> ProcResult:
        if not self.available:
            return ProcResult(127, "", "未找到 fastboot，请先在「设置 / 环境」里安装 platform-tools", args)
        # fastboot 的状态输出走 stderr，统一合并
        return run(self._cmd(args), timeout=timeout, merge=True)

    # ---------------------------------------------------------------- devices
    def devices(self) -> list[str]:
        r = self.run(["devices"], timeout=25)
        out = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line or "fastboot" not in line.lower():
                continue
            out.append(line.split()[0])
        return out

    def _parse_kv(self, text: str) -> dict[str, str]:
        kv = {}
        for line in text.splitlines():
            line = line.strip()
            # 真机每行常带 "(bootloader)" 前缀，先剥掉
            line = re.sub(r"^\([^)]*\)\s*", "", line)
            if ":" in line:
                k, _, v = line.partition(":")
                kv[k.strip().lower()] = v.strip()
        return kv

    def getvar(self, name: str, timeout: int = 30) -> str:
        r = self.run(["getvar", name], timeout=timeout)
        kv = self._parse_kv(r.text)
        return kv.get(name.lower(), "")

    def getvars(self, names) -> dict[str, str]:
        res = {}
        for n in names:
            res[n] = self.getvar(n, timeout=20)
        return res

    def oem_device_info(self) -> dict[str, str]:
        return self._parse_kv(self.run(["oem", "device-info"], timeout=30).text)

    def product(self) -> str:
        return self.getvar("product") or self.getvar("ro.product.device")

    def unlocked(self) -> bool | None:
        """True=已解锁 / False=已锁 / None=判断不了。"""
        v = self.getvar("unlocked", timeout=20).lower()
        if v in ("yes", "true", "1"):
            return True
        if v in ("no", "false", "0"):
            return False
        info = self.oem_device_info()
        for key in ("device unlocked", "unlocked"):
            if key in info:
                val = info[key].lower()
                if val in ("true", "yes"):
                    return True
                if val in ("false", "no"):
                    return False
        return None

    # ---------------------------------------------------------------- flash
    def flash(self, partition: str, img, timeout: int = 600) -> ProcResult:
        return self.run(["flash", partition, str(img)], timeout=timeout)

    def flash_all(self, images: dict[str, Path], timeout: int = 900) -> dict[str, bool]:
        res = {}
        for part, img in images.items():
            r = self.flash(part, img, timeout=timeout)
            res[part] = r.ok and "FAILED" not in r.text.upper()
        return res

    def boot(self, img, timeout: int = 300) -> ProcResult:
        return self.run(["boot", str(img)], timeout=timeout)

    def erase(self, partition: str, timeout: int = 120) -> ProcResult:
        return self.run(["erase", partition], timeout=timeout)

    def reboot(self, target: str = "", timeout: int = 60) -> ProcResult:
        args = ["reboot"] + ([target] if target else [])
        return self.run(args, timeout=timeout)

    def format(self, partition: str, fs: str = "ext4", timeout: int = 300) -> ProcResult:
        return self.run(["-w"] if partition == "userdata" and fs == "" else ["format", partition, fs],
                        timeout=timeout)

    def continue_boot(self, timeout: int = 120) -> ProcResult:
        return self.run(["continue"], timeout=timeout)
