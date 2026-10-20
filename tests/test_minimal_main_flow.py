import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.cli as cli_module
import app.main as main_module
import app.orchestrator as orchestrator
import app.worker_launcher as worker_launcher
from app.drive_letters import validate_drive_letters_are_free
from app.main import (
    build_drive_letter_allocations,
    build_forbidden_drive_letters,
    parse_command_line_args,
    parse_worker_drive_letters,
    run_minimal_main_flow,
    validate_required_paths,
)
from app.modules.common.service import (
    ConfigError,
    GhostError,
    IdentityMismatchError,
    InitializationError,
    PartitionError,
    PreflightError,
    ValidationError,
)

SAMPLE_DISK_SUMMARIES = [
    {
        "disk_number": 0,
        "model": "System Disk",
        "serial_number": "SN-SYSTEM",
        "unique_id": "UID-SYSTEM",
        "size_bytes": 1000204886016,
        "size_display": "931.51 GB",
        "partition_style": "GPT",
        "bus_type": "NVMe",
        "drive_letters": ["C"],
        "is_boot": True,
        "is_system": True,
        "is_offline": False,
        "is_read_only": False,
    },
    {
        "disk_number": 1,
        "model": "Target Disk",
        "serial_number": "SN-TARGET-1",
        "unique_id": "UID-TARGET-1",
        "size_bytes": 512040894464,
        "size_display": "476.94 GB",
        "partition_style": "GPT",
        "bus_type": "SATA",
        "drive_letters": [],
        "is_boot": False,
        "is_system": False,
        "is_offline": False,
        "is_read_only": False,
    },
    {
        "disk_number": 2,
        "model": "Second Target Disk",
        "serial_number": "SN-TARGET-2",
        "unique_id": "UID-TARGET-2",
        "size_bytes": 14398909440,
        "size_display": "13.41 GB",
        "partition_style": "GPT",
        "bus_type": "USB",
        "drive_letters": [],
        "is_boot": False,
        "is_system": False,
        "is_offline": False,
        "is_read_only": False,
    },
]



def build_successful_preflight_report(config_path: str | Path | None = None) -> dict:
    resolved_path = str(config_path) if config_path else "D:\\Code-Project\\Sisp2\\json\\win11.json"
    return {
        "all_passed": True,
        "results": [
            {"name": "管理员权限检查", "passed": True, "message": "当前程序已以管理员权限运行"},
            {"name": "PowerShell 可用性检查", "passed": True, "message": "PowerShell 可正常调用"},
            {"name": "配置文件存在性检查", "passed": True, "message": "配置文件存在"},
            {"name": "配置文件可解析性检查", "passed": True, "message": "配置文件可正常解析"},
        ],
        "config_payload": {
            "config_path": resolved_path,
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 300},
            "image_info": {"image_path": str(PROJECT_ROOT / "README.md")},
            "software_paths": {"ghost64_path": str(PROJECT_ROOT / "sw" / "ghost64.exe"), "bcdboot_path": str(PROJECT_ROOT / "sw" / "bcdboot.exe")},
            "copy_info": {"source_dir": str(PROJECT_ROOT)},
            "excluded_disk_names": [],
        },
    }



def build_failed_preflight_report(config_path: str | Path | None = None) -> dict:
    return {
        "all_passed": False,
        "results": [
            {"name": "管理员权限检查", "passed": False, "message": "当前程序未以管理员权限运行，请重新以管理员身份启动"},
        ],
        "config_payload": None,
    }



def validate_selected_disk_numbers(selected_disk_numbers: list[int]) -> None:
    if not isinstance(selected_disk_numbers, list) or not selected_disk_numbers:
        raise AssertionError("selected_disk_numbers 必须为非空列表")

    for number in selected_disk_numbers:
        if not isinstance(number, int):
            raise AssertionError("selected_disk_numbers 中的每一项都必须为整数")



