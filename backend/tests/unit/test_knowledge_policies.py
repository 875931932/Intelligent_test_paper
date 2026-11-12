"""知识域授权纯函数测试：主设计 §6.2/§6.4 权限矩阵的关键分支。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.knowledge_base.models import (
    ACTION_FORBIDDEN,
    RESOURCE_NOT_FOUND,
    Actor,
    DocumentVersion,
    Membership,
    Space,
)
from app.domain.knowledge_base.policies import (
    admin_can_manage_space,
    authorize,
    can_preview_draft,
    effective_role,
    resolve_organization_scope,
    scope_expansion_requires_confirmation,
    validate_classification,
    version_is_servable,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def _actor(**overrides) -> Actor:
    base = dict(
        user_id="u1",
        platform_role="teacher",
        status="active",
        school_id="school-1",
        college_id="college-1",
    )
    base.update(overrides)
    return Actor(**base)


def _space(**overrides) -> Space:
    base = dict(
        space_id="sp1",
        school_id="school-1",
        org_id="college-1",
        read_scope="members",
        status="active",
        policy_version=1,
    )
    base.update(overrides)
    return Space(**base)


def _version(**overrides) -> DocumentVersion:
    base = dict(
        version_id="v1",
        document_id="d1",
        status="published",
        classification="shareable_teaching",
        effective_from=NOW - timedelta(days=1),
        effective_to=None,
    )
    base.update(overrides)
    return DocumentVersion(**base)


def test_organization_scope_resolves_school_and_college():
    assert resolve_organization_scope("school-1", "school", None).school_id == "school-1"
    scope = resolve_organization_scope("college-1", "college", "school-1")
    assert (scope.school_id, scope.college_id) == ("school-1", "college-1")
    assert resolve_organization_scope(None, "school", None) is None
    assert resolve_organization_scope("college-1", "college", None) is None


def test_cross_school_would_be_not_found():
    outsider = _actor(school_id="school-2", college_id=None)
    decision = authorize(outsider, "read", _space(read_scope="school"))
    assert not decision.allowed
    assert decision.reason == RESOURCE_NOT_FOUND


def test_platform_admin_does_not_gain_content_read():
    admin = _actor(platform_role="admin", school_id=None, college_id=None)
    assert not authorize(admin, "read", _space(read_scope="school")).allowed
    # 但管理身份可以在范围内做治理动作（内容授权仍不继承）。
    assert admin_can_manage_space(_actor(platform_role="admin"), _space())


def test_reader_can_read_but_not_edit_or_publish():
    space = _space(read_scope="school")
    reader = _actor()
    assert authorize(reader, "read", space).role == "reader"
    assert authorize(reader, "edit", space).reason == ACTION_FORBIDDEN
    assert authorize(reader, "publish", space).reason == ACTION_FORBIDDEN
    assert can_preview_draft("reader") is False


def test_editor_cannot_publish_and_publisher_cannot_manage_members():
    space = _space()
    editor = Membership("sp1", "u1", "editor")
    publisher = Membership("sp1", "u1", "publisher")
    assert authorize(_actor(), "edit", space, editor).allowed
    assert not authorize(_actor(), "publish", space, editor).allowed
    assert authorize(_actor(), "publish", space, publisher).allowed
    assert not authorize(_actor(), "manage_members", space, publisher).allowed


def test_owner_has_full_matrix():
    space = _space()
    owner = Membership("sp1", "u1", "owner")
    for action in ("read", "edit", "publish", "import", "manage_members", "transfer_owner", "manage_space"):
        assert authorize(_actor(), action, space, owner).allowed, action


def test_membership_does_not_break_school_isolation():
    space = _space()
    other_school_member = Membership("sp1", "u1", "editor")
    actor = _actor(school_id="school-2")
    assert effective_role(actor, space, other_school_member) is None


def test_shared_scope_reader_downgrades_to_reader_even_if_scope_matches():
    space = _space(read_scope="organization")
    # 同学院教师自动获得 reader；其他学院教师不可见。
    assert effective_role(_actor(), space, None) == "reader"
    other_college = _actor(college_id="college-2")
    assert effective_role(other_college, space, None) is None
    school_wide = _space(read_scope="school")
    assert effective_role(other_college, school_wide, None) == "reader"


def test_archived_space_only_owner_can_access():
    space = _space(status="archived")
    assert effective_role(_actor(), space, None) is None
    assert effective_role(_actor(), space, Membership("sp1", "u1", "owner")) == "owner"


def test_expired_or_withdrawn_version_is_not_servable():
    assert version_is_servable(_version(), NOW)
    assert not version_is_servable(_version(status="withdrawn"), NOW)
    assert not version_is_servable(_version(effective_to=NOW - timedelta(minutes=1)), NOW)
    assert not version_is_servable(_version(effective_from=NOW + timedelta(days=1)), NOW)
    # 读者读不到过期版本；维护者仍可用于管理/预览。
    space = _space(read_scope="school")
    expired = _version(effective_to=NOW - timedelta(minutes=1))
    assert not authorize(_actor(), "read", space, None, expired, NOW).allowed
    assert authorize(_actor(), "read", space, Membership("sp1", "u1", "editor"), expired, NOW).allowed


def test_limited_teaching_cannot_expand_beyond_members():
    assert validate_classification("shareable_teaching", "school") is None
    assert validate_classification("limited_teaching", "members") is None
    assert validate_classification("limited_teaching", "school") == ACTION_FORBIDDEN
    assert validate_classification("exam_secret", "members") == ACTION_FORBIDDEN


def test_scope_expansion_requires_confirmation_only_when_growing():
    assert scope_expansion_requires_confirmation("members", "school")
    assert scope_expansion_requires_confirmation("organization", "school")
    assert not scope_expansion_requires_confirmation("school", "members")
