"""Sisp2 命令行入口：``python -m app.cli``。

命令行是 GUI 之外的“批量/自动化”路径，职责与 ``app.gui_web`` 一致：
运行前检查 → 扫描硬盘 → 生成执行计划 → 安全闸门 → 演练或真正执行。

五步固定流程（输出前缀 ``[1/5]`` … ``[5/5]`` 便于宿主脚本抓取）::

    [1/5] 运行前检查与配置加载      WorkflowController.start()
    [2/5] 扫描硬盘                  session.disks
    [3/5] 选择目标盘并生成执行计划  WorkflowController.prepare()
    [4/5] 安全闸门校验              --dry-run / --test-mode / --allow-disks
    [5/5] 执行或演练汇总            submit(require_confirmation=False) + wait_all()

安全约定（本文件强制，不依赖下游实现）
--------------------------------------
* 没有 ``--allow-disks`` 时，真实写盘分支一律拒绝执行（退出码 4）；
* ``--dry-run`` 时**任何情况下**都不提交任务，因此不会执行任何写盘命令；
* ``--test-mode`` 时目标盘必须是虚拟盘，否则拒绝（退出码 4）。

退出码
------
* ``0``  成功（演练成功同样是 0）
* ``1``  内部错误（后端模块缺失、未预期异常、Ctrl+C）
* ``2``  预检失败（配置不可用、无可用硬盘、计划存在阻断项）
* ``3``  执行失败（有任务未成功）
* ``4``  参数或安全策略拒绝（盘号非法、缺少 ``--allow-disks``、测试模式选中物理盘）

标准库 only：``pywebview`` 只在 ``requirements-web.txt`` 里，本文件绝不 import 它。
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from typing import Any, Mapping, NoReturn, Sequence

from app.gui.elevation import is_admin, relaunch_as_admin

# ---------------------------------------------------------------- 常量
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_PREFLIGHT = 2
EXIT_EXECUTION = 3
EXIT_REFUSED = 4
EXIT_INTERRUPTED = 130

STEP_TOTAL = 5

# 测试模式的约定环境变量，下游断言可直接读它
TEST_MODE_ENV = "SISP_TEST_MODE"

# 虚拟盘判断：总线类型或型号里出现下列特征之一
VIRTUAL_BUS_TYPES = frozenset({"file", "virtual", "vhd", "vhdx"})
VIRTUAL_MODEL_HINTS = (
    "virtual disk",
    "virtual hd",
    "vbox",
    "vmware",
    "qemu",
    "vhd",
    "vhdx",
    "hyper-v",
    "msft virtual",
)


class BackendUnavailable(RuntimeError):
    """后端应用层模块无法导入时抛出，提示使用者在完整仓库里运行。"""


# ---------------------------------------------------------------- 输出
class Reporter:
    """统一输出通道。

    * 人读文本：默认写 stdout；``--json`` 时改写 stderr，保证 stdout 只有 JSON；
    * 错误：始终写 stderr，方便宿主脚本与正常输出分开捕获。
    """

    def __init__(self, json_mode: bool = False) -> None:
        self.json_mode = bool(json_mode)
        self._stream = sys.stderr if self.json_mode else sys.stdout

    def _write(self, text: str = "") -> None:
        print(text, file=self._stream, flush=True)

    def step(self, index: int, title: str) -> None:
        """打印 ``[1/5] 标题`` 形式的步骤前缀。"""
        self._write(f"[{index}/{STEP_TOTAL}] {title}")

    def info(self, text: str) -> None:
        self._write(f"    {text}")

    def warn(self, text: str) -> None:
        self._write(f"    提醒：{text}")

    def error(self, text: str) -> None:
        print(f"错误：{text}", file=sys.stderr, flush=True)

    def emit_json(self, payload: dict[str, Any]) -> None:
        """把结果 JSON 打到 stdout；终端编码不支持中文时退回 ASCII 转义。"""
        if not self.json_mode:
            return
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except UnicodeEncodeError:
            sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2, default=str) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- 参数
class _ArgumentParser(argparse.ArgumentParser):
    """参数错误给中文提示，并使用独立退出码 4（2 留给“预检失败”）。"""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"参数错误：{message}", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_REFUSED)


def parse_disk_list(text: str) -> list[int]:
    """把 ``--allow-disks 1,2`` 解析成去重升序的 ``[1, 2]``。

    允许中文逗号与空格分隔；任何非整数、负数、空列表都给清晰的中文报错。
    """
    raw = str(text or "").strip()
    if not raw:
        raise argparse.ArgumentTypeError("盘号列表不能为空，示例：--allow-disks 1,2")

    normalized = raw.replace("，", ",").replace("、", ",").replace(" ", ",")
    numbers: list[int] = []
    for chunk in normalized.split(","):
        token = chunk.strip()
        if not token:
            continue
        if not token.lstrip("+-").isdigit():
            raise argparse.ArgumentTypeError(
                f"盘号必须是整数，收到 “{token}”；正确写法：--allow-disks 1,2"
            )
        number = int(token)
        if number < 0:
            raise argparse.ArgumentTypeError(f"盘号不能为负数，收到 {number}")
        if number not in numbers:
            numbers.append(number)

    if not numbers:
        raise argparse.ArgumentTypeError("盘号列表里没有任何有效数字，示例：--allow-disks 1,2")
    return sorted(numbers)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器（``--help`` 里的中文说明就是这里的内容）。"""
    parser = _ArgumentParser(
        prog="python -m app.cli",
        description="Sisp2 命令行入口：检查环境、扫描硬盘、生成执行计划，然后演练或真正分区写盘。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：\n"
            "  0  成功（演练成功也是 0）\n"
            "  1  内部错误（后端模块缺失、未预期异常）\n"
            "  2  预检失败（配置不可用、无可用硬盘、执行计划存在阻断项）\n"
            "  3  执行失败（有任务未成功）\n"
            "  4  参数或安全策略拒绝（盘号非法、缺少 --allow-disks、测试模式选中物理盘）\n"
            "  130 手动中断（Ctrl+C）\n"
            "\n"
            "安全说明：\n"
            "  真实写盘必须显式提供 --allow-disks；--dry-run 任何情况下都不会写盘。\n"
        ),
    )
    parser.add_argument(
        "-j",
        "--config",
        metavar="<path>",
        default=None,
        help="业务配置文件路径；不传则沿用后端默认值（通常是 json\\win11.json）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：只打印执行计划，不提交任务，绝不执行任何写盘命令",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="测试模式：只允许操作虚拟磁盘（同时设置 SISP_TEST_MODE=1 并传给下游）",
    )
    parser.add_argument("--allow-disks",
        metavar="1,2",
        default=None,
        type=parse_disk_list,
        help="盘号白名单，逗号分隔（例如 1,2）；真实写盘必须提供",
    )
    parser.add_argument(
        "--log-dir",
        metavar="<dir>",
        default=None,
        help="真实执行器命令日志目录；不传则不落盘（GUI 使用设置中的 logDir）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="把结果以 JSON 打到 stdout（人读进度改走 stderr），便于宿主脚本解析",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="只列出硬盘然后退出，不生成计划、不写盘",
    )
    parser.add_argument(
        "--elevate",
        action="store_true",
        help="非管理员且要真实写盘时，先通过 UAC 以管理员身份重启（可选）",
    )
    parser.add_argument(
        "--settings",
        metavar="<path>",
        default=None,
        help=(
            "界面设置文件路径（默认 %%APPDATA%%\\Sisp\\settings.json）；"
            "传入后安全开关、外部工具路径、系统 WIM/ISO 目录等设置对 CLI 也生效"
        ),
    )
    return parser


