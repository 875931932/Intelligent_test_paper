"""知识空间、成员与共享策略的用例编排（P1.2）。

路由只做协议转换；授权判定统一走 ``domain.knowledge_base.policies``。本层
负责把数据库当前状态组装成快照（Actor/Space/Membership/Version），并且
**不做正向授权缓存**——每次请求都读当前事实。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Organization,
    User,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_memberships,
    knowledge_spaces,
)
from app.domain.knowledge_base.models import (
    ACTION_FORBIDDEN,
    CLASSIFICATIONS,
    READ_SCOPES,
    RESOURCE_NOT_FOUND,
    Actor,
    Membership,
    Space,
    VERSION_CONFLICT,
)
from app.domain.knowledge_base.policies import (
    admin_can_manage_space,
    authorize,
    effective_role,
    resolve_organization_scope,
    scope_expansion_requires_confirmation,
)

READ_SCOPE_LABELS = {
    "members": "仅指定教师",
    "organization": "单位内教师",
    "school": "本校教师",
}


class KnowledgeError(Exception):
    """带 HTTP 语义的知识域错误。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _not_found() -> KnowledgeError:
    return KnowledgeError(404, RESOURCE_NOT_FOUND, "resource not found")


def _forbidden(message: str = "没有执行该操作的权限") -> KnowledgeError:
    return KnowledgeError(403, ACTION_FORBIDDEN, message)


def _conflict(message: str) -> KnowledgeError:
    return KnowledgeError(409, VERSION_CONFLICT, message)


# ── 快照组装 ────────────────────────────────────────────────────────────────


def load_actor(session: Session, user: User) -> Actor:
    """把当前账号解析成授权快照；组织缺失/停用时 school_id 为空。"""

    school_id: str | None = None
    college_id: str | None = None
    if user.organization_id:
        org = session.get(Organization, user.organization_id)
        if org is not None and org.status == "active":
            scope = resolve_organization_scope(org.id, org.kind, org.parent_id)
            if scope is not None:
                school_id, college_id = scope.school_id, scope.college_id
    return Actor(
        user_id=user.id,
        platform_role=user.role,
        status=user.status,
        school_id=school_id,
        college_id=college_id,
    )


def _space_snapshot(row) -> Space:
    return Space(
        space_id=row.id,
        school_id=row.school_id,
        org_id=row.org_id,
        read_scope=row.read_scope,
        status=row.status,
        policy_version=row.policy_version,
    )


def _membership_for(session: Session, *, space_id: str, user_id: str) -> Membership | None:
    row = session.execute(
        select(knowledge_memberships).where(
            knowledge_memberships.c.space_id == space_id,
            knowledge_memberships.c.user_id == user_id,
        )
    ).one_or_none()
    if row is None:
        return None
    return Membership(space_id=space_id, user_id=user_id, role=row.role)


def _load_space_row(session: Session, *, school_id: str, space_id: str):
    return session.execute(
        select(knowledge_spaces).where(
            knowledge_spaces.c.id == space_id,
            knowledge_spaces.c.school_id == school_id,
        )
    ).one_or_none()


def require_space_access(
    session: Session,
    actor: Actor,
    *,
    space_id: str,
    action: str = "read",
) -> tuple[object, str]:
    """读取空间并判定 action；返回 (行, 有效角色)。无权与不存在统一 404。"""

    row = _load_space_row(session, school_id=actor.school_id or "", space_id=space_id)
    if row is None:
        raise _not_found()
    membership = _membership_for(session, space_id=space_id, user_id=actor.user_id)
    decision = authorize(actor, action, _space_snapshot(row), membership)
    if not decision.allowed:
        if decision.reason == ACTION_FORBIDDEN:
            raise _forbidden()
        raise _not_found()
    return row, decision.role or "reader"


