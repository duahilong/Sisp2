import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVALID_INPUT_SCRIPT = PROJECT_ROOT / "tests" / "run_invalid_input_cases.py"


def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(PROJECT_ROOT / "tests")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        print("pytest 测试失败", file=sys.stderr)
        return completed.returncode

    # 无效输入场景报告：生成 tests/.output/invalid_input_results.txt
    print(f"运行测试: {INVALID_INPUT_SCRIPT.relative_to(PROJECT_ROOT)}")
    completed = subprocess.run([sys.executable, str(INVALID_INPUT_SCRIPT)], cwd=PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        print(f"测试失败: {INVALID_INPUT_SCRIPT.relative_to(PROJECT_ROOT)}", file=sys.stderr)
        return completed.returncode

    print("全部安全测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
