# -*- coding: utf-8 -*-
"""ADB 封装。"""
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


_HOME_ENV: dict | None = None


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def android_env() -> dict:
    """给 adb 准备一个「肯定能写」的环境。

    adb 会写两处：~/.android（密钥）和 %TEMP%/adb.log（日志）。
    受限账户 / 公司电脑 / 只读 U 盘上，这两处任一不可写 adb 就直接崩，
    所以这里探测一次，不可写就换到程序自己的 data 目录。
    """
    global _HOME_ENV
    if _HOME_ENV is not None:
        return _HOME_ENV

    import os
    import tempfile

    from .config import DIR_DATA

    env: dict[str, str] = {}

    home_ok = _writable(Path(os.path.expanduser("~")) / ".android")
    if not home_ok:
        alt = DIR_DATA / "android_home"
        alt.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "ANDROID_USER_HOME": str(alt),
                "ANDROID_SDK_HOME": str(alt),
                "ANDROID_EMULATOR_HOME": str(alt),
            }
        )

    # adb server 一定会往 %TEMP%\adb.log 写日志；某些受限环境（或被策略拦截时）
    # 即使目录看似可写也会失败，所以直接统一指到程序自己的 data/tmp，最稳。
    tmp = DIR_DATA / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update({"TEMP": str(tmp), "TMP": str(tmp)})

    _HOME_ENV = env
    return _HOME_ENV


class Adb:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.exe = _find_exe("adb")

    # ---------------------------------------------------------------- base
    @property
    def available(self) -> bool:
        return bool(self.exe)

    def _cmd(self, args) -> list[str]:
        base = [self.exe or "adb"]
        if self.serial:
            base += ["-s", self.serial]
        return base + [str(a) for a in args]

    def run(self, args, timeout: int = 60) -> ProcResult:
        if not self.available:
            return ProcResult(127, "", "未找到 adb，请先在「设置 / 环境」里安装 platform-tools", args)
        return run(self._cmd(args), timeout=timeout, env_extra=android_env())

    # ---------------------------------------------------------------- server
    def version(self) -> str:
        r = run([self.exe or "adb", "version"], timeout=15, env_extra=android_env())
        return r.text

    def ensure_server(self) -> bool:
        """拉起 adb server；版本不匹配时重启。"""
        v = self.version()
        if "Cannot mkdir" in v:
            from .util import BUS

            BUS.err(
                "adb 无法创建 ~/.android 目录（用户目录不可写）。\n"
                "常见于公司电脑或受限账户：把工具箱放到你有写权限的目录、用另一个账户运行，"
                "或者手动创建一个 %USERPROFILE%\\.android 文件夹再试。"
            )
            return False
        if "Android Debug Bridge" not in v:
            return False
        if "cannot connect" in v.lower() or "错误" in v:
            run([self.exe, "kill-server"], timeout=15, env_extra=android_env())
            run([self.exe, "start-server"], timeout=20, env_extra=android_env())
        r = self.run(["start-server"], timeout=25)
        return r.rc == 0 or "already" in (r.out + r.err).lower()

    # ---------------------------------------------------------------- devices
    def devices(self) -> list[tuple[str, str]]:
        """返回 [(serial, state)]，state ∈ device/unauthorized/offline/recovery。"""
        r = self.run(["devices"], timeout=25)
        out = []
        for line in r.out.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = re.split(r"\s+", line)
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
        return out

    def first_serial(self, wanted_state: str = "device") -> str | None:
        for serial, state in self.devices():
            if state == wanted_state:
                return serial
        return None

    def get_state(self) -> str:
        if not self.serial:
            return "none"
        for serial, state in self.devices():
            if serial == self.serial:
                return state
        return "none"

    # ---------------------------------------------------------------- shell
    def shell(self, cmd: str, timeout: int = 40) -> ProcResult:
        return self.run(["shell", cmd], timeout=timeout)

    def sh(self, cmd: str, timeout: int = 40, default: str = "") -> str:
        r = self.shell(cmd, timeout=timeout)
        return r.text.strip() if r.ok else default

    def prop(self, name: str, default: str = "") -> str:
        return self.sh(f"getprop {name}", timeout=15, default=default)

    def props(self, names) -> dict[str, str]:
        """一次 shell 批量读属性，减少往返。"""
        if not names:
            return {}
        chain = "; ".join(f'echo "{n}=$(getprop {n})"' for n in names)
        out = self.sh(chain, timeout=25)
        res = {}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                res[k.strip()] = v.strip()
        return {n: res.get(n, "") for n in names}

    # ---------------------------------------------------------------- files
    def push(self, local, remote: str, timeout: int = 300) -> ProcResult:
        return self.run(["push", str(local), remote], timeout=timeout)

    def pull(self, remote: str, local, timeout: int = 600) -> ProcResult:
        return self.run(["pull", remote, str(local)], timeout=timeout)

    def mkdir(self, remote: str) -> ProcResult:
        return self.shell(f"mkdir -p {remote}", timeout=20)

    def file_size(self, remote: str) -> int:
        out = self.sh(f"stat -c %s {remote} 2>/dev/null || wc -c < {remote}", timeout=20)
        m = re.search(r"\d+", out)
        return int(m.group(0)) if m else -1

    # ---------------------------------------------------------------- apps
    def install(self, apk, reinstall: bool = True, grant: bool = True, timeout: int = 600) -> ProcResult:
        args = ["install"]
        if reinstall:
            args.append("-r")
        if grant:
            args.append("-g")
        args.append(str(apk))
        return self.run(args, timeout=timeout)

    def installed_packages(self, third_party: bool = True) -> list[str]:
        flag = "-3" if third_party else ""
        out = self.sh(f"pm list packages {flag}", timeout=60)
        pkgs = [ln.replace("package:", "").strip() for ln in out.splitlines() if ln.startswith("package:")]
        return sorted(set(pkgs))

    # ---------------------------------------------------------------- capture
    def screencap(self, dest) -> bool:
        if not self.available:
            return False
        import subprocess

        from .util import CREATE_NO_WINDOW

        try:
            with open(dest, "wb") as f:
                p = subprocess.run(
                    self._cmd(["exec-out", "screencap", "-p"]),
                    stdout=f,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            return p.returncode == 0 and Path(dest).stat().st_size > 1024
        except Exception:
            return False

    # ---------------------------------------------------------------- reboot
    def reboot(self, target: str = "") -> ProcResult:
        args = ["reboot"] + ([target] if target else [])
        return self.run(args, timeout=30)

    def root(self) -> ProcResult:
        return self.run(["root"], timeout=30)

    def remount(self) -> ProcResult:
        return self.run(["remount"], timeout=30)