def _space_view(session: Session, row, *, role: str) -> dict:
    """前端展示用空间视图；不含任何无权空间的统计。"""

    document_count = session.execute(
        select(func.count())
        .select_from(knowledge_documents)
        .where(
            knowledge_documents.c.space_id == row.id,
            knowledge_documents.c.school_id == row.school_id,
            knowledge_documents.c.status == "published",
        )
    ).scalar_one()
    org = session.get(Organization, row.org_id)
    return {
        "id": row.id,
        "name": row.name,
        "school_id": row.school_id,
        "org_id": row.org_id,
        "org_name": getattr(org, "name", None),
        "read_scope": row.read_scope,
        "read_scope_label": READ_SCOPE_LABELS.get(row.read_scope, row.read_scope),
        "retrieval_mode": row.retrieval_mode,
        "allow_course_import": bool(row.allow_course_import),
        "subject_tags": list(row.subject_tags or []),
        "status": row.status,
        "policy_version": row.policy_version,
        "role": role,
        "document_count": int(document_count or 0),
        "created_at": _iso(row.created_at),
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


# ── 空间 ────────────────────────────────────────────────────────────────────


def create_space(
    session: Session,
    user: User,
    *,
    org_id: str | None,
    name: str,
    read_scope: str = "members",
    subject_tags: list[str] | None = None,
    allow_course_import: bool = False,
    owner_id: str | None = None,
) -> dict:
    """创建知识空间并同时写入唯一 owner 成员关系。

    - 普通教师只能在自己的责任单位（学院或学校）建库；
    - 平台/单位管理员可为范围内的单位建库并指派合格教师 owner；
    - 无组织归属的账号不能建校级空间。
    """

    actor = load_actor(session, user)
    if not actor.is_active:
        raise _forbidden("账号已停用")
    if not actor.has_organization:
        raise _forbidden("账号未归属有效组织，不能创建校级知识库")
    if read_scope not in READ_SCOPES:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "非法的共享范围")

    target_org_id = org_id or actor.college_id or actor.school_id
    org = session.get(Organization, target_org_id)
    if org is None or org.status != "active":
        raise KnowledgeError(422, RESOURCE_NOT_FOUND, "目标单位不存在或已停用")
    scope = resolve_organization_scope(org.id, org.kind, org.parent_id)
    if scope is None or scope.school_id != actor.school_id:
        raise _forbidden("只能在本校范围内创建知识库")
    if not actor.is_platform_admin and not actor.is_org_admin:
        # 教师只能在自己所属学院/学校建库；不能替别的学院建。
        allowed_orgs = {actor.school_id, actor.college_id}
        if target_org_id not in allowed_orgs:
            raise _forbidden("只能在自己所属单位创建知识库")

    # owner：默认是操作者本人；管理员可指派范围内的合格教师。
    resolved_owner_id = owner_id or user.id
    owner = session.get(User, resolved_owner_id)
    if owner is None or owner.status != "active":
        raise KnowledgeError(422, RESOURCE_NOT_FOUND, "指定的空间负责人不存在或已停用")
    owner_actor = load_actor(session, owner)
    if owner_actor.school_id != actor.school_id:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "负责人必须属于同一学校")
    if not actor.is_platform_admin and resolved_owner_id != user.id:
        raise _forbidden("只有管理员可以指派他人为空间负责人")

    name = (name or "").strip()
    if not name:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "知识库名称不能为空")
    duplicate = session.execute(
        select(knowledge_spaces.c.id).where(
            knowledge_spaces.c.school_id == actor.school_id,
            knowledge_spaces.c.org_id == target_org_id,
            knowledge_spaces.c.name == name,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _conflict("该单位下已存在同名知识库")

    now = datetime.now(timezone.utc)
    space_id = uuid4().hex
    session.execute(
        knowledge_spaces.insert().values(
            id=space_id,
            school_id=actor.school_id,
            org_id=target_org_id,
            name=name,
            read_scope=read_scope,
            retrieval_mode="hybrid",
            allow_course_import=bool(allow_course_import),
            subject_tags=list(subject_tags or []),
            status="active",
            policy_version=1,
            created_by=user.id,
            created_at=now,
            updated_at=now,
        )
    )
    session.execute(
        knowledge_memberships.insert().values(
            id=uuid4().hex,
            school_id=actor.school_id,
            space_id=space_id,
            user_id=resolved_owner_id,
            role="owner",
            created_by=user.id,
            created_at=now,
        )
    )
    session.commit()
    row = _load_space_row(session, school_id=actor.school_id, space_id=space_id)
    return _space_view(session, row, role="owner")


def list_spaces(session: Session, user: User, *, subject_tag: str | None = None) -> list[dict]:
    """当前账号可见的空间（成员 + 共享范围读者），按责任单位与名称排序。"""

    actor = load_actor(session, user)
    if not actor.is_active or not actor.has_organization:
        return []
    rows = session.execute(
        select(knowledge_spaces)
        .where(knowledge_spaces.c.school_id == actor.school_id)
        .order_by(knowledge_spaces.c.org_id, knowledge_spaces.c.name)
    ).fetchall()
    if not rows:
        return []
    memberships = {
        row.space_id: row.role
        for row in session.execute(
            select(knowledge_memberships).where(
                knowledge_memberships.c.user_id == actor.user_id,
                knowledge_memberships.c.school_id == actor.school_id,
            )
        ).fetchall()
    }
    views: list[dict] = []
    for row in rows:
        membership = (
            Membership(space_id=row.id, user_id=actor.user_id, role=memberships[row.id])
            if row.id in memberships
            else None
        )
        role = effective_role(actor, _space_snapshot(row), membership)
        if role is None:
            continue
        if subject_tag and subject_tag not in (row.subject_tags or []):
            continue
        views.append(_space_view(session, row, role=role))
    return views


def get_space(session: Session, user: User, space_id: str) -> dict:
    actor = load_actor(session, user)
    row, role = require_space_access(session, actor, space_id=space_id)
    return _space_view(session, row, role=role)


def update_space(
    session: Session,
    user: User,
    space_id: str,
    *,
    expected_policy_version: int | None = None,
    name: str | None = None,
    read_scope: str | None = None,
    allow_course_import: bool | None = None,
    subject_tags: list[str] | None = None,
    confirm_scope_expansion: bool = False,
) -> dict:
    """修改空间策略。扩大读者范围属于重新发布，必须显式确认。"""

    actor = load_actor(session, user)
    row, _role = require_space_access(session, actor, space_id=space_id, action="manage_space")
    _require_expected_version(row, expected_policy_version)
    if read_scope is not None and read_scope not in READ_SCOPES:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "非法的共享范围")
    if read_scope is not None and scope_expansion_requires_confirmation(row.read_scope, read_scope):
        if not confirm_scope_expansion:
            raise _conflict("扩大读者范围需要先查看影响预览并显式确认")
        # limited_teaching 文档不随范围扩大自动开放：有此类文档时阻止扩大。
        limited = session.execute(
            select(knowledge_documents.c.id)
            .where(
                knowledge_documents.c.space_id == space_id,
                knowledge_documents.c.school_id == row.school_id,
                knowledge_documents.c.classification == "limited_teaching",
                knowledge_documents.c.status == "published",
            )
            .limit(1)
        ).scalar_one_or_none()
        if limited is not None:
            raise _conflict("空间内存在限指定教师资料，不能直接扩大读者范围")

    name = (name or "").strip() or row.name
    changes: dict[str, object] = {
        "name": name,
        "read_scope": read_scope or row.read_scope,
        "subject_tags": list(subject_tags) if subject_tags is not None else list(row.subject_tags or []),
        "policy_version": int(row.policy_version) + 1,
        "updated_at": datetime.now(timezone.utc),
    }
    if allow_course_import is not None:
        changes["allow_course_import"] = bool(allow_course_import)
    session.execute(
        knowledge_spaces.update()
        .where(knowledge_spaces.c.id == space_id, knowledge_spaces.c.school_id == row.school_id)
        .values(**changes)
    )
    session.commit()
    refreshed = _load_space_row(session, school_id=row.school_id, space_id=space_id)
    return _space_view(session, refreshed, role="owner")


def _require_expected_version(row, expected: int | None) -> None:
    if expected is None:
        raise KnowledgeError(422, VERSION_CONFLICT, "缺少 expected_policy_version")
    if int(expected) != int(row.policy_version):
        raise _conflict("空间策略已被其他操作修改，请刷新后重试")


def policy_preview(session: Session, user: User, space_id: str, *, new_read_scope: str) -> dict:
    """扩大范围的影响预览：将新增可见的当前文档版本与读者规模。"""

    actor = load_actor(session, user)
    row, _role = require_space_access(session, actor, space_id=space_id, action="manage_space")
    if new_read_scope not in READ_SCOPES:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "非法的共享范围")
    documents = session.execute(
        select(knowledge_documents.c.id, knowledge_documents.c.title, knowledge_documents.c.classification)
        .where(
            knowledge_documents.c.space_id == space_id,
            knowledge_documents.c.school_id == row.school_id,
            knowledge_documents.c.published_version_id.is_not(None),
            knowledge_documents.c.status == "published",
        )
    ).fetchall()
    active_users = session.execute(
        select(func.count()).select_from(User).where(
            User.organization_id.is_not(None), User.status == "active"
        )
    ).scalar_one()
    return {
        "space_id": space_id,
        "current_read_scope": row.read_scope,
        "new_read_scope": new_read_scope,
        "requires_confirmation": scope_expansion_requires_confirmation(row.read_scope, new_read_scope),
        "newly_visible_documents": [
            {
                "document_id": doc.id,
                "title": doc.title,
                "classification": doc.classification,
            }
            for doc in documents
        ],
        "active_school_user_count": int(active_users or 0),
        "notes": [
            "扩大范围不会自动放开 marked limited_teaching 的历史版本",
            "已撤回或已过期的版本不会进入新读者列表",
        ],
    }


