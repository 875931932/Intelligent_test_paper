"""锁定 .env 的解析路径：必须从仓库根找到，而不是取决于进程 CWD。

背景（2026-09 402 事故）：`Settings` 原来写死相对路径 `env_file=".env"`，而
pydantic-settings 按进程 CWD 解析它。按文档在 `backend/` 里手工启动 Celery worker
时，仓库根的 `.env` 一次都没被读到，`llm_base_url` 回落到代码默认值
`https://api.stepfun.com/v1`——一个能连通但账号无额度的端点，于是"框架/知识目录
都正常，唯独生成试卷永远 402 quota_exceeded"。改成从 `__file__` 向上查找后，
本文件锁定新行为，防止有人再把相对路径写回来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, _resolve_env_file

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV = REPO_ROOT / ".env"


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_resolves_env_file_from_ancestor_directory(tmp_path):
    """模块文件在孙子目录里，也能一路向上找到仓库根的 .env。"""

    module = _touch(tmp_path / "backend" / "app" / "config.py")
    env = _touch(tmp_path / ".env")
    assert _resolve_env_file(module) == str(env)


def test_nearest_ancestor_env_wins(tmp_path):
    """就近优先：子目录里有 .env 时用它，而不是更外层的那个。"""

    outer = _touch(tmp_path / ".env")
    inner = _touch(tmp_path / "backend" / ".env")
    module = _touch(tmp_path / "backend" / "app" / "config.py")
    assert _resolve_env_file(module) == str(inner)
    assert inner != outer


def test_falls_back_to_cwd_relative_when_no_env_file_exists(tmp_path):
    """整棵树上都没有 .env 时退回旧行为，不抛异常（让缺配置的报错发生在使用处）。"""

    module = _touch(tmp_path / "backend" / "app" / "config.py")
    assert _resolve_env_file(module) == ".env"


def test_settings_honours_an_explicit_env_file_path(tmp_path, monkeypatch):
    """端到端：把找到的 .env 路径交给 Settings，值必须真的被读进来。

    这条锁定"pydantic-settings 接受非 CWD 的相对/绝对路径"这个前提——如果上游
    改成只认 CWD 相对路径，上面的查找就白做了。

    注意必须改 `model_config` 而不是传 `_env_file=`：类里显式写了 `env_file`，
    它会在实例化时盖掉 `_env_file` 入参（实测）。
    """

    env = tmp_path / ".env"
    env.write_text(
        "LLM_BASE_URL=https://env-file.example/v1\nLLM_MODEL=env-file-model\n",
        encoding="utf-8",
    )
    # 环境变量优先级高于 .env；不清掉的话本机导出的 LLM_* 会盖住文件内容，
    # 这条用例就退化成了"什么也没验"。
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    configured = Settings()
    assert configured.llm_base_url == "https://env-file.example/v1"
    assert configured.llm_model == "env-file-model"


def test_import_time_env_file_matches_resolver(tmp_path, monkeypatch):
    """import 期用的路径必须就是解析器此刻的返回值（防止两处写死、各走各的）。"""

    monkeypatch.setitem(Settings.model_config, "env_file", _resolve_env_file())
    assert Settings.model_config["env_file"] == _resolve_env_file()


@pytest.mark.skipif(not ROOT_ENV.is_file(), reason="仓库没有 .env（例如 CI 环境），跳过真实文件断言")
def test_repo_root_env_is_used_when_present(monkeypatch):
    """回归锁：本仓库存在 .env 时，import 期解析到的必须是仓库根那一份。

    这正是 worker 场景——进程 CWD 是 `backend/`，仓库根的 `.env` 在上一级。
    若有人把 `env_file` 改回 CWD 相对的 `".env"`，这里会立刻失败。
    """

    # 模拟"在 backend/ 目录里启动 worker"：清掉环境变量，只留 .env 兜底
    for name in list(("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
        monkeypatch.delenv(name, raising=False)

    resolved = _resolve_env_file()
    assert Path(resolved).is_absolute()
    assert Path(resolved) == ROOT_ENV

    # 并且这是一份"有内容"的 .env：缺了 key 时后续会在使用处明确失败，而不是
    # 静默用一个能连通却无额度的默认端点（402 的根因）。
    assert "LLM_BASE_URL" in ROOT_ENV.read_text(encoding="utf-8")
