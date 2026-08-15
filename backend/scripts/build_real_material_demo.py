"""Build an exam-point-led real-material demonstration pipeline.

The assessment syllabus defines exam points and weights. Teaching materials
are staged, embedded, retrieved, classified, and consolidated only after those
points exist. Source-bearing evidence is isolated from generation payloads.
"""

from __future__ import annotations

# The demo is executable from the repository root, so it adds ``backend``
# before importing the application package.
# ruff: noqa: E402

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.adapters.document.mineru_client import MineruClient
from app.adapters.document.protocol import ParseRequest, ParseState
from app.adapters.model.deepseek_gateway import DeepSeekGateway, DeepSeekJsonClient
from app.adapters.model.deepseek_semantic_extractors import (
    DeepSeekExamPointEvidenceClassifier,
    DeepSeekExamPointKnowledgeConsolidator,
    DeepSeekSyllabusExtractor,
)
from app.adapters.model.embedding_gateway import OpenAICompatibleEmbeddingGateway
from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy
from app.domain.framework.models import AssessmentOutline, TeachingTopic
from app.domain.generation.structure_signature import QuestionStructureSignature
from app.domain.knowledge.models import AssessmentUnitDraft, KnowledgeTreeCandidate
from app.domain.knowledge.relevance import ExamPointFileDecision, RelevanceClass, StagingChunk
from app.services.blueprint_service import allocate_plan_items
from app.services.document_processing_service import read_mineru_zip
from app.services.staging_retrieval_service import retrieve_for_exam_point
from app.workflows.generation_graph import build_generation_graph
from app.workflows.knowledge_catalog_subgraph import build_knowledge_catalog_candidate

SOURCE_DIR = ROOT / "docs" / "素材"
CACHE_DIR = ROOT / ".runtime" / "mineru"
MODEL_CACHE_DIR = ROOT / ".runtime" / "model-curation"
OUTPUT_DIR = ROOT / "frontend" / "public" / "demo"
OUTPUT_FILE = OUTPUT_DIR / "pipeline.json"
CURATION_SCHEMA_VERSION = "task10-exam-point-led-v1"


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
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
    if "课程教学大纲" in path.name:
        return "teaching_syllabus"
    if "课程考核大纲" in path.name:
        return "assessment_syllabus"
    return "teaching_material"


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
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
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


def extraction_blocks(document: dict[str, Any], limit: int = 100_000) -> list[str]:
    result: list[str] = []
    used = 0
    for block in document.get("blocks", []):
        text = re.sub(r"\s+", " ", str(block.get("text", ""))).strip()
        if not text:
            continue
        heading = " / ".join(str(item) for item in block.get("heading_path", []) if item)
        value = f"{heading}：{text}" if heading else text
        if used + len(value) > limit:
            break
        result.append(value)
        used += len(value)
    return result


def build_staging_chunks(document: dict[str, Any], *, max_chars: int = 1400) -> list[StagingChunk]:
    chunks: list[StagingChunk] = []
    for block_index, block in enumerate(document.get("blocks", [])):
        text = re.sub(r"\s+", " ", str(block.get("text", ""))).strip()
        if not text:
            continue
        headings = [str(item) for item in block.get("heading_path", []) if item]
        for piece_index, start in enumerate(range(0, len(text), max_chars)):
            chunks.append(
                StagingChunk(
                    id=f"{document['sha256'][:12]}-C{len(chunks):05d}",
                    material_version_id=document["sha256"],
                    content=text[start : start + max_chars],
                    locator={
                        "filename": document["filename"],
                        "page_index": block.get("page_index"),
                        "heading_path": headings,
                        "block_index": block_index,
                        "piece_index": piece_index,
                    },
                )
            )
    return chunks


def embed_staging_chunks(
    chunks: list[StagingChunk],
    embedder: OpenAICompatibleEmbeddingGateway,
    *,
    batch_size: int = 64,
) -> list[StagingChunk]:
    embedded: list[StagingChunk] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = embedder.embed([chunk.content for chunk in batch])
        if len(vectors) != len(batch):
            raise RuntimeError("embedding response count does not match staging chunks")
        embedded.extend(
            chunk.model_copy(update={"embedding": vector})
            for chunk, vector in zip(batch, vectors, strict=True)
        )
    return embedded