def test_build_drive_letter_allocations() -> None:
    single = build_drive_letter_allocations([2])
    if single != {2: {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}}:
        raise AssertionError(f"单盘盘符分配不正确: {single}")

    multi = build_drive_letter_allocations([2, 3])
    if multi != {
        2: {"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
        3: {"efi": "I", "windows": "J", "data1": "K", "data2": "L"},
    }:
        raise AssertionError(f"多盘盘符分配不正确: {multi}")

    triple = build_drive_letter_allocations([2, 3, 4])
    if triple[4] != {"efi": "M", "windows": "N", "data1": "O", "data2": "P"}:
        raise AssertionError(f"三盘第三块盘符分配不正确: {triple}")



def test_build_drive_letter_allocations_overflow() -> None:
    many_disks = list(range(2, 26))
    try:
        build_drive_letter_allocations(many_disks)
    except RuntimeError as exc:
        if "可用盘符不足" not in str(exc):
            raise AssertionError(f"盘符不足错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("盘符不足时应报错")



def test_parse_worker_drive_letters() -> None:
    result = parse_worker_drive_letters("E,F,G,H")
    if result != {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}:
        raise AssertionError(f"盘符解析不正确: {result}")

    try:
        parse_worker_drive_letters("E,F,G")
    except ValueError as exc:
        if "4 个字母" not in str(exc):
            raise AssertionError(f"盘符数量错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("3 个盘符时应报错")

    try:
        parse_worker_drive_letters("E1,F,G,H")
    except ValueError as exc:
        if "单个字母" not in str(exc):
            raise AssertionError(f"盘符格式错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("非字母盘符时应报错")

    for text, expected_message in [
        ("E,E,G,H", "不能重复"),
        ("A,F,G,H", "保留盘符"),
        ("C,F,G,H", "保留盘符"),
    ]:
        try:
            parse_worker_drive_letters(text)
        except ValueError as exc:
            if expected_message not in str(exc):
                raise AssertionError(f"盘符错误信息不正确: {exc}") from exc
        else:
            raise AssertionError(f"危险盘符参数应报错: {text}")

    try:
        parse_worker_drive_letters("E,F,G,H", used_letters=["F"])
    except ValueError as exc:
        if "已被占用" not in str(exc):
            raise AssertionError(f"已占用盘符错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("worker 不能使用其他卷已占用的盘符")


def test_validate_drive_letters_are_free_rejects_used_letters() -> None:
    letters = {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}
    validate_drive_letters_are_free(letters, used_letters=["C", "D"])

    try:
        validate_drive_letters_are_free(letters, used_letters=["F"])
    except ValueError as exc:
        if "已被占用" not in str(exc):
            raise AssertionError(f"盘符占用错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("已占用盘符应被拒绝")

    try:
        validate_drive_letters_are_free({"efi": "C", "windows": "F", "data1": "G", "data2": "H"})
    except ValueError as exc:
        if "保留盘符" not in str(exc):
            raise AssertionError(f"保留盘符错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("保留盘符应被拒绝")


def test_main_flow_rejects_resource_on_target_before_initialize(monkeypatch) -> None:
    initialized: list[list[int]] = []

    def fail_destructive(*args, **kwargs):
        raise AssertionError("资源冲突检查未在破坏性操作前中止")

    monkeypatch.setattr(orchestrator, "scan_disk_summaries", lambda: SAMPLE_DISK_SUMMARIES)
    monkeypatch.setattr(orchestrator, "initialize_disks", lambda disk_numbers, **kwargs: initialized.append(disk_numbers) or [])
    monkeypatch.setattr(orchestrator, "partition_and_format_disks", fail_destructive)
    monkeypatch.setattr(orchestrator, "write_ghost_image", fail_destructive)
    monkeypatch.setattr(orchestrator, "copy_directory", fail_destructive)
    monkeypatch.setattr(orchestrator, "create_boot_record", fail_destructive)
    monkeypatch.setattr(
        orchestrator,
        "resolve_path_disk_number",
        lambda path: 1 if str(path).endswith("README.md") else 0,
    )

    try:
        run_minimal_main_flow(
            input_func=lambda prompt: "1",
            preflight_runner=build_successful_preflight_report,
            config_path="D:\\Code-Project\\Sisp2\\json\\win11.json",
        )
    except ConfigError as exc:
        if "位于目标硬盘上" not in str(exc):
            raise AssertionError(f"资源冲突错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("Ghost 镜像位于目标盘时应拒绝执行")

    if initialized:
        raise AssertionError("资源冲突检查必须在初始化之前中止流程")



def test_parse_command_line_args() -> None:
    args = parse_command_line_args(["-j", "D:\\temp\\demo.json"])
    if args.config_path != "D:\\temp\\demo.json":
        raise AssertionError(f"-j 参数解析结果不正确: {args.config_path}")

    default_args = parse_command_line_args([])
    if default_args.config_path is not None:
        raise AssertionError("未传入 -j 参数时，config_path 应为 None")

    worker_args = parse_command_line_args([
        "--worker-disk",
        "2",
        "--worker-unique-id",
        "UID-TARGET-2",
        "--worker-serial-number",
        "SN-TARGET-2",
        "--worker-model",
        "Second Target Disk",
        "--worker-size-bytes",
        "14398909440",
        "--worker-drive-letters",
        "E,F,G,H",
        "-j",
        "D:\\temp\\demo.json",
    ])
    if worker_args.worker_disk != 2:
        raise AssertionError(f"--worker-disk 参数解析结果不正确: {worker_args.worker_disk}")
    if worker_args.worker_unique_id != "UID-TARGET-2":
        raise AssertionError(f"worker UniqueId 参数解析结果不正确: {worker_args.worker_unique_id}")
    if worker_args.worker_serial_number != "SN-TARGET-2":
        raise AssertionError(f"worker SerialNumber 参数解析结果不正确: {worker_args.worker_serial_number}")
    if worker_args.worker_model != "Second Target Disk":
        raise AssertionError(f"worker 型号参数解析结果不正确: {worker_args.worker_model}")
    if worker_args.worker_size_bytes != 14398909440:
        raise AssertionError(f"worker 容量参数解析结果不正确: {worker_args.worker_size_bytes}")
    if worker_args.worker_drive_letters != "E,F,G,H":
        raise AssertionError(f"worker 盘符参数解析结果不正确: {worker_args.worker_drive_letters}")
    if worker_args.config_path != "D:\\temp\\demo.json":
        raise AssertionError(f"worker 模式 -j 参数解析结果不正确: {worker_args.config_path}")



def test_validate_required_paths_rejects_missing_file() -> None:
    payload = build_successful_preflight_report()["config_payload"]
    payload["image_info"] = {"image_path": "D:\\missing\\missing.gho"}
    try:
        validate_required_paths(payload)
    except ConfigError as exc:
        if "不存在" not in str(exc):
            raise AssertionError(f"路径缺失错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("关键路径不存在时应拒绝继续执行")



def test_successful_main_flow() -> None:
    target_number = 1
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    original_partition_and_format_disks = orchestrator.partition_and_format_disks
    original_validate_partitioned_disks = orchestrator.validate_partitioned_disks
    original_write_ghost_image = orchestrator.write_ghost_image
    original_copy_directory = orchestrator.copy_directory
    original_create_boot_record = orchestrator.create_boot_record
    original_validate_drive_letter_ownership = orchestrator.validate_drive_letter_ownership
    ownership_calls: list[tuple[int, tuple[str, ...]]] = []
    orchestrator.validate_drive_letter_ownership = lambda disk_number, letters: ownership_calls.append(
        (disk_number, tuple(letters))
    )
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {
            "disk_number": number,
            "passed": True,
            "message": "硬盘初始化完成",
            "disk": {
                "disk_number": number,
                "partition_style": "GPT",
                "is_boot": False,
                "is_system": False,
                "is_offline": False,
                "is_read_only": False,
            },
        }
        for number in disk_numbers
    ]
    orchestrator.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None, **kwargs: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F", "d1_drive_letter": "G", "efi_drive_letter": "E"}}
        for number in disk_numbers
    ]
    orchestrator.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    orchestrator.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": True, "message": "Ghost 镜像写入成功"
    }
    orchestrator.copy_directory = lambda source_dir, data1_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "目录拷贝成功"
    }
    orchestrator.create_boot_record = lambda bcd_exe, windows_drive_letter, efi_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "引导记录创建成功"
    }

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            selected_disk_numbers = run_minimal_main_flow(
                input_func=lambda prompt: str(target_number),
                preflight_runner=build_successful_preflight_report,
            )
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks
        orchestrator.partition_and_format_disks = original_partition_and_format_disks
        orchestrator.validate_partitioned_disks = original_validate_partitioned_disks
        orchestrator.write_ghost_image = original_write_ghost_image
        orchestrator.copy_directory = original_copy_directory
        orchestrator.create_boot_record = original_create_boot_record
        orchestrator.validate_drive_letter_ownership = original_validate_drive_letter_ownership

    output = captured.getvalue()
    if "管理员权限检查" in output or "PowerShell 可用性检查" in output or "配置文件存在性检查" in output or "配置文件可解析性检查" in output:
        raise AssertionError("运行前检查全部通过时不应输出检查通过信息")
    if "Sisp" not in output:
        raise AssertionError("主流程成功时未继续进入正式入口")
    if "当前主程序持有的关键数据:" in output:
        raise AssertionError("用户完成硬盘选择后不应继续输出演示性关键数据")
    if "演示运行完成" in output or "演示运行结束" in output:
        raise AssertionError("用户完成硬盘选择后不应继续输出演示性收尾信息")
    if "硬盘 1 初始化通过" not in output:
        raise AssertionError("主流程选择硬盘后未进入初始化模块")
    if "目标硬盘信息:" not in output or "硬盘型号: Target Disk" not in output or "硬盘容量: 476.94 GB" not in output:
        raise AssertionError("主流程执行前未输出完整目标硬盘信息")
    if "硬盘 1 初始化验证通过" not in output:
        raise AssertionError("主流程初始化后未进入验证模块")
    if "硬盘 1 分区格式化通过" not in output:
        raise AssertionError("主流程初始化验证后未进入分区格式化模块")
    if "硬盘 1 分区格式化验证通过" not in output:
        raise AssertionError("主流程分区格式化后未进入分区验证模块")
    if "硬盘 1 目录拷贝通过" not in output:
        raise AssertionError("主流程分区验证后未进入目录拷贝模块")
    if "硬盘 1 引导创建通过" not in output:
        raise AssertionError("主流程目录拷贝后未进入引导创建模块")

    validate_selected_disk_numbers(selected_disk_numbers)
    if selected_disk_numbers != [target_number]:
        raise AssertionError(f"主流程选择结果不正确: {selected_disk_numbers}，期望: {[target_number]}")

    if not any(set(letters) == {"E", "F", "G", "H"} for _, letters in ownership_calls):
        raise AssertionError(f"Ghost 写入前未校验全部分区盘符归属: {ownership_calls}")
    if not any(letters == ("G",) for _, letters in ownership_calls):
        raise AssertionError(f"目录拷贝前未校验 Data1 盘符归属: {ownership_calls}")
    if not any(set(letters) == {"E", "F"} for _, letters in ownership_calls):
        raise AssertionError(f"bcdboot 前未校验 Windows/EFI 盘符归属: {ownership_calls}")

    print(output)
    print(f"最小主流程成功路径测试结果: 通过，已选择硬盘编号 {selected_disk_numbers}")



def test_successful_main_flow_with_custom_json_path() -> None:
    custom_path = "D:\\custom\\demo.json"
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    original_partition_and_format_disks = orchestrator.partition_and_format_disks
    original_validate_partitioned_disks = orchestrator.validate_partitioned_disks
    original_write_ghost_image = orchestrator.write_ghost_image
    original_copy_directory = orchestrator.copy_directory
    original_create_boot_record = orchestrator.create_boot_record
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {
            "disk_number": number,
            "passed": True,
            "message": "硬盘初始化完成",
            "disk": {
                "disk_number": number,
                "partition_style": "GPT",
                "is_boot": False,
                "is_system": False,
                "is_offline": False,
                "is_read_only": False,
            },
        }
        for number in disk_numbers
    ]
    orchestrator.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None, **kwargs: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F", "d1_drive_letter": "G", "efi_drive_letter": "E"}}
        for number in disk_numbers
    ]
    orchestrator.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    orchestrator.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": True, "message": "Ghost 镜像写入成功"
    }
    orchestrator.copy_directory = lambda source_dir, data1_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "目录拷贝成功"
    }
    orchestrator.create_boot_record = lambda bcd_exe, windows_drive_letter, efi_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "引导记录创建成功"
    }

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            selected_disk_numbers = run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
                config_path=custom_path,
            )
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks
        orchestrator.partition_and_format_disks = original_partition_and_format_disks
        orchestrator.validate_partitioned_disks = original_validate_partitioned_disks
        orchestrator.write_ghost_image = original_write_ghost_image
        orchestrator.copy_directory = original_copy_directory
        orchestrator.create_boot_record = original_create_boot_record

    output = captured.getvalue()
    if f"配置文件: {custom_path}" not in output:
        raise AssertionError("主流程未使用 -j 传入的自定义 JSON 路径")
    validate_selected_disk_numbers(selected_disk_numbers)



