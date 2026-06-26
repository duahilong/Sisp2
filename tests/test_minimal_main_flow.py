import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.main as main_module
from app.main import parse_command_line_args, run_minimal_main_flow, build_drive_letter_allocations, parse_worker_drive_letters

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
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 1536},
            "image_info": {"image_path": "D:\\sisp2\\img\\111.GHO"},
            "software_paths": {"ghost64_path": "D:\\sisp2\\sw\\ghost64.exe", "bcdboot_path": "D:\\sisp2\\sw\\bcdboot.exe"},
            "copy_info": {"source_dir": "D:\\常用软件"},
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



def test_successful_main_flow() -> None:
    target_number = 1
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    original_partition_and_format_disks = main_module.partition_and_format_disks
    original_validate_partitioned_disks = main_module.validate_partitioned_disks
    original_write_ghost_image = main_module.write_ghost_image
    original_copy_directory = main_module.copy_directory
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
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
    main_module.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F", "d1_drive_letter": "G"}}
        for number in disk_numbers
    ]
    main_module.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    main_module.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": True, "message": "Ghost 镜像写入成功"
    }
    main_module.copy_directory = lambda source_dir, data1_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "目录拷贝成功"
    }

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            selected_disk_numbers = run_minimal_main_flow(
                input_func=lambda prompt: str(target_number),
                preflight_runner=build_successful_preflight_report,
            )
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks
        main_module.partition_and_format_disks = original_partition_and_format_disks
        main_module.validate_partitioned_disks = original_validate_partitioned_disks
        main_module.write_ghost_image = original_write_ghost_image
        main_module.copy_directory = original_copy_directory

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

    validate_selected_disk_numbers(selected_disk_numbers)
    if selected_disk_numbers != [target_number]:
        raise AssertionError(f"主流程选择结果不正确: {selected_disk_numbers}，期望: {[target_number]}")

    print(output)
    print(f"最小主流程成功路径测试结果: 通过，已选择硬盘编号 {selected_disk_numbers}")



