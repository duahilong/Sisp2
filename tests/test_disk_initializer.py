import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_initializer.service import build_initialize_disk_script, initialize_disks, parse_initialized_disks



def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"期望包含: {expected}\n实际内容:\n{text}")



def test_build_initialize_disk_script() -> None:
    script = build_initialize_disk_script([2])

    assert_contains(script, "Get-Disk -Number 2")
    assert_contains(script, "select disk 2")
    assert_contains(script, "clean")
    assert_contains(script, "convert gpt")
    assert_contains(script, "diskpart")
    assert_contains(script, "IsBoot")
    assert_contains(script, "IsSystem")
    assert_contains(script, "IsReadOnly")
    assert_contains(script, "IsOffline")
    assert_contains(script, "ConvertTo-Json")



def test_build_initialize_disk_script_rejects_empty_numbers() -> None:
    try:
        build_initialize_disk_script([])
    except ValueError as exc:
        if "不能为空" not in str(exc):
            raise AssertionError(f"错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("空硬盘编号列表应失败")



def test_parse_initialized_disks() -> None:
    single = parse_initialized_disks(json.dumps({"disk_number": 2, "partition_style": "GPT"}))
    if single != [{"disk_number": 2, "partition_style": "GPT"}]:
        raise AssertionError(f"单个初始化结果解析不正确: {single}")

    multiple = parse_initialized_disks(json.dumps([{"disk_number": 2}, {"disk_number": 3}]))
    if multiple != [{"disk_number": 2}, {"disk_number": 3}]:
        raise AssertionError(f"多个初始化结果解析不正确: {multiple}")



def test_initialize_disks_success() -> None:
    captured_scripts: list[str] = []

    def fake_runner(script: str) -> CompletedProcess[str]:
        captured_scripts.append(script)
        return CompletedProcess(
            args=["powershell"],
            returncode=0,
            stdout=json.dumps({"disk_number": 2, "friendly_name": "Realtek USB 3.2 Device", "partition_style": "GPT"}),
            stderr="",
        )

    results = initialize_disks([2], powershell_runner=fake_runner)
    if not captured_scripts:
        raise AssertionError("initialize_disks 未执行注入的 PowerShell runner")
    if results != [
        {
            "disk_number": 2,
            "passed": True,
            "message": "硬盘初始化完成",
            "disk": {"disk_number": 2, "friendly_name": "Realtek USB 3.2 Device", "partition_style": "GPT"},
        }
    ]:
        raise AssertionError(f"初始化成功结果不正确: {results}")



def test_initialize_disks_failure() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=["powershell"], returncode=1, stdout="", stderr="boom")

    results = initialize_disks([2], powershell_runner=fake_runner)
    if results[0].get("passed"):
        raise AssertionError(f"初始化失败时不应通过: {results}")
    if "boom" not in results[0].get("message", ""):
        raise AssertionError(f"初始化失败信息不正确: {results}")



def test_initialize_disks_parse_failure() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=["powershell"], returncode=0, stdout="not json", stderr="")

    results = initialize_disks([2], powershell_runner=fake_runner)
    if results[0].get("passed"):
        raise AssertionError(f"解析失败时不应通过: {results}")
    if "结果解析失败" not in results[0].get("message", ""):
        raise AssertionError(f"解析失败信息不正确: {results}")



def main() -> int:
    try:
        test_build_initialize_disk_script()
        test_build_initialize_disk_script_rejects_empty_numbers()
        test_parse_initialized_disks()
        test_initialize_disks_success()
        test_initialize_disks_failure()
        test_initialize_disks_parse_failure()
        print("模块2初始化硬盘测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
