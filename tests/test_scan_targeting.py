"""定向扫描与批量快照：闸门不再每次全盘扫，会话只开一次 PowerShell。"""

from __future__ import annotations

from types import SimpleNamespace

from app.application.service import SispService
from app.modules.disk_info import service as disk_info


def _summary(number: int = 1, letters=("E",)) -> dict:
    return {
        "disk_number": number,
        "model": "Test Disk",
        "serial_number": "SN-1",
        "unique_id": "UID-1",
        "size_bytes": 500 * 1024**3,
        "size_display": "500.00 GB",
        "drive_letters": list(letters),
        "partitions": [],
        "is_boot": False,
        "is_system": False,
        "is_offline": False,
        "is_read_only": False,
    }


def test_disk_numbers_env_normalizes():
    assert disk_info._disk_numbers_env(None) is None
    assert disk_info._disk_numbers_env([3, 1, 3, "2"]) == {"VIX_DISK_NUMBERS": "1,2,3"}
    assert disk_info._disk_numbers_env(["x", True]) is None


def test_scan_disks_filters_injected_scanner():
    service = SispService(
        disk_scanner=lambda: [_summary(1), _summary(2)],
    )
    assert [item["disk_number"] for item in service.scan_disks([2])] == [2]
    assert [item["disk_number"] for item in service.scan_disks()] == [1, 2]


def test_find_disk_summary_asks_only_for_that_disk(monkeypatch):
    calls: list[list[int] | None] = []

    def fake_scan(numbers=None):
        calls.append(numbers)
        return [_summary(3)]

    monkeypatch.setattr(disk_info, "scan_disk_summaries", fake_scan)
    service = SispService()

    found = service._find_disk_summary(3)

    assert found and found["disk_number"] == 3
    assert calls == [[3]]


def test_scan_snapshot_merges_disks_and_letters(monkeypatch):
    payload = {
        "disks": [
            {
                "disk_number": 5,
                "friendly_name": "Disk 5",
                "size_bytes": 100,
                "partitions": [],
                "is_boot": False,
                "is_system": False,
                "is_offline": False,
                "is_read_only": False,
            }
        ],
        "used_letters": ["c", "E", "e"],
    }
    monkeypatch.setattr(disk_info, "_run_json", lambda script, env=None: payload)

    snapshot = disk_info.scan_snapshot()

    assert [disk["disk_number"] for disk in snapshot["disks"]] == [5]
    assert snapshot["used_letters"] == ["C", "E"]


def test_service_snapshot_uses_single_powershell(monkeypatch):
    calls: list[str] = []

    def fake_run(script, env=None):
        calls.append("snapshot")
        return {
            "disks": [{"disk_number": 1, "partitions": []}],
            "used_letters": ["E"],
        }

    monkeypatch.setattr(disk_info, "_run_json", fake_run)

    service = SispService()
    disks, letters = service.scan_snapshot()

    assert calls == ["snapshot"]
    assert disks[0]["disk_number"] == 1
    assert letters == ("E",)


def test_known_letters_skip_secondary_scan(monkeypatch):
    def boom():
        raise AssertionError("给了 known_letters 就不该再扫盘符")

    monkeypatch.setattr(disk_info, "scan_used_drive_letters", boom)
    service = SispService()

    plan = service.build_selection_plan(
        {},
        [_summary(1, letters=())],
        [1],
        known_letters=["C", "D", "E"],
    )

    assert "E" in plan.forbidden_drive_letters
    assert "C" in plan.forbidden_drive_letters


def test_progress_fraction_advances_within_image_step(monkeypatch):
    from app.application.jobs import JobManager

    manager = JobManager(executor=lambda **kwargs: None)
    job = SimpleNamespace(
        id="job-1",
        progress={
            "total": 7,
            "completed": ["efi"],
            "current": "image",
            "percent": 14,
            "message": "",
        },
        updated_at=None,
    )
    report = manager._make_reporter(job)

    report("image", "running", "应用镜像（Ghost） 42%", 0.42)

    # efi 已完成 + image 内部 42%：进度应落在 63%，而不是停在 14%/57%。
    assert job.progress["percent"] == 63
    assert job.progress["current"] == "image"

    report("image", "running", "应用镜像（Ghost） 10%", 0.10)
    assert job.progress["percent"] == 63  # 只前进不后退


# ------------------------------------------------- workflow：会话快照的失效
class _SnapshotService:
    """只实现 prepare 用到的两个方法：快照扫描 + 盘符分配。"""

    def __init__(self) -> None:
        self.allowed_disk_numbers = frozenset()
        self.plan_kwargs: dict | None = None

    def scan_protected_snapshot(self):
        from app.application.contracts import DiskInfo

        disk = DiskInfo(
            disk_number=1,
            model="test disk",
            serial_number=None,
            unique_id="unique-1",
            size_bytes=60 * 1024**3,
            size_display="60 GB",
            partition_style="RAW",
            bus_type="NVMe",
            drive_letters=(),
            is_boot=False,
            is_system=False,
            is_offline=False,
            is_read_only=False,
            is_selectable=True,
            protection_reasons=(),
        )
        return ([disk], ["E", "F"])

    def scan_protected_disks(self, excluded_names=None):
        raise AssertionError("有 scan_protected_snapshot 时不该走旧接口")

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        self.plan_kwargs = dict(kwargs)
        letters = {number: {"efi": "E", "windows": "F", "data": "G"} for number in selected}
        return SimpleNamespace(drive_letter_allocations=letters)


def _workflow(service):
    from app.application.workflow import WorkflowController
    from app.models import WorkerResult

    class _Service(_SnapshotService):
        def execute_spec(self, spec, cancel_event):
            return WorkerResult(disk_number=int(getattr(spec, "disk_number", 0) or 0), success=True, message="ok")

    actual = _Service()
    actual.__dict__.update(service.__dict__)
    controller = WorkflowController(
        service=actual,
        preflight_runner=lambda **kwargs: {
            "results": [{"name": "运行前检查", "passed": True, "message": "ok"}]
        },
        partition_defaults={
            "efi_size_mb": 300,
            "c_size_gb": 10,
            "allow_small_partitions": True,
        },
    )
    return controller, actual


def test_prepare_reuses_session_letters_until_a_job_runs():
    service = _SnapshotService()
    controller, actual = _workflow(service)

    controller.start()
    assert controller.session is not None
    assert controller.session.used_letters == ("E", "F")

    controller.prepare([1])
    assert actual.plan_kwargs is not None
    assert actual.plan_kwargs["known_letters"] == ("E", "F")


def test_prepare_falls_back_to_fresh_scan_after_jobs_exist(monkeypatch):
    """跑过任务后 EFI 盘符可能保留，会话快照会过期，必须回到现场扫描。"""
    service = _SnapshotService()
    controller, actual = _workflow(service)
    controller.start()
    monkeypatch.setattr(controller._manager, "jobs", lambda: [object()])

    controller.prepare([1])

    assert actual.plan_kwargs is not None
    assert actual.plan_kwargs["known_letters"] is None


def test_snapshot_script_does_not_echo_letters_before_json():
    """合并脚本时盘符只能进 JSON；直接输出会污染 stdout，解析必失败。"""
    emit = [
        line
        for line in disk_info.SNAPSHOT_SCRIPT.splitlines()
        if line.strip().startswith("$letters | Select-Object")
    ]
    assert not emit
    assert "used_letters" in disk_info.SNAPSHOT_SCRIPT
