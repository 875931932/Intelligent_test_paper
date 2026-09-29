"""试卷一键打包下载：全部导出产物 → 内存 ZIP（纯只读，复用既有导出渲染）。

包内六份与逐个导出端点**同源渲染**（调用同一批 export_* 函数，不复制渲染逻辑）：
考试卷 docx / 学生卷 html / 答题卡 docx + html / 答卷 html / 答案细则 json。
纯 CPU 字符串与字节组装（zipfile 标准库），不涉及 LLM 与外部系统，可同步返回；
所有查询带 course_id 过滤（版本不存在或跨课程访问沿用 PaperVersionError → 404）。
"""

from __future__ import annotations

import io
import json
import zipfile

from sqlalchemy.orm import Session

from app.services.answer_card_docx import export_answer_card_docx
from app.services.exam_paper_docx import export_exam_paper_docx
from app.services.paper_version_service import (
    export_answer_card_html,
    export_answer_detail_json,
    export_answer_key_html,
    export_student_paper_html,
    get_paper_version,
)

# 包内文件名模板：docx/html 带版本号便于教师区分多版本导出，json 沿用既有
# answer_detail_v{n}.json 命名（与单独导出端点的 Content-Disposition 同规则）
_ENTRIES = (
    "考试卷_v{v}.docx",
    "学生卷_v{v}.html",
    "答题卡_v{v}.docx",
    "答题卡_v{v}.html",
    "答卷_含答案_v{v}.html",
    "answer_detail_v{v}.json",
)


def bundle_paper_exports(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
    pv: dict | None = None,
) -> tuple[bytes, str]:
    """打包试卷全部导出产物为 ZIP 字节，返回 ``(zip_bytes, 下载文件名)``。

    任一环节试卷版本不存在/跨课程访问时抛 ``PaperVersionError``，由 API 层映射
    404——与其它导出端点同口径。``pv`` 可传入预加载的试卷 dict（归档导出用），
    逐个渲染器复用同一份，避免重复查库。
    """
    if pv is None:
        pv = get_paper_version(session, paper_version_id, course_id=course_id)
    version_no = int(pv.get("version_no") or 1)

    renderers = (
        export_exam_paper_docx,
        export_student_paper_html,
        export_answer_card_docx,
        export_answer_card_html,
        export_answer_key_html,
        export_answer_detail_json,
    )
    entries: list[tuple[str, bytes]] = []
    for name_tpl, render in zip(_ENTRIES, renderers, strict=True):
        data = render(session, paper_version_id, course_id=course_id, pv=pv)
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        entries.append((name_tpl.format(v=version_no), data))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buffer.getvalue(), f"paper-bundle-v{version_no}.zip"
