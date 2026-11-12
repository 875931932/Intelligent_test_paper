# 知识库评测与容量演练

本目录保存固定评测集、运行报告与容量/备份演练记录。所有结论必须与执行环境和数据版本一起引用。

## 当前状态（2026-11-12）

| 项目 | 状态 | 说明 |
|---|---|---|
| 评测运行器 | 已完成 | `backend/scripts/evaluate_knowledge_retrieval.py` |
| 打分逻辑单测 | 已完成 | `backend/tests/unit/test_knowledge_evaluation.py` |
| 100 条人工核对题 | **未建立** | 需要基于真实教学资料人工出题与核对，禁止用合成问题冒充 |
| 70/30 开发/保留拆分 | **未拆分** | 待真实题目建立后按标签与难度拆分 |
| PostgreSQL 1万/5万/10万块测量 | **未执行** | 脚本已就绪；本机无 PostgreSQL 实例，运行会标记 BLOCKED |
| 数据库恢复演练 | **未执行** | 需要 `pg_dump`/`pg_restore` 与隔离恢复库 |
| 对象存储哈希核对 | **未执行** | 需要可访问的对象存储凭据 |

## 评测集格式（JSONL，一行一题）

```json
{"id":"q001","question":"二叉树和哈希表在查找方式上有什么区别？","space_ids":["space-id"],"expected":[{"document_id":"doc-id","document_title":"数据结构讲义","version_no":2,"page_index":3,"heading":"树","quote":"最多有两个子结点"}],"must_refuse":false,"tags":["dev","concept"]}
```

- `expected` 为空且 `must_refuse=true` 表示应当拒答（资料中没有依据）。
- `space_ids` 为空时按该账号全部有权空间检索。
- `quote` 用于引用支持判定，取证据原文的连续片段。

## 运行

```powershell
cd backend
# 从当前库生成占位模板（合成占位，必须人工改写后才能用于验收）
uv run python scripts/evaluate_knowledge_retrieval.py --write-template ../docs/evaluation/knowledge-base/dataset-template.jsonl

# 运行评测
uv run python scripts/evaluate_knowledge_retrieval.py `
  --manifest ../docs/evaluation/knowledge-base/dataset-v1.jsonl `
  --user teacher-a --limit 8
```

指标：证据召回（命中的期望证据数 / 期望证据总数，支持文档/版本/页码/块号）、MRR、原文片段可定位率（前 5 条里能否找到标注片段）、拒答准确率、P50/P95 耗时、按标签分组。答案级“引用支持”尚未评估，报告中固定为 `null`；`quote_locatable` 不能替代它。报告不衡量答案措辞质量，也不代表真实教师使用效果。

## PostgreSQL 容量测量

```powershell
cd backend
uv run python scripts/knowledge_pg_capacity.py `
  --database-url "postgresql+psycopg://user:pass@localhost:5432/paperpact"
```

脚本在独立 schema 中构造 1万/5万/10万块，分别测量 GIN 词面召回、1% 空间过滤与关闭索引后的顺序扫描，结束后删除 schema。报告写入本目录 `pg-capacity-*.json`，并记录“热缓存、单线程”限制。

## 备份恢复演练

```powershell
cd backend
uv run python scripts/knowledge_backup_drill.py `
  --database-url "postgresql+psycopg://user:pass@host:5432/paperpact" `
  --restore-url "postgresql+psycopg://user:pass@host:5432/paperpact_restore" `
  --allow-clean --check-objects
```

流程：`pg_dump -Fc` → 记录 SHA-256 → `pg_restore --list` 校验归档 → 恢复到隔离库 → 对比 `knowledge_*` 关键表计数 → 抽样核对对象存储文件大小。恢复目标与源库相同会直接拒绝；覆盖目标库必须显式传 `--allow-clean`；报告中的连接串已脱敏；关键表计数读取失败（-1）时脚本失败，不会报告成功。
