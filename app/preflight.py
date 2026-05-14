import ctypes
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.modules.config_loader.service import DEFAULT_CONFIG_PATH, load_config


CheckResult = dict[str, Any]
CheckRunner = Callable[..., CheckResult]



def build_check_result(name: str, passed: bool, message: str) -> CheckResult:
    return {
        "name": name,
        "passed": passed,
        "message": message,
    }



def check_admin_privileges(is_admin_func: Callable[[], bool] | None = None) -> CheckResult:
    checker = is_admin_func or (lambda: bool(ctypes.windll.shell32.IsUserAnAdmin()))

    try:
        is_admin = checker()
    except Exception as exc:
        return build_check_result("管理员权限检查", False, f"管理员权限检查执行失败: {exc}")

    if is_admin:
        return build_check_result("管理员权限检查", True, "当前程序已以管理员权限运行")

    return build_check_result("管理员权限检查", False, "当前程序未以管理员权限运行，请重新以管理员身份启动")



def run_powershell_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Write-Output preflight-ok"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )



def check_powershell_available(
    powershell_probe: Callable[[], subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    probe = powershell_probe or run_powershell_probe

    try:
        completed = probe()
    except Exception as exc:
        return build_check_result("PowerShell 可用性检查", False, f"PowerShell 调用失败: {exc}")

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "未知错误"
        return build_check_result("PowerShell 可用性检查", False, f"PowerShell 不可用: {stderr}")

    return build_check_result("PowerShell 可用性检查", True, "PowerShell 可正常调用")



def check_config_file_exists(config_path: str | Path | None = None) -> CheckResult:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists():
        return build_check_result("配置文件存在性检查", True, f"配置文件存在: {path}")

    return build_check_result("配置文件存在性检查", False, f"配置文件不存在: {path}")



def check_config_file_parseable(
    config_path: str | Path | None = None,
    config_loader: Callable[[str | Path | None], dict[str, Any]] | None = None,
) -> CheckResult:
    loader = config_loader or load_config

    try:
        loader(config_path)
    except Exception as exc:
        return build_check_result("配置文件可解析性检查", False, f"配置文件解析失败: {exc}")

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    return build_check_result("配置文件可解析性检查", True, f"配置文件可正常解析: {path}")



def run_preflight_checks(
    config_path: str | Path | None = None,
    admin_checker: Callable[[], bool] | None = None,
    powershell_probe: Callable[[], subprocess.CompletedProcess[str]] | None = None,
    config_loader: Callable[[str | Path | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    results = [
        check_admin_privileges(admin_checker),
        check_powershell_available(powershell_probe),
        check_config_file_exists(config_path),
        check_config_file_parseable(config_path, config_loader),
    ]
    return {
        "all_passed": all(result["passed"] for result in results),
        "results": results,
    }



def print_preflight_report(report: dict[str, Any]) -> None:
    print("运行前检查结果:")
    for result in report.get("results", []):
        status_text = "通过" if result.get("passed") else "失败"
        print(f"[{status_text}] {result.get('name')}：{result.get('message')}")

    summary_text = "全部通过" if report.get("all_passed") else "存在失败项"
    print(f"运行前检查结论：{summary_text}")