def archive_space(session: Session, user: User, space_id: str) -> dict:
    actor = load_actor(session, user)
    row, _role = require_space_access(session, actor, space_id=space_id, action="manage_space")
    session.execute(
        knowledge_spaces.update()
        .where(knowledge_spaces.c.id == space_id, knowledge_spaces.c.school_id == row.school_id)
        .values(status="archived", policy_version=int(row.policy_version) + 1, updated_at=datetime.now(timezone.utc))
    )
    session.commit()
    refreshed = _load_space_row(session, school_id=row.school_id, space_id=space_id)
    return _space_view(session, refreshed, role="owner")


# ── 成员 ────────────────────────────────────────────────────────────────────


def list_members(session: Session, user: User, space_id: str) -> list[dict]:
    actor = load_actor(session, user)
    row, role = require_space_access(session, actor, space_id=space_id)
    if role != "owner" and not admin_can_manage_space(actor, _space_snapshot(row)):
        # 普通成员可以知道谁是 owner，但不枚举全部成员清单。
        owner = session.execute(
            select(knowledge_memberships.c.user_id)
            .where(knowledge_memberships.c.space_id == space_id, knowledge_memberships.c.role == "owner")
        ).scalar_one_or_none()
        return [{"user_id": owner, "role": "owner", "name": None}] if owner else []
    rows = session.execute(
        select(knowledge_memberships).where(knowledge_memberships.c.space_id == space_id)
        .order_by(knowledge_memberships.c.role, knowledge_memberships.c.created_at)
    ).fetchall()
    result = []
    for member in rows:
        member_user = session.get(User, member.user_id)
        result.append(
            {
                "user_id": member.user_id,
                "role": member.role,
                "name": getattr(member_user, "display_name", None),
                "username": getattr(member_user, "username", None),
                "status": getattr(member_user, "status", None),
            }
        )
    return result