# ---------------------------------------------------------------- 后端装配
def _workflow_module() -> Any:
    """惰性 import 应用层；缺失时给出可读的中文提示。"""
    try:
        from app.application import workflow as workflow_module
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise BackendUnavailable(
            f"后端模块缺失：{missing}。请在包含完整后端的仓库里运行本命令。"
        ) from exc
    return workflow_module


def _supports_keyword(function: Any, name: str) -> bool:
    """``function`` 的签名是否接受名为 ``name`` 的关键字参数。"""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return True
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def inject_policy_flags(
    controller: Any,
    *,
    test_mode: bool,
    allow_disks: Sequence[int] | None,
) -> None:
    """把 ``test_mode`` / ``allow_disks`` 传给下游（最不侵入的方式）。

    1. 构造参数里已有同名关键字就用关键字传（见 :func:`create_controller`）；
    2. 无论构造是否接受，都再以属性形式挂到 ``WorkflowController`` 和
       ``controller.service`` 上；下游只需
       ``getattr(self, "test_mode", False)`` / ``getattr(self, "allow_disks", None)``
       即可读到，不需要改任何签名。
    """
    values: dict[str, Any] = {
        "test_mode": bool(test_mode),
        "allow_disks": tuple(int(item) for item in allow_disks) if allow_disks else None,
    }
    targets = [controller]
    try:
        service = getattr(controller, "service", None)
    except Exception:  # noqa: BLE001 - 属性访问失败不影响主流程
        service = None
    if service is not None:
        targets.append(service)

    for target in targets:
        for name, value in values.items():
            try:
                setattr(target, name, value)
            except Exception:  # noqa: BLE001 - 下游若用 __slots__，能拿到构造参数就够了
                pass