def test_successful_main_flow_with_custom_json_path() -> None:
    custom_path = "D:\\custom\\demo.json"
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    original_partition_and_format_disks = main_module.partition_and_format_disks
    original_validate_partitioned_disks = main_module.validate_partitioned_disks
    original_write_ghost_image = main_module.write_ghost_image
    original_copy_directory = main_module.copy_directory
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
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
    main_module.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F", "d1_drive_letter": "G"}}
        for number in disk_numbers
    ]
    main_module.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    main_module.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": True, "message": "Ghost 镜像写入成功"
    }
    main_module.copy_directory = lambda source_dir, data1_drive_letter, disk_number: {
        "disk_number": disk_number, "passed": True, "message": "目录拷贝成功"
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
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks
        main_module.partition_and_format_disks = original_partition_and_format_disks
        main_module.validate_partitioned_disks = original_validate_partitioned_disks
        main_module.write_ghost_image = original_write_ghost_image
        main_module.copy_directory = original_copy_directory

    output = captured.getvalue()
    if f"配置文件: {custom_path}" not in output:
        raise AssertionError("主流程未使用 -j 传入的自定义 JSON 路径")
    validate_selected_disk_numbers(selected_disk_numbers)



def test_multi_disk_main_flow_launches_worker_windows() -> None:
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_run_single_disk_flow = main_module.run_single_disk_flow
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES

    launched: list[tuple[list[dict], str | None]] = []
    current_process_calls: list[int] = []

    def fake_launcher(disk_identities: list[dict], config_path: str | None) -> None:
        launched.append((disk_identities, config_path))

    main_module.run_single_disk_flow = lambda disk_number, config_payload, expected_identity=None, drive_letters=None: current_process_calls.append(disk_number)

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            selected_disk_numbers = run_minimal_main_flow(
                input_func=lambda prompt: "1,2",
                preflight_runner=build_successful_preflight_report,
                worker_launcher=fake_launcher,
            )
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.run_single_disk_flow = original_run_single_disk_flow

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
    original_run_single_disk_flow = main_module.run_single_disk_flow
    main_module.run_single_disk_flow = lambda disk_number, config_payload, expected_identity=None, drive_letters=None: received.append((disk_number, config_payload, expected_identity))

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            main_module.run_worker_flow(
                2,
                preflight_runner=build_successful_preflight_report,
                config_path="D:\\temp\\demo.json",
            )
    finally:
        main_module.run_single_disk_flow = original_run_single_disk_flow

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
    main_module.validate_worker_disk_identity(disk, expected_identity)

    try:
        main_module.validate_worker_disk_identity(disk, {**expected_identity, "unique_id": "WRONG-UID"})
    except RuntimeError as exc:
        if "UniqueId 不一致" not in str(exc):
            raise AssertionError(f"硬盘身份不一致错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("UniqueId 不一致时应拒绝执行")

    try:
        main_module.validate_worker_disk_identity(disk, {**expected_identity, "size_bytes": 1})
    except RuntimeError as exc:
        if "硬盘容量不一致" not in str(exc):
            raise AssertionError(f"硬盘容量不一致错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("硬盘容量不一致时应拒绝执行")



def test_launch_worker_windows_uses_powershell() -> None:
    launched: list[tuple[list[str], Path | str | None]] = []
    sleep_calls: list[float] = []
    original_popen = main_module.subprocess.Popen

    def fake_popen(args, cwd=None, creationflags=0):
        launched.append((args, cwd))

        class FakeProcess:
            pass

        return FakeProcess()

    main_module.subprocess.Popen = fake_popen
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            main_module.launch_worker_windows(
                [
                    {"disk_number": 2, "unique_id": "UID-2", "serial_number": "SN-2", "model": "Disk 2", "size_bytes": 2000, "drive_letters": {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}},
                    {"disk_number": 3, "unique_id": "UID-3", "serial_number": "SN-3", "model": "Disk 3", "size_bytes": 3000, "drive_letters": {"efi": "I", "windows": "J", "data1": "K", "data2": "L"}},
                ],
                "D:\\json\\custom.json",
                sleep_func=sleep_calls.append,
            )
    finally:
        main_module.subprocess.Popen = original_popen

    if len(launched) != 2:
        raise AssertionError(f"应启动两个 worker 窗口，实际为: {launched}")
    if launched[0][0][0] != "powershell" or launched[0][0][1] != "-NoExit" or "-NoProfile" not in launched[0][0]:
        raise AssertionError(f"worker 窗口启动命令不正确: {launched}")
    if "--worker-disk" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令缺少 --worker-disk: {launched}")
    if "chcp 65001" in launched[0][0][-1]:
        raise AssertionError(f"worker 命令不应切换控制台代码页: {launched}")
    if "$env:PYTHONUTF8" not in launched[0][0][-1] or "PYTHONIOENCODING" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未设置 Python UTF-8 环境: {launched}")
    if "OutputEncoding" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未设置 PowerShell 输出编码: {launched}")
    if "2" not in launched[0][0][-1] or "3" not in launched[1][0][-1]:
        raise AssertionError(f"worker 命令未包含正确硬盘编号: {launched}")
    if "D:\\json\\custom.json" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未包含配置路径: {launched}")
    if "--worker-unique-id" not in launched[0][0][-1] or "UID-2" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未包含 UniqueId: {launched}")
    if "--worker-serial-number" not in launched[0][0][-1] or "SN-2" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未包含 SerialNumber: {launched}")
    if "--worker-model" not in launched[0][0][-1] or "Disk 2" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未包含硬盘型号: {launched}")
    if "--worker-size-bytes" not in launched[0][0][-1] or "2000" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未包含硬盘容量: {launched}")
    if "--worker-drive-letters" not in launched[0][0][-1] or "E,F,G,H" not in launched[0][0][-1]:
        raise AssertionError(f"worker 命令未包含盘符: {launched}")
    if sleep_calls != [3.0]:
        raise AssertionError(f"worker 启动间隔不正确: {sleep_calls}")

    output = captured.getvalue()
    if "硬盘 2: 已启动独立执行窗口" not in output or "硬盘 3: 已启动独立执行窗口" not in output:
        raise AssertionError("启动 worker 窗口时未输出提示")
    if "等待 3 秒后启动下一个 worker" not in output:
        raise AssertionError("启动 worker 窗口时未输出等待提示")



def test_main_uses_worker_disk_argument() -> None:
    received: list[tuple[int, str | Path | None, dict, dict | None]] = []
    original_runner = main_module.run_worker_flow

    def fake_run_worker_flow(disk_number: int, preflight_runner=None, config_path: str | Path | None = None, expected_identity=None, drive_letters=None) -> None:
        received.append((disk_number, config_path, expected_identity, drive_letters))

    main_module.run_worker_flow = fake_run_worker_flow
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
        main_module.run_worker_flow = original_runner

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
    original_runner = main_module.run_minimal_main_flow

    def fake_run_minimal_main_flow(input_func=input, preflight_runner=None, config_path: str | Path | None = None) -> list[int]:
        received_config_paths.append(config_path)
        return [1]

    main_module.run_minimal_main_flow = fake_run_minimal_main_flow
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = main_module.main(["-j", "D:\\json\\custom.json"])
    finally:
        main_module.run_minimal_main_flow = original_runner

    output = captured.getvalue()
    if exit_code != 0:
        raise AssertionError(f"main 使用 -j 参数时应返回 0，实际为: {exit_code}")
    if received_config_paths != ["D:\\json\\custom.json"]:
        raise AssertionError(f"main 未将 -j 参数传递给主流程: {received_config_paths}")
    if "演示运行完成" in output or "演示运行结束" in output:
        raise AssertionError("main 不应在第一阶段结束后输出演示性收尾信息")



def test_main_flow_stops_when_initialization_fails() -> None:
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
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
    except RuntimeError as exc:
        if str(exc) != "硬盘初始化失败: 硬盘 1: 初始化失败":
            raise AssertionError(f"初始化失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("初始化失败时主流程应中止")
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks

    output = captured.getvalue()
    if "硬盘 1 初始化失败" not in output:
        raise AssertionError("初始化失败时未输出失败结果")



def test_main_flow_stops_when_initialization_validation_fails() -> None:
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
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
    except RuntimeError as exc:
        if str(exc) != "初始化结果验证失败: 硬盘 1: 初始化后分区表格式不是 GPT: MBR":
            raise AssertionError(f"初始化验证失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("初始化验证失败时主流程应中止")
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks

    output = captured.getvalue()
    if "硬盘 1 初始化验证失败" not in output:
        raise AssertionError("初始化验证失败时未输出失败结果")



def test_main_flow_stops_when_partition_fails() -> None:
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    original_partition_and_format_disks = main_module.partition_and_format_disks
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
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
    main_module.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None: [
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
    except RuntimeError as exc:
        if str(exc) != "硬盘分区和格式化失败: 硬盘 1: 分区失败":
            raise AssertionError(f"分区失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("分区失败时主流程应中止")
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks
        main_module.partition_and_format_disks = original_partition_and_format_disks

    output = captured.getvalue()
    if "硬盘 1 分区格式化失败" not in output:
        raise AssertionError("分区失败时未输出失败结果")



def test_main_flow_stops_when_partition_validation_fails() -> None:
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    original_partition_and_format_disks = main_module.partition_and_format_disks
    original_validate_partitioned_disks = main_module.validate_partitioned_disks
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
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
    main_module.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F"}}
        for number in disk_numbers
    ]
    main_module.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
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
    except RuntimeError as exc:
        if str(exc) != "分区和格式化结果验证失败: 硬盘 1: 分区验证失败":
            raise AssertionError(f"分区验证失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("分区验证失败时主流程应中止")
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks
        main_module.partition_and_format_disks = original_partition_and_format_disks
        main_module.validate_partitioned_disks = original_validate_partitioned_disks

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
    except RuntimeError as exc:
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



def test_main_flow_stops_when_ghost_fails() -> None:
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_initialize_disks = main_module.initialize_disks
    original_partition_and_format_disks = main_module.partition_and_format_disks
    original_validate_partitioned_disks = main_module.validate_partitioned_disks
    original_write_ghost_image = main_module.write_ghost_image
    main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
    main_module.initialize_disks = lambda disk_numbers: [
        {"disk_number": number, "passed": True, "message": "硬盘初始化完成", "disk": {"disk_number": number, "partition_style": "GPT", "is_boot": False, "is_system": False, "is_offline": False, "is_read_only": False}}
        for number in disk_numbers
    ]
    main_module.partition_and_format_disks = lambda disk_numbers, partition_info, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "硬盘分区和格式化完成", "partitions": {"c_drive_letter": "F"}}
        for number in disk_numbers
    ]
    main_module.validate_partitioned_disks = lambda disk_numbers, partition_info, disk_scanner=None, drive_letters=None: [
        {"disk_number": number, "passed": True, "message": "分区和格式化结果验证通过"}
        for number in disk_numbers
    ]
    main_module.write_ghost_image = lambda gho_exe, win_gho, disk_number, windows_drive_letter: {
        "disk_number": disk_number, "passed": False, "message": "Ghost 镜像写入失败"
    }

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_successful_preflight_report,
            )
    except RuntimeError as exc:
        if "Ghost 镜像写入失败" not in str(exc):
            raise AssertionError(f"Ghost 失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("Ghost 失败时主流程应中止")
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.initialize_disks = original_initialize_disks
        main_module.partition_and_format_disks = original_partition_and_format_disks
        main_module.validate_partitioned_disks = original_validate_partitioned_disks
        main_module.write_ghost_image = original_write_ghost_image

    output = captured.getvalue()
    if "硬盘 1 镜像写入失败" not in output:
        raise AssertionError("Ghost 失败时未输出失败结果")



def main() -> int:
    try:
        test_build_drive_letter_allocations()
        test_build_drive_letter_allocations_overflow()
        test_parse_worker_drive_letters()
        test_parse_command_line_args()
        test_successful_main_flow()
        test_successful_main_flow_with_custom_json_path()
        test_multi_disk_main_flow_launches_worker_windows()
        test_worker_flow_runs_single_disk_flow()
        test_validate_worker_disk_identity()
        test_launch_worker_windows_uses_powershell()
        test_main_uses_json_argument()
        test_main_uses_worker_disk_argument()
        test_main_flow_stops_when_initialization_fails()
        test_main_flow_stops_when_initialization_validation_fails()
        test_main_flow_stops_when_partition_fails()
        test_main_flow_stops_when_partition_validation_fails()
        test_main_flow_stops_when_ghost_fails()
        test_failed_preflight_main_flow()
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
