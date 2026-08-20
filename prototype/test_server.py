import json
import io
import tempfile
import unittest
from pathlib import Path
from threading import Barrier, Lock
from unittest.mock import patch
from urllib.error import URLError

import server


class BlueprintDistributionTests(unittest.TestCase):
    def _points(self, count: int) -> list[dict]:
        chapters = ["RAG应用", "模型微调", "推理部署", "评测与对齐"]
        return [
            {
                "id": f"kp_{index:02d}",
                "name": f"知识点{index:02d}",
                "chapter": chapters[index % len(chapters)],
                "importance": "key" if index % 3 == 0 else "normal",
            }
            for index in range(count)
        ]

    def test_assign_knowledge_points_caps_hot_topics(self) -> None:
        points = self._points(6)
        ordered = server.assign_knowledge_points(points, 15)

        counts: dict[str, int] = {}
        for point, _cursor, _total, _label in ordered:
            counts[point["id"]] = counts.get(point["id"], 0) + 1
        self.assertLessEqual(max(counts.values()), 4)
        self.assertEqual(sum(counts.values()), 15)

    def test_assign_knowledge_points_spreads_chapters(self) -> None:
        ordered = server.assign_knowledge_points(self._points(8), 12)

        chapters = [point["chapter"] for point, _cursor, _total, _label in ordered]
        # 相邻题位不应同章节，避免连续三题考同一主题。
        adjacent_same = sum(1 for first, second in zip(chapters, chapters[1:]) if first == second)
        self.assertLessEqual(adjacent_same, 2)

    def test_assign_follows_outline_chapter_weights(self) -> None:
        chapters = ["第2章 提示词工程", "第3章 模型微调", "第5章 模型评估"]
        points = [
            {
                "id": f"kp_{index:02d}",
                "name": f"知识点{index:02d}",
                "chapter": f"材料章节{index}",
                "framework_anchor_name": chapters[index % len(chapters)],
                "importance": "normal",
            }
            for index in range(12)
        ]
        weights = [
            {"name": chapters[0], "weight": 20},
            {"name": chapters[1], "weight": 60},
            {"name": chapters[2], "weight": 20},
        ]

        ordered = server.assign_knowledge_points(points, 10, weights)

        planned = {}
        for _point, _cursor, _total, label in ordered:
            planned[label] = planned.get(label, 0) + 1
        self.assertEqual(sum(planned.values()), 10)
        self.assertEqual(planned.get(chapters[1]), 6)
        self.assertEqual(planned.get(chapters[0]), 2)
        self.assertEqual(planned.get(chapters[2]), 2)

    def test_assign_weights_redistribute_when_chapter_has_no_points(self) -> None:
        points = [
            {
                "id": "kp_00",
                "name": "检索增强生成原理",
                "chapter": "材料章节",
                "framework_anchor_name": "第2章 提示词工程与RAG",
                "importance": "normal",
            }
        ]
        weights = [
            {"name": "第2章 提示词工程与RAG", "weight": 25},
            {"name": "第3章 监督微调", "weight": 75},
        ]

        ordered = server.assign_knowledge_points(points, 3, weights)

        labels = [label for _point, _cursor, _total, label in ordered]
        self.assertEqual(labels.count("第2章 提示词工程与RAG"), 3)

    def test_largest_remainder_split_sums_exactly(self) -> None:
        self.assertEqual(server.largest_remainder_split(40, [0.05, 0.25, 0.35, 0.05, 0.10, 0.15, 0.05]), [2, 10, 14, 2, 4, 6, 2])
        self.assertEqual(sum(server.largest_remainder_split(7, [1 / 3, 1 / 3, 1 / 3])), 7)

    def test_sections_from_structure_matches_outline_ratios(self) -> None:
        structure = {
            "question_type_ratios": [
                {"name": "选择题", "ratio": 20},
                {"name": "判断题", "ratio": 20},
                {"name": "填空题", "ratio": 10},
                {"name": "简答题", "ratio": 20},
                {"name": "综合题", "ratio": 30},
            ]
        }

        sections, warnings = server.sections_from_structure(structure, server.json_decimal(100))

        self.assertEqual(warnings, [])
        by_type = {section["question_type"]: section for section in sections}
        self.assertEqual(by_type["single_choice"]["score"], 20)
        self.assertEqual(by_type["true_false"]["score"], 20)
        self.assertEqual(by_type["fill_blank"]["score"], 10)
        self.assertEqual(by_type["short_answer"]["score"], 20)
        self.assertEqual(by_type["comprehensive"]["score"], 30)
        self.assertEqual(by_type["comprehensive"]["count"], 1)
        self.assertEqual(sum(section["score"] for section in sections), 100)
        total_items = sum(section["count"] for section in sections)
        self.assertLessEqual(total_items, 50)

    def test_difficulty_targets_form_gradient(self) -> None:
        targets = [server.difficulty_target_for("medium", index, 10) for index in range(1, 11)]
        self.assertEqual(targets[0], "easy")
        self.assertIn("hard", targets)
        self.assertLess(targets.count("easy"), targets.count("medium"))

    def test_true_false_targets_mix_verdicts(self) -> None:
        targets = server.balanced_true_false_targets(10)
        self.assertIn("正确", targets)
        self.assertIn("错误", targets)
        self.assertGreaterEqual(min(targets.count("正确"), targets.count("错误")), 3)

    def test_choice_targets_cycle_labels(self) -> None:
        targets = server.balanced_choice_targets(8)
        self.assertEqual(set(targets), {"A", "B", "C", "D"})
        self.assertEqual(targets.count("A"), 2)


