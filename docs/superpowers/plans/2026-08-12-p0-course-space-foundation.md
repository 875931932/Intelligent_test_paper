# P0 课程空间与安全基础设施 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可运行的后端基础，使教师能够创建相互隔离的课程空间、上传受控资料，并让后台任务与审计记录以课程为边界可靠运行。

**Architecture:** 采用 Python 模块化单体。FastAPI 只处理认证后的 HTTP 请求；业务服务接收明确的 `Actor` 与 `CourseScope`，不信任客户端传来的授权结论。PostgreSQL 保存资源、审计和可恢复任务状态；对象存储只保存文件字节，任何读取均由服务端先做课程授权；Redis 仅负责通知 Worker，数据库任务记录才是事实来源。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 16、Redis 7、S3 兼容对象存储（开发环境 MinIO）、pytest、httpx、testcontainers 或 Docker Compose。

---

## 实施前提与范围

本计划只实现设计文档的 P0：课程隔离、上传原文件、对象存储边界、审计、可重试的异步任务基础和测试夹具。它**不**提取 PDF/PPT/Word 内容、不创建知识点、不调用模型、不生成题目、不实现编辑器或 Word/PDF 导出。资料解析从 P1 开始，且必须只消费本计划定义的 `CourseMaterial` 与 `TaskRun` 契约。

当前工作区仅有设计资料、尚不存在应用代码或 Git 仓库。执行本计划时先在仓库根目录创建以下结构；若实际项目已有约定，以现有约定为准，但不得改变列出的模块责任和 API 契约。

```text
apps/api/
├─ pyproject.toml
├─ alembic.ini
├─ app/
│  ├─ main.py
│  ├─ api/
│  │  ├─ dependencies.py
│  │  └─ routes/{health,courses,materials}.py
│  ├─ core/{config,errors,security}.py
│  ├─ db/{base,session,models}.py
│  ├─ domain/{actors,courses,materials,audit,tasks}.py
│  ├─ services/{authorization,courses,materials,audit,tasks}.py
│  ├─ infrastructure/{object_store,task_queue}.py
│  └─ worker/{main,handlers}.py
├─ migrations/versions/
└─ tests/{conftest,api,services}/
compose.yaml
.env.example
README.md
```

### 固定术语与不可破坏的约束

- `user_id` 是认证后的教师身份；P0 不在 API 中接受可伪造的 `owner_id`。
- `course_id` 是 UUID；一切课程内资源均有不可为空的 `course_id`。
- `Actor` 是服务端认证结果，含 `user_id` 和 `kind`（`teacher`、`admin`、`service`）。
- `CourseScope` 是授权成功后取得的服务端对象，含 `course_id`、`actor` 与访问目的；领域服务只接受此对象，不接受裸 `course_id`。
- `CourseMaterial.status` 初始为 `staged`；P1 的 `OrganizationRun` 启动后，文件版本才可进入 `organizing`、`candidate`、`ready` 等资料整理状态。
- `AuditEvent` 只存元数据、资源 ID、动作、结果和原因；不得写入原文、题干、答案、预签名 URL 或令牌。
- `TaskRun` 使用 `(course_id, task_type, idempotency_key)` 唯一约束；重复请求返回同一任务，不可重复执行产生第二个业务结果。
- 所有对象存储键由服务端生成，格式为 `courses/{course_id}/materials/{material_id}/source/{filename-safe}`；浏览器不能指定键或直接取得桶权限。

## Task 1: 初始化可复现的 API 与本地依赖环境

**Files:**

- Create: `apps/api/pyproject.toml`
- Create: `apps/api/app/main.py`
- Create: `apps/api/app/core/config.py`
- Create: `apps/api/app/api/routes/health.py`
- Create: `apps/api/tests/api/test_health.py`
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `README.md`

- [ ] **Step 1: 写健康检查的失败测试**

```python
# apps/api/tests/api/test_health.py
from fastapi.testclient import TestClient
from app.main import create_app


def test_health_endpoint_reports_service_name() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "exam-paper-api"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/api; python -m pytest tests/api/test_health.py -q`

Expected: FAIL，原因是 `app.main` 尚不存在。

- [ ] **Step 3: 创建最小应用、配置和依赖声明**

