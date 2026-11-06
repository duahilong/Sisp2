"""后端能力模块：**不依赖编排**的单点业务单元。

这里放"一件事"的完整实现（跑命令、判定、诊断），入参是显式的请求对象，
不是 :class:`~app.application.phases.base.PhaseContext`：

- :mod:`~app.application.capabilities.ghost_apply` —— 把 GHO 还原到指定分区；
- :mod:`~app.application.capabilities.verify_check` —— 检查装机结果能不能开机。

依赖方向是**单向**的：编排层（``phases`` / ``pipeline`` / ``real_runner`` /
``jobs`` / ``service`` / ``gui_web``）可以调用这里的函数，这里**不允许**反向
import 编排层——否则"能力"就又寄生回编排上下文了。这条规则由
``tests/test_capabilities.py`` 里的 AST 守卫钉住。

调用方有三类，共用同一份实现：

1. 统一编排（GUI 走的就是这条）：``phases/ghost.py`` / ``phases/verify.py``
   只做"ctx → 请求 → 结果映射"的薄适配；
2. 后端单点调用（运维脚本、维护工具）：直接拿 ``ProcessShell`` 构造请求调用；
3. 单元测试：注入 fake shell，不需要碰磁盘。
"""

from __future__ import annotations

__all__: list[str] = []
