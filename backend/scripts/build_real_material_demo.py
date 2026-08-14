"""Build a real exam demo from docs/素材 through MinerU and DeepSeek.

The script intentionally keeps source-bearing extraction artifacts separate
from source-free question-generation payloads. Runtime artifacts are written
to frontend/public/demo so the development UI can display every checkpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.adapters.document.mineru_client import MineruClient
from app.adapters.document.protocol import ParseRequest, ParseState
from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.domain.blueprint.models import BlueprintRequest
from app.services.blueprint_service import allocate_plan_items
from app.services.document_processing_service import read_mineru_zip
from app.services.generation_service import validate_generated_question
from app.workflows.generation_graph import build_generation_graph

SOURCE_DIR = ROOT / "docs" / "素材"
CACHE_DIR = ROOT / ".runtime" / "mineru"
MODEL_CACHE_DIR = ROOT / ".runtime" / "model-json"
OUTPUT_DIR = ROOT / "frontend" / "public" / "demo"
OUTPUT_FILE = OUTPUT_DIR / "pipeline.json"


def load_env() -> None:
    env_file = ROOT / ".env"
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def write_snapshot(payload: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(OUTPUT_FILE)


def source_type(path: Path) -> str:
    name = path.name
    if "课程教学大纲" in name:
        return "teaching_syllabus"
    if "课程考核大纲" in name:
        return "assessment_syllabus"
    return "teaching_material"


def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


async def file_chunks(path: Path):
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            yield chunk


async def parse_file(client: MineruClient, path: Path, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache_json = CACHE_DIR / f"{digest}.json"
    if cache_json.exists():
        cached = json.loads(cache_json.read_text(encoding="utf-8"))
        log(f"MinerU cache hit: {path.name} ({len(cached['blocks'])} blocks)")
        return cached

    async with semaphore:
        request = ParseRequest(
            material_version_id=digest[:32],
            filename=path.name,
            content_type=content_type(path),
            content_factory=lambda: file_chunks(path),
            model_version=os.getenv("MINERU_MODEL_VERSION", "vlm"),
        )
        submission = await client.submit(request)
        log(f"MinerU submitted: {path.name} -> {submission.provider_batch_id}")
        while True:
            await asyncio.sleep(max(2, int(os.getenv("MINERU_POLL_INTERVAL_SECONDS", "5"))))
            progress = await client.poll(submission.provider_batch_id)
            if progress.state == ParseState.DONE:
                break
            if progress.state == ParseState.FAILED:
                raise RuntimeError(f"MinerU failed for {path.name}: {progress.error_summary}")
        artifact = await client.fetch(submission.provider_batch_id)
        parsed = read_mineru_zip(artifact.content)
        result = {
            "filename": path.name,
            "source_path": str(path.resolve()),
            "material_type": source_type(path),
            "sha256": digest,
            "provider_batch_id": submission.provider_batch_id,
            "blocks": [asdict(block) for block in parsed.blocks],
        }
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_json.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        (CACHE_DIR / f"{digest}.zip").write_bytes(artifact.content)
        log(f"MinerU ready: {path.name} ({len(parsed.blocks)} blocks)")
        return result


def compact_blocks(document: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    used = 0
    for index, block in enumerate(document["blocks"]):
        text = re.sub(r"\s+", " ", str(block.get("text", ""))).strip()
        if not text:
            continue
        value = {
            "block_id": f"B{index:04d}",
            "page": block.get("page_index"),
            "type": block.get("block_type"),
            "text": text,
        }
        encoded = len(text)
        if used + encoded > limit:
            break
        result.append(value)
        used += encoded
    return result


class JsonModel:
    def __init__(self) -> None:
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        self.api_key = os.environ["DEEPSEEK_API_KEY"]
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        self.client = httpx.AsyncClient(timeout=180)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, system: str, payload: dict[str, Any], *, retries: int = 4) -> dict[str, Any]:
        cache_material = json.dumps({"model": self.model, "system": system, "payload": payload}, ensure_ascii=False, sort_keys=True)
        cache_path = MODEL_CACHE_DIR / f"{hashlib.sha256(cache_material.encode()).hexdigest()}.json"
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
        error = ""
        for attempt in range(retries + 1):
            user_payload = payload if not error else {**payload, "previous_error": error, "repair_instruction": "严格修复JSON结构，不要解释"}
            try:
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                        ],
                    },
                )
                response.raise_for_status()
                content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
                if content:
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                            cache_path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
                            return parsed
                        error = "JSON根节点不是对象"
                    except json.JSONDecodeError as exc:
                        error = f"JSON解析失败: {exc}"
                else:
                    error = "模型返回空内容"
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            log(f"DeepSeek JSON retry {attempt + 1}: {error}")
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 12))
        raise RuntimeError(error)


class CachedQuestionGateway:
    def __init__(self, gateway: DeepSeekGateway) -> None:
        self.gateway = gateway

    def plan_coverage(self, payload) -> dict[str, Any]:
        return self.gateway.plan_coverage(payload)

    def audit_paper(self, payload) -> dict[str, Any]:
        return self.gateway.audit_paper(payload)

    def generate(self, payload) -> dict[str, Any]:
        raw = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
        material = json.dumps({"model": self.gateway.model, "payload": raw}, ensure_ascii=False, sort_keys=True)
        cache_path = MODEL_CACHE_DIR / f"question-{hashlib.sha256(material.encode()).hexdigest()}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            candidate = {**cached, "question_type": raw["question_type"], "score": raw["score"]}
            if validate_generated_question(candidate)["status"] == "pass":
                return cached
            cache_path.unlink()
        result = self.gateway.generate(payload)
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result


async def build_framework(model: JsonModel, documents: list[dict[str, Any]]) -> dict[str, Any]:
    teaching = next(item for item in documents if item["material_type"] == "teaching_syllabus")
    assessment = next(item for item in documents if item["material_type"] == "assessment_syllabus")
    teaching_task = model.call(
        "你是高校课程教学大纲分析专家。只提取教学内容与要求中的实际教学主题，不提取课程封面、日期、教材信息、表格标题和行政描述。返回严格JSON。",
        {
            "task": "从教学大纲提取教学主题，为考核大纲对齐提供依据",
            "schema": {"topics": [{"key": "stable_english_key", "title": "主题", "depth": "了解|理解|掌握|应用", "requirements": ["可观察学习要求"]}]},
            "blocks": compact_blocks(teaching, 100_000),
        },
    )
    assessment_task = model.call(
        "你是高校期末考试考核大纲分析专家。考试范围和权重必须以期末考试栏为主，禁止把平时作业、实验过程考核的权重当作期末试卷权重。返回严格JSON。",
        {
            "task": "提取期末考试考核锚点、比例、能力要求、可用题型和明确排除项；锚点权重合计必须为100",
            "schema": {
                "anchors": [{"key": "stable_english_key", "title": "考核内容", "exam_weight": 0, "ability_requirements": [], "allowed_question_types": ["single_choice", "true_false", "fill_blank", "short_answer"], "excluded_content": [], "alignment_keys": []}],
                "final_exam_rules": {"exam_form": "闭卷或开卷", "total_score": 100, "duration_minutes": 120, "source_statement": "从大纲提取的规则"},
            },
            "blocks": compact_blocks(assessment, 100_000),
        },
    )
    teaching_result, assessment_result = await asyncio.gather(teaching_task, assessment_task)
    topics = teaching_result.get("topics")
    anchors = assessment_result.get("anchors")
    if not isinstance(topics, list) or not topics:
        raise RuntimeError("教学大纲模型没有返回topics")
    if not isinstance(anchors, list) or not anchors:
        raise RuntimeError("考核大纲模型没有返回anchors")
    total = sum(float(item.get("exam_weight", 0)) for item in anchors)
    if abs(total - 100) > 0.01:
        repaired = await model.call(
            "你负责校正高校期末考试权重。只能依据原始提取结果校正明显的百分比结构，返回JSON。",
            {"task": "把anchors的exam_weight校正为合计100，其他内容保持不变", "anchors": anchors, "current_total": total},
        )
        anchors = repaired.get("anchors", anchors)
        total = sum(float(item.get("exam_weight", 0)) for item in anchors)
    if abs(total - 100) > 0.01:
        raise RuntimeError(f"期末考试权重合计为{total}，无法构建蓝图")
    topic_keys = {str(item.get("key")) for item in topics}
    for anchor in anchors:
        alignment = [key for key in anchor.get("alignment_keys", []) if key in topic_keys]
        if not alignment:
            title = str(anchor.get("title", ""))
            ranked = sorted(topics, key=lambda topic: len(set(title) & set(str(topic.get("title", "")))), reverse=True)
            alignment = [str(ranked[0]["key"])] if ranked else []
        anchor["alignment_keys"] = alignment
    return {
        "teaching_topics": topics,
        "anchors": anchors,
        "final_exam_rules": assessment_result.get("final_exam_rules", {}),
        "source_versions": {"teaching": teaching["sha256"], "assessment": assessment["sha256"]},
    }


async def extract_material_candidate(model: JsonModel, document: dict[str, Any], framework: dict[str, Any], semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        result = await model.call(
            "你是跨学科高校期末命题知识整理专家。依据考核框架从一份教学材料中提炼可考知识。重点保留理论、概念、原理、比较、理解、应用和问题解决；剔除安装下载命令、环境版本、操作系统要求、文件名、实验编号、报告封面、提交要求、截图要求、机械操作步骤和偶然参数。不要把资料来源写进知识名称。返回严格JSON。",
            {
                "task": "一个文件独立整理候选知识，所有内容必须落在给定考核锚点内",
                "framework": {"anchors": framework["anchors"]},
                "schema": {
                    "topics": [{
                        "code": "stable_code", "name": "知识主题", "framework_anchor_key": "anchor key", "status": "active",
                        "units": [{"code": "stable_code", "title": "考核单元", "performance_statement": "学生能够...", "scope_boundary": {"include": [], "exclude": []}, "status": "active", "cards": [{"name": "纯净知识点", "performance_statement": "学生能够...", "assessable_content": ["可直接命题的事实、原理或关系"], "scope_boundary": {"include": [], "exclude": []}, "cognitive_targets": ["remember|understand|apply|analyze"], "allowed_question_types": ["single_choice", "true_false", "fill_blank", "short_answer"], "importance": 1, "evidence_block_ids": ["B0001"], "status": "active"}]}]
                    }],
                    "unmatched": [{"label": "被剔除内容", "reason": "原因"}],
                },
                "blocks": compact_blocks(document, 65_000),
            },
        )
        topics = result.get("topics") if isinstance(result.get("topics"), list) else []
        valid_anchor_keys = {item["key"] for item in framework["anchors"]}
        cleaned_topics = []
        for topic in topics:
            if topic.get("framework_anchor_key") not in valid_anchor_keys:
                continue
            for unit in topic.get("units", []):
                for card in unit.get("cards", []):
                    refs = [str(item) for item in card.pop("evidence_block_ids", []) if re.fullmatch(r"B\d{4}", str(item))]
                    card["evidence_refs"] = [f"{document['sha256'][:12]}:{item}" for item in refs]
            cleaned_topics.append(topic)
        candidate = {
            "material_version": document["sha256"],
            "filename": document["filename"],
            "source_path": document["source_path"],
            "topics": cleaned_topics,
            "unmatched": result.get("unmatched", []),
        }
        log(f"DeepSeek material organized: {document['filename']} ({sum(len(t.get('units', [])) for t in cleaned_topics)} units)")
        return candidate


async def consolidate_tree(model: JsonModel, framework: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    result = await model.call(
        "你是高校期末考试知识树总编。合并多份材料的同义、近义和上下位知识，避免知识点过散。知识树必须服务于期末命题，而不是复述材料目录。保留候选中已有的evidence_refs供教师追溯，但名称和可考内容不得包含来源话术。返回严格JSON。",
        {
            "task": "形成L2主题-L3考核单元-L4纯净知识卡；每个考核锚点最多4个主题，每主题最多3个单元，每单元1至3张知识卡",
            "framework": {"anchors": framework["anchors"]},
            "schema": {
                "topics": [{"code": "topic_code", "name": "主题", "framework_anchor_key": "anchor key", "status": "active", "units": [{"code": "unit_code", "title": "考核单元", "performance_statement": "学生能够...", "scope_boundary": {"include": [], "exclude": []}, "status": "active", "cards": [{"id": "card_code", "name": "纯净知识点", "performance_statement": "学生能够...", "assessable_content": [], "scope_boundary": {"include": [], "exclude": []}, "cognitive_targets": [], "allowed_question_types": [], "importance": 1, "evidence_refs": [], "status": "active"}]}]}],
                "excluded_summary": ["被系统性剔除的内容类型"],
            },
            "file_candidates": candidates,
        },
    )
    topics = result.get("topics")
    if not isinstance(topics, list) or not topics:
        raise RuntimeError("知识树汇总没有返回topics")
    seen_cards: set[str] = set()
    for topic_index, topic in enumerate(topics):
        topic.setdefault("code", f"topic_{topic_index + 1}")
        for unit_index, unit in enumerate(topic.get("units", [])):
            unit.setdefault("code", f"{topic['code']}_unit_{unit_index + 1}")
            for card_index, card in enumerate(unit.get("cards", [])):
                base = re.sub(r"[^a-z0-9_]+", "_", str(card.get("id", "")).lower()).strip("_") or f"card_{topic_index + 1}_{unit_index + 1}_{card_index + 1}"
                card_id = base
                suffix = 2
                while card_id in seen_cards:
                    card_id = f"{base}_{suffix}"
                    suffix += 1
                card["id"] = card_id
                seen_cards.add(card_id)
    return {"topics": topics, "excluded_summary": result.get("excluded_summary", [])}


def build_blueprint(framework: dict[str, Any], tree: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    units = []
    knowledge_cards: dict[str, dict[str, Any]] = {}
    card_question_types: dict[str, list[str]] = {}
    anchors_with_units: set[str] = set()
    for topic in tree["topics"]:
        anchor = topic["framework_anchor_key"]
        for unit in topic.get("units", []):
            card_ids = []
            for card in unit.get("cards", []):
                card_id = card["id"]
                card_ids.append(card_id)
                cognitive_targets = card.get("cognitive_targets", ["understand"])
                allowed_types = list(card.get("allowed_question_types", []))
                if any(level in {"apply", "analyze", "evaluate", "create"} for level in cognitive_targets):
                    allowed_types = list(dict.fromkeys([*allowed_types, "short_answer", "comprehensive"]))
                if any(level in {"remember", "understand"} for level in cognitive_targets):
                    allowed_types = list(dict.fromkeys([*allowed_types, "single_choice", "true_false", "fill_blank"]))
                card_question_types[card_id] = allowed_types
                knowledge_cards[card_id] = {
                    "name": card.get("name", ""),
                    "performance_statement": card.get("performance_statement", unit.get("performance_statement", "")),
                    "assessable_content": card.get("assessable_content", []),
                    "scope_boundary": card.get("scope_boundary", unit.get("scope_boundary", {})),
                    "cognitive_targets": cognitive_targets,
                    "allowed_question_types": allowed_types,
                }
            if card_ids:
                units.append({"unit_id": unit["code"], "anchor_key": anchor, "card_ids": card_ids})
                anchors_with_units.add(anchor)
    anchors = [item for item in framework["anchors"] if item["key"] in anchors_with_units]
    total_weight = sum(float(item["exam_weight"]) for item in anchors)
    weights = {item["key"]: round(float(item["exam_weight"]) * 100 / total_weight, 6) for item in anchors}
    difference = 100 - sum(weights.values())
    if weights:
        first = next(iter(weights))
        weights[first] += difference
    request = BlueprintRequest(
        total_score=100,
        type_rules={
            "single_choice": {"count": 10, "score": 2, "difficulty_distribution": {"low": 40, "medium": 40, "high": 20}},
            "true_false": {"count": 10, "score": 2, "difficulty_distribution": {"low": 50, "medium": 40, "high": 10}},
            "fill_blank": {"count": 10, "score": 1, "difficulty_distribution": {"low": 60, "medium": 40, "high": 0}},
            "short_answer": {"count": 4, "score": 5, "difficulty_distribution": {"low": 20, "medium": 50, "high": 30}},
            "comprehensive": {"count": 3, "score": 10, "difficulty_distribution": {"low": 0, "medium": 40, "high": 60}},
        },
        chapter_weights=weights,
        units=units,
        card_question_types=card_question_types,
    )
    plan = allocate_plan_items(request)
    return {
        "request": request.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "allocation_basis": "严格采用考核大纲期末考试栏：选择20%、判断20%、填空10%、简答20%、综合30%；各题型独立配置低中高比例并按低到高排序",
    }, knowledge_cards


async def main() -> None:
    load_env()
    files = sorted(SOURCE_DIR.rglob("*.pdf")) + sorted(SOURCE_DIR.rglob("*.docx"))
    if not files:
        raise RuntimeError(f"no supported files under {SOURCE_DIR}")
    snapshot: dict[str, Any] = {
        "status": "parsing",
        "started_at": datetime.now().isoformat(),
        "source_directory": str(SOURCE_DIR.resolve()),
        "files_total": len(files),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    }
    write_snapshot(snapshot)

    mineru = MineruClient(base_url=os.environ["MINERU_BASE_URL"], token=os.environ["MINERU_API_TOKEN"], max_attempts=4)
    try:
        parse_semaphore = asyncio.Semaphore(8)
        documents = await asyncio.gather(*(parse_file(mineru, path, parse_semaphore) for path in files))
    finally:
        await mineru.close()
    snapshot.update({
        "status": "framework",
        "extraction": [{
            "filename": item["filename"], "source_path": item["source_path"], "material_type": item["material_type"],
            "sha256": item["sha256"], "provider_batch_id": item["provider_batch_id"], "block_count": len(item["blocks"]),
            "content_preview": compact_blocks(item, 5_000),
        } for item in documents],
    })
    write_snapshot(snapshot)

    model = JsonModel()
    try:
        framework = await build_framework(model, documents)
        snapshot.update({"status": "knowledge_organization", "framework": framework})
        write_snapshot(snapshot)
        material_documents = [item for item in documents if item["material_type"] == "teaching_material"]
        semaphore = asyncio.Semaphore(8)
        candidates = await asyncio.gather(*(extract_material_candidate(model, item, framework, semaphore) for item in material_documents))
        snapshot["file_candidates"] = candidates
        snapshot["status"] = "knowledge_consolidation"
        write_snapshot(snapshot)
        tree = await consolidate_tree(model, framework, candidates)
        snapshot.update({"status": "blueprint", "knowledge_tree": tree})
        blueprint, cards = build_blueprint(framework, tree)
        snapshot["blueprint"] = blueprint
        snapshot["status"] = "generating"
        write_snapshot(snapshot)

        gateway = DeepSeekGateway(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            timeout=180,
        )
        result = await asyncio.to_thread(
            build_generation_graph(CachedQuestionGateway(gateway)).invoke,
            {"plan_items": blueprint["plan"]["items"], "knowledge_cards": cards},
        )
        snapshot.update({
            "status": "complete",
            "completed_at": datetime.now().isoformat(),
            "paper": {"questions": result["questions"], "total_score": 100, "question_count": len(result["questions"])},
        })
        write_snapshot(snapshot)
        log(f"Demo complete: {OUTPUT_FILE}")
    except Exception as exc:
        snapshot.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        write_snapshot(snapshot)
        raise
    finally:
        await model.close()


if __name__ == "__main__":
    asyncio.run(main())