`pyproject.toml` 必须固定 Python 版本范围为 `>=3.12,<3.13`，并声明运行依赖 `fastapi`、`uvicorn[standard]`、`pydantic-settings`、`sqlalchemy`、`psycopg[binary]`、`alembic`、`boto3`、`redis`；开发依赖至少含 `pytest`、`pytest-asyncio`、`httpx`、`ruff`、`mypy`。`app/main.py` 只负责创建 `FastAPI(title="AI Final Exam Paper API")` 并挂载 health router；`config.py` 用 `BaseSettings` 读取 `DATABASE_URL`、`REDIS_URL`、`S3_ENDPOINT_URL`、`S3_BUCKET`、`JWT_ISSUER` 和 `JWT_AUDIENCE`，缺少生产必填配置时启动失败。

`compose.yaml` 创建 `postgres:16`、`redis:7` 和 `minio/minio` 三个服务，所有端口和默认开发凭据从 `.env` 读取。README 给出唯一的本地启动路径：复制 `.env.example` 为 `.env`，运行 `docker compose up -d`，安装 `apps/api` 依赖，执行迁移与 pytest。

- [ ] **Step 4: 运行测试确认通过，并检查静态规则**

Run: `cd apps/api; python -m pytest tests/api/test_health.py -q; python -m ruff check app tests`

Expected: 1 passed，ruff 无错误。

- [ ] **Step 5: 提交最小应用骨架**

```bash
git add apps/api compose.yaml .env.example README.md
git commit -m "chore: bootstrap exam paper API"
```

## Task 2: 建立数据库会话、课程模型与迁移

**Files:**

- Create: `apps/api/app/db/base.py`
- Create: `apps/api/app/db/session.py`
- Create: `apps/api/app/db/models.py`
- Create: `apps/api/app/domain/courses.py`
- Create: `apps/api/migrations/env.py`
- Create: `apps/api/migrations/versions/0001_create_courses.py`
- Create: `apps/api/tests/services/test_course_repository.py`
- Modify: `apps/api/app/core/config.py`
- Modify: `apps/api/pyproject.toml`

- [ ] **Step 1: 写课程持久化的失败测试**

```python
# apps/api/tests/services/test_course_repository.py
from uuid import uuid4

from app.domain.courses import CourseCreate, CourseRepository


def test_course_is_owned_by_creator(db_session) -> None:
    owner_id = uuid4()
    course = CourseRepository(db_session).create(
        CourseCreate(code="SK3020", name="大模型调优与部署技术"), owner_id
    )

    assert course.id is not None
    assert course.owner_user_id == owner_id
    assert course.code == "SK3020"
    assert course.archived_at is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/api; python -m pytest tests/services/test_course_repository.py -q`

Expected: FAIL，原因是数据库 fixture 和 `CourseRepository` 尚不存在。

- [ ] **Step 3: 实现数据库边界与首个迁移**

定义 SQLAlchemy `Base`、同步 `SessionLocal` 和测试专用 session fixture。迁移创建 `courses` 表：`id UUID PRIMARY KEY`、`owner_user_id UUID NOT NULL`、`code VARCHAR(64) NOT NULL`、`name VARCHAR(255) NOT NULL`、`created_at TIMESTAMPTZ NOT NULL`、`updated_at TIMESTAMPTZ NOT NULL`、`archived_at TIMESTAMPTZ NULL`，并建立 `(owner_user_id, archived_at)` 索引和同一教师活动课程 `(owner_user_id, code)` 的唯一部分索引。

`CourseCreate` 仅含去除首尾空白后的 `code` 与 `name`；拒绝空值及超过列长度的值。`CourseRepository.create(command, owner_user_id)` 在单个事务内写入，并返回领域对象。禁止在仓储层读取 HTTP 对象或自行推断当前用户。

- [ ] **Step 4: 配置测试数据库并验证迁移**

Run: `cd apps/api; alembic upgrade head; python -m pytest tests/services/test_course_repository.py -q`

Expected: 迁移成功，1 passed。

- [ ] **Step 5: 提交课程数据基础**

```bash
git add apps/api/app apps/api/migrations apps/api/tests apps/api/pyproject.toml
git commit -m "feat: add persisted course spaces"
```

## Task 3: 实现认证主体、课程授权与课程 API

**Files:**

