"""pywebview 的 ``js_api`` 对象。

职责只有四件：
1. 把 ``WorkflowController`` 的方法转发出去；
2. 用统一信封 ``{"ok": True, "data": ...}`` / ``{"ok": False, "error": ...}`` 包装；
3. 缓冲 ``JobEvent``，供前端按游标轮询；
4. 转发界面设置（``SettingsStore``）与原生文件选择框。

禁止在此层写任何磁盘、PowerShell 或业务判断逻辑；
也不 import 除 ``workflow`` 之外的模块，避免把重依赖带进界面进程。

P0 补丁：新增设置相关方法（get_settings / update_settings / reset_settings /
probe_path / probe_all_paths / pick_path / open_settings_folder），
``SettingsStore`` 通过构造参数注入，不传时按默认路径创建。
"""

from __future__ import annotations

import json
import platform
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只为类型标注，避免 import 时拉起整条应用层依赖链
    from app.application.workflow import WorkflowController
    from app.modules.settings.service import SettingsStore

MAX_BUFFER = 5000


def ok(data: Any) -> dict[str, Any]:
    """包装成功响应，并保证一定可以 JSON 序列化。"""
    return {"ok": True, "data": json.loads(json.dumps(data, ensure_ascii=False, default=str))}


def err(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """包装失败响应。"""
    return {"ok": False, "error": {"code": code, "message": message, "detail": detail}}


class WebApi:
    """暴露给 JavaScript 的接口对象。

    所有方法都返回信封 dict；pywebview 会把它转成 Promise 的结果。
    JavaScript 侧必须**按位置**传全部参数，不支持关键字参数。
    """

    def __init__(
        self,
        wf: WorkflowController,
        app_version: str = "0.0.0",
        settings: SettingsStore | None = None,
    ) -> None:
        self._wf = wf
        self._version = app_version
        self._events: list[dict[str, Any]] = []
        self._base = 0
        self._lock = threading.Lock()
        self._unsub = wf.job_manager.subscribe(self._on_event)
        if settings is None:
            from app.modules.settings.service import build_store

            settings = build_store()
        self._settings = settings

    # ------------------------------------------------------------------ 事件
    def _on_event(self, event: Any) -> None:
        try:
            data = event.to_dict() if hasattr(event, "to_dict") else dict(event)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - 缓冲失败绝不能拖垮工作线程
            data = {"type": getattr(event, "type", "unknown"), "payload": {}}
        with self._lock:
            self._events.append(data)
            overflow = len(self._events) - MAX_BUFFER
            if overflow > 0:
                self._events = self._events[overflow:]
                self._base += overflow

    def poll_events(self, cursor: int = 0, limit: int = 300) -> dict[str, Any]:
        """按游标取事件；``cursor`` 只增不减。"""
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            start = max(int(cursor) - self._base, 0)
            chunk = self._events[start:start + limit]
            next_cursor = self._base + start + len(chunk)
        return ok({
            "cursor": next_cursor,
            "events": chunk,
            "jobs": [job.to_dict() for job in self._wf.jobs()],
        })

    # ------------------------------------------------------------------ 会话
    def start_session(self, config_path: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        try:
            session = self._wf.start(config_path, dry_run=bool(dry_run))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成信封返回给界面
            return err("E_START", str(exc))
        with self._lock:
            self._events.clear()
            self._base = 0
        return ok(session.to_dict())

    def refresh_disks(self) -> dict[str, Any]:
        try:
            disks = self._wf.refresh_disks()
        except Exception as exc:  # noqa: BLE001
            return err("E_SCAN", str(exc))
        return ok([disk.to_dict() for disk in disks])

    # ------------------------------------------------------------------ 计划与执行
    def prepare(self, selection: Any, dry_run: bool | None = None) -> dict[str, Any]:
        """生成执行计划。

        ``selection`` 可以是 ``[1, 3]``，也可以是 ``{1: {"c_size": 400}}``
        （按盘覆盖配置，需要后端打上 workflow 的 G3 补丁）。
        """
        try:
            prepared = self._wf.prepare(selection, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            return err("E_PREPARE", str(exc))
        return ok([
            {"spec": item.spec.to_dict(), "plan": item.plan.to_dict(), "disk": item.disk.to_dict()}
            for item in prepared
        ])

    def submit(self) -> dict[str, Any]:
        try:
            jobs = self._wf.submit(require_confirmation=True)
        except Exception as exc:  # noqa: BLE001
            return err("E_SUBMIT", str(exc))
        return ok([job.to_dict() for job in jobs])

    def confirm(self, job_ids: list[str] | None = None) -> dict[str, Any]:
        try:
            jobs = self._wf.confirm(job_ids)
        except Exception as exc:  # noqa: BLE001
            return err("E_CONFIRM", str(exc))
        return ok([job.to_dict() for job in jobs])

    def cancel(self, job_id: str | None = None) -> dict[str, Any]:
        try:
            cancelled = self._wf.cancel([job_id] if job_id else None)
        except Exception as exc:  # noqa: BLE001
            return err("E_CANCEL", str(exc))
        return ok({"cancelled": cancelled})

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._wf.job_manager.get(job_id)
        if job is None:
            return err("E_NOT_FOUND", f"任务不存在: {job_id}")
        return ok(job.to_dict())

    # ------------------------------------------------------------------ 设置
    def get_settings(self) -> dict[str, Any]:
        try:
            return ok(self._settings.to_dict())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def update_settings(self, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        """局部更新；任一项不合法则整体拒绝，不落盘。"""
        try:
            return ok(self._settings.update(patch or {}))
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc), {"field": getattr(exc, "field", None)})

    def reset_settings(self) -> dict[str, Any]:
        try:
            return ok(self._settings.reset())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def probe_path(self, key: str) -> dict[str, Any]:
        try:
            return ok(self._settings.probe(key).to_dict())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def probe_all_paths(self) -> dict[str, Any]:
        try:
            return ok(self._settings.probe_all())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def pick_path(self, key: str = "", kind: str = "file") -> dict[str, Any]:
        """打开原生选择框；取消返回 ``{"cancelled": True}``。"""
        try:
            import webview  # 只在需要弹框时才 import
        except Exception as exc:  # noqa: BLE001
            return err("E_NO_WEBVIEW", f"当前进程没有 pywebview：{exc}")

        try:
            windows = getattr(webview, "windows", None) or []
            window = windows[0] if windows else None
            if window is None:
                return err("E_NO_WINDOW", "窗口尚未创建")

            dialog_module = getattr(webview, "FileDialog", None)
            if dialog_module is not None:
                dialog = dialog_module.FOLDER if kind == "dir" else dialog_module.OPEN
            else:
                dialog = getattr(webview, "FOLDER_DIALOG" if kind == "dir" else "OPEN_DIALOG")

            selection = window.create_file_dialog(dialog, allow_multiple=False)
        except Exception as exc:  # noqa: BLE001
            return err("E_PICK", str(exc))

        chosen = ""
        if selection:
            first = selection[0] if isinstance(selection, (list, tuple)) else selection
            chosen = str(first or "")
        if not chosen:
            return ok({"cancelled": True})

        if key:
            try:
                self._settings.update({key: chosen})
            except Exception as exc:  # noqa: BLE001
                return err("E_SETTINGS", str(exc), {"field": getattr(exc, "field", None)})
        return ok({"path": chosen, "cancelled": False})

    def open_settings_folder(self) -> dict[str, Any]:
        """在资源管理器里定位 settings.json。"""
        try:
            path = Path(getattr(self._settings, "path", ""))
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            if sys.platform.startswith("win"):
                import os

                os.startfile(str(path.parent))  # type: ignore[attr-defined]
            return ok({"path": str(path)})
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    # ------------------------------------------------------------------ 配置
    def check_config(self, config_path: str | None = None) -> dict[str, Any]:
        try:
            checker = getattr(self._wf, "check_config", None)
            if checker is not None:
                result = checker(config_path)
            else:
                # 后端尚未打上 workflow 补丁时的回退路径
                result = self._wf.service.check_config(config_path)
        except Exception as exc:  # noqa: BLE001
            return err("E_CONFIG", str(exc))
        return ok({
            "passed": bool(result.passed),
            "message": str(result.message or ""),
            "error_code": int(result.error_code or 0),
        })

    def app_info(self) -> dict[str, Any]:
        session = self._wf.session
        return ok({
            "version": self._version,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "dry_run": bool(session.dry_run) if session is not None else False,
        })

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        """窗口关闭时退订，避免线程残留。"""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
