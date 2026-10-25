"""GUI 冒烟测试：不启动真实事件循环，也不触碰磁盘。

未安装 PySide6 的环境会跳过本文件；GUI 业务逻辑本身由
``tests/test_workflow_controller.py`` 在不依赖 Qt 的情况下覆盖。这里只验证
控件装配、表格填充、状态刷新和简单的用户操作路径。
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.application.contracts import CheckItem, DiskInfo  # noqa: E402
from app.application.events import JobEvent, JobEventType  # noqa: E402
from app.application.jobs import ExecutionPlan, JobManager, JobSpec, JobStatus  # noqa: E402
from app.application.workflow import PreparedJob, SessionState  # noqa: E402
from app.gui.main_window import PAGE_CONFIG, PAGE_DISKS, PAGE_RESULTS, MainWindow, format_duration  # noqa: E402
from app.models import StepRecord, WorkerResult  # noqa: E402
from app.modules.common.constants import ErrorCode  # noqa: E402

APP = QApplication.instance() or QApplication([])


def make_disk(
    disk_number: int = 2,
    *,
    selectable: bool = True,
    reasons: tuple[str, ...] = (),
) -> DiskInfo:
    model = "Demo Disk" if selectable else "System Disk"
    return DiskInfo.from_summary(
        {
            "disk_number": disk_number,
            "model": model,
            "serial_number": f"SN-{disk_number}",
            "unique_id": f"UID-{disk_number}",
            "size_bytes": 500 * 1024 ** 3,
            "size_display": "465.66 GB",
            "partition_style": "GPT",
            "bus_type": "SATA",
            "drive_letters": ["C"] if not selectable else [],
            "is_boot": not selectable,
            "is_system": not selectable,
            "is_offline": False,
            "is_read_only": False,
            "is_selectable": selectable,
            "protection_reasons": list(reasons),
        }
    )


def make_prepared(
    *,
    disk_number: int = 2,
    blockers: tuple[str, ...] = (),
    dry_run: bool = False,
) -> PreparedJob:
    disk = make_disk(disk_number)
    identity = {
        "disk_number": disk.disk_number,
        "unique_id": disk.unique_id,
        "serial_number": disk.serial_number,
        "model": disk.model,
        "size_bytes": disk.size_bytes,
    }
    letters = {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}
    plan = ExecutionPlan(
        disk_number=disk.disk_number,
        model=disk.model or "",
        size_display=disk.size_display,
        identity=identity,
        drive_letters=dict(letters),
        config_path="D:\\config\\win11.json",
        dry_run=dry_run,
        warnings=("破坏性操作：将清空目标硬盘全部数据",),
        checks=(
            CheckItem("identity", "硬盘强身份", not blockers, "已具备 UniqueId"),
        ),
        blockers=blockers,
    )
    spec = JobSpec(
        disk_number=disk.disk_number,
        config_payload={"config_path": "D:\\config\\win11.json"},
        expected_identity=identity,
        drive_letters=dict(letters),
        dry_run=dry_run,
    )
    return PreparedJob(spec=spec, plan=plan, disk=disk)


def make_result(
    disk_number: int = 2,
    *,
    success: bool = True,
    error_code: int = ErrorCode.SUCCESS,
    message: str = "完成",
    log_path: str | None = None,
) -> WorkerResult:
    return WorkerResult(
        disk_number=disk_number,
        success=success,
        error_code=error_code,
        message=message,
        steps=(StepRecord("初始化硬盘", success, message, 1.25),),
        log_path=log_path,
    )


def make_job(
    manager: JobManager,
    disk_number: int = 2,
    *,
    status: JobStatus = JobStatus.SUCCEEDED,
    result: WorkerResult | None = None,
    events: tuple[JobEvent, ...] = (),
):
    prepared = make_prepared(disk_number=disk_number)
    job = manager.submit(prepared.spec, prepared.plan)
    job.status = status
    job.result = result
    job.events = list(events)
    job.started_at = "2026-10-25T10:00:00"
    job.finished_at = "2026-10-25T10:01:30"
    return job


class FakeController:
    """只实现 MainWindow 使用到的工作流接口。"""

    def __init__(
        self,
        *,
        session: SessionState | None = None,
        manager: JobManager | None = None,
        jobs: list | None = None,
    ) -> None:
        self.session = session
        self.job_manager = manager or JobManager(runner=lambda spec, context: None)
        self._jobs = list(jobs or [])
        self.cancelled: list[list[str]] = []
        self.refresh_calls = 0
        self.prepared: list[PreparedJob] = []

    def jobs(self) -> list:
        return list(self._jobs)

    def refresh_disks(self) -> list:
        self.refresh_calls += 1
        return list(self.session.disks) if self.session is not None else []

    def prepare(self, selected_disk_numbers, **_kwargs) -> list[PreparedJob]:
        _ = selected_disk_numbers
        return list(self.prepared)

    def cancel(self, job_ids=None, reason: str = "用户取消") -> list[str]:
        ids = [job.id for job in self._jobs] if job_ids is None else list(job_ids)
        self.cancelled.append(ids)
        cancelled: list[str] = []
        for job_id in ids:
            if self.job_manager.cancel(job_id, reason=reason):
                cancelled.append(job_id)
        return cancelled


def close_window(window: MainWindow) -> None:
    """把任务标记为结束后再关闭，避免触发“任务仍在执行”确认框。"""
    for job in window.controller.jobs():
        job.status = JobStatus.SUCCEEDED
    window.close()


def test_main_window_builds_five_pages() -> None:
    window = MainWindow()
    try:
        assert window.pages.count() == 5
        assert window.windowTitle().startswith("Sisp")
        assert window.config_next_button.isEnabled() is False
        assert window.prepare_button.isEnabled() is False
        assert window.plan_confirm_button.isEnabled() is False
    finally:
        close_window(window)


def test_disk_page_populates_rows_and_tracks_selection() -> None:
    session = SessionState(
        config_path="D:\\config\\win11.json",
        config_payload={"config_path": "D:\\config\\win11.json"},
        disks=(
            make_disk(2),
            make_disk(0, selectable=False, reasons=("系统盘",)),
        ),
    )
    controller = FakeController(session=session)
    window = MainWindow(controller)
    try:
        window._show_page(PAGE_DISKS)

        assert window.disk_table.rowCount() == 2
        assert window.disk_status_label.text().startswith("共 2 块硬盘")
        assert window._collect_selected_disks() == []
        assert window.prepare_button.isEnabled() is False

        protected = window.disk_table.item(1, 0)
        assert protected.flags() & Qt.ItemFlag.ItemIsUserCheckable == Qt.ItemFlag(0)
        assert window.disk_table.item(1, 1).text() == "0"
        assert window.disk_table.item(1, 2).text() == "受保护"
        assert window.disk_table.item(1, 3).text() == "System Disk"
        assert window.disk_table.item(1, 7).text() == "系统盘"

        window.disk_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        assert window._collect_selected_disks() == [2]
        assert window.prepare_button.isEnabled() is True
    finally:
        close_window(window)


def test_plan_page_blocks_confirmation_when_blockers_exist() -> None:
    window = MainWindow()
    try:
        window._prepared = [make_prepared(blockers=("缺少 UniqueId",))]
        window._render_plan_tabs()

        assert window.plan_blocker_label.isHidden() is False
        assert window.plan_confirm_check.isEnabled() is False
        window.plan_confirm_check.setChecked(True)
        assert window.plan_confirm_button.isEnabled() is False

        window._prepared = [make_prepared()]
        window._render_plan_tabs()
        assert window.plan_blocker_label.isHidden() is True
        assert window.plan_confirm_check.isEnabled() is True
        window.plan_confirm_check.setChecked(True)
        assert window.plan_confirm_button.isEnabled() is True
    finally:
        close_window(window)


def test_plan_page_switches_to_dry_run_wording() -> None:
    window = MainWindow()
    try:
        window._prepared = [make_prepared(dry_run=True)]
        window._render_plan_tabs()

        assert window.plan_tabs.count() == 1
        assert window.plan_tabs.tabText(0) == "硬盘 2"
        assert "dry-run" in window.plan_confirm_check.text()
        assert window.plan_confirm_button.text() == "开始 dry-run"
        assert "DRY-RUN" in window._plan_html(window._prepared[0])
    finally:
        close_window(window)


def test_plan_html_contains_identity_steps_and_warnings() -> None:
    window = MainWindow()
    try:
        text = window._plan_html(make_prepared())
        assert "硬盘 2" in text
        assert "UID-2" in text
        assert "初始化硬盘" in text
        assert "创建引导记录" in text
        assert "破坏性操作" in text
        assert "EFI = E" in text
    finally:
        close_window(window)


def test_prepared_and_refreshed_callbacks_update_window() -> None:
    session = SessionState(
        config_path="D:\\config\\win11.json",
        config_payload={"config_path": "D:\\config\\win11.json"},
        disks=(make_disk(2), make_disk(3)),
    )
    controller = FakeController(session=session)
    window = MainWindow(controller)
    try:
        window._show_page(PAGE_DISKS)
        window._on_disks_refreshed([])
        assert window.disk_table.rowCount() == 2
        assert window.refresh_button.isEnabled() is True

        prepared = [make_prepared(disk_number=3)]
        window._on_prepared(prepared)
        assert window._prepared == prepared
        assert window.pages.currentIndex() == 2
        assert window.plan_tabs.count() == 1
    finally:
        close_window(window)


def test_monitor_rows_and_logs_follow_job_events() -> None:
    manager = JobManager(runner=lambda spec, context: None)
    running = make_job(manager, status=JobStatus.RUNNING)
    running.events.append(
        JobEvent(
            job_id=running.id,
            sequence=1,
            type=JobEventType.STEP_STARTED.value,
            timestamp="2026-10-25T10:00:01",
            payload={"name": "分区并格式化"},
        )
    )
    done = make_job(
        manager,
        3,
        status=JobStatus.SUCCEEDED,
        result=make_result(3, log_path="D:\\logs\\disk3.log"),
    )
    controller = FakeController(manager=manager, jobs=[running, done])
    window = MainWindow(controller)
    try:
        window._job_ids = [running.id, done.id]
        window.monitor_table.setRowCount(0)
        window._monitor_rows.clear()
        for job in (running, done):
            window._ensure_monitor_row(job)
        assert window.monitor_table.rowCount() == 2

        window._update_monitor_row(running)
        assert window.monitor_table.item(0, 2).text() == "执行中"
        assert window.monitor_table.item(0, 4).text() == "分区并格式化"

        window._on_job_event(
            JobEvent(
                job_id=running.id,
                sequence=2,
                type=JobEventType.LOG.value,
                timestamp="2026-10-25T10:00:02",
                payload={"text": "正在创建分区"},
            )
        )
        assert "正在创建分区" in window.log_view.toPlainText()
        assert "硬盘 2" in window.log_view.toPlainText()

        window._refresh_monitor_summary()
        assert window.monitor_summary_label.text().startswith("总计 2 块硬盘")
        assert "执行 1" in window.monitor_summary_label.text()
        assert window.monitor_results_button.isEnabled() is False
    finally:
        close_window(window)


def test_cancel_all_requests_cooperative_cancellation() -> None:
    manager = JobManager(runner=lambda spec, context: None)
    job = make_job(manager, status=JobStatus.WAITING_CONFIRMATION)
    controller = FakeController(manager=manager, jobs=[job])
    window = MainWindow(controller)
    try:
        window._job_ids = [job.id]
        window._ensure_monitor_row(job)

        window._cancel_all()

        assert controller.cancelled == [[job.id]]
        assert job.status == JobStatus.CANCELLED
        assert "已请求取消" in window.log_view.toPlainText()
        assert window.cancel_all_button.isEnabled() is False
    finally:
        close_window(window)


def test_result_page_lists_jobs_and_shows_step_details() -> None:
    manager = JobManager(runner=lambda spec, context: None)
    success = make_job(
        manager,
        status=JobStatus.SUCCEEDED,
        result=make_result(2, log_path="D:\\logs\\disk2.log"),
    )
    cancelled = make_job(
        manager,
        3,
        status=JobStatus.CANCELLED,
        result=make_result(
            3,
            success=False,
            error_code=ErrorCode.CANCELLED,
            message="用户取消",
        ),
    )
    pending = make_job(manager, 4, status=JobStatus.RUNNING, result=None)
    failed = make_job(
        manager,
        5,
        status=JobStatus.FAILED,
        result=make_result(5, success=False, error_code=ErrorCode.GHOST_FAILED, message="Ghost 写入失败"),
    )
    controller = FakeController(manager=manager, jobs=[success, cancelled, pending, failed])
    window = MainWindow(controller)
    try:
        window._show_page(PAGE_RESULTS)

        assert window.result_table.rowCount() == 4
        assert window.result_table.item(0, 1).text() == "成功"
        assert window.result_table.item(1, 1).text() == "已取消"
        assert window.result_table.item(2, 1).text() == "未完成"
        assert window.result_table.item(3, 1).text() == "失败"
        assert "成功 1" in window.result_summary_label.text()
        assert "失败 1" in window.result_summary_label.text()
        assert "已取消 1" in window.result_summary_label.text()

        window.result_table.selectRow(0)
        assert window.open_log_button.isEnabled() is True
        detail = window.result_detail.toPlainText()
        assert "初始化硬盘" in detail
        assert "disk2.log" in detail

        window.result_table.selectRow(2)
        assert window.open_log_button.isEnabled() is False
    finally:
        close_window(window)


def test_rescan_from_results_without_session_returns_to_config_page() -> None:
    controller = FakeController(session=None)
    window = MainWindow(controller)
    try:
        window._rescan_from_results()
        assert window.pages.currentIndex() == PAGE_CONFIG
    finally:
        close_window(window)


def test_format_duration_handles_missing_and_minutes() -> None:
    manager = JobManager(runner=lambda spec, context: None)
    job = make_job(manager, status=JobStatus.SUCCEEDED)

    job.started_at = None
    assert format_duration(job) == "—"

    job.started_at = "not-a-timestamp"
    assert format_duration(job) == "—"

    job.started_at = "2026-10-25T10:00:00"
    job.finished_at = "2026-10-25T10:00:42"
    assert format_duration(job) == "42.0 秒"

    job.finished_at = "2026-10-25T10:02:05"
    assert format_duration(job) == "2 分 5 秒"
