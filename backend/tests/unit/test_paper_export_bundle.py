"""试卷一键打包：包内清单/命名/字节组装 + 版本 404 透传（渲染全部打桩）。"""
import io
import json
import zipfile

import pytest

from app.services import paper_export_bundle as bundle
from app.services.paper_version_service import PaperVersionError


def _stub_renderers(monkeypatch, *, version_no=3):
    """把六份渲染全部换成确定性桩件，聚焦打包本身的组装与命名。"""
    monkeypatch.setattr(
        bundle, "get_paper_version",
        lambda session, pv_id, *, course_id, pv=None: {"id": pv_id, "version_no": version_no},
    )
    monkeypatch.setattr(
        bundle, "export_exam_paper_docx",
        lambda session, pv_id, *, course_id, pv=None: b"PK-\x03\x04exam-docx",
    )
    monkeypatch.setattr(
        bundle, "export_student_paper_html",
        lambda session, pv_id, *, course_id, pv=None: "<html>学生卷</html>",
    )
    monkeypatch.setattr(
        bundle, "export_answer_card_docx",
        lambda session, pv_id, *, course_id, pv=None: b"PK-\x03\x04card-docx",
    )
    monkeypatch.setattr(
        bundle, "export_answer_card_html",
        lambda session, pv_id, *, course_id, pv=None: "<html>答题卡</html>",
    )
    monkeypatch.setattr(
        bundle, "export_answer_key_html",
        lambda session, pv_id, *, course_id, pv=None: "<html>答卷</html>",
    )
    monkeypatch.setattr(
        bundle, "export_answer_detail_json",
        lambda session, pv_id, *, course_id, pv=None: {"version_no": version_no, "questions": []},
    )


def test_bundle_contains_all_six_entries_with_version_names(monkeypatch):
    """六份产物齐、命名带版本号、zip 完整可解、各类型字节原样入包。"""
    _stub_renderers(monkeypatch, version_no=3)

    data, filename = bundle.bundle_paper_exports(object(), "pv1", course_id="c1")

    assert filename == "paper-bundle-v3.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == [
            "考试卷_v3.docx",
            "学生卷_v3.html",
            "答题卡_v3.docx",
            "答题卡_v3.html",
            "答卷_含答案_v3.html",
            "answer_detail_v3.json",
        ]
        assert zf.read("考试卷_v3.docx").startswith(b"PK")
        assert zf.read("学生卷_v3.html").decode("utf-8") == "<html>学生卷</html>"
        assert zf.read("答卷_含答案_v3.html").decode("utf-8") == "<html>答卷</html>"
        parsed = json.loads(zf.read("answer_detail_v3.json"))
        assert parsed == {"version_no": 3, "questions": []}


def test_bundle_defaults_to_v1_when_version_no_missing(monkeypatch):
    """版本号缺失时兜底为 1，命名与文件名不抛 KeyError。"""
    _stub_renderers(monkeypatch, version_no=1)
    monkeypatch.setattr(
        bundle, "get_paper_version",
        lambda session, pv_id, *, course_id, pv=None: {"id": pv_id},
    )

    data, filename = bundle.bundle_paper_exports(object(), "pv1", course_id="c1")

    assert filename == "paper-bundle-v1.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "考试卷_v1.docx" in zf.namelist()


def test_bundle_passes_course_id_to_every_renderer(monkeypatch):
    """课程隔离：六份渲染与版本读取都必须带同一 course_id。"""
    _stub_renderers(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        bundle, "get_paper_version",
        lambda session, pv_id, *, course_id, pv=None: (seen.append(course_id), {"version_no": 1})[1],
    )
    for name in (
        "export_exam_paper_docx", "export_student_paper_html",
        "export_answer_card_docx", "export_answer_card_html",
        "export_answer_key_html", "export_answer_detail_json",
    ):
        original = getattr(bundle, name)

        def _wrapped(session, pv_id, *, course_id, pv=None, _orig=original, _name=name):
            seen.append(f"{_name}:{course_id}")
            return _orig(session, pv_id, course_id=course_id, pv=pv)

        monkeypatch.setattr(bundle, name, _wrapped)

    bundle.bundle_paper_exports(object(), "pv1", course_id="c9")

    assert seen == [
        "c9",
        "export_exam_paper_docx:c9",
        "export_student_paper_html:c9",
        "export_answer_card_docx:c9",
        "export_answer_card_html:c9",
        "export_answer_key_html:c9",
        "export_answer_detail_json:c9",
    ]


def test_bundle_propagates_missing_version(monkeypatch):
    """版本不存在/跨课程访问沿用 PaperVersionError，由 API 层映射 404。"""

    def _boom(session, pv_id, *, course_id):
        raise PaperVersionError(f"试卷版本不存在或不属于该课程: {pv_id}")

    monkeypatch.setattr(bundle, "get_paper_version", _boom)

    with pytest.raises(PaperVersionError, match="试卷版本不存在"):
        bundle.bundle_paper_exports(object(), "pvX", course_id="c1")


def test_bundle_forwards_preloaded_pv_to_renderers(monkeypatch):
    """归档导出路径：调用方预加载 pv（归档快照），六份渲染复用同一份且不再回查版本。"""
    _stub_renderers(monkeypatch, version_no=7)
    pv = {"id": "archive-1", "version_no": 7}
    seen: list[int] = []
    monkeypatch.setattr(
        bundle,
        "get_paper_version",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("已预加载 pv，不应再回查版本")),
    )
    for name in (
        "export_exam_paper_docx", "export_student_paper_html",
        "export_answer_card_docx", "export_answer_card_html",
        "export_answer_key_html", "export_answer_detail_json",
    ):
        original = getattr(bundle, name)

        def _wrapped(session, pv_id, *, course_id, pv=None, _orig=original, _name=name):
            seen.append(id(pv) if pv is not None else -1)
            return _orig(session, pv_id, course_id=course_id, pv=pv)

        monkeypatch.setattr(bundle, name, _wrapped)

    data, filename = bundle.bundle_paper_exports(object(), "archive-1", course_id="c1", pv=pv)

    assert filename == "paper-bundle-v7.zip"
    assert seen == [id(pv)] * 6
