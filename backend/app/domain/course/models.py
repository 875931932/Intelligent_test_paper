"""Request and response models for a course."""

from pydantic import BaseModel, Field, model_validator


class CourseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(default="", max_length=120)
    description: str | None = Field(default=None, max_length=10_000)
    # 课程类别（题型集合/格式预设的选取键）；未知值由服务层回退默认类别
    category: str = Field(default="general", max_length=40)


class CourseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="before")
    @classmethod
    def required_fields_cannot_be_null(cls, value):
        if isinstance(value, dict) and any(value.get(field) is None for field in ("name", "slug") if field in value):
            raise ValueError("name and slug cannot be null")
        return value


class CourseResponse(BaseModel):
    id: str
    owner_id: str
    name: str
    slug: str
    description: str | None
    category: str


class CourseCategoryInfo(BaseModel):
    """课程类别清单条目（GET /courses/categories，数据源为类别档案）。"""

    key: str
    label: str
    description: str
    question_types: list[str]