def upsert_member(
    session: Session,
    user: User,
    space_id: str,
    *,
    target_user_id: str,
    role: str,
) -> dict:
    """新增或改角色。owner 不能通过该端点产生（移交走专用事务端点）。"""

    actor = load_actor(session, user)
    row, _role = require_space_access(session, actor, space_id=space_id, action="manage_members")
    if role not in {"editor", "publisher", "reader"}:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "非法成员角色（owner 需走移交）")
    target = session.get(User, target_user_id)
    if target is None or target.status != "active":
        raise KnowledgeError(422, RESOURCE_NOT_FOUND, "目标账号不存在或已停用")
    target_actor = load_actor(session, target)
    if target_actor.school_id != row.school_id:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "不能添加其他学校的账号")
    existing = _membership_for(session, space_id=space_id, user_id=target_user_id)
    now = datetime.now(timezone.utc)
    if existing is not None:
        if existing.role == "owner":
            raise _conflict("不能通过成员接口修改空间负责人")
        session.execute(
            knowledge_memberships.update()
            .where(knowledge_memberships.c.space_id == space_id, knowledge_memberships.c.user_id == target_user_id)
            .values(role=role)
        )
    else:
        session.execute(
            knowledge_memberships.insert().values(
                id=uuid4().hex,
                school_id=row.school_id,
                space_id=space_id,
                user_id=target_user_id,
                role=role,
                created_by=user.id,
                created_at=now,
            )
        )
    _bump_policy_version(session, row)
    session.commit()
    return {"space_id": space_id, "user_id": target_user_id, "role": role}


