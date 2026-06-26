import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_SCRIPTS = [
    PROJECT_ROOT / "tests" / "test_config_loader.py",
    PROJECT_ROOT / "tests" / "test_preflight.py",
    PROJECT_ROOT / "tests" / "test_user_interaction.py",
    PROJECT_ROOT / "tests" / "test_disk_info.py",
    PROJECT_ROOT / "tests" / "test_disk_initializer.py",
    PROJECT_ROOT / "tests" / "test_initialization_validator.py",
    PROJECT_ROOT / "tests" / "test_disk_partitioner.py",
    PROJECT_ROOT / "tests" / "test_partition_validator.py",
    PROJECT_ROOT / "tests" / "test_ghost_writer.py",
    PROJECT_ROOT / "tests" / "test_directory_copier.py",
    PROJECT_ROOT / "tests" / "test_boot_creator.py",
    PROJECT_ROOT / "tests" / "test_minimal_main_flow.py",
    PROJECT_ROOT / "tests" / "test_user_interaction_integration.py",
    PROJECT_ROOT / "tests" / "run_invalid_input_cases.py",
]



def main() -> int:
    for script in TEST_SCRIPTS:
        print(f"运行测试: {script.relative_to(PROJECT_ROOT)}")
        completed = subprocess.run([sys.executable, str(script)], cwd=PROJECT_ROOT, check=False)
        if completed.returncode != 0:
            print(f"测试失败: {script.relative_to(PROJECT_ROOT)}", file=sys.stderr)
            return completed.returncode

    print("全部安全测试通过")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
