import pytest


@pytest.fixture(autouse=True)
def stub_target_resource_resolution(monkeypatch):
    """让普通测试不依赖真实磁盘布局。

    资源路径位于目标盘的保护逻辑由专门的安全测试覆盖；其余流程测试
    默认把所有资源视为位于系统盘 0。
    """
    monkeypatch.setattr("app.orchestrator.resolve_path_disk_number", lambda path: 0)
    monkeypatch.setattr("app.orchestrator.validate_drive_letter_ownership", lambda *args, **kwargs: None)
