"""Course endpoints for the development owner."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.domain.course.models import CourseCreate, CourseResponse, CourseUpdate
from app.services import course_service

router = APIRouter(prefix="/api/v1/courses", tags=["courses"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="course not found")


@router.post("", response_model=CourseResponse, status_code=status.HTTP_201_CREATED)
def create(payload: CourseCreate, session: Session = Depends(get_session)) -> CourseResponse:
    try:
        return course_service.create_course(session, **payload.model_dump())
    except course_service.CourseConflictError:
        raise HTTPException(status_code=409, detail="course slug already exists")


@router.get("", response_model=list[CourseResponse])
def list_all(session: Session = Depends(get_session)) -> list[CourseResponse]:
    return course_service.list_courses(session)


@router.get("/{course_id}", response_model=CourseResponse)
def get_one(course_id: str, session: Session = Depends(get_session)) -> CourseResponse:
    try:
        return course_service.get_course(session, course_id)
    except course_service.CourseNotFoundError:
        raise _not_found()


@router.patch("/{course_id}", response_model=CourseResponse)
def patch(course_id: str, payload: CourseUpdate, session: Session = Depends(get_session)) -> CourseResponse:
    try:
        return course_service.update_course(session, course_id, **payload.model_dump(exclude_unset=True))
    except course_service.CourseNotFoundError:
        raise _not_found()
    except course_service.CourseConflictError:
        raise HTTPException(status_code=409, detail="course slug already exists")
