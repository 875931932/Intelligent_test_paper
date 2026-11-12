"""课程域统一授权依赖：服务端派生 actor 与 course 上下文。

课程级路由（资料、框架、知识目录、项目、试卷、档案、助手）此前只校验
"已登录"，任意教师凭已知 course_id 即可读写他人课程。这里把 owner 校验
收敛成一个依赖，路由组级挂载一次覆盖全部子路由，新增端点默认受保护。

纪律：管理身份不自动获得其他教师课程内容；前端传来的 owner_id 一律不采信。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.db.schema import Course, User
from app.db.session import get_session
from app.services import course_service


@dataclass(frozen=True)
class CourseContext:
    """一次课程作用域请求的服务端上下文（actor 一定是课程 owner）。"""

    course: Course
    actor: User

    @property
    def course_id(self) -> str:
        return self.course.id

    @property
    def actor_id(self) -> str:
        return self.actor.id


def require_owned_course(
    course_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> CourseContext:
    """校验当前账号是路径上课程的 owner，否则统一 404。

    不存在与无权访问返回同一响应，避免通过 403/404 差异探测他人课程 ID。
    """

    try:
        course = course_service.get_owned_course(session, current_user.id, course_id)
    except course_service.CourseNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="course not found")
    return CourseContext(course=course, actor=current_user)
