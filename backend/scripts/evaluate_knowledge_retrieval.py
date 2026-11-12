"""知识库检索固定评测集运行器（P4.1）。

用法（在 backend 目录）：
  uv run python scripts/evaluate_knowledge_retrieval.py \
      --manifest ../docs/evaluation/knowledge-base/dataset-v1.jsonl \
      --user teacher-a --limit 8

  # 从当前库的已发布内容生成占位模板（需人工核对后替换成真实问题）
  uv run python scripts/evaluate_knowledge_retrieval.py \
      --write-template ../docs/evaluation/knowledge-base/dataset-template.jsonl

说明：报告只衡量证据召回、引用支持、拒答与耗时，不代表真实教师使用效果。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.schema import (  # noqa: E402
    User,
    knowledge_blocks,
    knowledge_chunks,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_parse_runs,
)
from app.services.knowledge_evaluation import score_case, summarize  # noqa: E402
from app.services.knowledge_search_service import search  # noqa: E402
from app.services.knowledge_space_service import KnowledgeError  # noqa: E402


def _read_manifest(path: Path) -> list[dict]:
    items = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no} JSON 解析失败: {exc}") from exc
    return items


def _write_template(session, out: Path, limit: int) -> None:
    rows = session.execute(
        select(
            knowledge_documents.c.id.label("document_id"),
            knowledge_documents.c.title,
            knowledge_document_versions.c.id.label("version_id"),
            knowledge_document_versions.c.version_no,
            knowledge_blocks.c.block_index,
            knowledge_blocks.c.page_index,
            knowledge_blocks.c.heading_path,
            knowledge_blocks.c.text,
        )
        .join(
            knowledge_document_versions,
            (knowledge_document_versions.c.document_id == knowledge_documents.c.id)
            & (knowledge_document_versions.c.school_id == knowledge_documents.c.school_id)
            & (knowledge_document_versions.c.space_id == knowledge_documents.c.space_id),
        )
        .join(
            knowledge_parse_runs,
            (knowledge_parse_runs.c.version_id == knowledge_document_versions.c.id)
            & (knowledge_parse_runs.c.school_id == knowledge_document_versions.c.school_id),
        )
        .join(
            knowledge_blocks,
            (knowledge_blocks.c.parse_run_id == knowledge_parse_runs.c.id)
            & (knowledge_blocks.c.school_id == knowledge_parse_runs.c.school_id),
        )
        .where(
            knowledge_documents.c.status == "published",
            knowledge_documents.c.published_version_id == knowledge_document_versions.c.id,
            knowledge_parse_runs.c.state == "ready",
        )
        .order_by(knowledge_documents.c.title, knowledge_blocks.c.block_index)
        .limit(limit)
    ).mappings().all()
    if not rows:
        raise SystemExit("当前库没有已发布且解析完成的资料，无法生成模板。")

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows, start=1):
            heading = " / ".join(row.heading_path or [])
            item = {
                "id": f"template-{index:03d}",
                "question": (heading or row.title) + " 的主要内容是什么？",
                "space_ids": [],
                "expected": [
                    {
                        "document_id": row.document_id,
                        "document_title": row.title,
                        "version_no": row.version_no,
                        "block_index": row.block_index,
                        "page_index": row.page_index,
                        "heading": heading or None,
                        "quote": (row.text or "")[:60],
                    }
                ],
                "must_refuse": False,
                "tags": ["synthetic", "needs-review"],
                "note": "自动生成的占位问题，必须人工核对并改写后再用于验收。",
            }
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"已生成 {len(rows)} 条占位模板：{out}")
    print("注意：这是合成占位数据，不能作为真实评测结果或教师使用效果。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="JSONL 评测集路径")
    parser.add_argument("--write-template", type=Path, help="从当前库生成占位模板后退出")
    parser.add_argument("--template-limit", type=int, default=20)
    parser.add_argument("--user", default="", help="账号 id 或 username")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    database_url = args.database_url or "sqlite:///./paperpact.db"
    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with factory() as session:
            if args.write_template:
                _write_template(session, args.write_template, args.template_limit)
                return 0
            if not args.manifest or not args.manifest.exists():
                raise SystemExit("请提供存在的 --manifest，或使用 --write-template 生成模板。")
            if not args.user:
                raise SystemExit("请用 --user 指定有权限访问目标知识库的账号。")

            user = session.get(User, args.user) or session.execute(
                select(User).where(User.username == args.user)
            ).scalar_one_or_none()
            if user is None:
                raise SystemExit(f"账号不存在：{args.user}")

            cases = _read_manifest(args.manifest)
            results: list[dict] = []
            latencies: list[float] = []
            errors = 0
            for case in cases:
                started = time.perf_counter()
                try:
                    outcome = search(
                        session,
                        user,
                        query=str(case.get("question") or ""),
                        space_ids=list(case.get("space_ids") or []),
                        limit=max(1, min(int(args.limit), 20)),
                    )
                    hits = outcome["hits"]
                except KnowledgeError as exc:
                    errors += 1
                    results.append(
                        {
                            "id": case.get("id"),
                            "question": case.get("question"),
                            "tags": list(case.get("tags") or []),
                            "error": exc.message,
                        }
                    )
                    continue
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                scored = score_case(case, hits)
                scored["latency_ms"] = round(elapsed, 2)
                scored["top_hits"] = [
                    {
                        "document_title": hit.get("document_title"),
                        "version_no": hit.get("version_no"),
                        "page_index": hit.get("page_index"),
                        "heading_path": hit.get("heading_path"),
                        "score": hit.get("score"),
                    }
                    for hit in hits[:3]
                ]
                results.append(scored)

            summary = summarize([row for row in results if "error" not in row], latencies)
            summary["errors"] = errors
            report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "manifest": str(args.manifest),
                "database": engine.url.render_as_string(hide_password=True),
                "user": user.username,
                "limit": args.limit,
                "summary": summary,
                "cases": results,
            }
            out = args.out or (
                Path("../docs/evaluation/knowledge-base")
                / f"report-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print(f"报告已写入：{out.resolve()}")
            return 0 if errors == 0 else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
