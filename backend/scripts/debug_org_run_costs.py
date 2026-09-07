"""Diagnose organization run: token spend + empty-catalog root cause.

用法（本机执行，本机 IP 需在服务器 pg_hba.conf 白名单内）：
    "D:\\Program Files\\Python\\Python314\\python.exe" backend\\scripts\\debug_org_run_costs.py 2b1fac37145c43528b046dc9cee22bc8
依赖 psycopg3；连接串从项目根目录 .env 的 DATABASE_URL 读取。

较 debug_org_run.py 的修正/增强：
  - model_calls 表列名是 stage（旧脚本误写为 scope，导致查询被 except 吞掉）
  - 新增 candidate payload 的 failed_pairs 分布、coverage status 分布、
    evidence_decisions 计数（判定“是否分类无产出导致 0 主题/单元/卡”）
  - 新增 model_calls 的 stage × status × error_code token 分布 + 重试放大统计
"""
import json
import os
import re
import sys
from collections import Counter

import psycopg

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "2b1fac37145c43528b046dc9cee22bc8"

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


def dump(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str))


print("=" * 72)
print("target run =", RUN_ID)
print("=" * 72)

print("\n[1] organization_run 状态")
for r in q(
    """SELECT id, status, error_code, left(error_message, 200) AS err,
              created_at, completed_at
       FROM organization_runs WHERE id = %s""",
    (RUN_ID,),
):
    dump(r)

print("\n[2] candidate payload 统计")
rows = q(
    """SELECT payload FROM knowledge_catalog_versions
       WHERE organization_run_id = %s ORDER BY version_no DESC LIMIT 1""",
    (RUN_ID,),
)
if not rows:
    print("  (no candidate row)")
else:
    p = rows[0]["payload"] or {}
    print("  top-level keys:", sorted(p.keys()))
    topics = p.get("topics") or []
    units = [u for t in topics for u in (t.get("units") or [])]
    cards = [c for u in units for c in (u.get("cards") or [])]
    print(f"  topics={len(topics)} units={len(units)} cards={len(cards)}")

    decs = p.get("evidence_decisions") or []
    print(f"  evidence_decisions={len(decs)}")
    if decs:
        rc = Counter(d.get("relevance_class") for d in decs)
        print("    by relevance_class:", dict(rc))
    else:
        print("    (空 → 分类阶段没产出任何决策，归并/建卡无米下锅)")

    cov = p.get("coverage") or []
    print("  coverage status 分布:")
    if cov:
        for s, n in Counter(c.get("status") for c in cov).most_common():
            print(f"    {s}: {n}")
    else:
        print("    (coverage 为空)")

    fp = p.get("failed_pairs") or []
    print(f"  failed_pairs={len(fp)}")
    if fp:
        print("    按 (stage, error_code) 计数:")
        for (stage, code), n in Counter(
            (f.get("stage"), f.get("error_code")) for f in fp
        ).most_common(30):
            print(f"    {stage:16s} {code:44s} {n}")
        print("    失败最多的考点 (top 15):")
        for code, n in Counter(f.get("exam_point_code") for f in fp).most_common(15):
            print(f"    {code}: {n}")

print("\n[3] model_calls 按 stage × status × error_code 的 token 分布")
try:
    for r in q(
        """SELECT stage, status, error_code, count(*) AS calls,
                  sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok,
                  sum(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)) AS total_tok
           FROM model_calls
           WHERE organization_run_id = %s
           GROUP BY stage, status, error_code
           ORDER BY total_tok DESC NULLS LAST""",
        (RUN_ID,),
    ):
        print("  ", end="")
        dump(r)
except Exception as exc:
    print("  query failed:", exc)

print("\n[4] 重试放大统计 (调用数 / 重试数 / token)")
try:
    for r in q(
        """SELECT stage, status,
                  count(*) AS calls,
                  sum(COALESCE((details->>'retry_count')::int, 0)) AS total_retries,
                  sum(input_tokens) AS in_tok,
                  sum(output_tokens) AS out_tok
           FROM model_calls
           WHERE organization_run_id = %s
           GROUP BY stage, status
           ORDER BY in_tok DESC NULLS LAST""",
        (RUN_ID,),
    ):
        print("  ", end="")
        dump(r)
except Exception as exc:
    print("  query failed:", exc)

cur.close()
conn.close()