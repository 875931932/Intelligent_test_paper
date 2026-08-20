"""Request and response models for a course."""

from pydantic import BaseModel, Field, model_validator


class CourseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(default="", max_length=120)
    description: str | None = Field(default=None, max_length=10_000)


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