- Create: `apps/api/app/domain/actors.py`
- Create: `apps/api/app/services/authorization.py`
- Create: `apps/api/app/api/dependencies.py`
- Create: `apps/api/app/api/routes/courses.py`
- Create: `apps/api/app/core/errors.py`
- Create: `apps/api/tests/api/test_course_authorization.py`
- Modify: `apps/api/app/main.py`
- Modify: `apps/api/app/db/models.py`

- [ ] **Step 1: 写跨课程拒绝访问的失败测试**

```python
# apps/api/tests/api/test_course_authorization.py
def test_teacher_cannot_read_another_teachers_course(client, teacher_a, teacher_b, course_factory):
    course = course_factory(owner_user_id=teacher_a.id)

    response = client.get(
        f"/v1/courses/{course.id}", headers=teacher_b.auth_headers
    )

    assert response.status_code == 404


def test_administrator_cannot_read_course_content_by_default(client, admin, course_factory):
    course = course_factory()

    response = client.get(f"/v1/courses/{course.id}", headers=admin.auth_headers)

    assert response.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/api; python -m pytest tests/api/test_course_authorization.py -q`

Expected: FAIL，原因是认证依赖与课程路由尚不存在。

- [ ] **Step 3: 实现 Actor、CourseScope 和最小课程 API**

认证依赖验证签名、issuer、audience、过期时间和 subject 后才创建 `Actor`；开发测试令牌只能由测试 fixture 签发。`Actor.kind` 只允许 `teacher`、`admin`、`service`。授权服务提供唯一入口：

```python
def require_course_scope(
    session: Session, actor: Actor, course_id: UUID, purpose: str
) -> CourseScope: ...
```

P0 规则为：`teacher` 仅当 `courses.owner_user_id == actor.user_id` 才可获得 scope；`admin` 对课程内容一律返回不存在；`service` 必须同时传入受服务器签名任务声明绑定的相同 `course_id`；失败时返回 404，且由审计服务记录 `authorization.denied`。成功 scope 不包含文件正文、令牌或模型密钥。

实现端点：`POST /v1/courses`（教师创建）、`GET /v1/courses`（仅列自己未归档课程）、`GET /v1/courses/{course_id}`（需 scope）、`POST /v1/courses/{course_id}/archive`（仅所有者）。响应不得暴露其他教师 ID；所有写操作产生审计事件的接口调用由下一任务补齐。

- [ ] **Step 4: 运行授权、课程与静态检查**

Run: `cd apps/api; python -m pytest tests/api/test_course_authorization.py tests/services/test_course_repository.py -q; python -m ruff check app tests`

Expected: 全部通过；两种未授权访问均为 404，不泄露课程存在性。

- [ ] **Step 5: 提交授权边界**

```bash
git add apps/api/app apps/api/tests
git commit -m "feat: enforce course-scoped authorization"
```

## Task 4: 实现审计事件和不可变写入约束

**Files:**

- Create: `apps/api/app/domain/audit.py`
- Create: `apps/api/app/services/audit.py`
- Create: `apps/api/migrations/versions/0002_create_audit_events.py`
- Create: `apps/api/tests/services/test_audit_service.py`
- Modify: `apps/api/app/db/models.py`
- Modify: `apps/api/app/services/authorization.py`
- Modify: `apps/api/app/api/routes/courses.py`

- [ ] **Step 1: 写审计内容脱敏与不可修改的失败测试**

```python
# apps/api/tests/services/test_audit_service.py
from app.services.audit import AuditService


def test_audit_event_stores_metadata_not_sensitive_payload(db_session, teacher_actor):
    event = AuditService(db_session).record(
        actor=teacher_actor,
        course_id=None,
        action="course.created",
        resource_type="course",
        resource_id="course-1",
        result="success",
        metadata={"name": "机器学习", "token": "must-not-persist"},
    )

    assert event.metadata == {"name": "机器学习"}


def test_audit_event_cannot_be_updated_or_deleted(db_session, audit_event):
    audit_event.result = "changed"
    with pytest.raises(ImmutableAuditEventError):
        db_session.commit()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/api; python -m pytest tests/services/test_audit_service.py -q`

Expected: FAIL，原因是审计模型与写入策略尚不存在。

- [ ] **Step 3: 创建审计表、服务和 ORM 保护**

迁移创建 `audit_events` 表，字段为不可为空的 `id`、`occurred_at`、`actor_kind`、`actor_id`、`action`、`resource_type`、`resource_id`、`result`，可空的 `course_id`、`reason_code`、`metadata JSONB`。为 `(course_id, occurred_at)` 与 `(actor_id, occurred_at)` 建索引。