def remove_member(session: Session, user: User, space_id: str, target_user_id: str) -> None:
    """移除显式成员授权；不改变 organization/school 共享范围带来的读权限。"""

    actor = load_actor(session, user)
    row, _role = require_space_access(session, actor, space_id=space_id, action="manage_members")
    member = _membership_for(session, space_id=space_id, user_id=target_user_id)
    if member is None:
        raise _not_found()
    if member.role == "owner":
        raise _conflict("不能移除空间负责人（先移交 owner）")
    session.execute(
        knowledge_memberships.delete().where(
            knowledge_memberships.c.space_id == space_id,
            knowledge_memberships.c.user_id == target_user_id,
        )
    )
    _bump_policy_version(session, row)
    session.commit()


def transfer_owner(
    session: Session,
    user: User,
    space_id: str,
    *,
    target_user_id: str,
    expected_policy_version: int | None = None,
) -> dict:
    """单事务移交 owner：原 owner 降为 publisher，新 owner 升为 owner。"""

    actor = load_actor(session, user)
    row, _role = require_space_access(session, actor, space_id=space_id, action="transfer_owner")
    _require_expected_version(row, expected_policy_version)
    target = session.get(User, target_user_id)
    if target is None or target.status != "active":
        raise KnowledgeError(422, RESOURCE_NOT_FOUND, "目标账号不存在或已停用")
    target_actor = load_actor(session, target)
    if target_actor.school_id != row.school_id:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "负责人必须属于同一学校")
    if target_user_id == actor.user_id:
        raise KnowledgeError(422, ACTION_FORBIDDEN, "不能移交给自己")

    now = datetime.now(timezone.utc)
    session.execute(
        knowledge_memberships.update()
        .where(knowledge_memberships.c.space_id == space_id, knowledge_memberships.c.user_id == actor.user_id)
        .values(role="publisher")
    )
    existing = _membership_for(session, space_id=space_id, user_id=target_user_id)
    if existing is None:
        session.execute(
            knowledge_memberships.insert().values(
                id=uuid4().hex,
                school_id=row.school_id,
                space_id=space_id,
                user_id=target_user_id,
                role="owner",
                created_by=user.id,
                created_at=now,
            )
        )
    else:
        session.execute(
            knowledge_memberships.update()
            .where(knowledge_memberships.c.space_id == space_id, knowledge_memberships.c.user_id == target_user_id)
            .values(role="owner")
        )
    _bump_policy_version(session, row)
    session.commit()
    return {"space_id": space_id, "owner_id": target_user_id, "previous_owner_id": actor.user_id}


