from typing import Any



def validate_initialized_disk_result(initialized_result: dict[str, Any]) -> dict[str, Any]:
    disk_number = initialized_result.get("disk_number")

    if not initialized_result.get("passed"):
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"初始化步骤未通过: {initialized_result.get('message') or '未知错误'}",
        }

    disk = initialized_result.get("disk")
    if not isinstance(disk, dict):
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "初始化结果缺少硬盘信息",
        }

    partition_style = disk.get("partition_style")
    if partition_style != "GPT":
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"初始化后分区表格式不是 GPT: {partition_style}",
        }

    if disk.get("is_boot"):
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "初始化后硬盘仍被标记为启动盘",
        }

    if disk.get("is_system"):
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "初始化后硬盘仍被标记为系统盘",
        }

    if disk.get("is_offline"):
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "初始化后硬盘处于离线状态",
        }

    if disk.get("is_read_only"):
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "初始化后硬盘处于只读状态",
        }

    return {
        "disk_number": disk_number,
        "passed": True,
        "message": "初始化结果验证通过",
    }



def validate_initialized_disks(initialized_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not initialized_results:
        raise ValueError("初始化结果不能为空")

    return [validate_initialized_disk_result(result) for result in initialized_results]



def print_initialization_validation_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "通过" if result.get("passed") else "失败"
        print(f"硬盘 {result.get('disk_number')} 初始化验证{status}: {result.get('message')}")
