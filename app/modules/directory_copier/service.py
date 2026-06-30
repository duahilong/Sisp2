import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any


FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
FILE_ATTRIBUTE_READONLY = 0x01


def get_system_drive_letter() -> str:
    """获取系统盘盘符"""
    if sys.platform == "win32":
        return os.environ.get("SystemDrive", "C:")[0].upper()
    return "C"



def copy_file_with_attributes(src: str, dst: str) -> None:
    shutil.copy2(src, dst)
    try:
        src_stat = os.stat(src)
        if hasattr(src_stat, 'st_file_attributes'):
            attrs = src_stat.st_file_attributes
            if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY):
                os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
                dst_stat = os.stat(dst)
                new_attrs = dst_stat.st_file_attributes
                if attrs & FILE_ATTRIBUTE_HIDDEN:
                    new_attrs |= FILE_ATTRIBUTE_HIDDEN
                if attrs & FILE_ATTRIBUTE_SYSTEM:
                    new_attrs |= FILE_ATTRIBUTE_SYSTEM
                if attrs & FILE_ATTRIBUTE_READONLY:
                    new_attrs |= FILE_ATTRIBUTE_READONLY
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(dst, new_attrs)
    except (OSError, AttributeError):
        pass



def count_files_recursive(directory: Path) -> int:
    count = 0
    for item in directory.rglob('*'):
        if item.is_file():
            count += 1
    return count



def verify_copy_result(source_dir: Path, target_dir: Path) -> tuple[bool, str]:
    if not target_dir.exists():
        return False, f"验证失败: 目标目录不存在 {target_dir}"

    source_file_count = count_files_recursive(source_dir)
    target_file_count = count_files_recursive(target_dir)

    if source_file_count != target_file_count:
        return False, f"验证失败: 文件数量不一致，期望 {source_file_count}，实际 {target_file_count}"

    return True, f"验证通过: 目标目录存在，文件数量一致 ({target_file_count} 个文件)"



def copy_directory(
    source_dir: str,
    data1_drive_letter: str,
    disk_number: int,
) -> dict[str, Any]:
    source_path = Path(source_dir)
    
    # 路径遍历防护：检查源路径是否包含危险的路径组件
    if ".." in source_dir:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"源路径包含不安全的路径组件 '..': {source_dir}",
        }
    
    if not source_path.exists():
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"源目录不存在: {source_dir}",
        }

    if not source_path.is_dir():
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"源路径不是目录: {source_dir}",
        }

    try:
        resolved_source = source_path.resolve()
    except (OSError, ValueError) as exc:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"源路径解析失败: {exc}",
        }

    if not data1_drive_letter:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "Data1 分区盘符为空，无法拷贝",
        }

    system_drive = get_system_drive_letter()
    if data1_drive_letter.upper() == system_drive:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"拒绝向系统盘 {system_drive}: 写入数据",
        }

    target_root = Path(f"{data1_drive_letter}:\\")
    target_dir = target_root / source_path.name
    
    # 路径遍历防护：检查目标路径是否在预期的根目录下
    try:
        resolved_target = target_dir.resolve()
        resolved_root = target_root.resolve()
        if resolved_root not in resolved_target.parents and resolved_target != resolved_root:
            return {
                "disk_number": disk_number,
                "passed": False,
                "message": f"目标路径不在预期的根目录下: {target_dir}",
            }
    except (OSError, ValueError) as exc:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"目标路径解析失败: {exc}",
        }

    try:
        if target_dir.exists():
            shutil.rmtree(target_dir)

        shutil.copytree(
            str(source_path),
            str(target_dir),
            copy_function=copy_file_with_attributes,
        )
    except Exception as exc:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"目录拷贝失败: {exc}",
        }

    verified, verify_message = verify_copy_result(source_path, target_dir)
    if not verified:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"目录拷贝后验证失败: {verify_message}",
        }

    return {
        "disk_number": disk_number,
        "passed": True,
        "message": f"目录拷贝成功: {verify_message}",
    }



def print_copy_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "通过" if result.get("passed") else "失败"
        print(f"硬盘 {result.get('disk_number')} 目录拷贝{status}: {result.get('message')}")