`AuditService.record` 必须接收显式动作与资源 ID，且只白名单保留 `metadata` 的标量诊断键（如 `request_id`、`task_id`、`file_hash_prefix`、`template_version`）。过滤 key 名包含 `token`、`secret`、`authorization`、`content`、`prompt`、`answer`、`evidence_text` 的值。通过 SQLAlchemy `before_flush` 或数据库权限，阻止已持久化审计事件的 UPDATE/DELETE；应用业务逻辑不得提供删除审计事件 API。

在课程创建、归档、授权成功和授权拒绝路径接入审计。授权拒绝以 `course_id` 作为内部审计关联，但 API 响应仍为 404。

- [ ] **Step 4: 运行审计与原有回归测试**

Run: `cd apps/api; alembic upgrade head; python -m pytest tests/services/test_audit_service.py tests/api/test_course_authorization.py -q`

Expected: 全部通过；敏感 metadata 不会持久化，更新审计记录抛出领域错误。

- [ ] **Step 5: 提交审计能力**

```bash
git add apps/api/app apps/api/migrations apps/api/tests
git commit -m "feat: add immutable course audit events"
```

## Task 5: 建立对象存储端口和课程资料元数据

**Files:**

- Create: `apps/api/app/domain/materials.py`
- Create: `apps/api/app/services/materials.py`
- Create: `apps/api/app/infrastructure/object_store.py`
- Create: `apps/api/app/api/routes/materials.py`
- Create: `apps/api/migrations/versions/0003_create_course_materials.py`
- Create: `apps/api/tests/services/test_material_service.py`
- Create: `apps/api/tests/api/test_material_access.py`
- Modify: `apps/api/app/db/models.py`
- Modify: `apps/api/app/main.py`

- [ ] **Step 1: 写资料路径隔离和删除即停止使用的失败测试**

```python
# apps/api/tests/services/test_material_service.py
def test_material_storage_key_is_generated_inside_its_course(material_service, course_scope):
    material = material_service.create_upload(
        scope=course_scope,
        filename="第3章.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        byte_size=1024,
        sha256="a" * 64,
    )

    assert material.storage_key.startswith(f"courses/{course_scope.course_id}/materials/{material.id}/")


def test_deleted_material_is_not_eligible_for_future_processing(material_service, course_scope, material):
    material_service.delete(scope=course_scope, material_id=material.id)

    assert material_service.get_for_new_processing(course_scope, material.id) is None
```

```python
# apps/api/tests/api/test_material_access.py
def test_teacher_cannot_download_another_course_material(client, teacher_b, material):
    response = client.get(
        f"/v1/courses/{material.course_id}/materials/{material.id}/download",
        headers=teacher_b.auth_headers,
    )

    assert response.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/api; python -m pytest tests/services/test_material_service.py tests/api/test_material_access.py -q`

Expected: FAIL，原因是资料模型、存储端口和路由尚不存在。

- [ ] **Step 3: 实现资料领域模型与受控存储接口**

迁移创建 `course_materials`：`id UUID`、`course_id UUID NOT NULL REFERENCES courses`、`area`（`syllabus`、`assessment_outline`、`teaching_material`、`practice_bank`、`unclassified`）、`original_filename`、`storage_key`、`media_type`、`byte_size`、`sha256`、`status`、`created_at`、`deleted_at`、`purge_after`、`version_of_material_id`。设置 `(course_id, sha256)` 索引和 `(course_id, deleted_at)` 索引。

实现 `ObjectStore` 协议：`create_presigned_upload(key, content_type, expires_in)`、`head(key)`、`create_presigned_download(key, expires_in)`、`move_to_recycle_bin(key)`。生产实现使用 boto3；单元测试使用内存 fake，测试不访问真实 MinIO。`MaterialService.create_upload` 先验证扩展名和 MIME 白名单、最大大小、SHA-256 格式，再生成 UUID 和服务端 key，最后创建短时上传 URL。上传完成端点必须调用 `head()` 再把资料标记为 `staged`，不得相信客户端报送的大小或 Content-Type。

提供 API：

