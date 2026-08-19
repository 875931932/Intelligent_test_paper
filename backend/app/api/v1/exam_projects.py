"""exam_projects CRUD 端点（课程作用域）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services import exam_project_service

router = APIRouter(prefix="/api/v1/courses/{course_id}/exam-projects", tags=["exam-projects"])


class ExamProjectCreate(BaseModel):
    name: str


class ExamProjectStatusUpdate(BaseModel):
    status: str


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="exam project not found")


@router.get("", response_model=list[dict])
def list_projects(course_id: str, session: Session = Depends(get_session)) -> list[dict]:
    return exam_project_service.list_projects(session, course_id=course_id)


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
def create(course_id: str, payload: ExamProjectCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.create_project(session, course_id=course_id, name=payload.name)
    except exam_project_service.ExamProjectConflictError:
        raise HTTPException(status_code=409, detail="project name already exists in this course")


@router.get("/{project_id}", response_model=dict)
def get_one(course_id: str, project_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.get_project(session, course_id=course_id, project_id=project_id)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()


@router.patch("/{project_id}", response_model=dict)
def patch(course_id: str, project_id: str, payload: ExamProjectStatusUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.update_status(session, course_id=course_id, project_id=project_id, status=payload.status)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()
