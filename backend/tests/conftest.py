"""测试全局约定：LLM registry 只走 .env 兜底，隔离远程 DATABASE_URL。

本仓库 .env 的 DATABASE_URL 指向远程 Postgres；registry 默认按 30s TTL 查
llm_bindings 快照。若测试进程直连远程库，会引入网络依赖与不确定性（CI/离线
环境甚至会长时间挂起），故整个测试会话统一关闭 DB 查询——resolve 退化为读
settings，与既有测试打桩 llm_configured/monkeypatch settings 的口径一致。

registry 自身的测试在用例内用 llm_registry.set_db_enabled(True) + 本地
SQLite 会话工厂单独打开，结束后由本 fixture 恢复关闭态。
"""

from __future__ import annotations

import pytest

from app.services import llm_registry


@pytest.fixture(autouse=True)
def _llm_registry_env_only():
    llm_registry.set_db_enabled(False)
    yield
    llm_registry.clear_cache()
    llm_registry.set_db_enabled(False)  # 兜底：用例内开启后未恢复也不外泄
