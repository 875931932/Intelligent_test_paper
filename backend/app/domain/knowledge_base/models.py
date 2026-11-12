"""知识域（校级教师共享知识库）契约模型。

这里只放**纯数据**：身份/空间/成员/版本的最小快照与枚举。授权判定在
``policies``，数据库读取在服务层组装这些快照后传入——先做纯函数，避免把
权限规则散落在路由和 SQL 条件里。

与课程域的关系：课程隔离仍以 ``course_id`` 为强制键；知识域是新增的显式
隔离域，使用 ``school_id`` + ``space_id``，不把 ``course_id`` 放宽为可空。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# ── 枚举（存 String，不走数据库原生 enum，迁移不锁类型）──────────────────
# 平台身份保持 teacher/org_admin/admin 三种，不新增学生或其他身份。
PLATFORM_ROLES = ("teacher", "org_admin", "admin")

# 知识库成员角色：reader 不是新身份，只是"在该库无编辑权"的教师。
KNOWLEDGE_ROLES = ("owner", "editor", "publisher", "reader")

# 空间读者范围：默认仅指定成员；scope 只在校内生效，不能跨校。
READ_SCOPES = ("members", "organization", "school")

# 资料分类：可共享教学资料 / 限指定教师资料；考试保密材料禁止入库。
CLASSIFICATIONS = ("shareable_teaching", "limited_teaching")

SPACE_STATUSES = ("active", "archived")

DOCUMENT_STATUSES = (
    "draft",
    "processing",
    "ready",
    "published",
    "superseded",
    "withdrawn",
    "rejected",
)

# 授权动作（与主设计 §6.2 权限矩阵一致）。
ACTIONS = (
    "read",
    "edit",
    "publish",
    "import",
    "manage_members",
    "transfer_owner",
    "manage_space",
)

# 关键错误码（设计 §13）：无权与不存在统一 404，已可见但不可修改才 403。
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
ACTION_FORBIDDEN = "ACTION_FORBIDDEN"
VERSION_CONFLICT = "VERSION_CONFLICT"
INDEX_NOT_READY = "INDEX_NOT_READY"
ACCESS_REVOKED = "ACCESS_REVOKED"
FILE_VALIDATION_FAILED = "FILE_VALIDATION_FAILED"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class Actor:
    """一次请求的服务端身份快照（数据库当前值，不接受前端传入）。"""

    user_id: str
    platform_role: str
    status: str
    # 组织解析结果：无组织归属的账号两者都为 None，不能建校级空间。
    school_id: str | None
    college_id: str | None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def has_organization(self) -> bool:
        return bool(self.school_id)

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role == "admin"

    @property
    def is_org_admin(self) -> bool:
        return self.platform_role == "org_admin"


@dataclass(frozen=True)
class OrganizationScope:
    """账号所属组织的解析结果。``college_id`` 为空表示账号直接挂在学校。"""

    school_id: str
    college_id: str | None


@dataclass(frozen=True)
class Space:
    """空间最小快照。``org_id`` 是责任单位（学校或学院）。"""

    space_id: str
    school_id: str
    org_id: str
    read_scope: str
    status: str
    policy_version: int = 1


@dataclass(frozen=True)
class Membership:
    space_id: str
    user_id: str
    role: str


@dataclass(frozen=True)
class DocumentVersion:
    """文档版本快照：只有 published 且有效期覆盖当前时间才可被普通读者读取。"""

    version_id: str
    document_id: str
    status: str
    classification: str = "shareable_teaching"
    effective_from: datetime | None = None
    effective_to: datetime | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    # 命中的有效角色（owner/editor/publisher/reader），无权时为 None。
    role: str | None = None

    def __bool__(self) -> bool:  # 允许 `if decision:`
        return self.allowed
