"""知识域授权纯函数。

调用方负责从数据库读取**当前**身份/空间/成员/版本状态并组装快照；本模块
不做 I/O、不读缓存，保证每次判定都基于请求时刻的事实。

主设计 §6.4 的判定链：
账号有效 + 所属组织有效 -> school 匹配 -> 空间有效且范围/成员角色允许
-> 文档未撤回、版本已发布、有效期覆盖当前时间 -> action 对应权限。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.knowledge_base.models import (
    ACTION_FORBIDDEN,
    RESOURCE_NOT_FOUND,
    Actor,
    Decision,
    DocumentVersion,
    Membership,
    OrganizationScope,
    Space,
)

# §6.2 权限矩阵：owner > publisher > editor > reader。
# - reader 可读已发布资料、搜索、问答，但不能查看草稿或执行任何写操作。
# - editor 可上传/建新版/改草稿；不能发布、撤回或改共享范围。
# - publisher 在 editor 之上增加发布、撤回、设有效期；不能管成员或共享范围。
# - owner 额外拥有成员、共享范围、导入许可与移交。
_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": frozenset({"read"}),
    "editor": frozenset({"read", "edit"}),
    "publisher": frozenset({"read", "edit", "publish"}),
    "owner": frozenset(
        {"read", "edit", "publish", "import", "manage_members", "transfer_owner", "manage_space"}
    ),
}

# 仅 owner 能改的策略动作（扩大 read_scope 属于重新发布，需二次确认）。
OWNER_ONLY_ACTIONS = frozenset({"manage_members", "transfer_owner", "manage_space"})


def resolve_organization_scope(
    organization_id: str | None,
    organization_kind: str | None,
    parent_id: str | None,
) -> OrganizationScope | None:
    """把账号的 organization_id 解析成 (school_id, college_id)。

    ``kind='school'`` 的账号直接挂在学校；``kind='college'`` 的学校取 parent；
    组织缺失、被停用或层级不合法时返回 None（该账号不能建校级空间或获校内共享）。
    """

    if not organization_id or not organization_kind:
        return None
    if organization_kind == "school":
        return OrganizationScope(school_id=organization_id, college_id=None)
    if organization_kind == "college" and parent_id:
        return OrganizationScope(school_id=parent_id, college_id=organization_id)
    return None


def role_permissions(role: str) -> frozenset[str]:
    return _ROLE_PERMISSIONS.get(role, frozenset())


def read_scope_matches(actor: Actor, space: Space) -> bool:
    """共享范围是否覆盖该账号（显式成员之外的读者来源）。

    - members：不产生范围读者，只认成员表。
    - organization：责任单位内的教师。责任单位为学校时覆盖本校及其学院。
    - school：本校有效教职工账号。
    任何范围都不能突破学校隔离。
    """

    if not actor.is_active or actor.school_id != space.school_id:
        return False
    if space.read_scope == "school":
        return True
    if space.read_scope == "organization":
        if space.org_id == space.school_id:
            return True
        return actor.college_id == space.org_id
    return False


def effective_role(
    actor: Actor,
    space: Space,
    membership: Membership | None,
) -> str | None:
    """返回该账号在此空间的有效角色；显式成员优先，其次才是共享范围读者。"""

    if not actor.is_active:
        return None
    if actor.school_id != space.school_id:
        return None
    if space.status != "active":
        # 归档等价于全部文档停止对普通读者服务；维护者仍可管理恢复。
        if membership is None or membership.role != "owner":
            return None
    if membership is not None and membership.user_id == actor.user_id:
        # 显式成员也不能突破学校隔离（上面已校验 school_id）。
        return membership.role
    if read_scope_matches(actor, space):
        return "reader"
    return None


def version_is_servable(version: DocumentVersion, now: datetime | None = None) -> bool:
    """文档版本当前是否可被普通读者读取：已发布、未撤回、有效期覆盖当前时间。"""

    if version.status != "published":
        return False
    moment = now or datetime.now(timezone.utc)
    starts = _as_utc(version.effective_from)
    ends = _as_utc(version.effective_to)
    if starts is not None and starts > moment:
        return False
    if ends is not None and ends <= moment:
        return False
    return True


def authorize(
    actor: Actor,
    action: str,
    space: Space,
    membership: Membership | None = None,
    version: DocumentVersion | None = None,
    now: datetime | None = None,
) -> Decision:
    """统一授权判定。返回的 ``reason`` 是内部错误码，不向用户暴露资源存在性。"""

    if not actor.is_active or not actor.has_organization:
        return Decision(False, RESOURCE_NOT_FOUND)

    role = effective_role(actor, space, membership)
    if role is None:
        # 无权与不存在统一 404，避免用响应差异探测他人空间/资料。
        return Decision(False, RESOURCE_NOT_FOUND, None)

    if action not in role_permissions(role):
        return Decision(False, ACTION_FORBIDDEN, role)

    # owner 之外的读者/编辑只能接触当前可服务版本；草稿预览走单独管理权限。
    if action == "read" and version is not None and not version_is_servable(version, now):
        if role == "reader":
            return Decision(False, RESOURCE_NOT_FOUND, role)

    return Decision(True, "", role)


def can_preview_draft(role: str | None) -> bool:
    """草稿/处理中结果只对可编辑成员开放，普通读者不能借问答检索到。"""

    return role is not None and role in {"owner", "editor", "publisher"}


def validate_classification(classification: str, read_scope: str) -> str | None:
    """分类与共享范围的组合校验；返回错误码，None 表示允许。

    ``limited_teaching`` 只允许 members 空间；考试保密材料不在这两个分类
    之内，由上游直接拒绝入库，不能靠该函数放行。
    """

    if classification not in {"shareable_teaching", "limited_teaching"}:
        return ACTION_FORBIDDEN
    if classification == "limited_teaching" and read_scope != "members":
        return ACTION_FORBIDDEN
    return None


def scope_expansion_requires_confirmation(old_scope: str, new_scope: str) -> bool:
    """扩大读者范围属于重新发布：排序 index 变大即需要影响预览与显式确认。"""

    order = {"members": 0, "organization": 1, "school": 2}
    return order.get(new_scope, -1) > order.get(old_scope, -1)


def admin_can_manage_space(actor: Actor, space: Space) -> bool:
    """管理身份只用于建库/移交等治理动作，且不自动获得内容阅读权。"""

    if not actor.is_active:
        return False
    if actor.is_platform_admin:
        return True
    if not actor.is_org_admin:
        return False
    if actor.school_id != space.school_id:
        return False
    return actor.college_id in (None, space.org_id)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 取回的是 naive UTC，PostgreSQL 是 aware；统一成 UTC 再比较。"""

    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
