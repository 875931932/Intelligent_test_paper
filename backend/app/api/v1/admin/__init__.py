"""后台管理 API（/api/v1/admin/**）：组织账号、模型管理、调用监控。

三级角色（users.role）：admin（平台管理员）/ org_admin（单位管理员）/ teacher（教师）。
教师完全不可见；router 级统一挂 require_admin，端点内再按需收紧为
require_platform_admin / 单位范围过滤（见各子模块）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin import llm, monitoring, organizations, overview, users

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
router.include_router(overview.router)
router.include_router(organizations.router)
router.include_router(users.router)
router.include_router(llm.router)
router.include_router(monitoring.router)
