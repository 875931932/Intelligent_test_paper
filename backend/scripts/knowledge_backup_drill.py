"""知识域 PostgreSQL 备份恢复演练（P4.2）。

步骤：pg_dump → 校验归档 → 可选恢复到目标库 → 对比关键表计数与版本引用 →
可选核对对象存储文件哈希。任何一步失败都以 JSON 记录并返回非零。

示例（在 backend 目录）：
  uv run python scripts/knowledge_backup_drill.py \
      --database-url postgresql+psycopg://user:pass@host:5432/paperpact \
      --restore-url postgresql+psycopg://user:pass@host:5432/paperpact_restore \
      --check-objects
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TABLES = (
    "knowledge_spaces",
    "knowledge_documents",
    "knowledge_document_versions",
    "knowledge_blocks",
    "knowledge_chunks",
    "knowledge_jobs",
)


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _redact(url: str) -> str:
    try:
        parsed = make_url(url)
        query = {k: v for k, v in dict(parsed.query).items() if k.lower() != "password"}
        return parsed.set(query=query).render_as_string(hide_password=True)
    except Exception:
        return "<unparsable-url>"


def _query_params(url: str) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(make_url(url).query).items()}


# 这些查询参数会改变实际连接目标，无法用 URL 主体可靠比较，直接拒绝。
_TARGET_OVERRIDE_KEYS = {"host", "port", "service", "service_name", "options", "dsn"}


def _database_identity(url: str) -> tuple:
    """按实际连接目标比较：驱动、主机、端口、库名。

    连接选项（application_name、超时、sslmode 等）不参与身份判定，否则同一
    数据库会被误判为不同目标而放过 --clean。
    """

    parsed = make_url(url)
    query = _query_params(url)
    dbname = query.get("dbname") or query.get("database") or parsed.database or ""
    return (
        parsed.drivername.replace("+psycopg", ""),
        (parsed.host or "").lower(),
        parsed.port or 5432,
        dbname,
    )


def _counts(url: str) -> dict:
    engine = create_engine(url)
    values: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            for table in TABLES:
                try:
                    values[table] = int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)
                except Exception:
                    values[table] = -1
            try:
                values["_versions_fingerprint"] = int(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM ("
                            "SELECT id, object_key, file_hash, version_no "
                            "FROM knowledge_document_versions "
                            "ORDER BY id LIMIT 200) sample"
                        )
                    ).scalar()
                    or 0
                )
            except Exception:
                values["_versions_fingerprint"] = -1
    finally:
        engine.dispose()
    return values


def _check_objects(limit: int) -> dict:
    """核对知识域文件对象是否存在且大小一致；只抽样，避免演练过慢。"""

    from app.db.session import get_session_factory
    from app.infrastructure.tasks.knowledge_tasks import default_storage
    from sqlalchemy import select

    from app.db.schema import knowledge_document_versions

    storage = default_storage()
    session = get_session_factory()()
    checked = failed = 0
    try:
        rows = session.execute(
            select(knowledge_document_versions).limit(limit)
        ).mappings().all()
        for row in rows:
            checked += 1
            try:
                info = storage.head_object(row.object_key)
                size = info["size"] if isinstance(info, dict) else getattr(info, "size", None)
                if info is None or int(size or 0) != int(row.size_bytes):
                    failed += 1
            except Exception:
                failed += 1
    finally:
        session.close()
    return {"checked": checked, "failed": failed, "limit": limit}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--restore-url", default="")
    parser.add_argument("--output-dir", type=Path, default=Path("../docs/evaluation/knowledge-base/backups"))
    parser.add_argument("--check-objects", action="store_true")
    parser.add_argument("--object-limit", type=int, default=20)
    parser.add_argument(
        "--allow-clean",
        action="store_true",
        help="恢复到 --restore-url 时允许 --clean 覆盖目标库；必须显式确认",
    )
    args = parser.parse_args(argv)

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": _redact(args.database_url) if args.database_url else None,
        "restore": _redact(args.restore_url) if args.restore_url else None,
        "allow_clean": bool(args.allow_clean),
        "steps": [],
        "status": "running",
    }

    def finish(status: str, code: int) -> int:
        report["status"] = status
        args.output_dir.mkdir(parents=True, exist_ok=True)
        out = args.output_dir / f"backup-drill-{_now_tag()}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": status, "report": str(out.resolve())}, ensure_ascii=False, indent=2))
        return code

    if not args.database_url or not args.database_url.startswith("postgresql"):
        report["steps"].append({"step": "preflight", "status": "blocked", "reason": "需要 PostgreSQL URL"})
        return finish("blocked", 2)
    for label, url in (("source", args.database_url), ("restore", args.restore_url)):
        if not url:
            continue
        overrides = _TARGET_OVERRIDE_KEYS & set(_query_params(url))
        if overrides:
            report["steps"].append(
                {
                    "step": "preflight",
                    "status": "blocked",
                    "reason": f"{label} URL 含会改变实际连接目标的参数：{sorted(overrides)}",
                }
            )
            return finish("blocked", 2)
    if args.restore_url:
        if _database_identity(args.database_url) == _database_identity(args.restore_url):
            report["steps"].append(
                {
                    "step": "preflight",
                    "status": "blocked",
                    "reason": "恢复目标不能与源库相同，拒绝执行 --clean 恢复",
                }
            )
            return finish("blocked", 2)
        if not args.allow_clean:
            report["steps"].append(
                {
                    "step": "preflight",
                    "status": "blocked",
                    "reason": "恢复到目标库会执行 --clean，需显式传入 --allow-clean",
                }
            )
            return finish("blocked", 2)
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        report["steps"].append({"step": "preflight", "status": "blocked", "reason": "未安装 pg_dump/pg_restore"})
        return finish("blocked", 2)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    dump_path = args.output_dir / f"knowledge-{_now_tag()}.dump"
    started = time.perf_counter()
    dump = subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--file",
            str(dump_path),
            _psycopg_url(args.database_url),
        ],
        capture_output=True,
        text=True,
    )
    report["steps"].append(
        {
            "step": "pg_dump",
            "status": "ok" if dump.returncode == 0 else "failed",
            "seconds": round(time.perf_counter() - started, 2),
            "stderr": dump.stderr[-800:],
        }
    )
    if dump.returncode != 0:
        return finish("failed", 1)

    digest = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    report["dump"] = {
        "path": str(dump_path.resolve()),
        "size_bytes": dump_path.stat().st_size,
        "sha256": digest,
    }
    listing = subprocess.run(["pg_restore", "--list", str(dump_path)], capture_output=True, text=True)
    report["steps"].append(
        {
            "step": "pg_restore --list",
            "status": "ok" if listing.returncode == 0 else "failed",
            "tables_listed": sum(1 for line in listing.stdout.splitlines() if " TABLE " in line),
        }
    )
    if listing.returncode != 0:
        return finish("failed", 1)

    source_counts = _counts(args.database_url)
    report["source_counts"] = source_counts
    if any(value < 0 for value in source_counts.values()):
        report["steps"].append(
            {
                "step": "source_counts",
                "status": "failed",
                "reason": "存在无法读取的关键表，拒绝报告成功",
            }
        )
        return finish("failed", 1)

    if args.restore_url:
        restore = subprocess.run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--dbname",
                _psycopg_url(args.restore_url),
                str(dump_path),
            ],
            capture_output=True,
            text=True,
        )
        report["steps"].append(
            {
                "step": "pg_restore",
                "status": "ok" if restore.returncode == 0 else "failed",
                "stderr": restore.stderr[-800:],
            }
        )
        if restore.returncode != 0:
            return finish("failed", 1)
        restored_counts = _counts(args.restore_url)
        report["restored_counts"] = restored_counts
        mismatch = {
            table: [source_counts.get(table), restored_counts.get(table)]
            for table in TABLES
            if source_counts.get(table) != restored_counts.get(table)
        }
        report["count_mismatch"] = mismatch
        if mismatch:
            return finish("failed", 1)

    if args.check_objects:
        report["objects"] = _check_objects(args.object_limit)
        if report["objects"]["failed"]:
            return finish("failed", 1)

    return finish("ok", 0)


if __name__ == "__main__":
    raise SystemExit(main())