class FactSliceTests(unittest.TestCase):
    def test_repeated_point_gets_disjoint_fact_slices(self) -> None:
        facts = ["事实一内容", "事实二内容", "事实三内容", "事实四内容"]
        first, first_is_slice = server.allocate_facts(facts, 0, 2)
        second, second_is_slice = server.allocate_facts(facts, 1, 2)

        self.assertTrue(first_is_slice)
        self.assertTrue(second_is_slice)
        self.assertFalse(set(second) & set(first))

    def test_single_occurrence_keeps_all_facts(self) -> None:
        facts = ["事实一内容", "事实二内容", "事实三内容"]
        first, is_slice = server.allocate_facts(facts, 0, 1)

        self.assertEqual(first, facts)
        self.assertFalse(is_slice)

    def test_more_occurrences_than_facts_falls_back_to_tail(self) -> None:
        facts = ["唯一事实内容"]
        first, first_is_slice = server.allocate_facts(facts, 0, 3)
        third, third_is_slice = server.allocate_facts(facts, 2, 3)

        self.assertFalse(first_is_slice)
        self.assertFalse(third_is_slice)
        self.assertEqual(third, facts)

    def test_generation_spec_carries_difficulty_and_angle(self) -> None:
        plan = {
            "id": "Q01",
            "question_type": "single_choice",
            "question_type_label": "单项选择题",
            "score": 2,
            "knowledge_point_id": "kp_awq",
            "knowledge_point_name": "AWQ 权重量化",
            "chapter": "模型量化",
            "difficulty": "medium",
            "difficulty_target": "hard",
            "cognitive_level": "理解/应用",
            "cognitive_angle": "场景应用",
            "fact_cursor": 0,
        }
        point = {
            "id": "kp_awq",
            "name": "AWQ 权重量化",
            "assessable_content": ["AWQ 通过识别重要权重并采取保护策略，在低比特量化时尽量降低模型精度损失。"],
            "evidence_ids": ["ev_awq"],
        }

        spec = server.build_generation_item_spec(plan, point)

        self.assertEqual(spec["difficulty_target"], "hard")
        self.assertEqual(spec["cognitive_angle"], "场景应用")
        self.assertIn("assessable_content", spec)


class RepositionOptionsTests(unittest.TestCase):
    def test_correct_option_moves_to_target_label(self) -> None:
        item = {
            "options": [
                {"label": "A", "text": "干扰项一"},
                {"label": "B", "text": "正确答案文本"},
                {"label": "C", "text": "干扰项二"},
                {"label": "D", "text": "干扰项三"},
            ],
            "answer": {"correct_option": "B"},
        }

        server.reposition_options(item, "D")

        self.assertEqual(item["answer"]["correct_option"], "D")
        self.assertEqual(item["options"][3]["text"], "正确答案文本")
        self.assertEqual(item["options"][1]["text"], "干扰项三")


class CrossValidationTests(unittest.TestCase):
    def _plan(self, item_id: str, question_type: str, point: str = "默认知识点") -> dict:
        return {"id": item_id, "question_type": question_type, "knowledge_point_name": point}

    def test_answer_leak_across_questions_is_rejected(self) -> None:
        items = [
            {
                "plan_item_id": "Q01",
                "stem": "DPO 偏好样本由哪三个字段组成？",
                "options": [
                    {"label": "A", "text": "prompt、chosen、rejected"},
                    {"label": "B", "text": "其他"},
                    {"label": "C", "text": "别的"},
                    {"label": "D", "text": "再别的"},
                ],
                "answer": {"correct_option": "A"},
            },
            {
                "plan_item_id": "Q02",
                "stem": "在 DPO 偏好数据中，每条记录由 prompt、chosen 和 ______ 组成。",
                "answer": {"accepted_answers": ["rejected"]},
            },
        ]
        plans = {item_id: self._plan(item_id, "fill_blank") for item_id in ("Q01", "Q02")}
        plans["Q01"]["question_type"] = "single_choice"

        errors, _warnings, _stats, _conflicts = server.cross_validate_items(items, plans)

        self.assertTrue(any("互相提示" in error for error in errors))

    def test_all_true_verdicts_are_rejected(self) -> None:
        items = [
            {"plan_item_id": f"Q{index:02d}", "stem": f"陈述{index}", "answer": {"value": "正确"}}
            for index in range(1, 5)
        ]
        plans = {f"Q{index:02d}": self._plan(f"Q{index:02d}", "true_false") for index in range(1, 5)}

        errors, _warnings, stats, _conflicts = server.cross_validate_items(items, plans)

        self.assertTrue(any("判断题" in error for error in errors))
        self.assertEqual(stats["verdict_distribution"], {"正确": 4})

    def test_duplicate_stems_are_rejected(self) -> None:
        items = [
            {"plan_item_id": "Q01", "stem": "在 Agentic RAG 中系统提示词的作用是什么", "answer": {"accepted_answers": ["工具调用"]}},
            {"plan_item_id": "Q02", "stem": "在 Agentic RAG 中系统提示词的作用是什么", "answer": {"accepted_answers": ["路由规则"]}},
        ]
        plans = {item_id: self._plan(item_id, "fill_blank") for item_id in ("Q01", "Q02")}

        errors, _warnings, _stats, _conflicts = server.cross_validate_items(items, plans)

        self.assertTrue(any("题干高度相似" in error for error in errors))

    def test_balanced_paper_passes_cross_validation(self) -> None:
        items = [
            {"plan_item_id": "Q01", "stem": "陈述一关于检索增强生成", "answer": {"value": "正确"}},
            {"plan_item_id": "Q02", "stem": "陈述二关于低比特量化部署", "answer": {"value": "错误"}},
            {"plan_item_id": "Q03", "stem": "陈述三关于奖励模型训练", "answer": {"value": "正确"}},
            {"plan_item_id": "Q04", "stem": "陈述四关于适配器合并流程", "answer": {"value": "错误"}},
        ]
        plans = {f"Q{index:02d}": self._plan(f"Q{index:02d}", "true_false") for index in range(1, 5)}

        errors, warnings, stats, _conflicts = server.cross_validate_items(items, plans)

        self.assertEqual(errors, [])
        self.assertEqual(stats["verdict_distribution"], {"正确": 2, "错误": 2})


