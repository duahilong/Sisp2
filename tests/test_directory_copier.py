import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.directory_copier.service import copy_directory, verify_copy_result, count_files_recursive, get_system_drive_letter



def test_copy_directory_success() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_dir = Path(tmp_dir) / "常用软件"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1", encoding="utf-8")
        (source_dir / "file2.txt").write_text("content2", encoding="utf-8")
        sub_dir = source_dir / "subdir"
        sub_dir.mkdir()
        (sub_dir / "file3.txt").write_text("content3", encoding="utf-8")

        drive_letter = str(Path(tmp_dir))[0]

        with patch('app.modules.directory_copier.service.get_system_drive_letter', return_value='Z'):
            result = copy_directory(str(source_dir), drive_letter, 2)

        if not result.get("passed"):
            raise AssertionError(f"拷贝成功时应通过: {result}")

        expected_target = Path(f"{drive_letter}:\\") / "常用软件"
        if not expected_target.exists():
            raise AssertionError(f"目标目录不存在: {expected_target}")

        if not (expected_target / "file1.txt").exists():
            raise AssertionError("file1.txt 未拷贝")
        if not (expected_target / "file2.txt").exists():
            raise AssertionError("file2.txt 未拷贝")
        if not (expected_target / "subdir" / "file3.txt").exists():
            raise AssertionError("file3.txt 未拷贝")

        import shutil
        shutil.rmtree(expected_target, ignore_errors=True)



def test_copy_directory_source_not_exists() -> None:
    result = copy_directory("Z:\\nonexistent", "G", 2)

    if result.get("passed"):
        raise AssertionError(f"源目录不存在时不应通过: {result}")
    if "源目录不存在" not in result.get("message", ""):
        raise AssertionError(f"错误消息不正确: {result}")



def test_copy_directory_empty_drive_letter() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_dir = Path(tmp_dir) / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content", encoding="utf-8")

        result = copy_directory(str(source_dir), "", 2)

        if result.get("passed"):
            raise AssertionError(f"盘符为空时不应通过: {result}")
        if "盘符为空" not in result.get("message", ""):
            raise AssertionError(f"错误消息不正确: {result}")



def test_copy_directory_not_a_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_file = Path(tmp_dir) / "file.txt"
        source_file.write_text("content", encoding="utf-8")

        result = copy_directory(str(source_file), "G", 2)

        if result.get("passed"):
            raise AssertionError(f"源路径不是目录时不应通过: {result}")
        if "不是目录" not in result.get("message", ""):
            raise AssertionError(f"错误消息不正确: {result}")



def test_verify_copy_result_success() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_dir = Path(tmp_dir) / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1", encoding="utf-8")
        (source_dir / "file2.txt").write_text("content2", encoding="utf-8")

        target_dir = Path(tmp_dir) / "target"
        target_dir.mkdir()
        (target_dir / "file1.txt").write_text("content1", encoding="utf-8")
        (target_dir / "file2.txt").write_text("content2", encoding="utf-8")

        passed, message = verify_copy_result(source_dir, target_dir)

        if not passed:
            raise AssertionError(f"验证应通过: {message}")



def test_verify_copy_result_target_not_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_dir = Path(tmp_dir) / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content", encoding="utf-8")

        target_dir = Path(tmp_dir) / "nonexistent"

        passed, message = verify_copy_result(source_dir, target_dir)

        if passed:
            raise AssertionError(f"目标目录不存在时验证不应通过: {message}")
        if "目标目录不存在" not in message:
            raise AssertionError(f"错误消息不正确: {message}")



def test_verify_copy_result_file_count_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_dir = Path(tmp_dir) / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1", encoding="utf-8")
        (source_dir / "file2.txt").write_text("content2", encoding="utf-8")

        target_dir = Path(tmp_dir) / "target"
        target_dir.mkdir()
        (target_dir / "file1.txt").write_text("content1", encoding="utf-8")

        passed, message = verify_copy_result(source_dir, target_dir)

        if passed:
            raise AssertionError(f"文件数量不一致时验证不应通过: {message}")
        if "文件数量不一致" not in message:
            raise AssertionError(f"错误消息不正确: {message}")



def test_count_files_recursive() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_dir = Path(tmp_dir) / "test"
        test_dir.mkdir()
        (test_dir / "file1.txt").write_text("content", encoding="utf-8")
        (test_dir / "file2.txt").write_text("content", encoding="utf-8")
        sub_dir = test_dir / "subdir"
        sub_dir.mkdir()
        (sub_dir / "file3.txt").write_text("content", encoding="utf-8")

        count = count_files_recursive(test_dir)

        if count != 3:
            raise AssertionError(f"文件数量应为 3，实际为 {count}")



def main() -> int:
    try:
        test_copy_directory_success()
        test_copy_directory_source_not_exists()
        test_copy_directory_empty_drive_letter()
        test_copy_directory_not_a_directory()
        test_verify_copy_result_success()
        test_verify_copy_result_target_not_exists()
        test_verify_copy_result_file_count_mismatch()
        test_count_files_recursive()
        print("模块7目录拷贝测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
