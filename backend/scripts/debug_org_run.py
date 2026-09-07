"""Debug: inspect organization run state and candidate payload.

用法（在本机执行，本机 IP 需在服务器 pg_hba.conf 白名单内）：
    D:\\Program Files\\Python\\Python314\\python.exe backend\\scripts\\debug_org_run.py [run_id]
默认检查最近一次 organization run；传参可指定 run_id。
依赖 psycopg3（Python 3.14 已装）；连接串从项目根目录 .env 的 DATABASE_URL 读取。
"""
import json
import os
import re
import sys

import psycopg

COURSE_ID = "133267b3-3313-4967-99ca-6e1b3fdee2ae"
RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "898b9d2ab298463bb6300b935391d25b"

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
db_url = None
for line in open(env_path, encoding="utf-8"):
    m = re.match(r"^\s*DATABASE_URL\s*=\s*(.*)\s*$", line)
    if m:
        db_url = m.group(1).strip().strip('"').strip("'")
        break
assert db_url, "DATABASE_URL not found in .env"

dsn = db_url.replace("postgresql+psycopg://", "postgresql://", 1)
conn = psycopg.connect(dsn)
conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor()


def q(sql, params=None):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


print(f"=== organization_runs (course={COURSE_ID}, latest 10) ===")
rows = q(
    """SELECT id, status, error_code, left(error_message, 300) AS error_message,
              created_at, updated_at, completed_at
       FROM organization_runs
       WHERE course_id = %s
       ORDER BY created_at DESC LIMIT 10""",
    (COURSE_ID,),
)
for r in rows:
    print(json.dumps(r, ensure_ascii=False, default=str))

print(f"\n=== organization_runs (target run={RUN_ID}) ===")
rows = q("""SELECT * FROM organization_runs WHERE id = %s""", (RUN_ID,))
for r in rows:
    for k, v in r.items():
        if isinstance(v, (dict, list)):
            print(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)[:800]}")
        else:
            print(f"{k}: {v}")

print("\n=== knowledge_tree_candidates for run ===")
rows = q(
    """SELECT id, status, version_no, created_at, payload
       FROM knowledge_tree_candidates
       WHERE organization_run_id = %s ORDER BY created_at DESC""",
    (RUN_ID,),
)
for r in rows:
    payload = r.pop("payload")
    print(json.dumps(r, ensure_ascii=False, default=str))
    if payload:
        p = payload if isinstance(payload, dict) else json.loads(payload)
        topics = p.get("topics") or []
        units = [u for t in topics for u in (t.get("units") or [])]
        cards = [c for u in units for c in (u.get("cards") or [])]
        print(f"  payload topics={len(topics)} units={len(units)} cards={len(cards)}")
        print(f"  payload keys={list(p.keys())[:20]}")
    else:
        print("  payload NULL")

print("\n=== exam_point_evidence_links for run (by relevance) ===")
rows = q(
    """SELECT relevance_class, count(*) AS n FROM exam_point_evidence_links
       WHERE organization_run_id = %s GROUP BY relevance_class ORDER BY n DESC""",
    (RUN_ID,),
)
print(rows or "none")

print("\n=== model_calls for run (token usage by scope) ===")
try:
    rows = q(
        """SELECT scope, status, count(*) AS calls,
                  sum(tokens_in) AS in_tok, sum(tokens_out) AS out_tok,
                  sum(tokens_in + tokens_out) AS total_tok
           FROM model_calls
           WHERE organization_run_id = %s
           GROUP BY scope, status ORDER BY total_tok DESC NULLS LAST""",
        (RUN_ID,),
    )
    for r in rows:
        print(json.dumps(r, ensure_ascii=False, default=str))
except Exception as exc:
    print("model_calls query failed:", exc)

print("\n=== published catalog for course ===")
rows = q(
    """SELECT id, status, version_no, created_at, organization_run_id
       FROM knowledge_catalog_versions
       WHERE course_id = %s ORDER BY created_at DESC LIMIT 5""",
    (COURSE_ID,),
)
for r in rows:
    print(json.dumps(r, ensure_ascii=False, default=str))

cur.close()
conn.close()