class StructuredModelRequestTests(unittest.TestCase):
    def test_outline_organization_prompt_requests_semantic_requirements_and_keeps_sources(self) -> None:
        material = {
            "id": "outline_1",
            "original_filename": "课程教学大纲.pdf",
            "material_area": "outline",
        }
        chunks = [{
            "id": "outline_ev_1",
            "material_id": "outline_1",
            "material_name": "课程教学大纲.pdf",
            "source_location": "第 2 页",
            "section_title": "教学内容与要求",
            "text": "理解参数高效微调的基本原理，掌握 LoRA 参数作用和适用条件。",
        }]

        messages = server.build_outline_messages(material, chunks, "teaching")
        prompt = messages[1]["content"]

        self.assertIn("教学内容与要求", prompt)
        self.assertIn("teaching_requirements", prompt)
        self.assertIn("outline_ev_1", prompt)
        self.assertIn("第 2 页", prompt)

    def test_outline_organization_uses_model_output_as_framework_anchor(self) -> None:
        material = {
            "id": "outline_1",
            "original_filename": "课程教学大纲.txt",
            "storage_path": "unused.txt",
        }
        chunks = [{
            "id": "outline_ev_1",
            "material_id": "outline_1",
            "material_name": "课程教学大纲.txt",
            "source_location": "第 2 页",
            "section_title": "教学内容与要求",
            "text": "理解参数高效微调的基本原理，掌握 LoRA 参数作用和适用条件。",
        }]
        response = json.dumps({"framework_anchors": [{
            "name": "参数高效微调",
            "scope_text": "参数高效微调的基本原理与适用条件",
            "teaching_requirements": ["理解基本原理", "掌握 LoRA 参数作用"],
            "assessment_requirements": [],
            "capabilities": ["理解", "应用"],
            "exclusions": ["不考安装步骤"],
            "importance": "key",
            "confidence": 0.92,
            "evidence_ids": ["outline_ev_1"],
        }]}, ensure_ascii=False)

        with patch.object(server, "extract_material", return_value=(chunks, [])), patch.object(
            server, "model_config", return_value={"api_key": "test-key"}
        ), patch.object(server, "call_model", return_value=(response, {"call_id": "outline_call_1"})) as call:
            result = server.organize_single_outline(material)

        self.assertEqual(result["outline_kind"], "teaching")
        self.assertEqual(result["anchors"][0]["name"], "参数高效微调")
        self.assertEqual(result["anchors"][0]["teaching_requirements"], ["理解基本原理", "掌握 LoRA 参数作用"])
        self.assertEqual(result["anchors"][0]["source_location"], "第 2 页")
        call.assert_called_once()

    def test_organization_prompt_receives_source_free_confirmed_framework_context(self) -> None:
        chunks = [{
            "id": "ev_awq",
            "material_id": "material_1",
            "material_name": "实验16-AWQ权重量化实验.pdf",
            "source_location": "第 3 页",
            "section_title": "实验步骤",
            "text": "AWQ 通过保护重要权重降低低比特量化精度损失。",
        }]
        framework = {
            "anchors": [{
                "id": "anchor_1",
                "name": "模型量化",
                "scope_text": "教学内容与要求：理解权重量化的目标、方法和精度-资源取舍。",
                "outline_kind": "teaching",
                "source_material_name": "课程教学大纲.pdf",
                "source_location": "第 8 页",
            }]
        }

        messages = server.build_organization_messages(chunks, 1, framework)
        prompt = messages[1]["content"]

        self.assertIn("教学内容与要求", prompt)
        self.assertIn("模型量化", prompt)
        self.assertIn("精度-资源取舍", prompt)
        self.assertIn("实验16-AWQ权重量化实验.pdf", prompt)
        self.assertNotIn("anchor_1", prompt)

    def test_merge_rejects_candidate_without_source_free_assessable_content(self) -> None:
        chunks = [{"id": "ev_1", "text": "按实验手册完成安装、运行、截图并提交报告。"}]
        raw = [{
            "name": "安装与提交实验",
            "chapter": "实验步骤",
            "description": "按实验手册完成安装和截图。",
            "assessable_content": ["按照实验手册完成安装、运行、截图并提交报告。"],
            "evidence_ids": ["ev_1"],
        }]

        self.assertEqual(server.merge_candidate_points(raw, chunks), [])

    def test_merge_rejects_knowledge_point_name_with_source_provenance(self) -> None:
        chunks = [{"id": "ev_awq", "text": "AWQ 通过保护重要权重降低量化误差。"}]
        raw = [{
            "name": "实验16 AWQ权重量化",
            "chapter": "模型量化",
            "description": "理解 AWQ 的主要目标。",
            "assessable_content": ["AWQ 通过保护重要权重降低低比特量化误差。"],
            "evidence_ids": ["ev_awq"],
        }]

        self.assertEqual(server.merge_candidate_points(raw, chunks), [])

    def test_generation_spec_uses_distilled_knowledge_card_not_evidence_provenance(self) -> None:
        plan = {
            "id": "Q01",
            "question_type": "single_choice",
            "question_type_label": "单项选择题",
            "score": 2,
            "knowledge_point_id": "kp_awq",
            "knowledge_point_name": "AWQ 权重量化",
            "chapter": "模型量化",
            "difficulty": "medium",
            "cognitive_level": "理解",
        }
        point = {
            "id": "kp_awq",
            "name": "AWQ 权重量化",
            "chapter": "模型量化",
            "description": "来自实验16-AWQ权重量化实验手册的整理结果。",
            "assessable_content": [
                "AWQ 通过识别重要权重并采取保护策略，在低比特量化时尽量降低模型精度损失。"
            ],
            "evidence_ids": ["ev_awq"],
        }

        spec = server.build_generation_item_spec(plan, point)
        serialized = json.dumps(spec, ensure_ascii=False)

        self.assertIn("AWQ 通过识别重要权重", serialized)
        self.assertNotIn("实验16", serialized)
        self.assertNotIn("实验手册", serialized)
        self.assertNotIn("ev_awq", serialized)
        self.assertNotIn("chapter", spec)

    def test_generation_prompt_describes_knowledge_cards_not_raw_evidence(self) -> None:
        prompt = server.generation_system_prompt()

        self.assertIn("已提纯可考知识卡", prompt)
        self.assertNotIn("evidence ID", prompt)
        self.assertNotIn("给定 evidence", prompt)

    def test_teacher_traceability_uses_knowledge_point_evidence_mapping(self) -> None:
        point = {"id": "kp_awq", "evidence_ids": ["ev_exact"]}
        chunks = {
            "ev_exact": {"id": "ev_exact", "text": "AWQ 保护重要权重。"},
            "ev_similar": {"id": "ev_similar", "text": "另一种量化方法也讨论权重。"},
        }

        evidence = server.evidence_for_knowledge_point(point, chunks)

        self.assertEqual([item["id"] for item in evidence], ["ev_exact"])

    def test_confirm_framework_requires_both_outline_kinds(self) -> None:
        original_state = server.STATE
        try:
            server.STATE = {
                "framework_run": {
                    "status": "awaiting_teacher_confirmation",
                    "candidate_anchor_ids": ["anchor_teaching"],
                },
                "candidate_framework_anchors": {
                    "anchor_teaching": {"id": "anchor_teaching", "outline_kind": "teaching"},
                },
            }

            with self.assertRaisesRegex(ValueError, "教学大纲和考核大纲"):
                server.confirm_assessment_framework()
        finally:
            server.STATE = original_state

    def test_organize_teaching_material_requires_confirmed_framework(self) -> None:
        original_state = server.STATE
        try:
            server.STATE = {
                "candidate_run": None,
                "assessment_framework": None,
                "materials": {
                    "teaching_1": {
                        "id": "teaching_1",
                        "status": "staged",
                        "material_area": "teaching_material",
                    }
                },
            }

            with self.assertRaisesRegex(ValueError, "命题框架"):
                server.organize_materials(["teaching_1"])
        finally:
            server.STATE = original_state

    def test_framework_mapping_attaches_anchor_and_removes_out_of_scope_candidates(self) -> None:
        anchors = [
            {
                "id": "anchor_lora",
                "name": "LoRA 参数高效微调",
                "scope_text": "掌握 LoRA 的秩 r、缩放系数 alpha 与冻结基础模型参数的作用。",
            }
        ]
        candidates = [
            {
                "id": "kp_lora",
                "name": "LoRA 的秩与缩放系数",
                "chapter": "参数高效微调",
                "description": "分析秩 r 与 alpha 对增量更新的影响。",
                "evidence_ids": ["ev_lora"],
            },
            {
                "id": "kp_outside",
                "name": "无关的图像分割网络",
                "chapter": "计算机视觉",
                "description": "比较卷积网络结构。",
                "evidence_ids": ["ev_outside"],
            },
        ]

        mapped, excluded = server.map_candidates_to_framework(candidates, anchors)

        self.assertEqual([item["id"] for item in mapped], ["kp_lora"])
        self.assertEqual(mapped[0]["framework_anchor_id"], "anchor_lora")
        self.assertEqual(excluded, ["kp_outside"])

    def test_normalize_material_area_defaults_and_rejects_unknown_values(self) -> None:
        self.assertEqual(server.normalize_material_area(None), "teaching_material")
        self.assertEqual(server.normalize_material_area(""), "teaching_material")
        with self.assertRaisesRegex(ValueError, "资料区域"):
            server.normalize_material_area("unknown")

    def test_multipart_form_preserves_area_and_multiple_files(self) -> None:
        boundary = "----exam-paper-boundary"
        content_type = f"multipart/form-data; boundary={boundary}"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="material_area"\r\n'
            "\r\n"
            "outline\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="教学大纲.txt"\r\n'
            "Content-Type: text/plain\r\n"
            "\r\n"
            "教学大纲正文\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="考核大纲.txt"\r\n'
            "Content-Type: text/plain\r\n"
            "\r\n"
            "考核大纲正文\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        parsed = server.parse_multipart_form(content_type, body)

        self.assertEqual(parsed["fields"], {"material_area": "outline"})
        self.assertEqual(
            parsed["uploads"],
            [("教学大纲.txt", "教学大纲正文".encode("utf-8")), ("考核大纲.txt", "考核大纲正文".encode("utf-8"))],
        )

    def test_organize_rejects_outline_materials(self) -> None:
        original_state = server.STATE
        try:
            server.STATE = {
                "candidate_run": None,
                "materials": {
                    "outline_1": {
                        "id": "outline_1",
                        "status": "staged",
                        "material_area": "outline",
                    }
                },
            }

            with self.assertRaisesRegex(ValueError, "大纲区"):
                server.organize_materials(["outline_1"])
        finally:
            server.STATE = original_state

    def test_organize_rejects_mixed_outline_and_teaching_materials(self) -> None:
        original_state = server.STATE
        try:
            server.STATE = {
                "candidate_run": None,
                "materials": {
                    "outline_1": {
                        "id": "outline_1",
                        "status": "staged",
                        "material_area": "outline",
                    },
                    "teaching_1": {
                        "id": "teaching_1",
                        "status": "staged",
                        "material_area": "teaching_material",
                    },
                },
            }

            with self.assertRaisesRegex(ValueError, "大纲区"):
                server.organize_materials(["outline_1", "teaching_1"])
        finally:
            server.STATE = original_state

    def test_same_content_can_be_uploaded_once_per_material_area(self) -> None:
        original_state = server.STATE
        original_upload_dir = server.UPLOAD_DIR

        class FakeUploadHandler:
            def __init__(self) -> None:
                self.headers = {"Content-Length": "1", "Content-Type": "multipart/form-data; boundary=test"}
                self.rfile = io.BytesIO(b"x")
                self.response = None

            def send_json(self, payload, status=None) -> None:
                self.response = (payload, status)

        try:
            server.reset_state()
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.UPLOAD_DIR = Path(temporary_directory)
                server.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                entries = [("same.txt", b"same content")]
                for area in ("outline", "teaching_material"):
                    with patch.object(
                        server,
                        "parse_multipart_form",
                        return_value={"fields": {"material_area": area}, "uploads": entries},
                    ):
                        server.PrototypeHandler.handle_upload(FakeUploadHandler())

                materials = list(server.STATE["materials"].values())
                self.assertEqual(len(materials), 2)
                self.assertEqual({item["material_area"] for item in materials}, {"outline", "teaching_material"})
        finally:
            server.STATE = original_state
            server.UPLOAD_DIR = original_upload_dir

    def test_same_content_in_same_material_area_returns_duplicate(self) -> None:
        original_state = server.STATE
        original_upload_dir = server.UPLOAD_DIR

        class FakeUploadHandler:
            def __init__(self) -> None:
                self.headers = {"Content-Length": "1", "Content-Type": "multipart/form-data; boundary=test"}
                self.rfile = io.BytesIO(b"x")

            def send_json(self, payload, status=None) -> None:
                self.response = (payload, status)

        try:
            server.reset_state()
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.UPLOAD_DIR = Path(temporary_directory)
                server.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                with patch.object(
                    server,
                    "parse_multipart_form",
                    return_value={"fields": {"material_area": "teaching_material"}, "uploads": [("same.txt", b"same content")]},
                ):
                    first = FakeUploadHandler()
                    server.PrototypeHandler.handle_upload(first)
                    second = FakeUploadHandler()
                    server.PrototypeHandler.handle_upload(second)

                self.assertEqual(len(server.STATE["materials"]), 1)
                self.assertTrue(second.response[0]["materials"][0]["duplicate"])
        finally:
            server.STATE = original_state
            server.UPLOAD_DIR = original_upload_dir

    def test_upload_validation_is_atomic_when_one_entry_is_invalid(self) -> None:
        original_state = server.STATE
        original_upload_dir = server.UPLOAD_DIR

        class FakeUploadHandler:
            def __init__(self) -> None:
                self.headers = {"Content-Length": "1", "Content-Type": "multipart/form-data; boundary=test"}
                self.rfile = io.BytesIO(b"x")

            def send_json(self, payload, status=None) -> None:
                self.response = (payload, status)

        try:
            server.reset_state()
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.UPLOAD_DIR = Path(temporary_directory)
                server.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                with patch.object(
                    server,
                    "parse_multipart_form",
                    return_value={
                        "fields": {"material_area": "teaching_material"},
                        "uploads": [("valid.txt", b"valid"), ("invalid.exe", b"invalid")],
                    },
                ):
                    with self.assertRaisesRegex(ValueError, "格式不支持"):
                        server.PrototypeHandler.handle_upload(FakeUploadHandler())

                self.assertEqual(server.STATE["materials"], {})
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])
        finally:
            server.STATE = original_state
            server.UPLOAD_DIR = original_upload_dir

    def test_structured_request_forces_json_mode(self) -> None:
        payload = server.build_model_payload(
            {"model": "deepseek-v4-flash", "json_mode": False},
            [{"role": "user", "content": "test"}],
            2000,
            require_json=True,
        )

        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_organization_request_disables_thinking_for_speed(self) -> None:
        payload = server.build_model_payload(
            {"model": "deepseek-v4-flash", "json_mode": False},
            [{"role": "user", "content": "test"}],
            2000,
            require_json=True,
            stage="organize",
        )

        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_outline_organization_request_disables_thinking_for_json_output(self) -> None:
        payload = server.build_model_payload(
            {"model": "deepseek-v4-flash", "json_mode": False},
            [{"role": "user", "content": "test"}],
            3000,
            require_json=True,
            stage="organize_outline",
        )

        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)

    def test_outline_organization_batches_one_file_and_merges_duplicate_anchors(self) -> None:
        material = {
            "id": "outline_1",
            "original_filename": "课程教学大纲.pdf",
        }
        chunks = [
            {
                "id": f"outline_ev_{index}",
                "material_id": "outline_1",
                "material_name": "课程教学大纲.pdf",
                "source_location": f"第 {index} 页",
                "section_title": "教学内容与要求",
                "text": f"参数高效微调教学要求 {index}",
            }
            for index in range(1, 6)
        ]
        responses = [
            json.dumps({"framework_anchors": [{
                "name": "参数高效微调",
                "scope_text": "理解参数高效微调的基本原理",
                "teaching_requirements": ["理解基本原理"],
                "assessment_requirements": [],
                "capabilities": ["理解"],
                "exclusions": [],
                "importance": "normal",
                "confidence": 0.8,
                "evidence_ids": ["outline_ev_1"],
            }]}, ensure_ascii=False),
            json.dumps({"framework_anchors": [{
                "name": "参数高效微调",
                "scope_text": "掌握 LoRA 参数的作用与适用条件",
                "teaching_requirements": ["掌握 LoRA 参数作用"],
                "assessment_requirements": ["能够分析参数配置"],
                "capabilities": ["应用"],
                "exclusions": ["不考安装命令"],
                "importance": "key",
                "confidence": 0.92,
                "evidence_ids": ["outline_ev_3"],
            }]}, ensure_ascii=False),
            json.dumps({"framework_anchors": []}, ensure_ascii=False),
        ]
        call_results = [(response, {"call_id": f"outline_call_{index}"}) for index, response in enumerate(responses, start=1)]

        with patch.object(server, "OUTLINE_ORGANIZATION_BATCH_SIZE", 2, create=True), patch.object(
            server, "model_config", return_value={"api_key": "test-key"}
        ), patch.object(server, "call_model", side_effect=call_results) as call:
            anchors, warnings = server.model_outline_anchors(material, chunks, "teaching")

        self.assertEqual(warnings, [])
        self.assertEqual(call.call_count, 3)
        for batch_index, model_call in enumerate(call.call_args_list, start=1):
            diagnostic_context = model_call.kwargs["diagnostic_context"]
            self.assertEqual(diagnostic_context["material_id"], "outline_1")
            self.assertEqual(diagnostic_context["batch_index"], batch_index)
            prompt = model_call.args[1][1]["content"]
            self.assertIn(f"第 {batch_index} 批", prompt)

        self.assertEqual(len(anchors), 1)
        anchor = anchors[0]
        self.assertEqual(anchor["importance"], "key")
        self.assertEqual(anchor["confidence"], 0.92)
        self.assertEqual(anchor["evidence_ids"], ["outline_ev_1", "outline_ev_3"])
        self.assertEqual(anchor["teaching_requirements"], ["理解基本原理", "掌握 LoRA 参数作用"])
        self.assertEqual(anchor["assessment_requirements"], ["能够分析参数配置"])
        self.assertEqual(anchor["capabilities"], ["理解", "应用"])
        self.assertEqual(anchor["exclusions"], ["不考安装命令"])

    def test_file_jobs_run_concurrently_and_return_input_order(self) -> None:
        barrier = Barrier(2)
        active_lock = Lock()
        active = {"count": 0, "maximum": 0}

        def worker(material):
            with active_lock:
                active["count"] += 1
                active["maximum"] = max(active["maximum"], active["count"])
            try:
                barrier.wait(timeout=2)
                return {"material_id": material["id"]}
            finally:
                with active_lock:
                    active["count"] -= 1

        materials = [{"id": "mat_a"}, {"id": "mat_b"}]
        results = server.run_parallel_material_jobs(materials, worker, max_workers=2)

        self.assertEqual([item["material_id"] for item in results], ["mat_a", "mat_b"])
        self.assertEqual(active["maximum"], 2)

    def test_organization_messages_cap_evidence_and_candidate_count(self) -> None:
        chunks = [
            {
                "id": f"ev_{number}",
                "material_name": "测试资料.pdf",
                "material_id": "mat_test",
                "source_location": f"第 {number} 页",
                "section_title": "测试章节",
                "text": "a" * 900,
            }
            for number in range(3)
        ]

        messages = server.build_organization_messages(chunks, batch_index=1)
        user_message = messages[1]["content"]

        self.assertIn(f"最多输出 {server.ORGANIZATION_MAX_KNOWLEDGE_POINTS} 个", user_message)
        self.assertIn("2 到 6 条相互独立", user_message)
        self.assertNotIn("a" * 451, user_message)

    def test_organization_messages_reject_mixed_files(self) -> None:
        chunks = [
            {
                "id": "ev_a",
                "material_id": "mat_a",
                "material_name": "资料A.pdf",
                "source_location": "第1页",
                "section_title": "章节",
                "text": "内容A",
            },
            {
                "id": "ev_b",
                "material_id": "mat_b",
                "material_name": "资料B.pdf",
                "source_location": "第1页",
                "section_title": "章节",
                "text": "内容B",
            },
        ]

        with self.assertRaisesRegex(ValueError, "不能混合多个文件"):
            server.build_organization_messages(chunks, batch_index=1)

    def test_chunk_filter_removes_cover_templates_and_isolated_file_artifacts(self) -> None:
        chunks = [
            {
                "id": "ev_cover",
                "material_id": "mat_test",
                "material_name": "实验报告.docx",
                "section_title": "实验报告封面",
                "text": "实验报告封面\n姓名：张三\n学号：20260001\n实验日期：2026 年 8 月 13 日",
            },
            {
                "id": "ev_file",
                "material_id": "mat_test",
                "material_name": "实验报告.docx",
                "section_title": "config.json",
                "text": "config.json",
            },
            {
                "id": "ev_submission",
                "material_id": "mat_test",
                "material_name": "实验报告.docx",
                "section_title": "完成要求",
                "text": "运行后观察模型输出结果是否符合格式要求，并截图后提交实验报告。",
            },
            {
                "id": "ev_technical",
                "material_id": "mat_test",
                "material_name": "实验报告.docx",
                "section_title": "4bit 量化部署",
                "text": "使用 BitsAndBytesConfig(load_in_4bit=True) 配置 4bit 量化加载模型，可降低显存占用。",
            },
        ]

        eligible, excluded = server.filter_assessable_chunks(chunks)

        self.assertEqual([chunk["id"] for chunk in eligible], ["ev_technical"])
        self.assertEqual({item["chunk_id"] for item in excluded}, {"ev_cover", "ev_file", "ev_submission"})

    def test_chunk_filter_keeps_technical_explanation_under_a_file_name_heading(self) -> None:
        chunks = [
            {
                "id": "ev_config_fact",
                "material_id": "mat_test",
                "material_name": "实验报告.docx",
                "section_title": "config.json",
                "text": "配置文件定义模型层数、隐藏维度和注意力头数等网络结构参数。",
            }
        ]

        eligible, excluded = server.filter_assessable_chunks(chunks)

        self.assertEqual([chunk["id"] for chunk in eligible], ["ev_config_fact"])
        self.assertEqual(excluded, [])

    def test_chunking_splits_objectives_out_of_cover_metadata(self) -> None:
        material = {"id": "mat_test", "original_filename": "实验报告.docx"}
        segments = [
            {
                "source_location": "文档正文",
                "text": (
                    "实验报告封面\n\n"
                    "学生姓名：\n\n"
                    "学号：\n\n"
                    "三、实验目的：\n\n"
                    "了解模型量化的基本方式，掌握量化前后显存占用的比较方法。\n\n"
                    "五、实验的步骤和方法：\n\n"
                    "使用 BitsAndBytesConfig 的 load_in_4bit 参数启用 4bit 量化加载。"
                ),
            }
        ]

        chunks = server.chunk_segments(material, segments)
        eligible, _ = server.filter_assessable_chunks(chunks)

        self.assertTrue(any("实验目的" in chunk["section_title"] for chunk in eligible))
        self.assertTrue(any("load_in_4bit" in chunk["text"] for chunk in eligible))
        self.assertFalse(any("学生姓名" in chunk["text"] for chunk in eligible))

    def test_candidate_filter_rejects_administrative_names(self) -> None:
        chunks = [
            {"id": "ev_cover", "text": "实验报告封面"},
            {"id": "ev_technical", "text": "4bit 量化会改变模型加载时的显存占用。"},
        ]
        raw_candidates = [
            {
                "name": "实验报告封面",
                "chapter": "实验报告封面",
                "description": "填写姓名和学号。",
                "evidence_ids": ["ev_cover"],
            },
            {
                "name": "bitsandbytes 4bit量化部署配置",
                "chapter": "量化部署",
                "description": "理解 4bit 量化配置与显存占用的关系。",
                "assessable_content": ["4bit 量化配置会影响模型加载时的显存占用。"],
                "evidence_ids": ["ev_technical"],
            },
        ]

        candidates = server.merge_candidate_points(raw_candidates, chunks)

        self.assertEqual([candidate["name"] for candidate in candidates], ["bitsandbytes 4bit量化部署配置"])

    def test_candidate_filter_rejects_generic_experiment_heading(self) -> None:
        chunks = [{"id": "ev_technical", "text": "4bit量化部署需要设置量化配置。"}]
        raw_candidates = [
            {
                "name": "实验操作",
                "chapter": "实验操作",
                "description": "完成实验操作。",
                "evidence_ids": ["ev_technical"],
            }
        ]

        self.assertEqual(server.merge_candidate_points(raw_candidates, chunks), [])

    def test_candidate_filter_rejects_exact_model_file_name_but_keeps_semantic_name(self) -> None:
        chunks = [{"id": "ev_technical", "text": "配置文件定义网络结构参数。"}]
        raw_candidates = [
            {
                "name": "config.json",
                "chapter": "模型文件",
                "description": "配置文件定义网络结构参数。",
                "evidence_ids": ["ev_technical"],
            },
            {
                "name": "模型配置文件与网络结构参数",
                "chapter": "模型文件",
                "description": "配置文件定义网络结构参数。",
                "assessable_content": ["模型配置参数用于定义网络结构。"],
                "evidence_ids": ["ev_technical"],
            },
        ]

        candidates = server.merge_candidate_points(raw_candidates, chunks)

        self.assertEqual([candidate["name"] for candidate in candidates], ["模型配置文件与网络结构参数"])

    def test_semantic_failure_keeps_no_structural_title_fallback(self) -> None:
        chunks = [
            {
                "id": "ev_technical",
                "material_id": "mat_test",
                "material_name": "资料.pdf",
                "section_title": "任务1：文档载入",
                "text": "文档载入模块需要保留来源位置，供后续检索结果追溯。",
            }
        ]

        with patch.object(server, "model_config", return_value={"api_key": "test-key"}), patch.object(
            server, "call_model", side_effect=RuntimeError("network down")
        ):
            candidates, warnings = server.semantic_knowledge_points(chunks, material_name="资料.pdf")

        self.assertEqual(candidates, [])
        self.assertTrue(any("未自动生成候选知识点" in warning for warning in warnings))
        self.assertFalse(any("结构候选" in warning for warning in warnings))

    def test_organization_validation_logs_json_shape_without_model_text(self) -> None:
        chunks = [
            {
                "id": "ev_technical",
                "material_id": "mat_test",
                "material_name": "资料.pdf",
                "source_location": "第1页",
                "section_title": "量化部署",
                "text": "4bit量化通过配置参数降低显存占用。",
            }
        ]
        original_state = server.STATE
        original_path = getattr(server, "MODEL_DIAGNOSTICS_PATH", None)
        try:
            server.STATE = {"usage": [], "events": [], "model_diagnostics": []}
            malformed = '{"knowledge_points":{"name":"不应记录的模型原文"}}'
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.MODEL_DIAGNOSTICS_PATH = Path(temporary_directory) / "model-diagnostics.jsonl"
                with patch.object(server, "model_config", return_value={"api_key": "test-key"}), patch.object(
                    server, "call_model", return_value=(malformed, {"call_id": "call_schema_123"})
                ):
                    candidates, warnings = server.semantic_knowledge_points(chunks, material_name="资料.pdf")

                self.assertEqual(candidates, [])
                self.assertTrue(any("knowledge_points 不是数组" in warning for warning in warnings))
                validation = server.STATE["model_diagnostics"][0]
                self.assertEqual(validation["outcome"], "validation_error")
                self.assertEqual(validation["call_id"], "call_schema_123")
                self.assertEqual(validation["response"]["json_top_level_keys"], ["knowledge_points"])
                self.assertEqual(validation["response"]["knowledge_points_type"], "dict")
                self.assertNotIn("不应记录的模型原文", json.dumps(validation, ensure_ascii=False))
        finally:
            server.STATE = original_state
            if original_path is not None:
                server.MODEL_DIAGNOSTICS_PATH = original_path

    def test_organize_single_material_sends_only_eligible_evidence_to_model(self) -> None:
        material = {"id": "mat_test", "original_filename": "实验资料.docx"}
        raw_chunks = [
            {
                "id": "ev_cover",
                "material_id": "mat_test",
                "material_name": "实验资料.docx",
                "section_title": "实验报告封面",
                "text": "姓名：张三\n学号：20260001",
            },
            {
                "id": "ev_technical",
                "material_id": "mat_test",
                "material_name": "实验资料.docx",
                "section_title": "4bit量化",
                "text": "BitsAndBytesConfig 的 load_in_4bit 参数用于启用 4bit 量化加载。",
            },
        ]
        semantic_result = (
            [
                {
                    "id": "kp_test",
                    "name": "4bit量化加载配置",
                    "chapter": "4bit量化",
                    "description": "理解 load_in_4bit 参数的作用。",
                    "importance": "key",
                    "confidence": 0.9,
                    "evidence_ids": ["ev_technical"],
                    "review_reason": "可用于考查配置作用。",
                    "model_backed": True,
                    "status": "candidate",
                }
            ],
            [],
        )

        with patch.object(server, "extract_material", return_value=(raw_chunks, [])), patch.object(
            server, "semantic_knowledge_points", return_value=semantic_result
        ) as semantic:
            result = server.organize_single_material(material)

        sent_chunks = semantic.call_args.args[0]
        self.assertEqual([chunk["id"] for chunk in sent_chunks], ["ev_technical"])
        self.assertEqual([chunk["id"] for chunk in result["chunks"]], ["ev_technical"])
        self.assertEqual(len(result["excluded_chunks"]), 1)

    def test_length_limited_response_explains_missing_json_content(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "internal reasoning"},
                }
            ]
        }

        with self.assertRaisesRegex(server.ModelOutputError, "推理令牌耗尽"):
            server.extract_model_content(response)

    def test_empty_model_content_writes_redacted_diagnostic_metadata(self) -> None:
        class FakeResponse:
            status = 200
            headers = {"x-request-id": "request-test-123"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def getcode(self):
                return self.status

            def read(self):
                return json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "", "reasoning_content": ""},
                            }
                        ],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12},
                    }
                ).encode("utf-8")

        original_state = server.STATE
        original_path = getattr(server, "MODEL_DIAGNOSTICS_PATH", None)
        config = {
            "api_key": "test-key",
            "base_url": "https://api.example.test/v1",
            "model": "test-model",
            "json_mode": False,
            "organize_concurrency": 1,
            "reasoning_effort": "low",
        }
        try:
            server.STATE = {"usage": [], "events": [], "model_diagnostics": []}
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.MODEL_DIAGNOSTICS_PATH = Path(temporary_directory) / "model-diagnostics.jsonl"
                with patch.object(server, "model_config", return_value=config), patch.object(server, "urlopen", return_value=FakeResponse()):
                    with self.assertRaisesRegex(server.ModelOutputError, "空内容"):
                        server.call_model("organize", [{"role": "user", "content": "课程正文"}], 200, require_json=True)

                diagnostics = server.STATE["model_diagnostics"]
                self.assertEqual(len(diagnostics), server.MODEL_MAX_ATTEMPTS)
                self.assertTrue(all(item["outcome"] == "model_output_error" for item in diagnostics))
                self.assertTrue(all(item["http_status"] == 200 for item in diagnostics))
                self.assertTrue(all(item["response"]["finish_reason"] == "stop" for item in diagnostics))
                self.assertTrue(all(item["response"]["content_length"] == 0 for item in diagnostics))
                self.assertNotIn("课程正文", json.dumps(diagnostics, ensure_ascii=False))
                persisted = [json.loads(line) for line in server.MODEL_DIAGNOSTICS_PATH.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(persisted), server.MODEL_MAX_ATTEMPTS)
        finally:
            server.STATE = original_state
            if original_path is not None:
                server.MODEL_DIAGNOSTICS_PATH = original_path

    def test_successful_model_call_correlates_attempts_with_safe_batch_context(self) -> None:
        class FakeResponse:
            status = 200
            headers = {"x-request-id": "request-test-456"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def getcode(self):
                return self.status

            def read(self):
                return json.dumps(
                    {
                        "id": "chatcmpl-success",
                        "choices": [{"finish_reason": "stop", "message": {"content": "{\"knowledge_points\":[]}"}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
                    }
                ).encode("utf-8")

        original_state = server.STATE
        original_path = getattr(server, "MODEL_DIAGNOSTICS_PATH", None)
        config = {
            "api_key": "test-key",
            "base_url": "https://api.example.test/v1",
            "model": "test-model",
            "json_mode": False,
            "organize_concurrency": 1,
            "reasoning_effort": "low",
        }
        try:
            server.STATE = {"usage": [], "events": [], "model_diagnostics": []}
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.MODEL_DIAGNOSTICS_PATH = Path(temporary_directory) / "model-diagnostics.jsonl"
                with patch.object(server, "model_config", return_value=config), patch.object(server, "urlopen", return_value=FakeResponse()):
                    _, usage = server.call_model(
                        "organize",
                        [{"role": "user", "content": "课程正文"}],
                        200,
                        require_json=True,
                        diagnostic_context={"material_id": "mat_123", "material_name": "课程资料.pdf", "batch_index": 7},
                    )

                diagnostic = server.STATE["model_diagnostics"][0]
                self.assertEqual(diagnostic["call_id"], usage["call_id"])
                self.assertEqual(diagnostic["request"]["context"], {"material_id": "mat_123", "material_name": "课程资料.pdf", "batch_index": 7})
                self.assertNotIn("课程正文", json.dumps(diagnostic, ensure_ascii=False))
        finally:
            server.STATE = original_state
            if original_path is not None:
                server.MODEL_DIAGNOSTICS_PATH = original_path

    def test_network_failure_writes_exception_diagnostic(self) -> None:
        original_state = server.STATE
        original_path = getattr(server, "MODEL_DIAGNOSTICS_PATH", None)
        config = {
            "api_key": "test-key",
            "base_url": "https://api.example.test/v1",
            "model": "test-model",
            "json_mode": False,
            "organize_concurrency": 1,
            "reasoning_effort": "low",
        }
        try:
            server.STATE = {"usage": [], "events": [], "model_diagnostics": []}
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.MODEL_DIAGNOSTICS_PATH = Path(temporary_directory) / "model-diagnostics.jsonl"
                with patch.object(server, "model_config", return_value=config), patch.object(
                    server, "urlopen", side_effect=URLError("connection reset")
                ):
                    with self.assertRaisesRegex(RuntimeError, "网络请求"):
                        server.call_model("organize", [{"role": "user", "content": "课程正文"}], 200, require_json=True)

                diagnostics = server.STATE["model_diagnostics"]
                self.assertEqual(len(diagnostics), server.MODEL_MAX_ATTEMPTS)
                self.assertTrue(all(item["outcome"] == "network_error" for item in diagnostics))
                self.assertTrue(all(item["exception_type"] == "URLError" for item in diagnostics))
                self.assertTrue(all(item["http_status"] is None for item in diagnostics))
        finally:
            server.STATE = original_state
            if original_path is not None:
                server.MODEL_DIAGNOSTICS_PATH = original_path

    def test_invalid_http_success_body_writes_protocol_diagnostic(self) -> None:
        class FakeResponse:
            status = 200
            headers = {"x-request-id": "request-test-protocol"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def getcode(self):
                return self.status

            def read(self):
                return b"not-json-response"

        original_state = server.STATE
        original_path = getattr(server, "MODEL_DIAGNOSTICS_PATH", None)
        config = {
            "api_key": "test-key",
            "base_url": "https://api.example.test/v1",
            "model": "test-model",
            "json_mode": False,
            "organize_concurrency": 1,
            "reasoning_effort": "low",
        }
        try:
            server.STATE = {"usage": [], "events": [], "model_diagnostics": []}
            with tempfile.TemporaryDirectory() as temporary_directory:
                server.MODEL_DIAGNOSTICS_PATH = Path(temporary_directory) / "model-diagnostics.jsonl"
                with patch.object(server, "model_config", return_value=config), patch.object(server, "urlopen", return_value=FakeResponse()):
                    with self.assertRaisesRegex(RuntimeError, "有效 JSON"):
                        server.call_model("organize", [{"role": "user", "content": "课程正文"}], 200, require_json=True)

                diagnostics = server.STATE["model_diagnostics"]
                self.assertEqual(len(diagnostics), server.MODEL_MAX_ATTEMPTS)
                self.assertTrue(all(item["outcome"] == "protocol_error" for item in diagnostics))
                self.assertTrue(all(item["http_status"] == 200 for item in diagnostics))
                self.assertTrue(all(item["response"]["raw_body_length"] == len(b"not-json-response") for item in diagnostics))
                self.assertNotIn("not-json-response", json.dumps(diagnostics, ensure_ascii=False))
        finally:
            server.STATE = original_state
            if original_path is not None:
                server.MODEL_DIAGNOSTICS_PATH = original_path

    def test_record_model_usage_initializes_state_when_imported_directly(self) -> None:
        original_state = server.STATE
        try:
            server.STATE = {}
            server.record_model_usage({"stage": "diagnostic", "total_tokens": 1})
            self.assertEqual(server.STATE["usage"][0]["total_tokens"], 1)
        finally:
            server.STATE = original_state

    def test_clear_model_diagnostics_removes_only_diagnostic_file(self) -> None:
        original_path = getattr(server, "MODEL_DIAGNOSTICS_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                data_directory = Path(temporary_directory) / ".prototype-data"
                data_directory.mkdir()
                server.MODEL_DIAGNOSTICS_PATH = data_directory / "model-diagnostics.jsonl"
                server.MODEL_DIAGNOSTICS_PATH.write_text('{"safe":true}\n', encoding="utf-8")

                with patch.object(server, "DATA_DIR", data_directory), patch.object(server, "ROOT", Path(temporary_directory)):
                    server.clear_model_diagnostics()

                self.assertFalse(server.MODEL_DIAGNOSTICS_PATH.exists())
        finally:
            if original_path is not None:
                server.MODEL_DIAGNOSTICS_PATH = original_path


if __name__ == "__main__":
    unittest.main()