def test_multi_disk_main_flow_launches_worker_windows() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_run_single_disk_flow = orchestrator.run_single_disk_flow
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []

    launched: list[tuple[list[dict], str | None]] = []
    current_process_calls: list[int] = []

    def fake_launcher(disk_identities: list[dict], config_path: str | None) -> None:
        launched.append((disk_identities, config_path))

    orchestrator.run_single_disk_flow = lambda disk_number, config_payload, expected_identity=None, drive_letters=None, run_context=None, dry_run=False: current_process_calls.append(disk_number)

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            selected_disk_numbers = run_minimal_main_flow(
                input_func=lambda prompt: "1,2",
                preflight_runner=build_successful_preflight_report,
                worker_launcher=fake_launcher,
            )
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.run_single_disk_flow = original_run_single_disk_flow

    if selected_disk_numbers != [1, 2]:
        raise AssertionError(f"多硬盘选择结果不正确: {selected_disk_numbers}")
    expected_identities = [
        {
            "disk_number": 1,
            "unique_id": "UID-TARGET-1",
            "serial_number": "SN-TARGET-1",
            "model": "Target Disk",
            "size_bytes": 512040894464,
            "drive_letters": {"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
        },
        {
            "disk_number": 2,
            "unique_id": "UID-TARGET-2",
            "serial_number": "SN-TARGET-2",
            "model": "Second Target Disk",
            "size_bytes": 14398909440,
            "drive_letters": {"efi": "I", "windows": "J", "data1": "K", "data2": "L"},
        },
    ]
    if launched != [(expected_identities, "D:\\Code-Project\\Sisp2\\json\\win11.json")]:
        raise AssertionError(f"多硬盘未正确启动 worker: {launched}")
    if current_process_calls:
        raise AssertionError(f"多硬盘时不应在当前进程执行单盘流程: {current_process_calls}")

    output = captured.getvalue()
    if "已选择多个硬盘" not in output:
        raise AssertionError("多硬盘选择时未输出启动 worker 提示")



def test_worker_flow_runs_single_disk_flow() -> None:
    received: list[tuple[int, dict, dict | None]] = []
    original_run_single_disk_flow = orchestrator.run_single_disk_flow
    orchestrator.run_single_disk_flow = lambda disk_number, config_payload, expected_identity=None, drive_letters=None, run_context=None, dry_run=False: received.append((disk_number, config_payload, expected_identity))

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            cli_module.run_worker_flow(
                2,
                preflight_runner=build_successful_preflight_report,
                config_path="D:\\temp\\demo.json",
            )
    finally:
        orchestrator.run_single_disk_flow = original_run_single_disk_flow

    if len(received) != 1 or received[0][0] != 2:
        raise AssertionError(f"worker 模式未执行指定硬盘: {received}")
    if received[0][1].get("config_path") != "D:\\temp\\demo.json":
        raise AssertionError(f"worker 模式未使用指定配置路径: {received}")
    if received[0][2] is not None:
        raise AssertionError(f"未传入身份参数时 worker 不应携带身份校验数据: {received}")

    output = captured.getvalue()
    if "Sisp Worker 硬盘 2" not in output:
        raise AssertionError("worker 模式未输出 worker 标题")



def test_validate_worker_disk_identity() -> None:
    disk = SAMPLE_DISK_SUMMARIES[2]
    expected_identity = {
        "unique_id": "UID-TARGET-2",
        "serial_number": "SN-TARGET-2",
        "model": "Second Target Disk",
        "size_bytes": 14398909440,
    }
    orchestrator.validate_worker_disk_identity(disk, expected_identity)

    try:
        orchestrator.validate_worker_disk_identity(disk, {**expected_identity, "unique_id": "WRONG-UID"})
    except IdentityMismatchError as exc:
        if "UniqueId 不一致" not in str(exc):
            raise AssertionError(f"硬盘身份不一致错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("UniqueId 不一致时应拒绝执行")

    try:
        orchestrator.validate_worker_disk_identity(disk, {**expected_identity, "size_bytes": 1})
    except IdentityMismatchError as exc:
        if "硬盘容量不一致" not in str(exc):
            raise AssertionError(f"硬盘容量不一致错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("硬盘容量不一致时应拒绝执行")



def test_main_flow_rejects_insufficient_disk_capacity_before_initialize() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    initialized: list[list[int]] = []

    def oversized_preflight_report(config_path: str | Path | None = None) -> dict:
        report = build_successful_preflight_report(config_path)
        report["config_payload"]["partition_info"] = {"efi_size_mb": 100, "c_size_gb": 2048}
        return report

    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: initialized.append(disk_numbers) or []

    try:
        with redirect_stdout(io.StringIO()):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=oversized_preflight_report,
            )
    except ConfigError as exc:
        if "容量不足" not in str(exc):
            raise AssertionError(f"容量不足错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("目标盘容量不足时应在初始化前中止")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks

    if initialized:
        raise AssertionError(f"容量不足时不应执行初始化: {initialized}")



def test_single_disk_main_flow_passes_expected_identity() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_run_single_disk_flow = orchestrator.run_single_disk_flow
    received: list[tuple[int, dict | None, dict | None]] = []

    def fake_run_single_disk_flow(disk_number, config_payload, expected_identity=None, drive_letters=None, run_context=None, dry_run=False):
        received.append((disk_number, expected_identity, drive_letters))

    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.run_single_disk_flow = fake_run_single_disk_flow

    try:
        with redirect_stdout(io.StringIO()):
            selected = run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.run_single_disk_flow = original_run_single_disk_flow

    if selected != [1]:
        raise AssertionError(f"单盘选择结果不正确: {selected}")
    expected_identity = {
        "disk_number": 1,
        "unique_id": "UID-TARGET-1",
        "serial_number": "SN-TARGET-1",
        "model": "Target Disk",
        "size_bytes": 512040894464,
    }
    if received != [(1, expected_identity, {"efi": "E", "windows": "F", "data1": "G", "data2": "H"})]:
        raise AssertionError(f"单盘流程未传入身份校验数据: {received}")



def test_launch_worker_windows_uses_direct_python() -> None:
    launched: list[dict] = []
    sleep_calls: list[float] = []
    original_popen = worker_launcher.subprocess.Popen
    result_dir = Path(tempfile.mkdtemp(prefix="sisp-worker-test-"))

    def fake_popen(args, cwd=None, creationflags=0, env=None):
        launched.append({"args": args, "cwd": cwd, "creationflags": creationflags, "env": env})

        class FakeProcess:
            pass

        return FakeProcess()

    worker_launcher.subprocess.Popen = fake_popen
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            workers = worker_launcher.launch_worker_windows(
                [
                    {"disk_number": 2, "unique_id": "UID-2", "serial_number": "SN-2", "model": "Disk 2", "size_bytes": 2000, "drive_letters": {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}},
                    {"disk_number": 3, "unique_id": "UID-3", "serial_number": "SN-3", "model": "Disk 3", "size_bytes": 3000, "drive_letters": {"efi": "I", "windows": "J", "data1": "K", "data2": "L"}},
                ],
                "D:\\json\\custom.json",
                sleep_func=sleep_calls.append,
                result_dir=result_dir,
                log_dir=result_dir / "logs",
                no_log=True,
            )
    finally:
        worker_launcher.subprocess.Popen = original_popen

    if len(launched) != 2 or len(workers) != 2:
        raise AssertionError(f"应为每块硬盘启动一个 worker，实际为: {launched}")
    first = launched[0]["args"]
    if first[0] != sys.executable:
        raise AssertionError(f"worker 应直接使用当前 Python 解释器启动: {first}")
    if "powershell" in Path(first[0]).name.lower() or "-NoExit" in first or "-NoProfile" in first:
        raise AssertionError(f"worker 不应再通过 PowerShell 包裹: {first}")
    if "--worker-disk" not in first or "2" not in first:
        raise AssertionError(f"worker 命令缺少 --worker-disk: {first}")
    if "--worker-result-file" not in first:
        raise AssertionError(f"worker 命令缺少 --worker-result-file: {first}")
    if "--log-dir" not in first or "--no-log" not in first:
        raise AssertionError(f"worker 命令未继承主窗口日志配置: {first}")
    if "D:\\json\\custom.json" not in first:
        raise AssertionError(f"worker 命令未传递配置路径: {first}")
    if "--worker-unique-id" not in first or "UID-2" not in first:
        raise AssertionError(f"worker 命令未传递 UniqueId: {first}")
    if "--worker-serial-number" not in first or "SN-2" not in first:
        raise AssertionError(f"worker 命令未传递 SerialNumber: {first}")
    if "--worker-model" not in first or "Disk 2" not in first:
        raise AssertionError(f"worker 命令未传递硬盘型号: {first}")
    if "--worker-size-bytes" not in first or "2000" not in first:
        raise AssertionError(f"worker 命令未传递硬盘容量: {first}")
    if "--worker-drive-letters" not in first or "E,F,G,H" not in first:
        raise AssertionError(f"worker 命令未传递盘符: {first}")
    if launched[0]["env"].get("PYTHONUTF8") != "1":
        raise AssertionError(f"worker 环境变量缺少 PYTHONUTF8: {launched[0]['env']}")
    if launched[0]["env"].get("PYTHONIOENCODING") != "utf-8":
        raise AssertionError(f"worker 环境变量缺少 PYTHONIOENCODING: {launched[0]['env']}")
    if launched[0]["creationflags"] != getattr(worker_launcher.subprocess, "CREATE_NEW_CONSOLE", 0):
        raise AssertionError(f"worker 未使用独立控制台窗口: {launched[0]['creationflags']}")
    if workers[0].result_file.name != "disk_2.json" or workers[1].result_file.name != "disk_3.json":
        raise AssertionError(f"worker 结果文件命名不正确: {[str(w.result_file) for w in workers]}")
    if sleep_calls != [6.0]:
        raise AssertionError(f"worker 启动间隔不正确: {sleep_calls}")

    output = captured.getvalue()
    if "硬盘 2: 已启动独立执行窗口" not in output or "硬盘 3: 已启动独立执行窗口" not in output:
        raise AssertionError("启动 worker 时未输出提示")
    if "等待 6 秒后启动下一个 worker" not in output:
        raise AssertionError("启动 worker 时未输出等待提示")



def test_main_uses_worker_disk_argument() -> None:
    received: list[tuple[int, str | Path | None, dict, dict | None]] = []
    original_runner = cli_module.run_worker_flow

    def fake_run_worker_flow(disk_number: int, preflight_runner=None, config_path: str | Path | None = None, expected_identity=None, drive_letters=None, result_file=None, dry_run=False) -> None:
        received.append((disk_number, config_path, expected_identity, drive_letters))

    cli_module.run_worker_flow = fake_run_worker_flow
    try:
        exit_code = main_module.main([
            "--worker-disk",
            "2",
            "--worker-unique-id",
            "UID-TARGET-2",
            "--worker-serial-number",
            "SN-TARGET-2",
            "--worker-model",
            "Second Target Disk",
            "--worker-size-bytes",
            "14398909440",
            "--worker-drive-letters",
            "E,F,G,H",
            "-j",
            "D:\\json\\custom.json",
        ])
    finally:
        cli_module.run_worker_flow = original_runner

    if exit_code != 0:
        raise AssertionError(f"main worker 模式应返回 0，实际为: {exit_code}")
    if received != [(
        2,
        "D:\\json\\custom.json",
        {
            "unique_id": "UID-TARGET-2",
            "serial_number": "SN-TARGET-2",
            "model": "Second Target Disk",
            "size_bytes": 14398909440,
        },
        {"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
    )]:
        raise AssertionError(f"main 未正确调用 worker 模式: {received}")



def test_main_uses_json_argument() -> None:
    received_config_paths: list[str | Path | None] = []
    original_runner = cli_module.run_minimal_main_flow

    def fake_run_minimal_main_flow(input_func=input, preflight_runner=None, config_path: str | Path | None = None, **kwargs) -> list[int]:
        received_config_paths.append(config_path)
        return [1]

    cli_module.run_minimal_main_flow = fake_run_minimal_main_flow
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = main_module.main(["-j", "D:\\json\\custom.json"])
    finally:
        cli_module.run_minimal_main_flow = original_runner

    output = captured.getvalue()
    if exit_code != 0:
        raise AssertionError(f"main 使用 -j 参数时应返回 0，实际为: {exit_code}")
    if received_config_paths != ["D:\\json\\custom.json"]:
        raise AssertionError(f"main 未将 -j 参数传递给主流程: {received_config_paths}")
    if "演示运行完成" in output or "演示运行结束" in output:
        raise AssertionError("main 不应在第一阶段结束后输出演示性收尾信息")



def test_main_flow_stops_when_initialization_fails() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {"disk_number": number, "passed": False, "message": "初始化失败", "disk": None}
        for number in disk_numbers
    ]

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except InitializationError as exc:
        if str(exc) != "硬盘初始化失败: 硬盘 1: 初始化失败":
            raise AssertionError(f"初始化失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("初始化失败时主流程应中止")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks

    output = captured.getvalue()
    if "硬盘 1 初始化失败" not in output:
        raise AssertionError("初始化失败时未输出失败结果")



def test_main_flow_stops_when_initialization_validation_fails() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {
            "disk_number": number,
            "passed": True,
            "message": "硬盘初始化完成",
            "disk": {
                "disk_number": number,
                "partition_style": "MBR",
                "is_boot": False,
                "is_system": False,
                "is_offline": False,
                "is_read_only": False,
            },
        }
        for number in disk_numbers
    ]

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except ValidationError as exc:
        if str(exc) != "初始化结果验证失败: 硬盘 1: 初始化后分区表格式不是 GPT: MBR":
            raise AssertionError(f"初始化验证失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("初始化验证失败时主流程应中止")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks

    output = captured.getvalue()
    if "硬盘 1 初始化验证失败" not in output:
        raise AssertionError("初始化验证失败时未输出失败结果")



def test_main_flow_stops_when_partition_fails() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    original_partition_and_format_disks = orchestrator.partition_and_format_disks
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {
            "disk_number": number,
            "passed": True,
            "message": "硬盘初始化完成",
            "disk": {
                "disk_number": number,
                "partition_style": "GPT",
                "is_boot": False,
                "is_system": False,
                "is_offline": False,
                "is_read_only": False,
            },
        }
        for number in disk_numbers
    ]
    orchestrator.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None, **kwargs: [
        {"disk_number": number, "passed": False, "message": "分区失败", "partitions": None}
        for number in disk_numbers
    ]

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except PartitionError as exc:
        if str(exc) != "硬盘分区和格式化失败: 硬盘 1: 分区失败":
            raise AssertionError(f"分区失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("分区失败时主流程应中止")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks
        orchestrator.partition_and_format_disks = original_partition_and_format_disks

    output = captured.getvalue()
    if "硬盘 1 分区格式化失败" not in output:
        raise AssertionError("分区失败时未输出失败结果")



def test_main_flow_stops_when_partition_validation_fails() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    original_partition_and_format_disks = orchestrator.partition_and_format_disks
    original_validate_partitioned_disks = orchestrator.validate_partitioned_disks
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {
            "disk_number": number,
            "passed": True,
            "message": "硬盘初始化完成",
            "disk": {
                "disk_number": number,
                "partition_style": "GPT",
                "is_boot": False,
                "is_system": False,
                "is_offline": False,
                "is_read_only": False,
            },
        }
        for number in disk_numbers
    ]
    orchestrator.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None, **kwargs: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F"}}
        for number in disk_numbers
    ]
    orchestrator.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": False, "message": "分区验证失败", "partitions": None}
        for number in disk_numbers
    ]

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except ValidationError as exc:
        if str(exc) != "分区和格式化结果验证失败: 硬盘 1: 分区验证失败":
            raise AssertionError(f"分区验证失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("分区验证失败时主流程应中止")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks
        orchestrator.partition_and_format_disks = original_partition_and_format_disks
        orchestrator.validate_partitioned_disks = original_validate_partitioned_disks

    output = captured.getvalue()
    if "硬盘 1 分区格式化验证失败" not in output:
        raise AssertionError("分区验证失败时未输出失败结果")



def test_failed_preflight_main_flow() -> None:
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_failed_preflight_report,
            )
    except PreflightError as exc:
        if str(exc) != "运行前检查失败":
            raise AssertionError(f"运行前检查失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("运行前检查失败时主流程未中止")

    output = captured.getvalue()
    if "管理员权限检查：当前程序未以管理员权限运行，请重新以管理员身份启动" not in output:
        raise AssertionError("运行前检查失败时未输出失败项错误信息")
    if "Sisp\n========" in output:
        raise AssertionError("运行前检查失败后主流程不应继续进入正式入口")
    if "当前主程序持有的关键数据:" in output:
        raise AssertionError("运行前检查失败后不应输出主流程关键数据")

    print(output)
    print("最小主流程失败路径测试结果: 通过，运行前检查失败后已正确中止")



def test_default_main_flow_skips_redundant_powershell_probe() -> None:
    original_run_preflight_checks = orchestrator.run_preflight_checks
    calls: list[dict] = []

    def fake_run_preflight_checks(config_path=None, **kwargs):
        calls.append({"config_path": config_path, **kwargs})
        return {
            "all_passed": False,
            "results": [{"name": "配置文件存在性检查", "passed": False, "message": "stop"}],
            "config_payload": None,
        }

    orchestrator.run_preflight_checks = fake_run_preflight_checks
    try:
        with redirect_stdout(io.StringIO()):
            run_minimal_main_flow(config_path="D:\\json\\custom.json")
    except PreflightError:
        pass
    else:
        raise AssertionError("预检失败时主流程应中止")
    finally:
        orchestrator.run_preflight_checks = original_run_preflight_checks

    if calls != [{"config_path": "D:\\json\\custom.json", "skip_powershell_probe": True}]:
        raise AssertionError(f"默认主流程应跳过冗余 PowerShell 探测: {calls}")



def test_main_flow_wraps_disk_scan_failure_as_preflight_error() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    orchestrator.scan_disk_summaries = lambda: (_ for _ in ()).throw(RuntimeError("powershell failed"))

    try:
        with redirect_stdout(io.StringIO()):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except PreflightError as exc:
        if "硬盘信息扫描失败" not in str(exc) or "powershell failed" not in str(exc):
            raise AssertionError(f"磁盘扫描失败错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("磁盘扫描失败时应按预检失败处理")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries



def test_main_flow_stops_when_ghost_fails() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_initialize_disks = orchestrator.initialize_disks
    original_partition_and_format_disks = orchestrator.partition_and_format_disks
    original_validate_partitioned_disks = orchestrator.validate_partitioned_disks
    original_write_ghost_image = orchestrator.write_ghost_image
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: []
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {"disk_number": number, "passed": True, "message": "硬盘初始化完成", "disk": {"disk_number": number, "partition_style": "GPT", "is_boot": False, "is_system": False, "is_offline": False, "is_read_only": False}}
        for number in disk_numbers
    ]
    orchestrator.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None, **kwargs: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F"}}
        for number in disk_numbers
    ]
    orchestrator.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    orchestrator.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": False, "message": "Ghost 镜像写入失败"
    }

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except GhostError as exc:
        if "Ghost 镜像写入失败" not in str(exc):
            raise AssertionError(f"Ghost 失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("Ghost 失败时主流程应中止")
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.initialize_disks = original_initialize_disks
        orchestrator.partition_and_format_disks = original_partition_and_format_disks
        orchestrator.validate_partitioned_disks = original_validate_partitioned_disks
        orchestrator.write_ghost_image = original_write_ghost_image

    output = captured.getvalue()
    if "硬盘 1 镜像写入失败" not in output:
        raise AssertionError("Ghost 失败时未输出失败结果")



def test_build_drive_letter_allocations_with_forbidden() -> None:
    single = build_drive_letter_allocations([2], forbidden_letters=["E", "F"])
    if single != {2: {"efi": "G", "windows": "H", "data1": "I", "data2": "J"}}:
        raise AssertionError(f"跳过占用盘符后单盘分配不正确: {single}")

    multi = build_drive_letter_allocations([2, 3], forbidden_letters=["E", "F", "G"])
    if multi != {
        2: {"efi": "H", "windows": "I", "data1": "J", "data2": "K"},
        3: {"efi": "L", "windows": "M", "data1": "N", "data2": "O"},
    }:
        raise AssertionError(f"跳过占用盘符后多盘分配不正确: {multi}")

    no_forbidden = build_drive_letter_allocations([2])
    if no_forbidden != {2: {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}}:
        raise AssertionError(f"无禁用盘符时分配应与旧行为一致: {no_forbidden}")

    empty_forbidden = build_drive_letter_allocations([2], forbidden_letters=[])
    if empty_forbidden != {2: {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}}:
        raise AssertionError(f"空禁用列表时分配应与旧行为一致: {empty_forbidden}")



def test_build_drive_letter_allocations_insufficient_with_forbidden() -> None:
    try:
        build_drive_letter_allocations([2], forbidden_letters=list("EFGHIJKLMNOPQRSTUVWXYZ"))
    except RuntimeError as exc:
        if "可用盘符不足" not in str(exc):
            raise AssertionError(f"盘符不足错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("盘符不足时应报错")



def test_build_forbidden_drive_letters() -> None:
    disk_summaries = [
        {"disk_number": 0, "drive_letters": ["C"]},
        {"disk_number": 1, "drive_letters": ["E", "F"]},
        {"disk_number": 2, "drive_letters": []},
    ]

    result = build_forbidden_drive_letters(disk_summaries, [1, 2], ["C", "D", "E", "F"])
    if result != ["C", "D", "E", "F"]:
        raise AssertionError(f"所有已用盘符都必须禁止复用，实际: {result}")

    result2 = build_forbidden_drive_letters(disk_summaries, [1], ["C", "E", "F", "G"])
    if result2 != ["C", "E", "F", "G"]:
        raise AssertionError(f"选中盘自身盘符也不能复用，实际: {result2}")

    result3 = build_forbidden_drive_letters(disk_summaries, [], ["C", "E"])
    if result3 != ["C", "E"]:
        raise AssertionError(f"无选中盘时不应排除任何盘符: {result3}")

    result4 = build_forbidden_drive_letters(disk_summaries, [2], ["C", "E"])
    if result4 != ["C", "E"]:
        raise AssertionError(f"所选盘无盘符时不应排除: {result4}")



def test_main_flow_pre_scans_used_letters() -> None:
    original_scan_disk_summaries = orchestrator.scan_disk_summaries
    original_scan_used_drive_letters = orchestrator.scan_used_drive_letters
    original_partition_and_format_disks = orchestrator.partition_and_format_disks
    original_initialize_disks = orchestrator.initialize_disks
    original_validate_partitioned_disks = orchestrator.validate_partitioned_disks
    original_write_ghost_image = orchestrator.write_ghost_image
    original_copy_directory = orchestrator.copy_directory
    original_create_boot_record = orchestrator.create_boot_record
    orchestrator.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    orchestrator.scan_used_drive_letters = lambda: ["E", "F"]
    orchestrator.initialize_disks = lambda disk_numbers, **kwargs: [
        {"disk_number": number, "passed": True, "message": "硬盘初始化完成", "disk": {"disk_number": number, "partition_style": "GPT", "is_boot": False, "is_system": False, "is_offline": False, "is_read_only": False}}
        for number in disk_numbers
    ]
    received_drive_letters: list[dict | None] = []
    orchestrator.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None, **kwargs: (
        received_drive_letters.append(drive_letters)
        or [{"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "G", "d1_drive_letter": "H", "efi_drive_letter": "F", "d2_drive_letter": "I"}} for number in disk_numbers]
    )
    orchestrator.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    orchestrator.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": True, "message": "Ghost 镜像写入成功"
    }
    orchestrator.copy_directory = lambda source_dir, data1_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "目录拷贝成功"
    }
    orchestrator.create_boot_record = lambda bcd_exe, windows_drive_letter, efi_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "引导记录创建成功"
    }

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    finally:
        orchestrator.scan_disk_summaries = original_scan_disk_summaries
        orchestrator.scan_used_drive_letters = original_scan_used_drive_letters
        orchestrator.partition_and_format_disks = original_partition_and_format_disks
        orchestrator.initialize_disks = original_initialize_disks
        orchestrator.validate_partitioned_disks = original_validate_partitioned_disks
        orchestrator.write_ghost_image = original_write_ghost_image
        orchestrator.copy_directory = original_copy_directory
        orchestrator.create_boot_record = original_create_boot_record

    if not received_drive_letters:
        raise AssertionError("未接收到分区盘符")

    actual = received_drive_letters[0]
    if not actual:
        raise AssertionError("分区模块未收到盘符分配")

    if actual.get("efi") != "G" or actual.get("windows") != "H" or actual.get("data1") != "I" or actual.get("data2") != "J":
        raise AssertionError(
            f"预扫发现 E/F 被占用时盘符应跳过到 G/H/I/J，实际: {actual}"
        )



def test_multi_disk_flow_rejects_resource_on_any_target(monkeypatch) -> None:
    initialized: list[list[int]] = []
    monkeypatch.setattr(orchestrator, "scan_disk_summaries", lambda: SAMPLE_DISK_SUMMARIES)
    monkeypatch.setattr(orchestrator, "initialize_disks", lambda disk_numbers, **kwargs: initialized.append(disk_numbers) or [])
    monkeypatch.setattr(orchestrator, "launch_worker_windows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        orchestrator,
        "resolve_path_disk_number",
        lambda path: 2 if str(path).endswith("README.md") else 0,
    )

    try:
        run_minimal_main_flow(
            input_func=lambda prompt: "a",
            preflight_runner=build_successful_preflight_report,
            config_path="D:\\Code-Project\\Sisp2\\json\\win11.json",
        )
    except ConfigError as exc:
        if "位于目标硬盘上" not in str(exc):
            raise AssertionError(f"多盘资源冲突错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("任一目标盘上的资源冲突都应拒绝执行")

    if initialized:
        raise AssertionError("多盘资源冲突检查必须在初始化之前中止流程")


def main() -> int:
    try:
        test_build_drive_letter_allocations()
        test_build_drive_letter_allocations_overflow()
        test_build_drive_letter_allocations_with_forbidden()
        test_build_drive_letter_allocations_insufficient_with_forbidden()
        test_build_forbidden_drive_letters()
        test_parse_worker_drive_letters()
        test_validate_drive_letters_are_free_rejects_used_letters()
        test_parse_command_line_args()
        test_validate_required_paths_rejects_missing_file()
        test_successful_main_flow()
        test_successful_main_flow_with_custom_json_path()
        test_multi_disk_main_flow_launches_worker_windows()
        test_worker_flow_runs_single_disk_flow()
        test_validate_worker_disk_identity()
        test_main_flow_rejects_insufficient_disk_capacity_before_initialize()
        test_single_disk_main_flow_passes_expected_identity()
        test_launch_worker_windows_uses_direct_python()
        test_main_uses_json_argument()
        test_main_uses_worker_disk_argument()
        test_main_flow_stops_when_initialization_fails()
        test_main_flow_stops_when_initialization_validation_fails()
        test_main_flow_stops_when_partition_fails()
        test_main_flow_stops_when_partition_validation_fails()
        test_main_flow_stops_when_ghost_fails()
        test_failed_preflight_main_flow()
        test_default_main_flow_skips_redundant_powershell_probe()
        test_main_flow_wraps_disk_scan_failure_as_preflight_error()
        test_main_flow_pre_scans_used_letters()
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
