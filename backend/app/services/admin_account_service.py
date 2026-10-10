"""组织与账号管理（后台）：两级组织 CRUD + 账号全生命周期（范围过滤内）。

红线口径：
- 停用取代删除（课程外键不悬空）；无删除账号接口。
- org_admin 的一切读写都显式带单位范围过滤，默认拒绝；越权返回 NotFound
  （不泄露存在性），平台管理员全局可见可管。
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import Organization, User
from app.services.auth_service import hash_password

VALID_ROLES = ("admin", "org_admin", "teacher")
ORG_KINDS = ("school", "college")


class OrgNotFoundError(Exception):
    """组织不存在或不在操作者可见范围（合并语义，避免泄露存在性）。"""


class OrgConflictError(Exception):
    """组织名称/代码冲突，或层级非法（如学院挂到学院下）。"""


class UserNotFoundError(Exception):
    """账号不存在或不在操作者管理范围（合并语义）。"""


class UsernameTakenError(Exception):
    """用户名已被占用。"""


class UserValidationError(Exception):
    """建号/改号的业务校验失败（角色越权、范围越权、停用自己等）。"""


# ── 组织 ─────────────────────────────────────────────────────────────────────


def _org_dict(org: Organization) -> dict:
    return {
        "id": org.id,
        "parent_id": org.parent_id,
        "kind": org.kind,
        "name": org.name,
        "code": org.code,
        "status": org.status,
    }


def list_org_tree(
    session: Session,
    *,
    visible_org_ids: set[str] | None = None,
    flat: bool = False,
    kind: str | None = None,
) -> list[dict]:
    """组织树（学校 → 学院）。visible_org_ids 非 None 时仅返回范围内的行。

    flat=True 返回平铺列表（下拉选择用，不含 children 字段）；kind 过滤类型。
    """
    stmt = select(Organization)
    if visible_org_ids is not None:
        if not visible_org_ids:
            return []
        stmt = stmt.where(Organization.id.in_(visible_org_ids))
    if kind:
        stmt = stmt.where(Organization.kind == kind)
    orgs = session.scalars(stmt.order_by(Organization.kind, Organization.name)).all()
    if flat:
        return [_org_dict(o) for o in orgs]
    by_id = {o.id: {**_org_dict(o), "children": []} for o in orgs}
    tree = []
    for org in orgs:
        node = by_id[org.id]
        parent = by_id.get(org.parent_id) if org.parent_id else None
        if parent is not None:
            parent["children"].append(node)
        else:
            tree.append(node)
    return tree


def get_org(session: Session, org_id: str, *, visible_org_ids: set[str] | None = None) -> Organization:
    org = session.get(Organization, org_id)
    if org is None or (visible_org_ids is not None and org.id not in visible_org_ids):
        raise OrgNotFoundError(org_id)
    return org


def create_org(
    session: Session,
    *,
    name: str,
    kind: str,
    parent_id: str | None = None,
    code: str | None = None,
) -> dict:
    """新建组织：学校 parent_id 必须为空；学院必须挂在学校下（固定两级）。"""
    if kind not in ORG_KINDS:
        raise OrgConflictError("组织类型只能是 school 或 college")
    if kind == "school" and parent_id is not None:
        raise OrgConflictError("学校不能有上级组织")
    if kind == "college":
        if not parent_id:
            raise OrgConflictError("学院必须归属一所学校")
        parent = session.get(Organization, parent_id)
        if parent is None or parent.kind != "school":
            raise OrgConflictError("学院只能挂在学校下")
    name_exists = session.scalar(
        select(func.count()).select_from(Organization).where(Organization.name == name)
    )
    if name_exists:
        raise OrgConflictError("同名组织已存在")
    if code:
        code_exists = session.scalar(
            select(func.count()).select_from(Organization).where(Organization.code == code)
        )
        if code_exists:
            raise OrgConflictError("组织代码已被占用")
    org = Organization(
        id=uuid4().hex, parent_id=parent_id, kind=kind, name=name, code=code or None
    )
    session.add(org)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise OrgConflictError("组织名称或代码冲突") from exc
    session.refresh(org)
    return _org_dict(org)


def update_org(
    session: Session,
    org_id: str,
    *,
    name: str | None = None,
    code: str | None = None,
    status: str | None = None,
    visible_org_ids: set[str] | None = None,
) -> dict:
    """改组织（名称/代码/停启用）。不支持移动层级（两级结构简单，避免子树悬空）。"""
    org = get_org(session, org_id, visible_org_ids=visible_org_ids)
    if status is not None and status not in ("active", "disabled"):
        raise OrgConflictError("状态只能是 active 或 disabled")
    if status == "disabled":
        # 有启用中的账号或下级学院时禁止停用（保护范围语义完整）
        has_children = session.scalar(
            select(func.count()).select_from(Organization).where(Organization.parent_id == org.id)
        )
        has_users = session.scalar(
            select(func.count()).select_from(User).where(User.organization_id == org.id, User.status == "active")
        )
        if has_children or has_users:
            raise OrgConflictError("该组织下仍有学院或启用中的账号，不能停用")
    if name is not None and name != org.name:
        dup = session.scalar(
            select(func.count()).select_from(Organization).where(Organization.name == name)
        )
        if dup:
            raise OrgConflictError("同名组织已存在")
        org.name = name
    if code is not None and code != org.code:
        if code:
            dup = session.scalar(
                select(func.count()).select_from(Organization).where(Organization.code == code)
            )
            if dup:
                raise OrgConflictError("组织代码已被占用")
        org.code = code or None
    if status is not None:
        org.status = status
    session.commit()
    session.refresh(org)
    return _org_dict(org)


# ── 账号 ─────────────────────────────────────────────────────────────────────


def _user_dict(user: User, org_name: str | None = None) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "name": user.display_name,
        "role": user.role,
        "organization_id": user.organization_id,
        "organization_name": org_name,
        "title": user.title,
        "employee_no": user.employee_no,
        "status": user.status,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _org_names(session: Session, org_ids: set[str]) -> dict[str, str]:
    if not org_ids:
        return {}
    rows = session.execute(select(Organization.id, Organization.name).where(Organization.id.in_(org_ids))).all()
    return {oid: name for oid, name in rows}


def list_users(
    session: Session,
    *,
    q: str | None = None,
    organization_id: str | None = None,
    role: str | None = None,
    status: str | None = None,
    org_ids_in_scope: set[str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """账号列表（筛选 + 分页）。org_ids_in_scope 非 None 时强制范围过滤。"""
    stmt = select(User)
    if org_ids_in_scope is not None:
        if not org_ids_in_scope:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        stmt = stmt.where(User.organization_id.in_(org_ids_in_scope))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            (User.username.like(like)) | (User.display_name.like(like)) | (User.employee_no.like(like))
        )
    if organization_id:
        stmt = stmt.where(User.organization_id == organization_id)
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(User.created_at.desc()).offset((max(page, 1) - 1) * page_size).limit(page_size)
    ).all()
    names = _org_names(session, {r.organization_id for r in rows if r.organization_id})
    return {
        "items": [_user_dict(r, names.get(r.organization_id)) for r in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


def _validate_target_role(operator: User, target_role: str) -> None:
    """角色授予校验：org_admin 只能建/改 teacher；admin 可以任意角色。"""
    if target_role not in VALID_ROLES:
        raise UserValidationError("角色无效")
    if operator.role != "admin" and target_role != "teacher":
        raise UserValidationError("单位管理员只能管理教师账号")


def create_user(
    session: Session,
    operator: User,
    *,
    username: str,
    password: str,
    display_name: str,
    role: str = "teacher",
    organization_id: str | None = None,
    title: str | None = None,
    employee_no: str | None = None,
    org_scope: set[str] | None = None,
) -> dict:
    _validate_target_role(operator, role)
    if operator.role != "admin":
        if organization_id is None or org_scope is None or organization_id not in org_scope:
            raise UserValidationError("单位管理员创建的账号必须归属本单位范围")
    if organization_id is not None:
        if session.get(Organization, organization_id) is None:
            raise UserValidationError("指定的单位不存在")
    if session.scalar(select(User).where(User.username == username)) is not None:
        raise UsernameTakenError(username)
    user = User(
        id=uuid4().hex,
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        role=role,
        organization_id=organization_id,
        title=title or None,
        employee_no=employee_no or None,
        created_by=operator.id,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise UsernameTakenError(username) from exc
    session.refresh(user)
    return _user_dict(user)


def update_user(
    session: Session,
    operator: User,
    target: User,
    *,
    display_name: str | None = None,
    role: str | None = None,
    organization_id: str | None = None,
    title: str | None = None,
    employee_no: str | None = None,
    org_scope: set[str] | None = None,
) -> dict:
    if role is not None:
        _validate_target_role(operator, role)
        # org_admin 不得改平台管理员的角色/资料（目标已是 admin 时只有平台管理员可动）
        if operator.role != "admin" and target.role == "admin":
            raise UserValidationError("无权操作平台管理员账号")
        target.role = role
    if operator.role != "admin":
        if operator.id == target.id:
            raise UserValidationError("不能修改自己的账号资料")
        if target.role == "admin":
            raise UserValidationError("无权操作平台管理员账号")
    if organization_id is not None and organization_id != target.organization_id:
        if operator.role != "admin":
            if org_scope is None or organization_id not in org_scope:
                raise UserValidationError("只能将账号调整到本单位范围")
        if session.get(Organization, organization_id) is None:
            raise UserValidationError("指定的单位不存在")
        target.organization_id = organization_id
    if display_name is not None:
        target.display_name = display_name
    if title is not None:
        target.title = title or None
    if employee_no is not None:
        target.employee_no = employee_no or None
    session.commit()
    session.refresh(target)
    return _user_dict(target)


def set_user_status(
    session: Session, operator: User, target: User, *, status: str
) -> dict:
    """停用/启用账号。禁止停用自己；org_admin 不能动平台管理员。"""
    if status not in ("active", "disabled"):
        raise UserValidationError("状态只能是 active 或 disabled")
    if operator.id == target.id and status == "disabled":
        raise UserValidationError("不能停用自己的账号")
    if operator.role != "admin" and target.role == "admin":
        raise UserValidationError("无权操作平台管理员账号")
    target.status = status
    session.commit()
    session.refresh(target)
    return _user_dict(target)


def reset_password(session: Session, operator: User, target: User, *, new_password: str) -> None:
    if operator.role != "admin" and target.role == "admin":
        raise UserValidationError("无权操作平台管理员账号")
    target.password_hash = hash_password(new_password)
    session.commit()
