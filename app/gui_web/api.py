"""REST 与 SSE 背后的应用门面。

职责只有四件：
1. 把 ``WorkflowController`` 的方法转发出去；
2. 用统一信封 ``{"ok": True, "data": ...}`` / ``{"ok": False, "error": ...}`` 包装；
3. 缓冲 ``JobEvent``，供 SSE 首帧与 ``/api/events/poll`` 按游标补发；
4. 转发界面设置（``SettingsStore``）与原生文件选择框。

本层由 ``app/gui_web/server.py`` 调用，不直接接触 pywebview；文件选择框经
``app.gui_web.native_dialog``（本进程弹 WinForms 对话框，跨进程拿不到窗口）。
禁止在此层写任何磁盘、PowerShell 或业务判断逻辑；
也不 import 除 ``workflow`` 之外的模块，避免把重依赖带进界面进程。

设置相关方法（get_settings / update_settings / reset_settings /
probe_path / pick_path / open_settings_folder），
``SettingsStore`` 通过构造参数注入，不传时按默认路径创建。
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from datetime import datetime
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

    number = int(disk_number)
    summaries = scan_disk_summaries([number])
    for summary in summaries:
        if int(summary.get("disk_number") or -1) == number:
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
    def start_session(self) -> dict[str, Any]:
        """启动会话（运行前检查 + 扫描硬盘 + 生成任务载荷快照）。"""
        try:
            session = self._wf.start()
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
    def prepare(self, selection: Any) -> dict[str, Any]:
        """生成执行计划（只读：不碰盘、不执行命令）。

        ``selection`` 可以是 ``[1, 3]``，也可以是 ``{1: {"c_size": 400}}``
        （按盘覆盖配置）。
        """
        try:
            prepared = self._wf.prepare(selection)
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

    def list_jobs(self) -> dict[str, Any]:
        """当前所有任务（界面首屏/日志坞在没有 SSE 时的兜底）。"""
        return ok([job.to_dict() for job in self._wf.jobs()])

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._wf.job_manager.get(job_id)
        if job is None:
            return err("E_NOT_FOUND", f"任务不存在: {job_id}")
        return ok(job.to_dict())

    def _latest_disk_log(self, job: Any) -> str | None:
        """运行中的任务还没有 ``log_path``：按「盘号 + 启动时间」兜底找日志。

        执行器的 ``CommandLog`` 文件名里带盘号（``disk-runner-d2-*.log``），
        任务结束前 ``details["log_path"]`` 还不存在，界面展开日志时会空着。
        这里只在「文件比任务启动时刻新」时采用，避免翻到同一块盘上一轮
        的旧日志。
        """
        root = self._log_root
        spec = getattr(job, "spec", None)
        try:
            number = int(getattr(spec, "disk_number", None))
        except (TypeError, ValueError):
            return None
        if not root:
            return None
        began: float | None = None
        started = str(getattr(job, "started_at", "") or "").strip()
        if started:
            try:
                began = datetime.fromisoformat(started).timestamp()
            except ValueError:
                began = None
        candidates: list[tuple[float, str]] = []
        for item in sorted(Path(root).glob(f"disk-runner-d{number}-*.log")):
            try:
                stamp = item.stat().st_mtime
            except OSError:
                continue
            if began is not None and stamp + 2.0 < began:
                continue
            candidates.append((stamp, str(item)))
        if not candidates:
            return None
        return max(candidates)[1]

    def job_log(self, job_id: str, tail: int = 200) -> dict[str, Any]:
        """读取任务日志尾部；路径必须落在配置的日志目录内。"""
        job = self._wf.job_manager.get(job_id)
        if job is None:
            return err("E_NOT_FOUND", f"任务不存在: {job_id}")
        path = _job_log_path(job) or self._latest_disk_log(job)
        if path is None:
            return ok(
                {
                    "path": "",
                    "lines": [],
                    "total": 0,
                    "message": "还没有日志：任务可能刚启动，或没配置日志目录（设置 · 日志与诊断）",
                }
            )
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
    def probe_images(self, path: str | None = None) -> dict[str, Any]:
        """设置里的两个系统版本（带文件头校验），或单独校验某个 GHO。

        不带 ``path`` 时返回 ``{"versions": [...], "default": 第一个可用路径}``：
        向导第 3 步直接拿它渲染按钮。装机只认设置里的 A/B 两个槽位，
        没有“扫描目录挑一个”的入口。
        """
        try:
            from app.modules.image.service import configured_versions, inspect_image
            from app.modules.image.service import probe as probe_image

            if path is not None:
                return ok(probe_image(str(path)))

            versions: list[dict[str, Any]] = []
            for version in configured_versions(self._settings.get()):
                entry: dict[str, Any] = dict(version)
                try:
                    info = inspect_image(version["path"])
                except Exception as exc:  # noqa: BLE001
                    entry.update({"ok": False, "reason": str(exc), "exists": False})
                else:
                    entry.update(
                        {
                            "ok": bool(info.get("magic_ok")),
                            "exists": bool(info.get("exists")),
                            "reason": info.get("reason", ""),
                            "size_bytes": info.get("size_bytes"),
                            "size_display": info.get("size_display"),
                        }
                    )
                versions.append(entry)
            default = next((item["path"] for item in versions if item.get("ok")), "")
            if not default and versions:
                default = versions[0]["path"]
            return ok({"versions": versions, "default": default})
        except Exception as exc:  # noqa: BLE001
            return err("E_IMAGE", str(exc))

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
                    item.spec.task_payload, letters=item.spec.drive_letters
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
    ) -> dict[str, Any]:
        """重写指定硬盘的 UEFI 引导；写盘前过同一套闸门。"""
        try:
            number = int(disk_number)
        except (TypeError, ValueError):
            return err("E_BOOT", "硬盘编号不合法")
        self._wf.authorize_disks([number])
        try:
            result = self._boot_repairer().repair(
                number, identity=self._resolve_identity(number, identity)
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
            bcdboot=str(settings.get("extBcdboot") or "") or None,
            log_dir=self._log_root,
            # GUI 是生产入口：用户点"修复引导"就是要真写盘，这里显式授权。
            allow_real_disk=True,
        )

    # ------------------------------------------------------------------ 分区/引导
    def _resolve_identity(
        self, disk_number: int, identity: dict[str, Any] | None
    ) -> dict[str, Any]:
        """取这块盘的身份：调用方给了就用，没给就现场扫一次。

        写盘闸门要拿“期望身份”与执行时重扫的结果比对，防止“确认的盘”和
        “实际要写的盘”不是同一块；而界面上的修复引导只传盘号。扫描本身是只读的，
        补这一下比放宽闸门安全得多。
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

    def _gate(self, disk_number: int, identity: dict[str, Any]) -> Any:
        """分区/引导共用的写盘闸门。"""
        return self._wf.service.require_disk_writable(disk_number, identity)

    # ------------------------------------------------------------------ 设置
    def get_settings(self) -> dict[str, Any]:
        try:
            return ok(self._settings.to_dict())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def update_settings(self, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        """局部更新；任一项不合法则整体拒绝，不落盘。

        回包与 ``GET /api/settings`` 同形（``settings`` / ``warnings`` / …）：界面保存
        后要拿它直接刷新本地快照和提醒，扁平回包会让界面停在旧值上。
        """
        try:
            self._settings.update(patch or {})
            return ok(self._settings.to_dict())
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
            self._settings.update(dict(patch))
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc), {"field": getattr(exc, "field", None)})
        payload = {"source": source or "inline"}
        payload.update(self._settings.to_dict())
        return ok(payload)

    def reset_settings(self) -> dict[str, Any]:
        try:
            self._settings.reset()
            return ok(self._settings.to_dict())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def probe_path(self, key: str, path: str | None = None) -> dict[str, Any]:
        try:
            return ok(self._settings.probe(key, path).to_dict())
        except Exception as exc:  # noqa: BLE001
            return err("E_SETTINGS", str(exc))

    def pick_path(self, key: str = "", kind: str = "file") -> dict[str, Any]:
        """打开原生选择框，只把选中的路径交回去；取消返回 ``{"cancelled": True}``。

        不落盘：设置页把选中值填进草稿，由「保存设置」统一写入 —— 选完立刻写盘
        却同时显示「未保存的改动」会自相矛盾，用户也无法反悔。``key`` 只用来取
        当前值作对话框的起始位置。

        不能用 pywebview 的 ``window.create_file_dialog``：桌面模式下窗口在父
        进程，本 API 跑在 UAC 拉起的提权子进程里，子进程的 ``webview.windows``
        恒为空列表，只会得到 ``E_NO_WINDOW 窗口尚未创建``（点「浏览…」什么也不
        弹）。所以选择框交给本进程用 WinForms 弹（见
        :mod:`app.gui_web.native_dialog`，与 pywebview 内部用的是同一套对话框）。
        """
        from app.gui_web import native_dialog

        current = ""
        if key:
            try:
                current = str((self._settings.get() or {}).get(key) or "")
            except Exception:  # noqa: BLE001 - 读不到当前值不影响弹框
                current = ""
        try:
            chosen = native_dialog.pick_path(kind, initial=current)
        except Exception as exc:  # noqa: BLE001
            return err("E_PICK", str(exc))

        if not chosen:
            return ok({"cancelled": True})

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

    def app_info(self) -> dict[str, Any]:
        return ok({
            "version": self._version,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        })

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        """窗口关闭时退订，避免线程残留。"""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
