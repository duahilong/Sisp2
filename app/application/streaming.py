"""流式子进程：边跑边记日志、边解析进度、可随时取消。

拆出来的原因：

1. 以前 Ghost / diskpart 都走 ``subprocess.run(capture_output=True)``、
   ``timeout=2h``：进程跑起来以后输出全在内存里，日志要等结束才落盘，
   取消事件也完全没人看——用户点“取消”之后最长要等两小时；
2. 进度条只能按“步骤完成数”跳变（7 步里镜像卡在 57%），因为执行层
   拿不到 Ghost 的百分比输出；
3. 真正的“终止”必须是明确的：先 ``terminate()``，宽限期内没退再
   ``kill()``，并把“被取消”与“超时”分开报，否则界面无法区分
   “用户取消”和“程序崩了”。

约定：本模块只负责 **跑进程 + 写日志 + 调用回调**，不做任何业务判断；
业务语义（例如“镜像没写完 = 任务取消”）留在 phases 里。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.application.command_log import CommandLog
from app.application.runner_utils import RunnerError, decode_output
from app.safety_guard import ensure_command_allowed

# 轮询间隔：0.2 s 足够让取消“立刻”生效，又不会把 CPU 吃满。
POLL_SECONDS = 0.2
# 取消/超时后等待进程自行退出的宽限期；超过就 kill。
KILL_GRACE_SECONDS = 5.0
# 单条命令保留在内存里的输出上限（日志文件不受此限制）：Ghost 正常输出只有
# 几 KB，但坏盘/循环重试时可能刷屏，这里兜住内存。
OUTPUT_LIMIT = 200_000
READ_CHUNK = 8192

# Ghost 的进度行形如 ``  42% complete``；一行里可能有多个百分比，取最大的。
_PERCENT = re.compile(r"(\d{1,3})\s*%")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamOutcome:
    """一次流式执行的结果。``cancelled`` 与超时（抛异常）是两件事。"""

    returncode: int | None
    output: str
    elapsed: float
    cancelled: bool = False


def parse_percent(line: str) -> int | None:
    """从一行输出里取进度百分比（0–100）；没有就返回 ``None``。"""
    matches = _PERCENT.findall(line or "")
    if not matches:
        return None
    value = max(int(item) for item in matches)
    return max(0, min(value, 100))


def stream_process(
    command: Sequence[str],
    timeout: float,
    *,
    log: CommandLog,
    allow_real_disk: bool,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    source: str = "ProcessShell",
    cancel: threading.Event | None = None,
    on_line: Callable[[str], None] | None = None,
    poll: float = POLL_SECONDS,
    kill_grace: float = KILL_GRACE_SECONDS,
) -> StreamOutcome:
    """执行一条命令，实时落日志并支持取消。

    参数与 :func:`app.application.shell.run_process` 对齐，额外两项：

    - ``cancel``：被置位时终止子进程并返回 ``cancelled=True``；
    - ``on_line``：每读到一行就回调（Ghost 用它解析百分比）。

    超时与之前一致：终止进程后抛 ``RunnerError("命令超时（N 秒）：...")``，
    让调用方的失败文案保持原样。
    """
    # 护栏必须在写日志之前：被拦下的命令不应该看起来像执行过。
    ensure_command_allowed(command, allow_real_disk=allow_real_disk, source=source)

    started = datetime.now()
    started_monotonic = time.monotonic()
    verbose = list(command)
    chunks: list[str] = []
    chunk_chars = 0
    dropped = False

    def emit_text(text: str, write: Callable[[str], None]) -> None:
        nonlocal chunk_chars, dropped
        if not text.strip():
            return
        write(f"    {text}")
        chunks.append(text)
        chunk_chars += len(text) + 1
        while chunk_chars > OUTPUT_LIMIT and len(chunks) > 1:
            removed = chunks.pop(0)
            chunk_chars -= len(removed) + 1
            dropped = True
        if on_line is not None:
            try:
                on_line(text)
            except Exception:
                _LOGGER.debug("进度回调失败", exc_info=True)

    def pump(proc: subprocess.Popen[bytes], write: Callable[[str], None]) -> None:
        if proc.stdout is None:  # 理论上 stdout=PIPE 一定有；防御性检查
            raise RunnerError("无法读取子进程输出（stdout 未打开）")
        buffer = b""
        while True:
            try:
                chunk = os.read(proc.stdout.fileno(), READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            # Ghost 用 ``\r`` 刷新同一行进度；统一按换行切，才能一行行解析。
            buffer += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                emit_text(decode_output(raw), write)
        if buffer.strip():
            emit_text(decode_output(buffer), write)

    with log.streaming() as write:
        write(f"$ {' '.join(verbose)}")
        try:
            proc = subprocess.Popen(  # noqa: S603 - 命令由本模块拼装，参数来自配置
                verbose,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd or None,
                creationflags=_NO_WINDOW | _NEW_PROCESS_GROUP,
                env=dict(env) if env else None,
            )
        except OSError as exc:
            write(f"--- 无法启动：{exc} ---")
            raise RunnerError(f"无法启动 {verbose[0]}：{exc}") from exc

        reader = threading.Thread(target=pump, args=(proc, write), daemon=True)
        reader.start()

        cancelled = False
        deadline = None if timeout is None else started_monotonic + float(timeout)
        while True:
            if proc.poll() is not None:
                break
            if cancel is not None and cancel.is_set():
                cancelled = True
                write("--- 收到取消请求，正在终止进程 ---")
                _terminate(proc, kill_grace)
                break
            if deadline is not None and time.monotonic() > deadline:
                _terminate(proc, kill_grace)
                elapsed = time.monotonic() - started_monotonic
                write(f"--- 超时（{timeout:.0f}s），已终止进程 ---")
                reader.join(timeout=kill_grace)
                raise RunnerError(f"命令超时（{timeout:.0f} 秒）：{verbose[0]}")
            time.sleep(max(0.01, poll))

        reader.join(timeout=kill_grace)
        try:
            proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            _terminate(proc, kill_grace)

        elapsed = (datetime.now() - started).total_seconds()
        code = proc.returncode
        output = "\n".join(chunks)
        if dropped:
            output = "…（前略）\n" + output
        if cancelled:
            write(f"--- 已取消（退出码 {code}，用时 {elapsed:.1f}s） ---")
        else:
            write(f"--- 退出码 {code}，用时 {elapsed:.1f}s ---")
        return StreamOutcome(
            returncode=code, output=output, elapsed=elapsed, cancelled=cancelled
        )


def _terminate(proc: subprocess.Popen[bytes], grace: float) -> None:
    """先礼后兵：terminate → 宽限 → kill。任何一步失败都只吞掉。"""
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=max(0.0, grace))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, grace))
        except subprocess.TimeoutExpired:
            pass


__all__ = [
    "KILL_GRACE_SECONDS",
    "OUTPUT_LIMIT",
    "POLL_SECONDS",
    "StreamOutcome",
    "parse_percent",
    "stream_process",
]