- `POST /v1/courses/{course_id}/materials/upload-sessions`：仅所有者，创建资料记录与短时上传 URL；
- `POST /v1/courses/{course_id}/materials/{material_id}/complete-upload`：仅所有者，验证对象并置 `staged`；
- `GET /v1/courses/{course_id}/materials`：只列当前课程且未删除资料；
- `GET /v1/courses/{course_id}/materials/{material_id}/download`：先取得 `CourseScope` 再发放 60 秒下载 URL；
- `DELETE /v1/courses/{course_id}/materials/{material_id}`：标记删除、设置 30 天 `purge_after`，并使其不可作为后续处理输入。

每个操作记录审计；响应中不返回其他课程 ID、存储服务凭据或永久 URL。P0 不接受 ZIP、可执行文件、宏启用 Office 文件、外部链接处理或文件正文解析。

- [ ] **Step 4: 运行资料服务、跨课程访问和完整回归**

Run: `cd apps/api; alembic upgrade head; python -m pytest tests/services/test_material_service.py tests/api/test_material_access.py tests/api/test_course_authorization.py -q`

Expected: 全部通过；下载路径始终先校验课程授权；删除资料后不会被 `get_for_new_processing` 返回。

- [ ] **Step 5: 提交安全上传能力**

```bash
git add apps/api/app apps/api/migrations apps/api/tests
git commit -m "feat: add course-scoped material uploads"
```

## Task 6: 实现可恢复、幂等的后台任务记录和 Worker 骨架

**Files:**

- Create: `apps/api/app/domain/tasks.py`
- Create: `apps/api/app/services/tasks.py`
- Create: `apps/api/app/infrastructure/task_queue.py`
- Create: `apps/api/app/worker/main.py`
- Create: `apps/api/app/worker/handlers.py`
- Create: `apps/api/migrations/versions/0004_create_task_runs.py`
- Create: `apps/api/tests/services/test_task_runs.py`
- Create: `apps/api/tests/worker/test_task_recovery.py`
- Modify: `apps/api/app/db/models.py`
- Modify: `apps/api/app/services/materials.py`

- [ ] **Step 1: 写幂等提交和崩溃恢复的失败测试**

```python
# apps/api/tests/services/test_task_runs.py
def test_same_idempotency_key_returns_same_task(task_service, course_scope):
    first = task_service.enqueue(
        scope=course_scope,
        task_type="material.validate_upload",
        resource_id="material-1",
        input_version="1",
        idempotency_key="upload-material-1-v1",
    )
    second = task_service.enqueue(
        scope=course_scope,
        task_type="material.validate_upload",
        resource_id="material-1",
        input_version="1",
        idempotency_key="upload-material-1-v1",
    )

    assert first.id == second.id
    assert task_service.count_runs(course_scope.course_id) == 1
```

```python
# apps/api/tests/worker/test_task_recovery.py
def test_expired_running_task_can_be_claimed_again(task_service, worker, course_scope):
    task = task_service.enqueue(scope=course_scope, task_type="noop", resource_id="x", input_version="1", idempotency_key="x-1")
    task_service.force_running_with_expired_lease(task.id)

    claimed = worker.claim_next(course_scope.course_id)

    assert claimed.id == task.id
    assert claimed.attempt_count == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/api; python -m pytest tests/services/test_task_runs.py tests/worker/test_task_recovery.py -q`

Expected: FAIL，原因是 `TaskRun` 和 Worker 尚不存在。

- [ ] **Step 3: 实现数据库任务事实来源、队列通知和 Worker 租约**

迁移创建 `task_runs`，字段包括：`id UUID`、`course_id UUID NOT NULL`、`task_type`、`resource_id`、`input_version`、`idempotency_key`、`status`（`queued`、`running`、`succeeded`、`failed`、`cancelled`、`superseded`）、`attempt_count`、`max_attempts`、`lease_expires_at`、`last_error_code`、`created_at`、`started_at`、`finished_at`。建立唯一约束 `(course_id, task_type, idempotency_key)` 与可领取任务索引 `(status, lease_expires_at, created_at)`。

`TaskService.enqueue` 在同一数据库事务内创建或返回既有任务，并只在新任务入队后向 `TaskQueue.notify(task_id)` 发送通知；通知失败时任务仍处于 `queued`，Worker 定时轮询会发现它。Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 原子领取 `queued` 或租约过期的 `running` 任务，领取时增加 `attempt_count`、设置短租约；完成时仅在输入版本仍匹配时标为成功。达到 `max_attempts` 后标记 `failed` 并产生审计事件。