def load_settings(path: str | None) -> dict[str, Any]:
    """读取界面设置；没传路径或模块缺失时返回空字典（全部用默认值）。"""
    if not path:
        return {}
    try:
        from app.modules.settings.service import SettingsStore
    except ImportError:
        return {}
    try:
        store = SettingsStore(path)
    except Exception:  # noqa: BLE001 - 设置文件坏了不能拖死 CLI
        return {}
    return dict(store.get())


def create_controller(
    *,
    dry_run: bool,
    test_mode: bool,
    allow_disks: Sequence[int] | None,
    log_dir: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> Any:
    """构造 ``WorkflowController``，并把两个新开关尽力传下去。"""
    workflow_module = _workflow_module()
    controller_class = workflow_module.WorkflowController

    settings = dict(settings or {})
    # 界面设置里的日志目录是 ``--log-dir`` 的兜底：用户设过的目录就要用。
    if not log_dir:
        log_dir = str(settings.get("logDir") or "").strip() or None
    kwargs: dict[str, Any] = {"dry_run": bool(dry_run)}
    for name, value in (
        ("test_mode", bool(test_mode)),
        ("allow_disks", list(allow_disks) if allow_disks else None),
        ("protect_system_disk", bool(settings.get("protectSystemDisk", True))),
        ("block_unverified", bool(settings.get("blockUnverified", True))),
    ):
        if _supports_keyword(controller_class.__init__, name):
            kwargs[name] = value
    # 控制器自己装配服务（dry_run / 白名单 / real_runner），
    # 白名单必须走构造参数，否则真实写盘会在安全闸门处被拒绝。
    if allow_disks and _supports_keyword(controller_class.__init__, "allowed_disk_numbers"):
        kwargs["allowed_disk_numbers"] = list(allow_disks)
    if log_dir and _supports_keyword(controller_class.__init__, "log_dir"):
        kwargs["log_dir"] = str(log_dir)
    # 预检、执行器可选项（临时目录/应答文件/驱动目录/引导源）都来自设置。
    if settings and _supports_keyword(controller_class.__init__, "settings_provider"):
        snapshot = dict(settings)
        kwargs["settings_provider"] = lambda: dict(snapshot)

    controller = controller_class(**kwargs)
    inject_policy_flags(controller, test_mode=test_mode, allow_disks=allow_disks)
    # 日志保留天数：只清理本工具自己的日志文件。
    if log_dir:
        try:
            from app.application.real_runner import prune_runner_logs

            prune_runner_logs(log_dir, int(settings.get("logRetentionDays") or 30))
        except Exception:  # noqa: BLE001 - 清理失败不影响主流程
            pass
    return controller


# ---------------------------------------------------------------- 小工具
def _unique(items: Sequence[str]) -> list[str]:
    """去重且保持原顺序。"""
    return list(dict.fromkeys(str(item) for item in items if str(item).strip()))


def is_virtual_disk(disk: Any) -> bool:
    """按总线类型或型号判断是不是虚拟盘。"""
    bus_type = str(getattr(disk, "bus_type", "") or "").strip().lower()
    model = str(getattr(disk, "model", "") or "").strip().lower()
    if bus_type in VIRTUAL_BUS_TYPES:
        return True
    return any(hint in model for hint in VIRTUAL_MODEL_HINTS)


def resolve_targets(
    disks: Sequence[Any],
    allow_disks: Sequence[int] | None,
) -> tuple[list[Any], list[str]]:
    """确定本次要操作的硬盘；返回 ``(目标盘, 问题列表)``。

    有白名单时只允许白名单内的盘，且必须存在、必须可操作（未被保护）；
    没有白名单时退回“全部可选盘”，真实写盘随后会在安全闸门处被拒绝。
    """
    by_number = {int(getattr(disk, "disk_number", -1)): disk for disk in disks}

    if allow_disks:
        missing = [number for number in allow_disks if number not in by_number]
        if missing:
            available = "、".join(str(number) for number in sorted(by_number)) or "无"
            return [], [
                f"--allow-disks 里的硬盘不存在：{'、'.join(str(n) for n in missing)}"
                f"（当前枚举到的盘号：{available}）"
            ]

        protected = [
            number
            for number in allow_disks
            if not bool(getattr(by_number[number], "is_selectable", False))
        ]
        if protected:
            details = "；".join(
                f"Disk {number}（{'、'.join(getattr(by_number[number], 'protection_reasons', ()) or ()) or '受保护'}）"
                for number in protected
            )
            return [], [f"--allow-disks 里的硬盘受保护，拒绝操作：{details}"]

        return [by_number[number] for number in allow_disks], []

    return [disk for disk in disks if bool(getattr(disk, "is_selectable", False))], []


def job_succeeded(job: Any) -> bool:
    """判断任务是否成功；兼容 ``Job.succeeded`` / ``WorkerResult.success`` / 状态名。"""
    flag = getattr(job, "succeeded", None)
    if isinstance(flag, bool):
        return flag

    result = getattr(job, "result", None)
    if result is not None:
        success = getattr(result, "success", None)
        if isinstance(success, bool):
            return success

    status = getattr(job, "status", None)
    name = str(getattr(status, "name", None) or status or "").strip().upper()
    if name in {"SUCCEEDED", "SUCCESS", "DONE", "COMPLETED", "FINISHED"}:
        return True
    if isinstance(status, bool):
        return status
    return False


def _job_dict(job: Any) -> dict[str, Any]:
    """把 Job 转成可序列化字典；没有 ``to_dict`` 时退回最小字段。"""
    to_dict = getattr(job, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {
        "disk_number": getattr(job, "disk_number", None),
        "status": str(getattr(job, "status", "")),
        "succeeded": job_succeeded(job),
    }


def _print_disks(reporter: Reporter, disks: Sequence[Any]) -> None:
    """逐行打印硬盘概要（不用表格，避免中文对齐问题）。"""
    if not disks:
        reporter.warn("未检测到任何硬盘。")
        return

    for disk in disks:
        number = getattr(disk, "disk_number", "?")
        model = getattr(disk, "model", None) or "未知型号"
        size = getattr(disk, "size_display", None) or "未知容量"
        bus_type = getattr(disk, "bus_type", None) or "未知总线"
        style = getattr(disk, "partition_style", None) or "未知分区表"
        letters = getattr(disk, "drive_letters", ()) or ()
        letters_text = "、".join(str(letter) for letter in letters) or "无盘符"

        if bool(getattr(disk, "is_selectable", False)):
            state = "可操作"
        else:
            reasons = "、".join(str(item) for item in (getattr(disk, "protection_reasons", ()) or ()))
            state = f"受保护（{reasons or '保护规则命中'}）"

        reporter.info(
            f"Disk {number}：{model} / {size} / 总线 {bus_type} / {style}"
            f" / 盘符 {letters_text} / {state}"
        )


def _print_plans(reporter: Reporter, prepared: Sequence[Any]) -> None:
    """打印每块盘的计划要点（盘号、盘符、提醒与阻断项）。"""
    for item in prepared:
        plan = getattr(item, "plan", None)
        if plan is None:
            continue
        number = getattr(plan, "disk_number", "?")
        model = getattr(plan, "model", None) or "未知型号"
        size = getattr(plan, "size_display", None) or "未知容量"
        config_path = getattr(plan, "config_path", None) or "（后端默认值）"
        letters = getattr(plan, "drive_letters", None) or {}
        letters_text = "，".join(f"{role}={letter}" for role, letter in letters.items()) or "未分配"

        reporter.info(f"Disk {number} 执行计划：{model} / {size}")
        reporter.info(f"  盘符：{letters_text}")
        reporter.info(f"  配置：{config_path}")
        for warning in getattr(plan, "warnings", ()) or ():
            reporter.info(f"  提醒：{warning}")
        for blocker in getattr(plan, "blockers", ()) or ():
            reporter.info(f"  阻断：{blocker}")


def _print_step_records(reporter: Reporter, result: Any) -> None:
    """打印 ``WorkerResult.steps``；兼容 ``StepResult`` 与旧字典式步骤。"""
    steps = getattr(result, "steps", None)
    if not steps:
        return
    for index, step in enumerate(steps, start=1):
        if isinstance(step, dict):
            title = step.get("title") or step.get("name")
            status = str(step.get("status") or "")
            passed = step.get("passed")
            message = step.get("message") or ""
        else:
            title = getattr(step, "title", None) or getattr(step, "name", None)
            status = str(getattr(step, "status", "") or "")
            passed = getattr(step, "passed", None)
            message = getattr(step, "message", "") or ""

        name = title or f"第 {index} 步"
        if status == "failed" or passed is False:
            mark = "失败"
        elif status == "skipped":
            mark = "跳过"
        else:
            mark = "完成"
        reporter.info(f"  步骤 {index} [{mark}] {name}" + (f"：{message}" if message else ""))


def _result_log_path(result: Any) -> str | None:
    """执行器把日志路径挂在最后一个步骤的 ``details`` 里；兼容旧字段。"""
    steps = getattr(result, "steps", None) or ()
    for step in reversed(steps):
        details = step.get("details") if isinstance(step, dict) else getattr(step, "details", None)
        if isinstance(details, dict) and details.get("log_path"):
            return str(details["log_path"])
    log_path = getattr(result, "log_path", None)
    return str(log_path) if log_path else None


def _print_jobs(reporter: Reporter, jobs: Sequence[Any]) -> None:
    """打印任务结果与步骤明细。"""
    for job in jobs:
        disk_number = getattr(job, "disk_number", "?")
        ok = job_succeeded(job)
        reporter.info(f"Disk {disk_number}：{'成功' if ok else '失败'}")

        result = getattr(job, "result", None)
        if result is not None:
            _print_step_records(reporter, result)
            log_path = _result_log_path(result)
            if log_path:
                reporter.info(f"  日志：{log_path}")
            error = getattr(result, "error", None) or getattr(result, "message", None)
            if error and not ok:
                reporter.info(f"  原因：{error}")


# ---------------------------------------------------------------- 主流程
def _run(
    args: argparse.Namespace,
    raw_argv: Sequence[str],
    reporter: Reporter,
    report: dict[str, Any],
    settings: Mapping[str, Any] | None = None,
) -> int:
    workflow_module = _workflow_module()

    # 测试模式以环境变量形式广播（下游若实现了 _assert_test_mode 会直接生效）
    if args.test_mode:
        os.environ[TEST_MODE_ENV] = "1"

    # --elevate：真实写盘且当前不是管理员时，先走 UAC 重启
    if args.elevate and not args.dry_run and not is_admin():
        if relaunch_as_admin(argv=list(raw_argv), module="app.cli"):
            reporter.info("已请求以管理员身份重新启动（UAC），本进程退出。")
            report["elevated"] = True
            return EXIT_OK
        reporter.warn("提权被取消，继续以普通权限运行；真实写盘会被安全闸门拒绝。")

    # ------------------------------------------------------------ [1/5]
    reporter.step(1, "运行前检查与配置加载")
    controller = create_controller(
        dry_run=bool(args.dry_run),
        test_mode=bool(args.test_mode),
        allow_disks=args.allow_disks,
        log_dir=args.log_dir,
        settings=settings if settings is not None else load_settings(args.settings),
    )
    try:
        session = controller.start(args.config, dry_run=bool(args.dry_run))
    except Exception as exc:  # noqa: BLE001
        reporter.error(f"运行前检查失败：{exc}")
        return EXIT_PREFLIGHT

    report["config_path"] = getattr(session, "config_path", None)
    report["dry_run"] = bool(getattr(session, "dry_run", args.dry_run))
    report["checks"] = [check.to_dict() for check in (getattr(session, "checks", ()) or ())]

    for check in getattr(session, "checks", ()) or ():
        mark = "通过" if bool(getattr(check, "passed", False)) else "失败"
        message = str(getattr(check, "message", "") or "无详细信息")
        reporter.info(f"[{mark}] {getattr(check, 'label', '检查项')}：{message}")

    for warning in getattr(session, "warnings", ()) or ():
        reporter.warn(str(warning))

    # ------------------------------------------------------------ [2/5]
    reporter.step(2, "扫描硬盘")
    disks = list(getattr(session, "disks", ()) or ())
    report["disks"] = [disk.to_dict() for disk in disks]
    _print_disks(reporter, disks)

    scan_blockers = [
        str(blocker)
        for blocker in (getattr(session, "blockers", ()) or ())
        if str(blocker).startswith("硬盘扫描失败")
    ]
    if scan_blockers:
        for blocker in scan_blockers:
            reporter.error(blocker)
        report["blockers"] = scan_blockers
        return EXIT_PREFLIGHT

    # --list 是只读操作：配置不可用也应能列出磁盘，不进入计划阶段。
    if args.list_only:
        if not disks:
            reporter.error("未检测到任何硬盘。")
            return EXIT_PREFLIGHT
        report["targets"] = [disk.disk_number for disk in disks]
        reporter.info("列表模式：已列出硬盘并退出，未生成计划、未写盘。")
        return EXIT_OK

    if getattr(session, "config_payload", None) is None:
        for blocker in dict.fromkeys(str(item) for item in (getattr(session, "blockers", ()) or ())):
            reporter.error(blocker)
        report["blockers"] = list(getattr(session, "blockers", ()) or ())
        return EXIT_PREFLIGHT

    # ------------------------------------------------------------ [3/5]
    reporter.step(3, "选择目标盘并生成执行计划")
    targets, problems = resolve_targets(disks, args.allow_disks)
    report["targets"] = [int(disk.disk_number) for disk in targets]
    if problems:
        for problem in problems:
            reporter.error(problem)
        report["blockers"] = problems
        return EXIT_REFUSED

    if not targets:
        reporter.error("没有可操作的硬盘：全部被保护规则命中。")
        return EXIT_PREFLIGHT

    reporter.info("目标硬盘：" + "、".join(f"Disk {disk.disk_number}" for disk in targets))

    try:
        prepared = list(
            controller.prepare([disk.disk_number for disk in targets], dry_run=bool(args.dry_run))
        )
    except workflow_module.WorkflowError as exc:
        reporter.error(str(exc))
        return EXIT_PREFLIGHT
    except Exception as exc:  # noqa: BLE001
        reporter.error(f"生成执行计划失败：{exc}")
        return EXIT_PREFLIGHT

    if not prepared:
        reporter.error("没有生成任何执行计划。")
        return EXIT_PREFLIGHT

    report["plans"] = [item.plan.to_dict() for item in prepared]
    _print_plans(reporter, prepared)

    blockers: list[str] = []
    warnings: list[str] = []
    for item in prepared:
        plan = item.plan
        blockers.extend(getattr(plan, "blockers", ()) or ())
        warnings.extend(getattr(plan, "warnings", ()) or ())
    blockers = _unique(blockers)
    report["blockers"] = blockers
    report["warnings"] = _unique(list(getattr(session, "warnings", ()) or ()) + warnings)

    # ------------------------------------------------------------ [4/5]
    reporter.step(4, "安全闸门校验")
    if args.test_mode:
        physical = [disk for disk in targets if not is_virtual_disk(disk)]
        if physical:
            for disk in physical:
                reporter.error(
                    f"测试模式拒绝操作物理盘：Disk {getattr(disk, 'disk_number', '?')}"
                    f"（{getattr(disk, 'model', None) or '未知型号'}"
                    f"，总线 {getattr(disk, 'bus_type', None) or '未知'}）"
                )
            reporter.error("测试模式只允许虚拟盘（VHD/VHDX/VMware/VirtualBox/QEMU）。")
            return EXIT_REFUSED
        reporter.info("测试模式：目标盘均为虚拟盘，允许继续。")
    else:
        reporter.info("测试模式：未开启（虚拟盘护栏不生效，请用 --allow-disks 限定范围）。")

    if args.dry_run:
        reporter.info("演练模式：不提交任何任务，不会执行任何写盘命令。")
    else:
        if not args.allow_disks:
            reporter.error("拒绝执行真实写盘：没有提供 --allow-disks 白名单。")
            reporter.error(
                "请显式列出允许操作的盘号，例如：python -m app.cli --allow-disks "
                + ",".join(str(disk.disk_number) for disk in targets)
            )
            return EXIT_REFUSED

        if blockers:
            for blocker in blockers:
                reporter.error(f"计划存在阻断项：{blocker}")
            reporter.error("存在阻断项，拒绝执行真实写盘。")
            return EXIT_PREFLIGHT

        if not is_admin():
            reporter.error("当前进程不是管理员，真实写盘会失败。")
            reporter.error("请用管理员身份运行，或加 --elevate 通过 UAC 重新启动。")
            return EXIT_REFUSED

        reporter.info(
            "真实写盘：目标盘 "
            + "、".join(f"Disk {disk.disk_number}" for disk in targets)
            + "（已提供 --allow-disks 白名单）。"
        )

    # ------------------------------------------------------------ [5/5]
    reporter.step(5, "演练结果汇总" if args.dry_run else "执行")
    if args.dry_run:
        reporter.info("演练完成：未提交任务，未执行任何写盘命令。")
        if blockers:
            reporter.info(
                f"注意：存在 {len(blockers)} 项阻断项，真实执行会被拒绝（退出码 {EXIT_PREFLIGHT}）。"
            )
        report["would_execute"] = False
        return EXIT_OK

    try:
        jobs = list(controller.submit(prepared, require_confirmation=False))
    except workflow_module.WorkflowError as exc:
        reporter.error(f"提交任务失败：{exc}")
        return EXIT_PREFLIGHT
    except Exception as exc:  # noqa: BLE001
        reporter.error(f"提交任务失败：{exc}")
        return EXIT_EXECUTION

    reporter.info(f"已提交 {len(jobs)} 个任务，等待执行完成……")
    try:
        finished = list(controller.wait_all())
    except Exception as exc:  # noqa: BLE001
        reporter.error(f"等待任务完成时出错：{exc}")
        return EXIT_EXECUTION

    report["jobs"] = [_job_dict(job) for job in finished]
    _print_jobs(reporter, finished)

    failed = [job for job in finished if not job_succeeded(job)]
    if failed:
        reporter.error(f"执行失败：{len(failed)}/{len(finished)} 个任务未成功。")
        return EXIT_EXECUTION

    reporter.info("执行成功：所有任务均已完成。")
    return EXIT_OK


def _relax_stream_errors() -> None:
    """把标准输出/错误的编码错误处理放宽为替换，避免罕见字符把入口打崩。

    只改 ``errors`` 不改编码：中文控制台（cp936）下中文仍按本机编码输出，
    重定向到文件时也一样。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码。"""
    _relax_stream_errors()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)

    reporter = Reporter(json_mode=bool(args.json))
    settings = load_settings(args.settings)
    # 报告里的 log_dir 要与实际生效的一致（--log-dir 优先，其次设置里的 logDir）。
    effective_log_dir = str(args.log_dir or "").strip() or (
        str(settings.get("logDir") or "").strip() or None
    )
    report: dict[str, Any] = {
        "ok": False,
        "mode": "list" if args.list_only else ("dry-run" if args.dry_run else "execute"),
        "config_path": args.config,
        "dry_run": bool(args.dry_run),
        "test_mode": bool(args.test_mode),
        "allow_disks": list(args.allow_disks) if args.allow_disks else None,
        "log_dir": effective_log_dir,
        "targets": [],
        "disks": [],
        "checks": [],
        "plans": [],
        "jobs": [],
        "blockers": [],
        "warnings": [],
        "would_execute": bool(not args.dry_run and not args.list_only),
    }

    try:
        code = _run(args, raw_argv, reporter, report, settings)
    except KeyboardInterrupt:
        reporter.error("已中断（Ctrl+C），未完成的任务请到任务日志里确认状态。")
        code = EXIT_INTERRUPTED
    except BackendUnavailable as exc:
        reporter.error(str(exc))
        code = EXIT_INTERNAL
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 入口绝不能抛栈给使用者
        reporter.error(f"未预期错误：{exc}")
        code = EXIT_INTERNAL

    report["exit_code"] = int(code)
    report["ok"] = int(code) == EXIT_OK
    reporter.emit_json(report)
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