def _bump_policy_version(session: Session, row) -> None:
    session.execute(
        knowledge_spaces.update()
        .where(knowledge_spaces.c.id == row.id, knowledge_spaces.c.school_id == row.school_id)
        .values(policy_version=int(row.policy_version) + 1, updated_at=datetime.now(timezone.utc))
    )


# ── 文档列表（P1.4 发布前先提供只读视图）────────────────────────────────────


def list_documents(session: Session, user: User, space_id: str) -> list[dict]:
    actor = load_actor(session, user)
    row, role = require_space_access(session, actor, space_id=space_id)
    query = select(knowledge_documents).where(
        knowledge_documents.c.space_id == space_id,
        knowledge_documents.c.school_id == row.school_id,
    )
    if role == "reader":
        # 普通读者只看到当前有效发布版本；草稿与撤回不混入列表。
        query = query.where(
            knowledge_documents.c.status == "published",
            knowledge_documents.c.published_version_id.is_not(None),
        )
    docs = session.execute(query.order_by(knowledge_documents.c.updated_at.desc())).fetchall()
    version_ids = [doc.published_version_id for doc in docs if doc.published_version_id]
    versions = {}
    if version_ids:
        versions = {
            ver.id: ver
            for ver in session.execute(
                select(knowledge_document_versions).where(
                    knowledge_document_versions.c.id.in_(version_ids)
                )
            ).fetchall()
        }
    now = datetime.now(timezone.utc)
    latest_versions = {}
    if role != "reader" and docs:
        for latest in session.execute(select(knowledge_document_versions).where(
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id == space_id,
            knowledge_document_versions.c.document_id.in_([doc.id for doc in docs]),
        ).order_by(knowledge_document_versions.c.version_no.desc())).mappings():
            latest_versions.setdefault(latest.document_id, latest)
    result = []
    for doc in docs:
        version = versions.get(doc.published_version_id) if doc.published_version_id else None
        if role == "reader":
            if version is None or version.state != "published":
                continue
            if not _servable(version, now):
                continue
        result.append(
            {
                "id": doc.id,
                "title": doc.title,
                "classification": doc.classification,
                "status": doc.status,
                "published_version_id": doc.published_version_id,
                "access_version": doc.access_version,
                "updated_at": _iso(doc.updated_at),
                "latest_version": ({
                    "id": latest_versions[doc.id].id,
                    "version_no": latest_versions[doc.id].version_no,
                    "state": latest_versions[doc.id].state,
                } if doc.id in latest_versions else None),
                "version": (
                    {
                        "id": version.id,
                        "version_no": version.version_no,
                        "state": version.state,
                        "mime_type": version.mime_type,
                        "size_bytes": version.size_bytes,
                        "effective_from": _iso(version.effective_from),
                        "effective_to": _iso(version.effective_to),
                        "published_at": _iso(version.published_at),
                    }
                    if version is not None
                    else None
                ),
            }
        )
    return result


def _servable(version, now: datetime) -> bool:
    starts = _aware(version.effective_from)
    ends = _aware(version.effective_to)
    if starts is not None and starts > now:
        return False
    if ends is not None and ends <= now:
        return False
    return True


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def classification_options() -> list[dict]:
    """发布界面用的分类说明（考试保密材料不在可选范围内）。"""

    return [
        {
            "value": CLASSIFICATIONS[0],
            "label": "可共享教学资料",
            "description": "公开练习、教学例题与讲解；发布者需确认可共享。",
        },
        {
            "value": CLASSIFICATIONS[1],
            "label": "限指定教师资料",
            "description": "仅 members 范围知识库可存放，不能扩大为全校共享。",
        },
    ]