实现 `noop` handler 作为 P0 端到端验证。资料完成上传后创建 `material.validate_upload` 任务，但 P0 handler 只再次检查对象 metadata、更新状态与记录审计；不得读取或解析文件正文。Redis adapter 仅传递任务 ID，所有任务参数均从数据库按 `task_id` 读取。

- [ ] **Step 4: 运行任务恢复测试与所有 P0 回归**

Run: `cd apps/api; alembic upgrade head; python -m pytest tests/services/test_task_runs.py tests/worker/test_task_recovery.py tests/api tests/services -q`

Expected: 全部通过；同一幂等键只存在一个任务，过期租约可安全重新领取。

- [ ] **Step 5: 提交可恢复任务基础**

```bash
git add apps/api/app apps/api/migrations apps/api/tests
git commit -m "feat: add recoverable scoped background tasks"
```

## Task 7: 端到端安全回归、运行手册与 P0 验收

**Files:**

- Create: `apps/api/tests/api/test_p0_security_regression.py`
- Create: `docs/operations/p0-local-runbook.md`
- Create: `docs/operations/p0-threat-model.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`

- [ ] **Step 1: 写 P0 端到端验收的失败测试**

```python
# apps/api/tests/api/test_p0_security_regression.py
def test_p0_flow_is_course_isolated_and_audited(client, teacher_a, teacher_b, object_store):
    course = create_course(client, teacher_a, code="SK3020")
    material = create_and_complete_material(client, teacher_a, course.id, object_store, "outline.pdf")

    foreign_list = client.get(f"/v1/courses/{course.id}/materials", headers=teacher_b.auth_headers)
    foreign_download = client.get(
        f"/v1/courses/{course.id}/materials/{material.id}/download", headers=teacher_b.auth_headers
    )
    deleted = client.delete(
        f"/v1/courses/{course.id}/materials/{material.id}", headers=teacher_a.auth_headers
    )

    assert foreign_list.status_code == 404
    assert foreign_download.status_code == 404
    assert deleted.status_code == 204
    assert material_is_not_eligible_for_processing(course.id, material.id)
    assert audit_contains("authorization.denied")
    assert audit_contains("material.deleted")
```

- [ ] **Step 2: 运行测试确认失败或暴露未覆盖缺口**

Run: `cd apps/api; python -m pytest tests/api/test_p0_security_regression.py -q`

Expected: 若有未接入的审计或删除状态传播，测试失败并精确指出断言；修复前不得标记 P0 完成。

- [ ] **Step 3: 补齐测试揭示的边界实现，并编写运行文档**

修复只限于让既定 P0 契约成立。`p0-local-runbook.md` 必须包含启动依赖、迁移、创建两个测试教师与课程、上传/删除一个资料、启动 Worker、检查任务状态和运行测试的精确命令。`p0-threat-model.md` 必须列出资产、信任边界、攻击面、控制措施和验证用例，至少覆盖跨课程 ID 枚举、预签名 URL 泄露、恶意文件伪装、审计内容泄露、Worker 跨课程读取和重复任务执行。

同步更新主设计文档的修订记录：只有实际完成 P0 后，才新增 P0 已实现的版本记录；在尚未实现时不得把 P0 写成已交付。

- [ ] **Step 4: 执行最终验证**

Run: `cd apps/api; alembic upgrade head; python -m pytest -q; python -m ruff check app tests; python -m mypy app`

Expected: 全部测试、ruff 和 mypy 通过；Docker Compose 服务健康；两门课程之间无法列举、下载或处理对方资料。

- [ ] **Step 5: 提交 P0 验收与运行文档**

```bash
git add apps/api docs/operations README.md docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md
git commit -m "test: verify course-space security foundation"
```

## 计划自检

本计划覆盖设计文档 13.3 的 P0 项：教师课程空间、课程边界、对象文件、审计、异步任务、恢复和测试夹具。P1 的解析/OCR、知识点、证据与检索未被提前实现；P2 的蓝图与题位、P3 的模型命题、P4 的编辑审核、P5 的导出均未混入本计划。所有写入资源均有 `course_id`，所有敏感下载均需服务端授权，后台任务具有数据库持久化状态与幂等键。

执行前必须确认：目标部署是否已有统一身份提供方、对象存储和 PostgreSQL/Redis；若已有，适配其连接与密钥管理方式，不创建第二套生产身份或存储系统。
