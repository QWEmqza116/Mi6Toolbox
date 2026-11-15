# -*- coding: utf-8 -*-
"""基础设施：子进程执行、事件总线、格式化工具。"""
from __future__ import annotations

import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0


# ------------------------------------------------------------------ 命令执行
def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class ProcResult:
    __slots__ = ("rc", "out", "err", "cmd", "seconds")

    def __init__(self, rc: int, out: str, err: str, cmd, seconds: float = 0.0):
        self.rc = rc
        self.out = out or ""
        self.err = err or ""
        self.cmd = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.seconds = seconds

    @property
    def text(self) -> str:
        return (self.out + ("\n" + self.err if self.err.strip() else "")).strip()

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def __repr__(self):
        return f"<ProcResult rc={self.rc} {self.cmd!r}>"


def run(cmd, cwd=None, timeout=90, input_text: str | None = None, merge: bool = False, env_extra=None) -> ProcResult:
    """执行命令，永不抛异常，返回 ProcResult。"""
    if isinstance(cmd, str):
        cmd = [cmd]
    env = None
    if env_extra:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in env_extra.items()})
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            input=input_text.encode("utf-8") if input_text else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        out = _decode(p.stdout)
        err = "" if merge else _decode(p.stderr)
        return ProcResult(p.returncode, out, err, cmd, time.time() - t0)
    except FileNotFoundError:
        return ProcResult(127, "", f"找不到可执行文件：{cmd[0]}", cmd, time.time() - t0)
    except subprocess.TimeoutExpired:
        return ProcResult(124, "", f"命令超时（>{timeout}s）：{cmd[0]}", cmd, time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        return ProcResult(1, "", f"{type(exc).__name__}: {exc}", cmd, time.time() - t0)


def popen(cmd, cwd=None) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )


def which(name: str) -> str | None:
    return shutil.which(name)


# ------------------------------------------------------------------ 事件总线
class Bus:
    """线程安全的事件队列，GUI 定时 drain。"""

    def __init__(self):
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=200000)

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            pass

    # --- 日志
    def log(self, text: str, level: str = "info"):
        self._put(("log", time.time(), level, str(text)))

    def info(self, text):
        self.log(text, "info")

    def ok(self, text):
        self.log(text, "ok")

    def warn(self, text):
        self.log(text, "warn")

    def err(self, text):
        self.log(text, "err")

    def cmd(self, text):
        self.log(text, "cmd")

    def step(self, text):
        self.log(text, "step")

    def raw(self, text):
        self.log(text, "raw")

    # --- 进度
    def progress(self, done: float, total: float | None = None, label: str = ""):
        self._put(("progress", time.time(), done, total, label))

    def progress_end(self):
        self._put(("progress_end", time.time()))

    # --- 状态 / 提示
    def status(self, text: str):
        self._put(("status", time.time(), text))

    def device(self, info):
        self._put(("device", time.time(), info))

    def toast(self, text: str, kind: str = "info"):
        self._put(("toast", time.time(), kind, text))

    def done(self, task_id: str, ok: bool, msg: str = ""):
        self._put(("task_done", time.time(), task_id, ok, msg))

    def task_finished(self, ok: bool, result, callback):
        """工作线程把结果丢进队列，由主线程的 _pump 调 callback。

        不能在子线程里直接调 Tk 的 after()：Tk 不是线程安全的，
        after() 会静默失败，任务就永远完不成（busy 卡死）。
        """
        self._put(("task_finished", time.time(), ok, result, callback))

    def ui(self, fn):
        """把一段界面更新丢回主线程执行（子线程里改控件必须走这里）。"""
        self._put(("ui", time.time(), fn))

    def drain(self):
        items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items


BUS = Bus()


# ------------------------------------------------------------------ 小工具
def human_size(n: float | int | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_admin() -> bool:
    if not IS_WIN:
        return os.geteuid() == 0
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def open_path(path):
    """用系统默认方式打开文件/文件夹。"""
    p = Path(path)
    if not p.exists():
        return False
    try:
        if IS_WIN:
            os.startfile(str(p))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception:
        return False


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(name)).strip() or "file"


def tail_lines(text: str, n: int = 30) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def find_first(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1) if m else default
