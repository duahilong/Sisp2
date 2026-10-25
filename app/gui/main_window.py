"""PySide6 最小可用界面。

界面严格分成五个区域：

1. 运行环境与配置检查；
2. 硬盘选择；
3. 计划确认（硬盘身份、容量、盘符、步骤、阻断项、破坏性警告）；
4. 执行监控（实时步骤、日志、协作取消）；
5. 结果汇总。

所有业务判断都来自 ``WorkflowController``、``ExecutionPlan`` 和
``JobManager``，本模块只做展示和用户操作。
"""

from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.application.events import JobEvent, JobEventType
from app.application.jobs import Job, JobStatus
from app.application.workflow import (
    PreparedJob,
    SessionState,
    WorkflowController,
    WorkflowError,
)
from app.gui.jobs_bridge import CallWorker, JobEventBridge
from app.modules.common.constants import ErrorCode
from app.modules.config_loader.service import DEFAULT_CONFIG_PATH

PAGE_CONFIG = 0
PAGE_DISKS = 1
PAGE_PLAN = 2
PAGE_MONITOR = 3
PAGE_RESULTS = 4

STATUS_LABELS = {
    JobStatus.WAITING_CONFIRMATION: "等待启动",
    JobStatus.RUNNING: "执行中",
    JobStatus.SUCCEEDED: "成功",
    JobStatus.FAILED: "失败",
    JobStatus.CANCELLED: "已取消",
}

STATUS_COLORS = {
    JobStatus.WAITING_CONFIRMATION: "#52606d",
    JobStatus.RUNNING: "#1f6feb",
    JobStatus.SUCCEEDED: "#1e7d4f",
    JobStatus.FAILED: "#c0392b",
    JobStatus.CANCELLED: "#7b8794",
}

STYLE_SHEET = """
QMainWindow, QWidget {
    background: #f5f7fa;
    color: #1f2933;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}
QLabel#Title {
    font-size: 24px;
    font-weight: 600;
    color: #102a43;
}
QLabel#Subtitle {
    color: #52606d;
}
QLabel#Hint {
    color: #627d98;
}
QLabel#Blocked {
    color: #a93226;
    font-weight: 600;
}
QLabel#Warning {
    color: #9a6700;
}
QGroupBox {
    background: #ffffff;
    border: 1px solid #d9e2ec;
    border-radius: 8px;
    margin-top: 12px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: #334e68;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #bcccdc;
    border-radius: 6px;
    padding: 7px 15px;
}
QPushButton:hover {
    background: #eef4fb;
}
QPushButton:pressed {
    background: #d9e8f8;
}
QPushButton:disabled {
    color: #9aa5b1;
    background: #f0f2f5;
    border-color: #e4e7eb;
}
QPushButton#Primary {
    background: #1f6feb;
    border: 1px solid #1a5fd0;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#Primary:hover {
    background: #1a5fd0;
}
QPushButton#Primary:disabled {
    background: #9db8e8;
    border-color: #9db8e8;
    color: #f0f4f8;
}
QPushButton#Danger {
    background: #c0392b;
    border: 1px solid #a93226;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#Danger:hover {
    background: #a93226;
}
QPushButton#Danger:disabled {
    background: #d9a6a0;
    border-color: #d9a6a0;
    color: #f8eeee;
}
QLineEdit {
    background: #ffffff;
    border: 1px solid #bcccdc;
    border-radius: 6px;
    padding: 6px 8px;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid #d9e2ec;
    border-radius: 6px;
    gridline-color: #e4e7eb;
    selection-background-color: #d9e8f8;
    selection-color: #102a43;
}
QHeaderView::section {
    background: #eef2f6;
    border: none;
    border-bottom: 1px solid #d9e2ec;
    padding: 6px;
    font-weight: 600;
}
QPlainTextEdit {
    background: #0f1720;
    color: #d9e2ec;
    border: 1px solid #243b53;
    border-radius: 6px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
}
QTextBrowser#PlanView {
    background: #ffffff;
    color: #1f2933;
    border: 1px solid #d9e2ec;
    border-radius: 6px;
    padding: 10px;
}
QTabWidget::pane {
    border: 1px solid #d9e2ec;
    border-radius: 6px;
    background: #ffffff;
}
QTabBar::tab {
    background: #eef2f6;
    border: 1px solid #d9e2ec;
    padding: 7px 14px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #ffffff;
    border-bottom-color: #ffffff;
    font-weight: 600;
}
QSplitter::handle {
    background: #d9e2ec;
    height: 3px;
}
"""


def format_duration(job: Job) -> str:
    """根据任务的开始/结束时间生成简短耗时。"""
    if not job.started_at or not job.finished_at:
        return "—"
    try:
        started = datetime.fromisoformat(job.started_at)
        finished = datetime.fromisoformat(job.finished_at)
    except ValueError:
        return "—"
    seconds = max((finished - started).total_seconds(), 0.0)
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes} 分 {remainder} 秒"


