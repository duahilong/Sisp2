"""REST 与 SSE 背后的应用门面。

职责只有四件：
1. 把 ``WorkflowController`` 的方法转发出去；
2. 用统一信封 ``{"ok": True, "data": ...}`` / ``{"ok": False, "error": ...}`` 包装；
3. 缓冲 ``JobEvent``，供 SSE 首帧与 ``/api/events/poll`` 按游标补发；
4. 转发界面设置（``SettingsStore``）与原生文件选择框。

本层由 ``app/gui_web/server.py`` 调用，不直接接触 pywebview；只有
``pick_path`` 在桌面窗口模式下会惰性导入 pywebview 弹出原生选择框。
禁止在此层写任何磁盘、PowerShell 或业务判断逻辑；
也不 import 除 ``workflow`` 之外的模块，避免把重依赖带进界面进程。

设置相关方法（get_settings / update_settings / reset_settings /
probe_path / probe_all_paths / pick_path / open_settings_folder），
``SettingsStore`` 通过构造参数注入，不传时按默认路径创建。
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只为类型标注，避免 import 时拉起整条应用层依赖链
    from app.application.workflow import WorkflowController
    from app.modules.settings.service import SettingsStore

# 版本号只有一个来源（app/version.py），打包时的 Windows 版本资源也用它
from app.version import __version__

MAX_BUFFER = 5000


def ok(data: Any) -> dict[str, Any]:
    """包装成功响应，并保证一定可以 JSON 序列化。"""
    return {"ok": True, "data": json.loads(json.dumps(data, ensure_ascii=False, default=str))}


def err(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """包装失败响应。"""
    return {"ok": False, "error": {"code": code, "message": message, "detail": detail}}


def _scan_summary(disk_number: int) -> dict[str, Any]:
    from app.modules.disk_info.service import scan_disk_summaries

    for summary in scan_disk_summaries():
        if int(summary.get("disk_number") or -1) == int(disk_number):
            return summary
    raise PermissionError(f"硬盘 {disk_number} 当前不存在")


def _job_log_path(job: Any) -> str | None:
    """从任务结果最后一步的 details 里找日志路径。"""
    result = getattr(job, "result", None)
    steps = getattr(result, "steps", ()) or ()
    for step in reversed(tuple(steps)):
        details = getattr(step, "details", None) or {}
        path = str(details.get("log_path") or "").strip()
        if path:
            return path
    return None


class WebApi:
    """暴露给 JavaScript 的接口对象。

    所有方法都返回信封 dict；pywebview 会把它转成 Promise 的结果。
    JavaScript 侧必须**按位置**传全部参数，不支持关键字参数。
    """

    def __init__(
        self,
        wf: WorkflowController,
        app_version: str = __version__,
        settings: SettingsStore | None = None,
        log_dir: str | None = None,
    ) -> None:
        self._wf = wf
        self._version = app_version
        # 日志读取的白名单根目录：只能读这个目录下的 *.log。
        self._log_root = log_dir
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
    def start_session(
        self,
        config_path: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """启动会话。

        ``dry_run`` 省略（``None``）表示“保持进程当前的模式”——GUI 的模式在
        ``gui_web/main.py`` 里已经按 ``--dry-run`` / ``dryRunByDefault`` 定好了，
        界面不该在每次刷新会话时把它改回真实写盘。
        """
        try:
            session = self._wf.start(
                config_path, dry_run=None if dry_run is None else bool(dry_run)
            )
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

    def confirm(
        self,
        job_ids: list[str] | None = None,
        confirm_text: str | None = None,
    ) -> dict[str, Any]:
        try:
            jobs = self._wf.confirm(job_ids, confirm_text=confirm_text)
        except Exception as exc:  # noqa: BLE001
            return err("E_CONFIRM", str(exc))
        return ok([job.to_dict() for job in jobs])

    def cancel(self, job_id: str | None = None) -> dict[str, Any]:
        try:
            cancelled = self._wf.cancel([job_id] if job_id else None)
        except Exception as exc:  # noqa: BLE001
            return err("E_CANCEL", str(exc))
        return ok({"cancelled": cancelled})

    def list_jobs(self) -> dict[str, Any]:
        """当前所有任务（界面首屏/日志坞在没有 SSE 时的兜底）。"""
        return ok([job.to_dict() for job in self._wf.jobs()])

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._wf.job_manager.get(job_id)
        if job is None:
            return err("E_NOT_FOUND", f"任务不存在: {job_id}")
        return ok(job.to_dict())

    def job_log(self, job_id: str, tail: int = 200) -> dict[str, Any]:
        """读取任务日志尾部；路径必须落在配置的日志目录内。"""
        job = self._wf.job_manager.get(job_id)
        if job is None:
            return err("E_NOT_FOUND", f"任务不存在: {job_id}")
        path = _job_log_path(job)
        if path is None:
            return ok({"path": "", "lines": [], "total": 0, "message": "该任务没有日志文件"})
        allowed = self._allowed_log_path(path)
        if allowed is None:
            return err("E_PATH", "日志路径不在日志目录内，拒绝读取", {"path": str(path)})
        try:
            text = allowed.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return err("E_LOG", f"读取日志失败：{exc}")
        lines = text.splitlines()
        limit = max(1, min(int(tail or 200), 5000))
        return ok({"path": str(allowed), "lines": lines[-limit:], "total": len(lines)})

    def _allowed_log_path(self, path: str) -> Path | None:
        """把日志路径收敛到 ``log_dir`` 内的 ``.log`` 文件；越界返回 None。"""
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_file() or resolved.suffix.lower() != ".log":
            return None
        if not self._log_root:
            return resolved
        try:
            root = Path(self._log_root).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return resolved if root == resolved.parent or root in resolved.parents else None

    # ------------------------------------------------------------------ 镜像
    def probe_images(self, directory: str | None = None, path: str | None = None) -> dict[str, Any]:
        """列镜像目录，或校验单个 GHO（读文件头，全部是纯文件操作）。"""
        try:
            from app.modules.image.service import probe as probe_image

            target_dir = directory
            if path is None and not target_dir:
                target_dir = str(self._settings.get().get("sysIsoDir") or "")
            data = probe_image(directory=target_dir, path=path)
        except Exception as exc:  # noqa: BLE001
            return err("E_IMAGE", str(exc))
        return ok(data)

    # ------------------------------------------------------------------ 分区预览
    def partition_preview(self, disk_number: Any = None) -> dict[str, Any]:
        """返回**真实会执行的分区脚本**（三段，与执行器同源生成）。

        只读：不 prepare、不改会话、不碰盘。数据来自最近的 ``prepare()`` 结果，
        所以确认页（第 4 步）能拿到与真跑逐字一致的脚本文本；还没 prepare 时
        返回 ``E_PREPARE``，提示先生成计划。
        """
        try:
            prepared = self._wf.prepared_jobs
        except Exception as exc:  # noqa: BLE001
            return err("E_PREPARE", str(exc))
        if not prepared:
            return err("E_PREPARE", "尚未生成执行计划，请先调用 /api/prepare")
        wanted = None
        if disk_number is not None:
            try:
                wanted = int(disk_number)
            except (TypeError, ValueError):
                return err("E_PREPARE", "硬盘编号不合法")
        items: list[dict[str, Any]] = []
        for item in prepared:
            number = int(item.spec.disk_number)
            if wanted is not None and number != wanted:
                continue
            try:
                # 纯函数生成（与真实执行器同一套）：不经过 service，也不碰盘。
                from app.application.partitions import plan_partitions, preview_segments

                parts = plan_partitions(
                    item.spec.config_payload, letters=item.spec.drive_letters
                )
                segments = preview_segments(number, parts)
            except Exception as exc:  # noqa: BLE001 - 布局非法也要能回一句人话
                return err("E_PREPARE", f"无法生成分区脚本：{exc}")
            script = chr(10).join(
                line for segment in segments for line in segment["lines"]
            )
            items.append(
                {
                    "disk_number": number,
                    "model": item.plan.model,
                    "drive_letters": dict(item.spec.drive_letters),
                    "partitions": [dict(part) for part in item.plan.partition_plan],
                    "segments": segments,
                    "script": script,
                }
            )
        if wanted is not None and not items:
            return err("E_PREPARE", f"硬盘 {wanted} 还没有计划，请先调用 /api/prepare")
        return ok(items)

    # ------------------------------------------------------------------ 引导
    def boot_report(self) -> dict[str, Any]:
        """扫描全部硬盘的 Windows 目录与 EFI 引导文件。"""
        try:
            report = self._boot_repairer().report()
        except Exception as exc:  # noqa: BLE001
            return err("E_BOOT", str(exc))
        return ok(report)

    def boot_repair(
        self,
        disk_number: Any,
        identity: dict[str, Any] | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """重写指定硬盘的 UEFI 引导；写盘前过同一套闸门。"""
        try:
            number = int(disk_number)
        except (TypeError, ValueError):
            return err("E_BOOT", "硬盘编号不合法")
        self._wf.authorize_disks([number], dry_run=dry_run)
        try:
            result = self._boot_repairer().repair(
                number, identity=self._resolve_identity(number, identity), dry_run=dry_run
            )
        except PermissionError as exc:
            return err("E_CONFIRM", str(exc))
        except Exception as exc:  # noqa: BLE001
            return err("E_BOOT", str(exc))
        return ok(result)

    def _boot_repairer(self) -> Any:
        from app.modules.boot.service import BootRepairer

        settings = self._settings.get()
        return BootRepairer(
            self._gate,
            diskpart=str(settings.get("extDiskpart") or "") or None,
            temp_dir=str(settings.get("sysTemp") or "") or None,
            log_dir=self._log_root,
            dry_run=self._service_dry_run(),
            # GUI 是生产入口：用户点"修复引导"就是要真写盘，这里显式授权。
            allow_real_disk=True,
        )

    # ------------------------------------------------------------------ 分区/引导
    def _resolve_identity(
        self, disk_number: int, identity: dict[str, Any] | None
    ) -> dict[str, Any]:
        """取这块盘的强身份：调用方给了就用，没给就现场扫一次。

        ``blockUnverified`` 默认是打开的，写盘闸门要求“期望身份”里有 UniqueId 或
        序列号；而界面上的修复引导只传盘号。如果不在这里补齐身份，真实操作
        会一律被拒（写入前复核拿不到可比对的东西）——扫描本身是只读的，补这一下
        比放宽闸门安全得多。
        """
        if identity:
            return dict(identity)
        try:
            summary = _scan_summary(disk_number)
        except Exception:  # noqa: BLE001 - 盘不存在时让闸门自己报错
            return {}
        return {
            "disk_number": disk_number,
            "unique_id": summary.get("unique_id"),
            "serial_number": summary.get("serial_number"),
            "model": summary.get("model"),
            "size_bytes": summary.get("size_bytes"),
            "size_display": summary.get("size_display"),
        }

    def _excluded_names(self) -> list[str]:
        """当前生效的“禁止操作”的硬盘型号名单。

        两个来源并存：设置页的 ``excludedDiskNames``（用户在界面上维护）与会话
        业务配置里的 ``excluded_disk_names``（``-j`` 指定的配置文件）。取并集、
        去重、丢空项；大小写不在这里处理，``_protection_reasons`` 自己 casefold。
        """
        session = getattr(self._wf, "session", None)
        payload = getattr(session, "config_payload", None) if session is not None else None
        sources = (
            self._settings.get().get("excludedDiskNames"),
            payload.get("excluded_disk_names") if isinstance(payload, Mapping) else None,
        )
        names: list[str] = []
        for source in sources:
            if isinstance(source, (list, tuple)):
                names.extend(str(item).strip() for item in source if str(item).strip())
        return list(dict.fromkeys(names))

    def _gate(self, disk_number: int, identity: dict[str, Any]) -> Any:
        """分区/引导共用的写盘闸门；dry-run 下由各 runner 自己短路。

        ``excluded_names`` 必须带上：``require_disk_writable`` 的第 5 道判定只认
        入参里的名单，不读全局状态；漏传等于让“排除名单”在分区与修复引导两条
        旁路上完全失效（整盘安装走的是 ``spec.config_payload``，不受影响）。
        """
        service = self._wf.service
        gate = getattr(service, "require_disk_writable", None)
        if gate is None:  # 测试替身没有该方法时退回“只读扫描”
            return _scan_summary(disk_number)
        return gate(disk_number, identity, excluded_names=self._excluded_names())

    def _service_dry_run(self) -> bool:
        return bool(getattr(self._wf.service, "dry_run", True))

    # ------------------------------------------------------------------ 设置
    def settings_data(self) -> dict[str, Any]:
        """当前设置快照（供 main.py 读 rememberWindow 等行为开关）。"""
        return self._settings.get()

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

    def export_settings(self, path: str | None = None) -> dict[str, Any]:
        """把当前设置写成一个可携带的 JSON 文件。

        不传 ``path`` 时写到设置目录下的 ``sisp2-settings-<时间戳>.json``，
        返回值里带最终路径（浏览器模式下没有原生保存框，靠这个路径就能找到文件）。
        """
        try:
            payload = {
                "version": 1,  # 与 SettingsStore.save() 的封套版本保持一致
                "settings": self._settings.get(),
            }
            if path:
                target = Path(str(path)).expanduser()
            else:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                base = Path(getattr(self._settings, "path", "")).parent
                target = base / f"sisp2-settings-{stamp}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", f"导出设置失败：{exc}")
        return ok({"path": str(target), "settings": payload["settings"]})

    def import_settings(
        self, path: str | None = None, content: str | None = None
    ) -> dict[str, Any]:
        """从文件（或直接给的 JSON 文本）导入设置；整体校验，不合法一个字段都不写。"""
        raw = content
        source = ""
        if raw in (None, ""):
            if not path:
                return err("E_SETTINGS", "导入设置需要文件路径或 JSON 内容")
            source = str(path)
            try:
                raw = Path(source).expanduser().read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                return err("E_SETTINGS", f"读取设置文件失败：{exc}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return err("E_SETTINGS", f"设置文件不是合法 JSON：{exc}")
        if not isinstance(parsed, dict):
            return err("E_SETTINGS", "设置文件顶层必须是 JSON 对象")
        # 兼容 {"version":1,"settings":{...}} 封套与裸设置对象两种形态
        stored = parsed.get("settings")
        patch = stored if isinstance(stored, dict) else parsed
        try:
            settings = self._settings.update(dict(patch))
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc), {"field": getattr(exc, "field", None)})
        return ok({"settings": settings, "source": source or "inline"})

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
                if kind == "dir":
                    dialog = dialog_module.FOLDER
                elif kind == "save":
                    dialog = dialog_module.SAVE
                else:
                    dialog = dialog_module.OPEN
            else:
                legacy = {"dir": "FOLDER_DIALOG", "save": "SAVE_DIALOG"}
                dialog = getattr(webview, legacy.get(kind, "OPEN_DIALOG"))

            kwargs: dict[str, Any] = {"allow_multiple": False}
            if kind == "save":
                # 保存框需要一个默认文件名；旧版 pywebview 不认这个参数
                kwargs["save_filename"] = "sisp2-settings.json"
            try:
                selection = window.create_file_dialog(dialog, **kwargs)
            except TypeError:
                kwargs.pop("save_filename", None)
                selection = window.create_file_dialog(dialog, **kwargs)
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

                # 打开资源管理器是外壳动作，不经 shell 才是正确做法
                os.startfile(str(path.parent))  # type: ignore[attr-defined]  # noqa: S606
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