class CachedJsonRequester:
    """Cache semantic curation calls while rerunning validators on every hit."""

    def __init__(self, client: DeepSeekJsonClient, *, context: dict[str, Any]):
        self.client = client
        self.context = context

    def request_json(
        self,
        *,
        system_prompt: str,
        payload: Any,
        temperature: float,
        call_context=None,
        response_validator=None,
    ) -> dict:
        raw = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        cache_contract = {
            "model": self.client.model,
            "schema_version": CURATION_SCHEMA_VERSION,
            "exam_point": self.context.get("exam_point"),
            "material_hash": self.context.get("material_hash"),
            "candidate_chunk_hashes": self.context.get("candidate_chunk_hashes", []),
            "stage": self.context.get("stage"),
            "system_prompt": system_prompt,
            "input_payload": raw,
        }
        digest = hashlib.sha256(
            json.dumps(cache_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache_path = MODEL_CACHE_DIR / f"{digest}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            try:
                if response_validator is not None:
                    response_validator(cached)
                return cached
            except Exception:
                cache_path.unlink(missing_ok=True)
        result = self.client.request_json(
            system_prompt=system_prompt,
            payload=raw,
            temperature=temperature,
            call_context=call_context,
            response_validator=response_validator,
        )
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result


def semantic_client() -> DeepSeekJsonClient:
    return DeepSeekJsonClient(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        timeout=180,
    )


def align_teaching_scope(points: list[ExamPoint], topics: list[TeachingTopic]) -> list[ExamPoint]:
    valid_keys = {topic.key for topic in topics}
    result: list[ExamPoint] = []
    for point in points:
        aligned = [key for key in point.teaching_anchor_keys if key in valid_keys]
        if not aligned:
            ranked = sorted(topics, key=lambda topic: len(set(point.title) & set(topic.title)), reverse=True)
            if ranked and len(set(point.title) & set(ranked[0].title)) >= 2:
                aligned = [ranked[0].key]
        result.append(point.model_copy(update={"teaching_anchor_keys": aligned}))
    return result


def source_free_card(card: dict[str, Any], fallback_statement: str) -> dict[str, Any]:
    prompt_material = card.get("prompt_material") or []
    if isinstance(prompt_material, str):
        prompt_material = [prompt_material]
    return {
        "name": card.get("name", ""),
        "performance_statement": card.get("performance_statement") or fallback_statement,
        "assessable_content": list(card.get("assessable_content") or []),
        "scope_boundary": dict(card.get("scope_boundary") or {}),
        "cognitive_targets": list(card.get("cognitive_targets") or ["understand"]),
        "allowed_question_types": list(card.get("allowed_question_types") or []),
        "prompt_material": list(prompt_material),
        "importance": card.get("importance", 1),
    }


def tree_for_snapshot(tree: KnowledgeTreeCandidate) -> dict[str, Any]:
    topics: list[dict[str, Any]] = []
    for topic in tree.topics:
        units: list[dict[str, Any]] = []
        for unit in topic.units:
            units.append(
                {
                    "code": unit.code,
                    "title": unit.title,
                    "performance_statement": unit.performance_statement,
                    "exam_point_code": unit.exam_point_code,
                    "scope_boundary": unit.scope_boundary,
                    "status": unit.status,
                    "cards": [
                        {
                            "id": f"{unit.code}:{index + 1}",
                            **source_free_card(card.model_dump(mode="json"), unit.performance_statement),
                            "status": card.status,
                        }
                        for index, card in enumerate(unit.cards)
                    ],
                }
            )
        topics.append(
            {
                "code": topic.code,
                "name": topic.name,
                "framework_anchor_key": topic.framework_anchor_key,
                "status": topic.status,
                "units": units,
            }
        )
    return {"framework_version_id": tree.framework_version_id, "topics": topics}


def cards_for_generation(tree: KnowledgeTreeCandidate, points: list[ExamPoint]) -> dict[str, dict[str, Any]]:
    points_by_code = {point.code: point for point in points}
    cards: dict[str, dict[str, Any]] = {}
    for topic in tree.topics:
        for unit in topic.units:
            point = points_by_code.get(unit.exam_point_code)
            if point is None:
                continue
            for index, card in enumerate(unit.cards):
                card_id = f"{unit.code}:{index + 1}"
                semantic = source_free_card(card.model_dump(mode="json"), unit.performance_statement)
                allowed = list(dict.fromkeys(semantic["allowed_question_types"]))
                levels = set(semantic["cognitive_targets"])
                if not allowed:
                    allowed = ["single_choice", "true_false", "fill_blank"]
                if levels & {"understand", "apply", "analyze", "evaluate", "create"}:
                    allowed = list(dict.fromkeys([*allowed, "short_answer"]))
                if levels & {"apply", "analyze", "evaluate", "create"} or point.assessment_orientations:
                    allowed = list(dict.fromkeys([*allowed, "comprehensive"]))
                semantic["allowed_question_types"] = allowed
                cards[card_id] = semantic
    return cards


def build_blueprint(points: list[ExamPoint], tree: KnowledgeTreeCandidate) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    cards = cards_for_generation(tree, points)
    points_by_code = {point.code: point for point in points}
    units: list[UnitCoverage] = []
    active_points: set[str] = set()
    for topic in tree.topics:
        for unit in topic.units:
            point = points_by_code.get(unit.exam_point_code)
            if point is None:
                continue
            card_ids = [f"{unit.code}:{index + 1}" for index in range(len(unit.cards)) if f"{unit.code}:{index + 1}" in cards]
            if not card_ids:
                continue
            active_points.add(point.code)
            modes = ["theory_recall", "conceptual", "application", "problem_solving"]
            if point.operational_detail_policy is OperationalDetailPolicy.DIRECTLY_ASSESSABLE:
                modes.append("practical_operation")
            units.append(
                UnitCoverage(
                    unit_id=unit.code,
                    exam_point_id=point.code,
                    anchor_key=point.code,
                    card_ids=card_ids,
                    allowed_assessment_modes=modes,
                    operational_detail_policy=point.operational_detail_policy.value,
                )
            )
    if not units:
        raise RuntimeError("knowledge tree has no publishable cards")
    raw_weights = {point.code: point.weight_value for point in points if point.code in active_points}
    total = sum(raw_weights.values())
    if total <= 0:
        raise RuntimeError("exam-point weights must contain a positive total")
    weights = {code: round(value * 100 / total, 6) for code, value in raw_weights.items()}
    first = next(iter(weights))
    weights[first] += 100 - sum(weights.values())
    request = BlueprintRequest(
        total_score=100,
        type_rules={
            "single_choice": {"count": 10, "score": 2, "difficulty_distribution": {"low": 40, "medium": 40, "high": 20}, "assessment_mode_distribution": {"theory_recall": 50, "conceptual": 50}},
            "true_false": {"count": 10, "score": 2, "difficulty_distribution": {"low": 50, "medium": 40, "high": 10}, "assessment_mode_distribution": {"theory_recall": 50, "conceptual": 50}},
            "fill_blank": {"count": 10, "score": 1, "difficulty_distribution": {"low": 60, "medium": 40, "high": 0}, "assessment_mode_distribution": {"theory_recall": 80, "conceptual": 20}},
            "short_answer": {"count": 4, "score": 5, "difficulty_distribution": {"low": 20, "medium": 50, "high": 30}, "assessment_mode_distribution": {"conceptual": 40, "application": 30, "problem_solving": 30}},
            "comprehensive": {"count": 3, "score": 10, "difficulty_distribution": {"low": 0, "medium": 40, "high": 60}, "assessment_mode_distribution": {"application": 30, "problem_solving": 70}},
        },
        chapter_weights=weights,
        units=units,
        card_question_types={card_id: card["allowed_question_types"] for card_id, card in cards.items()},
    )
    plan = allocate_plan_items(request)
    return {
        "request": request.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "allocation_basis": "以考核大纲 exam_points 的期末权重归一化分配；UnitCoverage 保留 exam_point_id 与操作政策；每种题型显式配置低→中→高难度与考查方式分布",
        "weight_source": "assessment_syllabus.exam_points",
    }, cards


def relevance_counts(tree: KnowledgeTreeCandidate) -> dict[str, int]:
    counts = {member.value: 0 for member in RelevanceClass}
    for decision in tree.evidence_decisions:
        counts[decision.relevance_class.value] += 1
    return counts


def teacher_review_snapshot(
    documents: list[dict[str, Any]],
    chunks_by_material: dict[str, list[StagingChunk]],
    decisions: list[ExamPointFileDecision],
) -> dict[str, Any]:
    sources = []
    for document in documents:
        if document["material_type"] != "teaching_material":
            continue
        sources.append(
            {
                "material_version_id": document["sha256"],
                "filename": document["filename"],
                "source_path": document["source_path"],
                "chunks": [
                    {"chunk_id": chunk.id, "content": chunk.content, "locator": chunk.locator}
                    for chunk in chunks_by_material.get(document["sha256"], [])
                ],
            }
        )
    return {
        "evidence_source_locations": sources,
        "file_decisions": [decision.model_dump(mode="json") for decision in decisions],
    }


_FORBIDDEN_SOURCE_KEYS = (
    "filename", "source_path", "provider_batch_id", "material_version_id", "material_id",
    "evidence_chunk_id", "evidence_ids", "evidence_refs", "source_locator", "locator",
    "page_index", "page_number", "block_id", "chunk_id", "sha256", "signature_hash",
)
_FORBIDDEN_SOURCE_VALUE = re.compile(
    r"(?:第\s*\d+\s*页|\bpage\s*\d+\b|\bp\.\s*\d+\b|"
    r"文件名|页码|证据(?:id|编号)|材料(?:id|版本)|"
    r"\b[\w./ -]+\.(?:pdf|docx|pptx|py|md|txt)\b)",
    re.IGNORECASE,
)


def assert_source_free(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if any(token in str(key).casefold() for token in _FORBIDDEN_SOURCE_KEYS):
                raise ValueError(f"source-free serialization rejected field: {path}.{key}")
            assert_source_free(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_source_free(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and _FORBIDDEN_SOURCE_VALUE.search(value):
        raise ValueError(f"source-free serialization rejected source locator text: {path}")


def signature_dto(signature: dict[str, Any] | QuestionStructureSignature) -> dict[str, Any]:
    raw = signature.model_dump(mode="json") if hasattr(signature, "model_dump") else dict(signature)
    return {
        "archetype": raw.get("archetype", ""),
        "material_form": raw.get("material_form", ""),
        "cognitive_sequence": list(raw.get("cognitive_sequence") or []),
        "subquestion_actions": list(raw.get("subquestion_actions") or []),
        "answer_boundaries": list(raw.get("answer_boundaries") or []),
        "structure_key": raw.get("structure_key", ""),
        "signature_hash": raw.get("signature_hash", ""),
    }


def read_signature_history() -> list[dict[str, Any]]:
    if not OUTPUT_FILE.exists():
        return []
    try:
        previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = previous.get("signature_history") if isinstance(previous, dict) else None
    if not isinstance(rows, list):
        rows = []
        paper = previous.get("paper", {}) if isinstance(previous, dict) else {}
        questions = paper.get("questions", []) if isinstance(paper, dict) else []
        for question in questions:
            if isinstance(question, dict) and question.get("question_type") == "comprehensive" and isinstance(question.get("structure_signature"), dict):
                rows.append(question["structure_signature"])
    result = []
    for row in rows:
        try:
            result.append(signature_dto(row))
        except (TypeError, ValueError):
            continue
    return result[:5]


def required_environment_missing() -> list[str]:
    required = [
        "MINERU_API_TOKEN", "DEEPSEEK_API_KEY", "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "MINERU_BASE_URL",
    ]
    missing = [name for name in required if not os.getenv(name, "").strip()]
    base = os.getenv("MINERU_BASE_URL", "").strip()
    if base and not re.match(r"^https?://", base):
        missing.append("MINERU_BASE_URL")
    return list(dict.fromkeys(missing))


async def main() -> None:
    load_env()
    missing = required_environment_missing()
    if missing:
        raise RuntimeError("missing required environment variables: " + ", ".join(missing))
    files = sorted(SOURCE_DIR.rglob("*.pdf")) + sorted(SOURCE_DIR.rglob("*.docx"))
    if not files:
        raise RuntimeError(f"no supported files under {SOURCE_DIR}")

    recent_signatures = read_signature_history()
    snapshot: dict[str, Any] = {
        "status": "parsing",
        "started_at": datetime.now().isoformat(),
        "source_directory": str(SOURCE_DIR.resolve()),
        "files_total": len(files),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "curation_schema_version": CURATION_SCHEMA_VERSION,
        "signature_history": recent_signatures,
    }
    write_snapshot(snapshot)

    mineru = MineruClient(base_url=os.environ["MINERU_BASE_URL"], token=os.environ["MINERU_API_TOKEN"], max_attempts=4)
    try:
        semaphore = asyncio.Semaphore(8)
        documents = await asyncio.gather(*(parse_file(mineru, path, semaphore) for path in files))
    finally:
        await mineru.close()
    snapshot.update(
        {
            "status": "framework",
            "extraction": [
                {
                    "filename": item["filename"],
                    "source_path": item["source_path"],
                    "material_type": item["material_type"],
                    "sha256": item["sha256"],
                    "provider_batch_id": item["provider_batch_id"],
                    "block_count": len(item["blocks"]),
                    "content_preview": [
                        {"page": block.get("page_index"), "type": block.get("block_type"), "text": str(block.get("text", ""))[:500]}
                        for block in item["blocks"][:10]
                    ],
                }
                for item in documents
            ],
        }
    )
    write_snapshot(snapshot)

    assessment_document = next(item for item in documents if item["material_type"] == "assessment_syllabus")
    teaching_document = next(item for item in documents if item["material_type"] == "teaching_syllabus")
    teaching_extractor = DeepSeekSyllabusExtractor(
        CachedJsonRequester(semantic_client(), context={"stage": "teaching_syllabus", "material_hash": teaching_document["sha256"]})
    )
    assessment_extractor = DeepSeekSyllabusExtractor(
        CachedJsonRequester(semantic_client(), context={"stage": "assessment_syllabus", "material_hash": assessment_document["sha256"]})
    )
    teaching_result, assessment_result = await asyncio.gather(
        asyncio.to_thread(teaching_extractor.extract_teaching, extraction_blocks(teaching_document)),
        asyncio.to_thread(assessment_extractor.extract_assessment, extraction_blocks(assessment_document)),
    )
    if not isinstance(assessment_result, AssessmentOutline) or not assessment_result.exam_points:
        raise RuntimeError("assessment syllabus did not return exam_points")
    points = align_teaching_scope(assessment_result.exam_points, teaching_result)
    teaching_by_key = {topic.key: topic for topic in teaching_result}
    snapshot.update(
        {
            "status": "knowledge_organization",
            "framework": {
                "teaching_topics": [topic.model_dump(mode="json") for topic in teaching_result],
                "anchors": [anchor.model_dump(mode="json") for anchor in assessment_result.anchors],
                "exam_points": [point.model_dump(mode="json") for point in points],
                "final_exam_rules": assessment_result.final_exam_rules,
                "weight_source": "assessment_syllabus",
                "scope_depth_alignment": {
                    point.code: [
                        teaching_by_key[key].model_dump(mode="json")
                        for key in point.teaching_anchor_keys
                        if key in teaching_by_key
                    ]
                    for point in points
                },
            },
        }
    )
    write_snapshot(snapshot)

    material_documents = [item for item in documents if item["material_type"] == "teaching_material"]
    embedder = OpenAICompatibleEmbeddingGateway(
        api_key=os.environ["EMBEDDING_API_KEY"],
        base_url=os.environ["EMBEDDING_BASE_URL"],
        model=os.environ["EMBEDDING_MODEL"],
        timeout=180,
    )
    chunks_by_material: dict[str, list[StagingChunk]] = {}
    for document in material_documents:
        chunks_by_material[document["sha256"]] = await asyncio.to_thread(
            embed_staging_chunks, build_staging_chunks(document), embedder
        )
    chunk_by_id = {
        chunk.id: chunk
        for chunks in chunks_by_material.values()
        for chunk in chunks
    }
    snapshot["staging"] = {
        "material_files": len(material_documents),
        "chunk_count": len(chunk_by_id),
        "embedding_batch_size": 64,
    }
    write_snapshot(snapshot)

    def classify_one(point: ExamPoint, document: dict[str, Any]) -> ExamPointFileDecision:
        material_hash = document["sha256"]
        ranked = retrieve_for_exam_point(
            point,
            chunks_by_material[material_hash],
            embedder,
            top_k=24,
            minimum_score=0.25,
        )
        recalled = [item.chunk for item in ranked]
        requester = CachedJsonRequester(
            semantic_client(),
            context={
                "stage": "classify_exam_point_file",
                "exam_point": point.model_dump(mode="json"),
                "material_hash": material_hash,
                "candidate_chunk_hashes": [hashlib.sha256(chunk.content.encode()).hexdigest() for chunk in recalled],
            },
        )
        return DeepSeekExamPointEvidenceClassifier(requester).classify(
            exam_point=point,
            material_version_id=material_hash,
            chunks=recalled,
        )

    decisions: list[ExamPointFileDecision] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(classify_one, point, document) for point in points for document in material_documents]
        for future in as_completed(futures):
            decisions.append(future.result())
    decisions.sort(key=lambda item: (item.exam_point_code, item.material_version_id))
    snapshot["knowledge_organization"] = {"pair_count": len(decisions), "classifier_max_workers": 16}
    write_snapshot(snapshot)

    admitted_by_point: dict[str, list] = {point.code: [] for point in points}
    for file_decision in decisions:
        admitted_by_point[file_decision.exam_point_code].extend(
            decision
            for decision in file_decision.decisions
            if decision.relevance_class in {RelevanceClass.DIRECT, RelevanceClass.SUPPORTING}
        )

    def consolidate_one(point: ExamPoint) -> tuple[str, list[AssessmentUnitDraft]]:
        admitted = admitted_by_point.get(point.code, [])
        if not admitted:
            return point.code, []
        candidate_hashes = [
            hashlib.sha256(chunk_by_id[decision.evidence_chunk_id].content.encode()).hexdigest()
            for decision in admitted
            if decision.evidence_chunk_id in chunk_by_id
        ]
        requester = CachedJsonRequester(
            semantic_client(),
            context={
                "stage": "consolidate_exam_point",
                "exam_point": point.model_dump(mode="json"),
                "material_hash": "multi-material",
                "candidate_chunk_hashes": candidate_hashes,
            },
        )
        units = DeepSeekExamPointKnowledgeConsolidator(requester).consolidate(
            exam_point=point,
            admitted_decisions=admitted,
        )
        return point.code, units

    consolidated: dict[str, list[AssessmentUnitDraft]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(consolidate_one, point) for point in points]
        for future in as_completed(futures):
            code, units = future.result()
            consolidated[code] = units
    tree = build_knowledge_catalog_candidate(
        framework_version_id=f"demo:{assessment_document['sha256'][:12]}",
        exam_points=points,
        file_decisions=decisions,
        consolidated_units=consolidated,
    )
    snapshot.update(
        {
            "status": "blueprint",
            "knowledge_tree": tree_for_snapshot(tree),
            "knowledge_organization": {
                **snapshot.get("knowledge_organization", {}),
                "relevance_counts": relevance_counts(tree),
                "consolidator_max_workers": 8,
            },
            "exam_point_coverage": [coverage.model_dump(mode="json") for coverage in tree.coverage],
            "teacher_review": teacher_review_snapshot(documents, chunks_by_material, decisions),
        }
    )
    blueprint, cards = build_blueprint(points, tree)
    assert_source_free(cards, path="knowledge_cards")
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
        build_generation_graph(gateway).invoke,
        {
            "plan_items": blueprint["plan"]["items"],
            "knowledge_cards": cards,
            "recent_structure_signatures": recent_signatures[:5],
        },
    )
    payload_snapshots = {
        str(index): payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        for index, payload in (result.get("payloads") or {}).items()
    }
    assert_source_free(payload_snapshots, path="source_free_generation_payloads")
    questions = result.get("questions", [])
    signatures = [
        signature_dto(question["structure_signature"])
        for question in questions
        if question.get("question_type") == "comprehensive"
        and isinstance(question.get("structure_signature"), dict)
    ]
    snapshot.update(
        {
            "status": "complete",
            "completed_at": datetime.now().isoformat(),
            "source_free_generation_payloads": payload_snapshots,
            "paper": {
                "questions": questions,
                "total_score": 100,
                "question_count": len(questions),
                "comprehensive_signatures": signatures,
            },
            "signature_history": (signatures + recent_signatures)[:5],
        }
    )
    write_snapshot(snapshot)
    log(f"Demo complete: {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        log(f"Demo failed: {type(exc).__name__}: {exc}")
        raise
