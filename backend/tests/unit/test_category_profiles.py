"""课程类别档案：自洽性、回退语义与清单形状。"""

from app.domain.course.category_profiles import (
    COURSE_CATEGORY_PROFILES,
    DEFAULT_CATEGORY,
    available_categories,
    category_profile,
    normalize_category,
)
from app.domain.generation.question_formats import QUESTION_TEMPLATES

REGISTERED_TYPES = set(QUESTION_TEMPLATES) | {"comprehensive"}


def test_profiles_are_self_consistent():
    for key, profile in COURSE_CATEGORY_PROFILES.items():
        assert key and profile["label"] and profile["description"], key
        assert profile["question_types"], key
        for qt in profile["question_types"]:
            # 题型集合可含综合题；但格式覆盖只能落在已登记的基础题型
            assert qt in REGISTERED_TYPES, (key, qt)
        for qt, template in profile["type_formats"].items():
            assert qt in QUESTION_TEMPLATES, (key, qt)  # 综合题格式归原型档案
            assert isinstance(template, str) and template.strip(), (key, qt)
            assert len(template) <= 2000, (key, qt)


def test_general_profile_defers_to_global_archive():
    assert category_profile(DEFAULT_CATEGORY)["type_formats"] == {}
    assert normalize_category(DEFAULT_CATEGORY) == DEFAULT_CATEGORY


def test_unknown_category_falls_back_to_general():
    assert category_profile("bogus") is COURSE_CATEGORY_PROFILES[DEFAULT_CATEGORY]
    assert category_profile(None) is COURSE_CATEGORY_PROFILES[DEFAULT_CATEGORY]
    assert category_profile("  ") is COURSE_CATEGORY_PROFILES[DEFAULT_CATEGORY]
    assert normalize_category("bogus") == DEFAULT_CATEGORY
    assert normalize_category(None) == DEFAULT_CATEGORY


def test_available_categories_matches_profiles():
    items = available_categories()
    assert [item["key"] for item in items] == list(COURSE_CATEGORY_PROFILES)
    for item in items:
        profile = COURSE_CATEGORY_PROFILES[item["key"]]
        assert item["label"] == profile["label"]
        assert item["question_types"] == profile["question_types"]


def test_styled_categories_really_differ_from_global_default():
    """类别要有实际区分度：除通用档外都带格式预设，且模板长于占位符。"""
    styled = [key for key, profile in COURSE_CATEGORY_PROFILES.items() if profile["type_formats"]]
    assert set(styled) >= {"computer", "science_engineering", "humanities"}
    for key in styled:
        for template in COURSE_CATEGORY_PROFILES[key]["type_formats"].values():
            assert len(template) > 60, key


def test_objective_formats_carry_uniqueness_guard():
    """客观题模板是**完整**任务卡：替换而非追加，必须自带答案唯一性防线。"""
    for key, profile in COURSE_CATEGORY_PROFILES.items():
        for qt in ("single_choice", "multiple_choice", "fill_blank"):
            template = profile["type_formats"].get(qt)
            if template is not None:
                assert "唯一" in template, (key, qt)
