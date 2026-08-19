"""Course-scoped browser-direct uploads, staged material and parse endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import MinioStorage, StoragePort, StoragePreconditionError, StorageUnavailableError
from app.config import settings
from app.db.session import get_session
from app.domain.material.models import MaterialResponse, MaterialVersionResponse, UploadSessionCreate, UploadSessionResponse
from app.services import course_service, material_service
from app.services import parse_service

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["materials"])


def get_storage(request: Request) -> StoragePort:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        try:
            storage = MinioStorage(
                endpoint=settings.s3_endpoint, access_key=settings.s3_access_key, secret_key=settings.s3_secret_key,
                bucket=settings.s3_bucket, region=settings.s3_region,
            )
        except Exception:
            raise HTTPException(status_code=503, detail="object storage unavailable")
        request.app.state.storage = storage
    return storage


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="resource not found")


@router.post("/upload-sessions", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
def create_upload_session(
    course_id: str, payload: UploadSessionCreate, session: Session = Depends(get_session), storage: StoragePort = Depends(get_storage)
) -> UploadSessionResponse:
    try:
        result, _ = material_service.create_upload_session(
            session, storage, course_id=course_id, request=payload, max_bytes=settings.upload_max_bytes
        )
        return result
    except course_service.CourseNotFoundError:
        raise _not_found()
    except material_service.MaterialNotFoundError:
        raise _not_found()
    except material_service.UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except material_service.MaterialConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="object storage unavailable")


@router.post("/upload-sessions/{session_id}/complete", response_model=MaterialVersionResponse)
def complete_upload_session(
    course_id: str, session_id: str, session: Session = Depends(get_session), storage: StoragePort = Depends(get_storage)
) -> MaterialVersionResponse:
    try:
        return material_service.complete_upload_session(session, storage, course_id=course_id, session_id=session_id)
    except (course_service.CourseNotFoundError, material_service.UploadSessionNotFoundError):
        raise _not_found()
    except material_service.StorageMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except material_service.UploadCompletionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except material_service.UploadExpiredError as exc:
        raise HTTPException(status_code=410, detail=str(exc))
    except StoragePreconditionError:
        raise HTTPException(status_code=409, detail="uploaded object changed during completion")
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="object storage unavailable")
    except material_service.MaterialConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/materials", response_model=list[MaterialResponse])
def list_materials(course_id: str, include_deleted: bool = False, session: Session = Depends(get_session)) -> list[dict]:
    try:
        items = material_service.list_materials(session, course_id=course_id, include_deleted=include_deleted)
    except course_service.CourseNotFoundError:
        raise _not_found()
    # 附带最新版本的解析状态（无版本/未解析为 null），前端资料区直显
    for item in items:
        version = item.get("latest_version")
        if version is not None:
            item["parse_status"] = parse_service.latest_parse_status(
                session, course_id=course_id, material_version_id=version["id"]
            )
    return items

@router.get("/materials/{material_id}", response_model=MaterialResponse)
def get_material(course_id: str, material_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        item = material_service.get_material(session, course_id=course_id, material_id=material_id)
    except (course_service.CourseNotFoundError, material_service.MaterialNotFoundError):
        raise _not_found()
    version = item.get("latest_version")
    if version is not None:
        item["parse_status"] = parse_service.latest_parse_status(
            session, course_id=course_id, material_version_id=version["id"]
        )
    return item

@router.post("/materials/{material_id}/parse", status_code=status.HTTP_202_ACCEPTED)
def start_material_parse(
    course_id: str, material_id: str, session: Session = Depends(get_session), storage: StoragePort = Depends(get_storage)
) -> dict:
    """为资料最新版本启动 MinerU 解析（同哈希 ready 结果直接复用）。"""
    try:
        return parse_service.start_parse(session, storage, course_id=course_id, material_id=material_id)
    except (course_service.CourseNotFoundError, material_service.MaterialNotFoundError):
        raise _not_found()
    except parse_service.ParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

@router.post("/materials/{material_id}/parse/poll")
def poll_material_parse(
    course_id: str, material_id: str, session: Session = Depends(get_session), storage: StoragePort = Depends(get_storage)
) -> dict:
    """推进一次解析状态机（轮询 MinerU；完成则落块），前端周期调用直至 ready/failed。"""
    try:
        return parse_service.advance_parse(session, storage, course_id=course_id, material_id=material_id)
    except (course_service.CourseNotFoundError, material_service.MaterialNotFoundError):
        raise _not_found()
    except parse_service.ParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(course_id: str, material_id: str, session: Session = Depends(get_session)) -> Response:
    try:
        material_service.delete_material(session, course_id=course_id, material_id=material_id)
    except (course_service.CourseNotFoundError, material_service.MaterialNotFoundError):
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
