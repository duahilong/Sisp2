import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.preflight import check_admin_privileges, check_config_file_exists, check_config_file_parseable, check_powershell_available, print_preflight_report, run_preflight_checks



def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"期望输出包含: {expected}\n实际输出:\n{text}")



def test_check_admin_privileges() -> None:
    passed = check_admin_privileges(lambda: True)
    if not passed.get("passed"):
        raise AssertionError(f"管理员权限检查应通过，实际为: {passed}")

    failed = check_admin_privileges(lambda: False)
    if failed.get("passed"):
        raise AssertionError(f"管理员权限检查应失败，实际为: {failed}")



def test_check_powershell_available() -> None:
    passed = check_powershell_available(lambda: CompletedProcess(args=["powershell"], returncode=0, stdout="preflight-ok\n", stderr=""))
    if not passed.get("passed"):
        raise AssertionError(f"PowerShell 可用性检查应通过，实际为: {passed}")

    failed = check_powershell_available(lambda: CompletedProcess(args=["powershell"], returncode=1, stdout="", stderr="powershell error"))
    if failed.get("passed"):
        raise AssertionError(f"PowerShell 可用性检查应失败，实际为: {failed}")



def test_check_config_file_exists() -> None:
    existing = PROJECT_ROOT / "json" / "win11.json"
    passed = check_config_file_exists(existing)
    if not passed.get("passed"):
        raise AssertionError(f"配置文件存在性检查应通过，实际为: {passed}")

    missing = PROJECT_ROOT / "json" / "missing.json"
    failed = check_config_file_exists(missing)
    if failed.get("passed"):
        raise AssertionError(f"配置文件存在性检查应失败，实际为: {failed}")



def test_check_config_file_parseable() -> None:
    result, config_payload = check_config_file_parseable(config_loader=lambda path: {"config_path": str(path), "config": {}})
    if not result.get("passed"):
        raise AssertionError(f"配置文件可解析性检查应通过，实际为: {result}")
    if not isinstance(config_payload, dict):
        raise AssertionError("配置文件可解析性检查通过时应返回配置数据")

    failed_result, failed_payload = check_config_file_parseable(config_loader=lambda path: (_ for _ in ()).throw(ValueError("bad config")))
    if failed_result.get("passed"):
        raise AssertionError(f"配置文件可解析性检查应失败，实际为: {failed_result}")
    if failed_payload is not None:
        raise AssertionError("配置文件可解析性检查失败时不应返回配置数据")



def test_run_preflight_checks() -> None:
    report = run_preflight_checks(
        admin_checker=lambda: True,
        powershell_probe=lambda: CompletedProcess(args=["powershell"], returncode=0, stdout="preflight-ok\n", stderr=""),
        config_loader=lambda path: {"config_path": str(path), "config": {}},
    )
    if not report.get("all_passed"):
        raise AssertionError(f"运行前检查应全部通过，实际为: {report}")
    if not isinstance(report.get("config_payload"), dict):
        raise AssertionError("运行前检查全部通过时应返回配置数据")

    failed_report = run_preflight_checks(
        admin_checker=lambda: False,
        powershell_probe=lambda: CompletedProcess(args=["powershell"], returncode=0, stdout="preflight-ok\n", stderr=""),
        config_loader=lambda path: {"config_path": str(path), "config": {}},
    )
    if failed_report.get("all_passed"):
        raise AssertionError(f"运行前检查应存在失败项，实际为: {failed_report}")



def test_print_preflight_report() -> None:
    passed_report = {
        "all_passed": True,
        "results": [
            {"name": "管理员权限检查", "passed": True, "message": "当前程序已以管理员权限运行"},
        ],
        "config_payload": {"config_path": "demo"},
    }

    passed_captured = io.StringIO()
    with redirect_stdout(passed_captured):
        print_preflight_report(passed_report)

    if passed_captured.getvalue():
        raise AssertionError("运行前检查全部通过时不应输出任何信息")

    failed_report = {
        "all_passed": False,
        "results": [
            {"name": "管理员权限检查", "passed": True, "message": "当前程序已以管理员权限运行"},
            {"name": "PowerShell 可用性检查", "passed": False, "message": "PowerShell 不可用: error"},
        ],
        "config_payload": None,
    }

    failed_captured = io.StringIO()
    with redirect_stdout(failed_captured):
        print_preflight_report(failed_report)

    output = failed_captured.getvalue()
    assert_contains(output, "PowerShell 可用性检查：PowerShell 不可用: error")
    if "管理员权限检查" in output:
        raise AssertionError("运行前检查失败时不应输出通过项")
    if "运行前检查结果:" in output:
        raise AssertionError("运行前检查失败时不应输出统一标题")
    if "运行前检查结论" in output:
        raise AssertionError("运行前检查失败时不应输出统一结论")



def main() -> int:
    try:
        test_check_admin_privileges()
        test_check_powershell_available()
        test_check_config_file_exists()
        test_check_config_file_parseable()
        test_run_preflight_checks()
        test_print_preflight_report()
        print("运行前检查测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
