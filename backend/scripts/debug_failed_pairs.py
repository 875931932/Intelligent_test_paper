"""Aggregate failed_pairs error codes from candidate payload."""
import json
import urllib.request
from collections import Counter

CID = "133267b3-3313-4967-99ca-6e1b3fdee2ae"
RID = "55f27fd92c994a7580bd509ee30fddd7"
URL = f"http://139.199.205.136/api/v1/courses/{CID}/organization-runs/{RID}/candidate"

with urllib.request.urlopen(URL, timeout=120) as resp:
    data = json.loads(resp.read().decode("utf-8"))

payload = data.get("payload", {})
failed = payload.get("failed_pairs", [])
unmatched = payload.get("unmatched", [])
coverage = payload.get("coverage", [])
decisions = payload.get("evidence_decisions", [])

print("total failed_pairs:", len(failed))
stage_counter = Counter((f.get("stage"), f.get("error_code")) for f in failed)
print("\n=== failed_pairs by (stage, error_code) ===")
for (stage, code), n in stage_counter.most_common():
    print(f"{stage:20s} {code:40s} {n}")

points_with_fail = Counter(f.get("exam_point_code") for f in failed)
print("\n=== exam points with most failures ===")
for code, n in points_with_fail.most_common(15):
    print(f"{code}: {n}")

print("\n=== unmatched ===")
print(json.dumps(unmatched, ensure_ascii=False, indent=1)[:2000])

print("\n=== coverage status distribution ===")
print(Counter(c.get("status") for c in coverage))

print("\n=== evidence_decisions count:", len(decisions))
if decisions:
    print("sample:", json.dumps(decisions[0], ensure_ascii=False)[:500])
