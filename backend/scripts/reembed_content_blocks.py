# -*- coding: utf-8 -*-
"""按当前清洗版本全库重嵌 content_blocks（bump EMBEDDING_TEXT_VERSION 后执行一次）。

用法：cd backend && uv run python scripts/reembed_content_blocks.py
离线任务（与 Celery worker 同语义）：ensure_embedded 内部调嵌入 API，
绝不在 HTTP 请求线程执行；函数自行 commit、天然幂等（已追平版本的块跳过）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text as sql_text

from app.db.session import get_session_factory
from app.services import content_index_service as cix


def main() -> None:
    if not cix.embedding_configured():
        raise SystemExit("embedding is not configured (EMBEDDING_* missing)")
    with get_session_factory()() as session:
        course_ids = [
            row[0]
            for row in session.execute(
                sql_text(
                    "SELECT DISTINCT course_id FROM document_parse_runs WHERE status='ready'"
                )
            )
        ]
        print(
            f"courses with ready runs: {len(course_ids)} "
            f"(text_version={cix.EMBEDDING_TEXT_VERSION})"
        )
        total = 0
        for course_id in course_ids:
            n = cix.ensure_embedded(session, course_id=course_id)
            total += n
            print(f"  {course_id}: re-embedded {n}", flush=True)
        print(f"total re-embedded: {total}")


if __name__ == "__main__":
    main()