class MainWindow(QMainWindow):
    """Sisp 图形界面主窗口。"""

    def __init__(self, controller: WorkflowController | None = None) -> None:
        super().__init__()
        self.controller = controller or WorkflowController()
        self._bridge = JobEventBridge(self.controller.job_manager, self)
        self._bridge.event_received.connect(self._on_job_event)
        self._workers: set[CallWorker] = set()
        self._prepared: list[PreparedJob] = []
        self._job_ids: list[str] = []
        self._monitor_rows: dict[str, int] = {}
        self._closing = False
        self._busy = False

        self.setWindowTitle("Sisp - 系统安装工具")
        self.setMinimumSize(980, 680)
        self.resize(1120, 780)
        self.setStyleSheet(STYLE_SHEET)
        self.setFont(QFont("Microsoft YaHei UI", 9))

        self._build_ui()
        self._show_page(PAGE_CONFIG)

    # ------------------------------------------------------------------ 构建界面
    def _build_ui(self) -> None:
        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)
        self.pages.addWidget(self._build_config_page())
        self.pages.addWidget(self._build_disk_page())
        self.pages.addWidget(self._build_plan_page())
        self.pages.addWidget(self._build_monitor_page())
        self.pages.addWidget(self._build_result_page())
        self.statusBar().showMessage("准备就绪")

    def _new_page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("Title")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("Subtitle")
        subtitle_label.setWordWrap(True)

        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return page, layout

    def _build_config_page(self) -> QWidget:
        page, layout = self._new_page(
            "Sisp 系统安装工具",
            "第一步：检查管理员权限、PowerShell 和配置文件，然后扫描硬盘。",
        )

        config_group = QGroupBox("配置文件")
        config_layout = QVBoxLayout(config_group)
        config_row = QHBoxLayout()
        self.config_path_edit = QLineEdit(str(DEFAULT_CONFIG_PATH))
        self.config_path_edit.setPlaceholderText("选择 JSON 配置文件")
        browse_button = QPushButton("浏览...")
        browse_button.clicked.connect(self._browse_config)
        config_row.addWidget(self.config_path_edit, 1)
        config_row.addWidget(browse_button)
        config_layout.addLayout(config_row)

        self.dry_run_check = QCheckBox("dry-run：只生成执行计划，不执行任何写盘操作")
        config_layout.addWidget(self.dry_run_check)
        layout.addWidget(config_group)

        checks_group = QGroupBox("运行前检查")
        checks_layout = QVBoxLayout(checks_group)
        self.checks_table = QTableWidget(0, 3)
        self.checks_table.setHorizontalHeaderLabels(["检查项", "结果", "说明"])
        self.checks_table.verticalHeader().setVisible(False)
        self.checks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.checks_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.checks_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.checks_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.checks_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        checks_layout.addWidget(self.checks_table)
        layout.addWidget(checks_group, 1)

        action_row = QHBoxLayout()
        self.config_status_label = QLabel("尚未检查")
        self.config_status_label.setObjectName("Hint")
        self.config_status_label.setWordWrap(True)
        self.start_button = QPushButton("开始检查")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self._start_checks)
        action_row.addWidget(self.config_status_label, 1)
        action_row.addWidget(self.start_button)
        layout.addLayout(action_row)

        summary_group = QGroupBox("检查结果")
        summary_layout = QVBoxLayout(summary_group)
        self.session_summary = QLabel("尚未检查。")
        self.session_summary.setWordWrap(True)
        self.session_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        summary_layout.addWidget(self.session_summary)
        self.session_warnings = QLabel("")
        self.session_warnings.setObjectName("Warning")
        self.session_warnings.setWordWrap(True)
        self.session_warnings.hide()
        summary_layout.addWidget(self.session_warnings)
        layout.addWidget(summary_group)

        next_row = QHBoxLayout()
        next_row.addStretch(1)
        self.config_next_button = QPushButton("下一步：选择硬盘")
        self.config_next_button.setEnabled(False)
        self.config_next_button.clicked.connect(self._show_disks)
        next_row.addWidget(self.config_next_button)
        layout.addLayout(next_row)
        return page

    def _build_disk_page(self) -> QWidget:
        page, layout = self._new_page(
            "选择硬盘",
            "第二步：勾选要处理的硬盘。系统盘、启动盘、离线/只读盘和配置排除盘不可选择。",
        )

        self.disk_table = QTableWidget(0, 8)
        self.disk_table.setHorizontalHeaderLabels(
            ["选择", "编号", "状态", "型号", "容量", "接口", "盘符", "保护原因"]
        )
        self.disk_table.verticalHeader().setVisible(False)
        self.disk_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.disk_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.disk_table.setAlternatingRowColors(True)
        self.disk_table.itemChanged.connect(lambda _item: self._update_disk_next_state())
        header = self.disk_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.disk_table, 1)

        self.disk_status_label = QLabel("")
        self.disk_status_label.setObjectName("Hint")
        self.disk_status_label.setWordWrap(True)
        layout.addWidget(self.disk_status_label)

        actions = QHBoxLayout()
        back_button = QPushButton("返回检查")
        back_button.clicked.connect(lambda: self._show_page(PAGE_CONFIG))
        self.refresh_button = QPushButton("刷新硬盘")
        self.refresh_button.clicked.connect(self._refresh_disks)
        self.prepare_button = QPushButton("生成执行计划")
        self.prepare_button.setObjectName("Primary")
        self.prepare_button.setEnabled(False)
        self.prepare_button.clicked.connect(self._prepare_plan)
        actions.addWidget(back_button)
        actions.addStretch(1)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.prepare_button)
        layout.addLayout(actions)
        return page

    def _build_plan_page(self) -> QWidget:
        page, layout = self._new_page(
            "确认执行计划",
            "第三步：逐盘核对硬盘编号、身份、容量、盘符和将执行的步骤。存在阻断项时无法执行。",
        )

        self.plan_tabs = QTabWidget()
        layout.addWidget(self.plan_tabs, 1)

        self.plan_blocker_label = QLabel("")
        self.plan_blocker_label.setObjectName("Blocked")
        self.plan_blocker_label.setWordWrap(True)
        self.plan_blocker_label.hide()
        layout.addWidget(self.plan_blocker_label)

        self.plan_confirm_check = QCheckBox("我已核对以上信息")
        self.plan_confirm_check.stateChanged.connect(
            lambda _state: self._update_plan_confirm_state()
        )
        layout.addWidget(self.plan_confirm_check)

        actions = QHBoxLayout()
        back_button = QPushButton("返回选择")
        back_button.clicked.connect(self._show_disks)
        self.plan_confirm_button = QPushButton("确认并执行")
        self.plan_confirm_button.setObjectName("Danger")
        self.plan_confirm_button.setEnabled(False)
        self.plan_confirm_button.clicked.connect(self._confirm_execution)
        actions.addWidget(back_button)
        actions.addStretch(1)
        actions.addWidget(self.plan_confirm_button)
        layout.addLayout(actions)
        return page

    def _build_monitor_page(self) -> QWidget:
        page, layout = self._new_page(
            "执行监控",
            "实时步骤与日志。取消为协作式：当前步骤完成后才会停止，不会强行中断正在执行的磁盘命令。",
        )

        self.monitor_summary_label = QLabel("尚未开始")
        self.monitor_summary_label.setObjectName("Hint")
        layout.addWidget(self.monitor_summary_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.monitor_table = QTableWidget(0, 5)
        self.monitor_table.setHorizontalHeaderLabels(
            ["硬盘", "型号", "状态", "进度", "当前步骤"]
        )
        self.monitor_table.verticalHeader().setVisible(False)
        self.monitor_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.monitor_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.monitor_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.monitor_table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(6000)
        self.log_view.setPlaceholderText("实时日志将在这里显示")
        splitter.addWidget(self.log_view)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.cancel_selected_button = QPushButton("取消选中任务")
        self.cancel_selected_button.clicked.connect(self._cancel_selected)
        self.cancel_all_button = QPushButton("全部取消")
        self.cancel_all_button.clicked.connect(self._cancel_all)
        self.monitor_results_button = QPushButton("查看结果")
        self.monitor_results_button.setEnabled(False)
        self.monitor_results_button.clicked.connect(lambda: self._show_page(PAGE_RESULTS))
        actions.addWidget(self.cancel_selected_button)
        actions.addWidget(self.cancel_all_button)
        actions.addStretch(1)
        actions.addWidget(self.monitor_results_button)
        layout.addLayout(actions)
        return page

    def _build_result_page(self) -> QWidget:
        page, layout = self._new_page(
            "结果汇总",
            "第五步：查看每块硬盘的最终状态、错误码、步骤明细和日志位置。",
        )

        self.result_summary_label = QLabel("")
        self.result_summary_label.setObjectName("Hint")
        layout.addWidget(self.result_summary_label)

        self.result_table = QTableWidget(0, 6)
        self.result_table.setHorizontalHeaderLabels(
            ["硬盘", "结果", "错误码", "消息", "耗时", "日志文件"]
        )
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.result_table.itemSelectionChanged.connect(self._on_result_selected)
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.result_table, 2)

        self.result_detail = QTextBrowser()
        self.result_detail.setObjectName("PlanView")
        layout.addWidget(self.result_detail, 3)

        actions = QHBoxLayout()
        self.open_log_button = QPushButton("打开选中日志")
        self.open_log_button.setEnabled(False)
        self.open_log_button.clicked.connect(self._open_selected_log)
        rescan_button = QPushButton("重新扫描硬盘")
        rescan_button.clicked.connect(self._rescan_from_results)
        close_button = QPushButton("退出")
        close_button.clicked.connect(self.close)
        actions.addWidget(self.open_log_button)
        actions.addStretch(1)
        actions.addWidget(rescan_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        return page

    # ------------------------------------------------------------------ 页面切换
    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == PAGE_DISKS:
            self._populate_disks()
        elif index == PAGE_RESULTS:
            self._populate_results()

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 JSON 配置文件",
            self.config_path_edit.text().strip() or str(DEFAULT_CONFIG_PATH),
            "JSON 配置文件 (*.json);;所有文件 (*.*)",
        )
        if path:
            self.config_path_edit.setText(path)

    # ------------------------------------------------------------------ 后台任务
    def _run_worker(self, func, on_success, on_error=None) -> None:
        if self._busy:
            return
        self._busy = True
        self.setCursor(Qt.CursorShape.BusyCursor)
        worker = CallWorker(func, self)
        self._workers.add(worker)

        def finish(result) -> None:
            self._workers.discard(worker)
            self._busy = False
            self.unsetCursor()
            on_success(result)

        def fail(message: str) -> None:
            self._workers.discard(worker)
            self._busy = False
            self.unsetCursor()
            if on_error is not None:
                on_error(message)
            else:
                QMessageBox.critical(self, "操作失败", message)

        worker.succeeded.connect(finish)
        worker.failed.connect(fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # ------------------------------------------------------------------ 检查页
    def _start_checks(self) -> None:
        config_path = self.config_path_edit.text().strip() or None
        dry_run = self.dry_run_check.isChecked()
        self.start_button.setEnabled(False)
        self.config_next_button.setEnabled(False)
        self._set_status_style(self.config_status_label, "hint")
        self.config_status_label.setText("正在检查运行环境并扫描硬盘...")
        self._run_worker(
            lambda: self.controller.start(config_path, dry_run=dry_run),
            self._on_session_ready,
            self._on_session_error,
        )

    def _on_session_ready(self, session: SessionState) -> None:
        self.start_button.setEnabled(True)
        self._populate_checks(session)

        selectable = session.selectable_disks
        summary = (
            f"配置文件：{session.config_path or '未确定'}\n"
            f"硬盘：{len(session.disks)} 块，其中可选 {len(selectable)} 块"
        )
        if session.ready:
            self._set_status_style(self.config_status_label, "hint")
            self.config_status_label.setText("运行前检查通过，可以继续。")
            summary += "\n运行前检查：全部通过"
            self.session_summary.setStyleSheet("")
        elif session.can_prepare:
            self._set_status_style(self.config_status_label, "warning")
            self.config_status_label.setText(
                "存在阻断项：可以查看执行计划，但无法确认执行。"
            )
            summary += "\n运行前检查：存在阻断项"
            self.session_summary.setStyleSheet("color: #9a6700;")
        else:
            self._set_status_style(self.config_status_label, "blocked")
            self.config_status_label.setText("配置不可用，无法继续。")
            summary += "\n运行前检查：失败"
            self.session_summary.setStyleSheet("color: #a93226;")

        if session.blockers:
            summary += "\n\n阻断项：\n- " + "\n- ".join(session.blockers)
        self.session_summary.setText(summary)

        if session.warnings:
            self.session_warnings.setText("提示：" + "；".join(session.warnings))
            self.session_warnings.show()
        else:
            self.session_warnings.hide()

        self.config_next_button.setEnabled(session.can_prepare)
        self.statusBar().showMessage("运行前检查完成", 5000)

    def _on_session_error(self, message: str) -> None:
        self.start_button.setEnabled(True)
        self._set_status_style(self.config_status_label, "blocked")
        self.config_status_label.setText("检查失败。")
        self.session_summary.setText(f"运行前检查执行失败：{message}")
        self.session_summary.setStyleSheet("color: #a93226;")
        self.session_warnings.hide()
        QMessageBox.critical(self, "检查失败", message)

    @staticmethod
    def _set_status_style(label: QLabel, state: str) -> None:
        colors = {"hint": "#627d98", "warning": "#9a6700", "blocked": "#a93226"}
        label.setStyleSheet(f"color: {colors.get(state, '#627d98')};")

    def _populate_checks(self, session: SessionState) -> None:
        self.checks_table.setRowCount(len(session.checks))
        for row, check in enumerate(session.checks):
            label_item = QTableWidgetItem(check.label)
            result_item = QTableWidgetItem("通过" if check.passed else "未通过")
            message_item = QTableWidgetItem(check.message)
            color = QColor("#1e7d4f" if check.passed else "#c0392b")
            result_item.setForeground(color)
            self.checks_table.setItem(row, 0, label_item)
            self.checks_table.setItem(row, 1, result_item)
            self.checks_table.setItem(row, 2, message_item)

    def _show_disks(self) -> None:
        session = self.controller.session
        if session is None or not session.can_prepare:
            QMessageBox.information(self, "无法继续", "请先完成运行前检查。")
            return
        self._show_page(PAGE_DISKS)

    # ------------------------------------------------------------------ 硬盘页
    def _populate_disks(self) -> None:
        session = self.controller.session
        disks = list(session.disks) if session is not None else []
        self.disk_table.blockSignals(True)
        self.disk_table.setRowCount(len(disks))
        for row, disk in enumerate(disks):
            check_item = QTableWidgetItem()
            if disk.is_selectable:
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                check_item.setCheckState(Qt.CheckState.Unchecked)
            else:
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
            check_item.setData(Qt.ItemDataRole.UserRole, disk.disk_number)
            self.disk_table.setItem(row, 0, check_item)

            status = "可选" if disk.is_selectable else "受保护"
            reasons = "、".join(disk.protection_reasons) if disk.protection_reasons else "-"
            values = [
                str(disk.disk_number),
                status,
                disk.model or "未知",
                disk.size_display,
                disk.bus_type or "未知",
                ",".join(disk.drive_letters) or "-",
                reasons if not disk.is_selectable else "-",
            ]
            for column, text in enumerate(values, start=1):
                item = QTableWidgetItem(text)
                if not disk.is_selectable:
                    item.setForeground(QColor("#9aa5b1"))
                self.disk_table.setItem(row, column, item)
        self.disk_table.blockSignals(False)
        self.disk_status_label.setText(
            f"共 {len(disks)} 块硬盘，可选 {sum(1 for disk in disks if disk.is_selectable)} 块。"
        )
        self._update_disk_next_state()

    def _collect_selected_disks(self) -> list[int]:
        selected: list[int] = []
        for row in range(self.disk_table.rowCount()):
            item = self.disk_table.item(row, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            number = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(number, int):
                selected.append(number)
        return selected

    def _update_disk_next_state(self) -> None:
        self.prepare_button.setEnabled(
            not self._busy and bool(self._collect_selected_disks())
        )

    def _refresh_disks(self) -> None:
        self.refresh_button.setEnabled(False)
        self.prepare_button.setEnabled(False)
        self.disk_status_label.setText("正在重新扫描硬盘...")
        self._run_worker(
            self.controller.refresh_disks,
            self._on_disks_refreshed,
            self._on_refresh_error,
        )

    def _on_disks_refreshed(self, _disks) -> None:
        self.refresh_button.setEnabled(True)
        self._populate_disks()
        self.statusBar().showMessage("硬盘列表已刷新", 5000)

    def _on_refresh_error(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self._update_disk_next_state()
        self.disk_status_label.setText("刷新失败。")
        QMessageBox.critical(self, "刷新失败", message)

    def _prepare_plan(self) -> None:
        selected = self._collect_selected_disks()
        if not selected:
            QMessageBox.information(self, "请选择硬盘", "至少勾选一块硬盘。")
            return
        self.prepare_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.disk_status_label.setText("正在执行写前校验并生成执行计划...")
        self._run_worker(
            lambda: self.controller.prepare(selected),
            self._on_prepared,
            self._on_prepare_error,
        )

    def _on_prepared(self, prepared: list[PreparedJob]) -> None:
        self.prepare_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self._prepared = list(prepared)
        self._render_plan_tabs()
        self._show_page(PAGE_PLAN)

    def _on_prepare_error(self, message: str) -> None:
        self.prepare_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.disk_status_label.setText("生成执行计划失败。")
        QMessageBox.critical(self, "无法生成执行计划", message)

    # ------------------------------------------------------------------ 计划页
    def _render_plan_tabs(self) -> None:
        self.plan_tabs.clear()
        # 每次生成新计划都必须重新确认，避免上一轮（例如 dry-run）的勾选被带过来。
        self.plan_confirm_check.setChecked(False)
        dry_run = any(item.plan.dry_run for item in self._prepared)
        for item in self._prepared:
            view = QTextBrowser()
            view.setObjectName("PlanView")
            view.setHtml(self._plan_html(item))
            self.plan_tabs.addTab(view, f"硬盘 {item.plan.disk_number}")

        blocked = [item for item in self._prepared if item.plan.blockers]
        if blocked:
            details = "；".join(
                f"硬盘 {item.plan.disk_number}：{'；'.join(item.plan.blockers)}"
                for item in blocked
            )
            self.plan_blocker_label.setText(f"存在阻断项，无法执行：{details}")
            self.plan_blocker_label.show()
            self.plan_confirm_check.setChecked(False)
        else:
            self.plan_blocker_label.hide()

        if dry_run:
            self.plan_confirm_check.setText("我已核对以上执行计划（dry-run 不会写盘）")
            self.plan_confirm_button.setText("开始 dry-run")
        else:
            self.plan_confirm_check.setText(
                "我已核对以上硬盘编号和身份，明白继续将清空这些硬盘的全部数据"
            )
            self.plan_confirm_button.setText("确认并执行")
        self._update_plan_confirm_state()

    def _plan_html(self, item: PreparedJob) -> str:
        plan = item.plan
        identity = plan.identity
        model = html.escape(plan.model or "未知型号")
        badge = (
            '<span style="background:#e8f0fe;color:#1a5fd0;padding:3px 8px;'
            'border-radius:4px;">DRY-RUN</span>'
            if plan.dry_run
            else '<span style="background:#fdecea;color:#a93226;padding:3px 8px;'
            'border-radius:4px;">破坏性操作</span>'
        )
        rows = [
            ("容量", plan.size_display or "未知"),
            ("UniqueId", str(identity.get("unique_id") or "—")),
            ("SerialNumber", str(identity.get("serial_number") or "—")),
            ("配置文件", plan.config_path or "—"),
        ]
        info_rows = "".join(
            f"<tr><td style='color:#627d98;padding:3px 16px 3px 0;'>{html.escape(label)}</td>"
            f"<td style='padding:3px 0;'>{html.escape(value)}</td></tr>"
            for label, value in rows
        )

        letter_names = {
            "efi": "EFI",
            "windows": "Windows",
            "data1": "Data1",
            "data2": "Data2",
        }
        letters = " · ".join(
            f"{letter_names.get(key, key)} = {html.escape(str(value))}"
            for key, value in plan.drive_letters.items()
        ) or "未分配"

        steps = "".join(
            f"<li style='margin:3px 0;'>{html.escape(step)}</li>" for step in plan.steps
        )
        checks = "".join(
            "<li style='margin:3px 0;'>"
            f"<span style='color:{'#1e7d4f' if check.passed else '#c0392b'};'>"
            f"{'✓' if check.passed else '✗'} {html.escape(check.label)}</span>"
            f"<span style='color:#627d98;'> — {html.escape(check.message)}</span>"
            "</li>"
            for check in plan.checks
        )

        sections = [
            f"<h2 style='margin:0 0 6px 0;'>硬盘 {plan.disk_number} · {model}</h2>",
            f"<div style='margin-bottom:10px;'>{badge}</div>",
            f"<table cellspacing='0' cellpadding='0'>{info_rows}"
            f"<tr><td style='color:#627d98;padding:3px 16px 3px 0;'>目标盘符</td>"
            f"<td style='padding:3px 0;'>{letters}</td></tr></table>",
            "<h3 style='margin:16px 0 4px 0;'>将执行的步骤</h3>",
            f"<ol style='margin:4px 0 0 18px;padding:0;'>{steps}</ol>",
            "<h3 style='margin:16px 0 4px 0;'>检查项</h3>",
            f"<ul style='margin:4px 0 0 18px;padding:0;list-style:none;'>{checks}</ul>",
        ]
        if plan.blockers:
            blockers = "".join(
                f"<li>{html.escape(blocker)}</li>" for blocker in plan.blockers
            )
            sections.append(
                "<div style='margin-top:14px;background:#fdecea;border:1px solid #f5b7b1;"
                "border-radius:6px;padding:10px;color:#a93226;'>"
                f"<b>阻断项（无法执行）</b><ul style='margin:6px 0 0 18px;'>{blockers}</ul></div>"
            )
        if plan.warnings:
            warnings = "".join(
                f"<li>{html.escape(warning)}</li>" for warning in plan.warnings
            )
            sections.append(
                "<div style='margin-top:14px;background:#fff8e6;border:1px solid #f0d58c;"
                "border-radius:6px;padding:10px;color:#7a5b00;'>"
                f"<b>请确认</b><ul style='margin:6px 0 0 18px;'>{warnings}</ul></div>"
            )
        return "".join(sections)

    def _update_plan_confirm_state(self) -> None:
        blocked = any(item.plan.blockers for item in self._prepared)
        self.plan_confirm_check.setEnabled(bool(self._prepared) and not blocked)
        self.plan_confirm_button.setEnabled(
            bool(self._prepared) and not blocked and self.plan_confirm_check.isChecked()
        )

    def _confirm_execution(self) -> None:
        if not self._prepared:
            return
        blocked = [item for item in self._prepared if item.plan.blockers]
        if blocked:
            QMessageBox.critical(self, "存在阻断项", "计划包含阻断项，无法执行。")
            return

        dry_run = any(item.plan.dry_run for item in self._prepared)
        disk_text = "、".join(str(item.plan.disk_number) for item in self._prepared)
        if dry_run:
            answer = QMessageBox.question(
                self,
                "开始 dry-run",
                f"将只生成硬盘 {disk_text} 的执行计划，不会执行任何写盘操作。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            answer = QMessageBox.warning(
                self,
                "确认破坏性操作",
                f"即将清空硬盘 {disk_text} 的全部数据，并写入系统镜像。\n\n"
                "请再次确认硬盘编号、型号和身份信息无误。此操作不可撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            jobs = self.controller.submit(self._prepared)
        except WorkflowError as exc:
            QMessageBox.critical(self, "提交失败", str(exc))
            return

        self._job_ids = [job.id for job in jobs]
        self._monitor_rows.clear()
        self.monitor_table.setRowCount(0)
        self.log_view.clear()
        self.monitor_summary_label.setText("正在启动任务...")
        for job in jobs:
            self._ensure_monitor_row(job)
        self._show_page(PAGE_MONITOR)

        try:
            self.controller.confirm()
        except WorkflowError as exc:
            QMessageBox.critical(self, "启动失败", str(exc))
            return
        self.statusBar().showMessage("任务已启动", 5000)

    # ------------------------------------------------------------------ 监控页
    def _ensure_monitor_row(self, job: Job) -> int:
        row = self._monitor_rows.get(job.id)
        if row is not None:
            return row
        row = self.monitor_table.rowCount()
        self.monitor_table.insertRow(row)
        values = [
            f"硬盘 {job.spec.disk_number}",
            job.plan.model or "未知",
            "",
            "",
            "等待启动",
        ]
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, job.id)
            self.monitor_table.setItem(row, column, item)
        self._monitor_rows[job.id] = row
        return row

    def _job_progress(self, job: Job) -> tuple[str, str]:
        if job.spec.dry_run:
            progress = "dry-run"
        else:
            finished = sum(
                1
                for event in job.events
                if event.type == JobEventType.STEP_FINISHED.value
            )
            progress = f"{finished}/7"

        if job.status == JobStatus.SUCCEEDED:
            current = "完成"
        elif job.status == JobStatus.CANCELLED:
            current = "已取消"
        elif job.status == JobStatus.FAILED:
            current = job.error or "失败"
        elif job.cancel_token.cancelled:
            current = "当前步骤完成后停止"
        else:
            current = "等待启动"
            for event in reversed(job.events):
                if event.type == JobEventType.STEP_STARTED.value:
                    current = str(event.payload.get("name") or "执行中")
                    break
                if event.type == JobEventType.STEP_FINISHED.value:
                    current = f"完成：{event.payload.get('name') or ''}"
                    break
        return progress, current

    def _update_monitor_row(self, job: Job) -> None:
        row = self._ensure_monitor_row(job)
        status = "取消中" if job.cancel_token.cancelled and not job.is_terminal else STATUS_LABELS[job.status]
        progress, current = self._job_progress(job)
        status_item = self.monitor_table.item(row, 2)
        progress_item = self.monitor_table.item(row, 3)
        current_item = self.monitor_table.item(row, 4)
        if status_item is None or progress_item is None or current_item is None:
            return
        status_item.setText(status)
        status_item.setForeground(QColor(STATUS_COLORS[job.status]))
        progress_item.setText(progress)
        current_item.setText(current)

    def _on_job_event(self, event: JobEvent) -> None:
        job = self.controller.job_manager.get(event.job_id)
        if job is None:
            return
        self._ensure_monitor_row(job)
        if event.type == JobEventType.LOG.value:
            text = str(event.payload.get("text") or "")
            if text.strip():
                self._append_log(f"[硬盘 {job.spec.disk_number}] {text}")
        self._update_monitor_row(job)
        self._refresh_monitor_summary()

    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _refresh_monitor_summary(self) -> None:
        jobs = [
            job
            for job in (
                self.controller.job_manager.get(job_id) for job_id in self._job_ids
            )
            if job is not None
        ]
        if not jobs:
            self.monitor_summary_label.setText("尚未开始")
            self.monitor_results_button.setEnabled(False)
            return

        waiting = sum(1 for job in jobs if job.status == JobStatus.WAITING_CONFIRMATION)
        running = sum(1 for job in jobs if job.status == JobStatus.RUNNING)
        done = sum(1 for job in jobs if job.is_terminal)
        tests = "dry-run · " if any(job.spec.dry_run for job in jobs) else ""
        self.monitor_summary_label.setText(
            f"{tests}总计 {len(jobs)} 块硬盘 · 等待 {waiting} · 执行 {running} · 完成 {done}"
        )
        all_done = all(job.is_terminal for job in jobs)
        self.monitor_results_button.setEnabled(all_done)
        self.cancel_selected_button.setEnabled(not all_done)
        self.cancel_all_button.setEnabled(not all_done)
        if all_done:
            self.statusBar().showMessage("全部任务已结束", 5000)
            if self._closing:
                self.close()

    # ------------------------------------------------------------------ 取消
    def _selected_monitor_jobs(self) -> list[str]:
        model = self.monitor_table.selectionModel()
        if model is None:
            return []
        job_ids: list[str] = []
        for index in model.selectedRows():
            item = self.monitor_table.item(index.row(), 0)
            job_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if job_id:
                job_ids.append(str(job_id))
        return job_ids

    def _cancel_selected(self) -> None:
        job_ids = self._selected_monitor_jobs()
        if not job_ids:
            QMessageBox.information(self, "未选择任务", "请先在任务列表中选择要取消的硬盘。")
            return
        self._request_cancel(job_ids)

    def _cancel_all(self) -> None:
        job_ids = [
            job.id for job in self.controller.jobs() if not job.is_terminal
        ]
        if not job_ids:
            return
        self._request_cancel(job_ids)

    def _request_cancel(self, job_ids: list[str]) -> None:
        self.controller.cancel(job_ids)
        for job_id in job_ids:
            job = self.controller.job_manager.get(job_id)
            if job is not None:
                self._ensure_monitor_row(job)
                self._update_monitor_row(job)
        self._append_log("已请求取消：运行中的任务会在当前步骤完成后停止。")
        self._refresh_monitor_summary()

    # ------------------------------------------------------------------ 结果页
    def _populate_results(self) -> None:
        jobs = self.controller.jobs()
        self.result_table.setRowCount(len(jobs))
        successes = 0
        failures = 0
        cancelled = 0
        for row, job in enumerate(jobs):
            result = job.result
            success = bool(result and result.success)
            if success:
                successes += 1
            elif result is not None:
                if result.error_code == ErrorCode.CANCELLED:
                    cancelled += 1
                else:
                    failures += 1
            outcome = (
                "成功"
                if success
                else "已取消"
                if result is not None and result.error_code == ErrorCode.CANCELLED
                else "失败"
            )
            if result is None:
                outcome = "未完成"
            values = [
                f"硬盘 {job.spec.disk_number}",
                outcome,
                str(result.error_code) if result is not None else "—",
                result.message if result is not None else "任务尚未结束",
                format_duration(job),
                (result.log_path if result is not None and result.log_path else "—"),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, job.id)
                if column == 1:
                    item.setForeground(
                        QColor(
                            "#1e7d4f"
                            if success
                            else "#7b8794"
                            if outcome == "已取消"
                            else "#c0392b"
                        )
                    )
                self.result_table.setItem(row, column, item)

        summary = f"总计 {len(jobs)} 块硬盘 · 成功 {successes} · 失败 {failures}"
        if cancelled:
            summary += f" · 已取消 {cancelled}"
        self.result_summary_label.setText(summary)
        self.result_detail.clear()
        self.open_log_button.setEnabled(False)

    def _selected_result_job(self) -> Job | None:
        model = self.result_table.selectionModel()
        if model is None:
            return None
        rows = model.selectedRows()
        if not rows:
            return None
        item = self.result_table.item(rows[0].row(), 0)
        job_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not job_id:
            return None
        return self.controller.job_manager.get(str(job_id))

    def _on_result_selected(self) -> None:
        job = self._selected_result_job()
        if job is None:
            self.result_detail.clear()
            self.open_log_button.setEnabled(False)
            return

        self.open_log_button.setEnabled(bool(job.result and job.result.log_path))
        steps = "".join(
            "<tr>"
            f"<td style='padding:4px 10px;'>{html.escape(step.name)}</td>"
            f"<td style='padding:4px 10px;color:{'#1e7d4f' if step.passed else '#c0392b'};'>"
            f"{'通过' if step.passed else '失败'}</td>"
            f"<td style='padding:4px 10px;'>{html.escape(step.message)}</td>"
            f"<td style='padding:4px 10px;'>{step.duration_seconds:.2f} 秒</td>"
            "</tr>"
            for step in (job.result.steps if job.result else ())
        )
        if not steps:
            steps = "<tr><td colspan='4' style='padding:8px;color:#627d98;'>没有步骤记录</td></tr>"

        result = job.result
        status = STATUS_LABELS[job.status]
        message = result.message if result is not None else "任务尚未结束"
        html_text = (
            f"<h2 style='margin:0 0 10px 0;'>硬盘 {job.spec.disk_number} · {html.escape(job.plan.model or '未知型号')}</h2>"
            f"<p><b>状态：</b>{html.escape(status)}<br>"
            f"<b>消息：</b>{html.escape(message)}<br>"
            f"<b>错误码：</b>{result.error_code if result is not None else '—'}<br>"
            f"<b>耗时：</b>{format_duration(job)}<br>"
            f"<b>日志：</b>{html.escape(result.log_path or '—') if result is not None else '—'}</p>"
            "<h3 style='margin:14px 0 6px 0;'>步骤明细</h3>"
            "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;'>"
            "<tr style='background:#eef2f6;'><th style='padding:5px 10px;text-align:left;'>步骤</th>"
            "<th style='padding:5px 10px;text-align:left;'>结果</th>"
            "<th style='padding:5px 10px;text-align:left;'>说明</th>"
            "<th style='padding:5px 10px;text-align:left;'>耗时</th></tr>"
            f"{steps}</table>"
        )
        self.result_detail.setHtml(html_text)

    def _open_selected_log(self) -> None:
        job = self._selected_result_job()
        if job is None or job.result is None or not job.result.log_path:
            QMessageBox.information(self, "没有日志", "当前任务没有可用的日志文件。")
            return
        path = Path(job.result.log_path)
        if not path.exists():
            QMessageBox.warning(self, "日志不存在", f"日志文件不存在：\n{path}")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            QMessageBox.critical(self, "无法打开日志", f"{path}\n\n{exc}")

    def _rescan_from_results(self) -> None:
        session = self.controller.session
        if session is not None and session.can_prepare:
            self._show_page(PAGE_DISKS)
            self._refresh_disks()
        else:
            self._show_page(PAGE_CONFIG)

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 回调命名
        if self._workers:
            QMessageBox.information(
                self, "正在检查", "后台检查仍在进行，请等待完成后再退出。"
            )
            event.ignore()
            return

        active_jobs = [job for job in self.controller.jobs() if not job.is_terminal]
        if active_jobs and not self._closing:
            answer = QMessageBox.question(
                self,
                "任务仍在执行",
                "仍有硬盘任务未结束。是否请求取消，并在当前步骤完成后自动退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing = True
            self._cancel_all()
            self._show_page(PAGE_MONITOR)
            self.monitor_summary_label.setText(
                "正在取消：当前步骤完成后将自动退出，请勿强制结束进程。"
            )
            event.ignore()
            return

        if active_jobs and self._closing:
            event.ignore()
            return

        self._bridge.close()
        event.accept()
