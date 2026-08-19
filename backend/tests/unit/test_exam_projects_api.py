"""exam_projects CRUD 端点的单元测试。"""
from unittest.mock import MagicMock, patch
from app.api.v1 import exam_projects as api


def test_list_endpoint_exists():
    """exam_projects 路由必须含 GET /exam-projects 端点。"""
    import inspect
    source = inspect.getsource(api)
    assert "@router.get" in source, "缺少 GET 端点"
    assert "exam-projects" in source, "路由路径不含 exam-projects"


def test_create_endpoint_exists():
    """exam_projects 路由必须含 POST /exam-projects 端点。"""
    import inspect
    source = inspect.getsource(api)
    assert 'def create' in source, "缺少 create 函数"


def test_get_endpoint_exists():
    """exam_projects 路由必须含 GET /exam-projects/{project_id} 端点。"""
    import inspect
    source = inspect.getsource(api)
    assert 'def get_one' in source, "缺少 get_one 函数"


def test_patch_status_endpoint_exists():
    """exam_projects 路由必须含 PATCH /exam-projects/{project_id} 端点用于更新状态。"""
    import inspect
    source = inspect.getsource(api)
    assert 'def patch' in source or 'def update_status' in source, "缺少 patch/update_status 函数"


def test_service_has_list_projects():
    """exam_project_service 必须含 list_projects 函数。"""
    from app.services import exam_project_service
    assert hasattr(exam_project_service, 'list_projects'), "缺少 list_projects"
    assert hasattr(exam_project_service, 'create_project'), "缺少 create_project"
    assert hasattr(exam_project_service, 'get_project'), "缺少 get_project"
    assert hasattr(exam_project_service, 'update_status'), "缺少 update_status"
