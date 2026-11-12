"""备份演练脚本的目标库保护与脱敏测试（不执行真实 pg_dump）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "knowledge_backup_drill.py"
_spec = importlib.util.spec_from_file_location("knowledge_backup_drill", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

SOURCE = "postgresql+psycopg://user:secret@localhost:5432/paperpact"
RESTORE = "postgresql+psycopg://user:secret@localhost:5432/paperpact_restore"


def test_redact_hides_password():
    assert "secret" not in mod._redact(SOURCE)
    assert "paperpact" in mod._redact(SOURCE)


def test_same_database_is_blocked(tmp_path):
    code = mod.main(["--database-url", SOURCE, "--restore-url", SOURCE, "--output-dir", str(tmp_path)])
    assert code == 2
    report = json.loads(next(tmp_path.glob("backup-drill-*.json")).read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "不能与源库相同" in report["steps"][0]["reason"]


def test_restore_requires_allow_clean(tmp_path):
    code = mod.main(
        ["--database-url", SOURCE, "--restore-url", RESTORE, "--output-dir", str(tmp_path)]
    )
    assert code == 2
    report = json.loads(next(tmp_path.glob("backup-drill-*.json")).read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "allow-clean" in report["steps"][0]["reason"]
    assert "secret" not in json.dumps(report)


def test_identity_ignores_driver_suffix():
    assert mod._database_identity(SOURCE) == mod._database_identity(
        SOURCE.replace("postgresql+psycopg://", "postgresql://")
    )


def test_dbname_query_parameter_is_part_of_identity(tmp_path):
    a = "postgresql+psycopg://user:secret@localhost:5432/other?dbname=actual"
    b = "postgresql+psycopg://user:secret@localhost:5432/another?dbname=actual"
    assert mod._database_identity(a) == mod._database_identity(b)
    code = mod.main(["--database-url", a, "--restore-url", b, "--output-dir", str(tmp_path)])
    assert code == 2
    report = json.loads(next(tmp_path.glob("backup-drill-*.json")).read_text(encoding="utf-8"))
    assert "不能与源库相同" in report["steps"][0]["reason"]


def test_target_override_query_is_blocked(tmp_path):
    target = "postgresql+psycopg://user:secret@localhost:5432/paperpact?host=prod-db"
    code = mod.main(["--database-url", target, "--output-dir", str(tmp_path)])
    assert code == 2
    report = json.loads(next(tmp_path.glob("backup-drill-*.json")).read_text(encoding="utf-8"))
    assert "host" in report["steps"][0]["reason"]


def test_query_password_is_redacted():
    url = "postgresql+psycopg://user@localhost:5432/db?password=topsecret"
    redacted = mod._redact(url)
    assert "topsecret" not in redacted
    assert "db" in redacted


def test_connection_options_do_not_hide_same_database(tmp_path):
    plain = "postgresql+psycopg://user:secret@localhost:5432/paperpact"
    decorated = plain + "?application_name=restore_probe"
    assert mod._database_identity(plain) == mod._database_identity(decorated)
    code = mod.main(["--database-url", plain, "--restore-url", decorated, "--output-dir", str(tmp_path)])
    assert code == 2
    report = json.loads(next(tmp_path.glob("backup-drill-*.json")).read_text(encoding="utf-8"))
    assert "不能与源库相同" in report["steps"][0]["reason"]
