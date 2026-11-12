"""PostgreSQL 知识块容量测量（P4.2）。

只在 PostgreSQL 上运行；SQLite 直接退出并标记 BLOCKED。脚本使用独立
schema，不触碰业务表。

示例（在 backend 目录）：
  uv run python scripts/knowledge_pg_capacity.py \
      --database-url postgresql+psycopg://user:pass@localhost:5432/paperpact

输出：docs/evaluation/knowledge-base/pg-capacity-<timestamp>.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _timed(conn, sql: str, params: dict, repeats: int = 3) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        conn.execute(text(sql), params).fetchall()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 2)


def _explain(conn, sql: str, params: dict) -> str:
    rows = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql), params).fetchall()
    return "\n".join(row[0] for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--sizes", default="10000,50000,100000")
    parser.add_argument("--out-dir", type=Path, default=Path("../docs/evaluation/knowledge-base"))
    args = parser.parse_args(argv)

    if not args.database_url:
        print("BLOCKED: 未提供 --database-url 或 DATABASE_URL")
        return 2
    if not args.database_url.startswith("postgresql"):
        print("BLOCKED: 容量测量只支持 PostgreSQL（当前不是 postgresql URL）")
        return 2

    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    engine = create_engine(args.database_url)
    if engine.dialect.name != "postgresql":
        print("BLOCKED: 驱动未识别为 PostgreSQL")
        return 2

    schema = f"knowledge_bench_{_now_tag().lower()}"
    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": engine.url.render_as_string(hide_password=True),
        "schema": schema,
        "sizes": sizes,
        "measurements": [],
        "limitations": "单机、热缓存、单线程顺序查询；过滤比例为单空间占比（随规模变化，报告中记录实际值）；未覆盖真实并发与冷启动。",
    }
    index_ddl = (
        "CREATE INDEX ix_bench_lexical_gin ON chunks USING gin "
        "(to_tsvector('simple', coalesce(lexical_tsv, '')))"
    )
    query = "SELECT id FROM chunks WHERE to_tsvector('simple', coalesce(lexical_tsv, '')) @@ plainto_tsquery('simple', :q)"
    filtered_query = (
        "SELECT id FROM chunks WHERE school_id = :school AND space_id = :space "
        "AND to_tsvector('simple', coalesce(lexical_tsv, '')) @@ plainto_tsquery('simple', :q)"
    )

    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {schema}"))
            conn.execute(text(f"SET search_path TO {schema}"))
            conn.execute(
                text(
                    "CREATE TABLE chunks ("
                    "id text PRIMARY KEY, school_id text NOT NULL, space_id text NOT NULL, "
                    "build_id text NOT NULL, chunk_index integer NOT NULL, "
                    "text text NOT NULL, lexical_tsv text NOT NULL)"
                )
            )
            conn.execute(
                text("CREATE INDEX ix_bench_scope ON chunks (school_id, space_id, build_id)")
            )

        inserted = 0
        with engine.begin() as conn:
            conn.execute(text(f"SET search_path TO {schema}"))
            for size in sizes:
                batch = []
                for index in range(inserted, size):
                    # 每个空间 10000 块；查询词覆盖多个空间，过滤查询取单一空间（约 1%）。
                    space = f"space-{index // 10000}"
                    batch.append(
                        {
                            "id": f"c-{index}",
                            "school": "school-bench",
                            "space": space,
                            "build": f"build-{index // 10000}",
                            "idx": index % 10000,
                            "body": f"术语{index % 500} 教学内容 示例 {index}",
                            "tsv": f"术语{index % 500} 教学内容 示例 {index}",
                        }
                    )
                    if len(batch) >= 2000:
                        conn.execute(
                            text(
                                "INSERT INTO chunks (id, school_id, space_id, build_id, chunk_index, text, lexical_tsv) "
                                "VALUES (:id, :school, :space, :build, :idx, :body, :tsv)"
                            ),
                            batch,
                        )
                        batch.clear()
                if batch:
                    conn.execute(
                        text(
                            "INSERT INTO chunks (id, school_id, space_id, build_id, chunk_index, text, lexical_tsv) "
                            "VALUES (:id, :school, :space, :build, :idx, :body, :tsv)"
                        ),
                        batch,
                    )
                inserted = size
                conn.execute(text("ANALYZE chunks"))
                conn.execute(text(index_ddl))
                conn.execute(text("ANALYZE chunks"))

                with_index = _timed(conn, query, {"q": "术语42 教学内容"})
                filtered = _timed(
                    conn,
                    filtered_query,
                    {"q": "术语42 教学内容", "school": "school-bench", "space": "space-0"},
                )
                plan = _explain(conn, filtered_query, {
                    "q": "术语42 教学内容",
                    "school": "school-bench",
                    "space": "space-0",
                })
                conn.execute(text("DROP INDEX ix_bench_lexical_gin"))
                seq_scan = _timed(conn, query, {"q": "术语42 教学内容"})
                # 索引留待下一档重建，避免重复 CREATE INDEX 失败。

                report["measurements"].append(
                    {
                        "chunks": size,
                        "filter_space_ratio": round(min(10000, size) / size, 4),
                        "gin_ms": with_index,
                        "filtered_gin_ms": filtered,
                        "seq_scan_ms": seq_scan,
                        "speedup": round(seq_scan / with_index, 2) if with_index else None,
                        "plan_tail": plan.splitlines()[:6],
                    }
                )
                print(
                    f"{size:>7} chunks  gin={with_index:>8.2f}ms  "
                    f"filtered={filtered:>8.2f}ms  seq={seq_scan:>8.2f}ms"
                )

        args.out_dir.mkdir(parents=True, exist_ok=True)
        out = args.out_dir / f"pg-capacity-{_now_tag()}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{out.resolve()}")
        return 0
    except Exception as exc:  # noqa: BLE001 - 演练脚本需明确报告阻塞原因
        print(f"BLOCKED: {exc}")
        return 2
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        except Exception:
            pass
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
