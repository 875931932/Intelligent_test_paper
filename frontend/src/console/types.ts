/** 教师控制台全部 API 类型（与后端 Pydantic 模型一一对应）。 */

// ── 课程 ──────────────────────────────────────────────
export type Course = { id: string; name: string; slug: string; description?: string | null }

// ── 资料 / 上传 / 解析 ────────────────────────────────
export type MaterialType = 'teaching_syllabus' | 'assessment_syllabus' | 'teaching_material' | 'exercise'

export const MATERIAL_TYPE_LABELS: Record<MaterialType, string> = {
  teaching_syllabus: '教学大纲',
  assessment_syllabus: '考核大纲',
  teaching_material: '教学材料',
  exercise: '习题资料',
}

export type MaterialVersion = {
  id: string
  material_id: string
  status: string
  version_no: number
  sha256: string
  mime_type: string
  size_bytes: number
}

export type ParseStatus = {
  id: string
  status: string
  error_code?: string | null
  error_summary?: string | null
}

export type Material = {
  id: string
  course_id: string
  logical_name: string
  material_type: MaterialType
  status: string
  latest_version: MaterialVersion | null
  parse_status: ParseStatus | null
}

export type UploadSession = {
  session_id: string
  object_key: string
  upload_url: string
  expires_at: string
  headers: Record<string, string>
}

// ── 考纲框架 ──────────────────────────────────────────
export type AssessmentAnchor = {
  key: string
  title: string
  exam_weight: number
  ability_requirements: string[]
  allowed_question_types: string[]
  excluded_content: string[]
  alignment_keys: string[]
}

export type ExamPoint = {
  code: string
  title: string
  anchor_key: string
  assessment_requirement: string
  weight_value: number
  weight_source: string
  weight_group_id: string
  priority?: string
  cognitive_targets: string[]
  assessment_orientations?: string[]
  allowed_question_types: string[]
  operational_detail_policy?: string
  scope_boundary?: Record<string, unknown>
  required_evidence_roles?: string[]
  retrieval_intent: string
  assessment_anchor_keys?: string[]
  teaching_anchor_keys?: string[]
  status?: string
}

export type TeachingTopic = { key: string; title: string; depth: string; requirements: string[] }

export type FrameworkConflict = {
  key: string
  kind: string
  message: string
  status: 'open' | 'resolved'
}

export type FrameworkCandidate = {
  anchors: AssessmentAnchor[]
  exam_points: ExamPoint[]
  teaching_topics: TeachingTopic[]
  conflicts: FrameworkConflict[]
  final_exam_rules: Record<string, unknown>
}

export type AnchorRevision = {
  key: string
  title: string
  exam_weight: number
  ability_requirements: string[]
  allowed_question_types: string[]
  excluded_content: string[]
  alignment_keys: string[]
}

export type FrameworkConfirmation = {
  anchors: AnchorRevision[]
  exam_points: ExamPoint[]
  conflict_resolutions: Record<string, string>
  teacher_exclusions: string[]
}

export type FrameworkRunCreated = { run_id: string; candidate_id: string; status: string }

// ── 知识整理 ──────────────────────────────────────────
export type KnowledgeCardDraft = {
  name: string
  performance_statement: string
  assessable_content: string[]
  scope_boundary?: Record<string, unknown>
  cognitive_targets?: string[]
  allowed_question_types?: string[]
  importance?: number
  prompt_material?: string[]
  concept_cluster?: string
  answer_proposition?: string
  status: 'active' | 'excluded' | 'material_only' | 'needs_teacher_review'
}

export type AssessmentUnitDraft = {
  code: string
  title: string
  performance_statement: string
  exam_point_code: string
  scope_boundary?: Record<string, unknown>
  cards: KnowledgeCardDraft[]
  status: 'active' | 'excluded' | 'needs_teacher_review'
  origin: 'syllabus_core' | 'material_evidence'
}

export type KnowledgeTopicDraft = {
  code: string
  name: string
  framework_anchor_key: string
  units: AssessmentUnitDraft[]
  status: 'active' | 'excluded' | 'needs_teacher_review'
}

export type UnmatchedCandidate = { material_version_id: string; label: string; reason: string }

export type KnowledgeTreeCandidate = {
  framework_version_id: string
  topics: KnowledgeTopicDraft[]
  unmatched: UnmatchedCandidate[]
  coverage?: unknown[]
  evidence_decisions?: unknown[]
}

export type KnowledgeTreeConfirmation = {
  operations: { operation: string; target_code: string; value?: string | null }[]
  reviewed_topic_codes: string[]
  reviewed_exam_point_codes: string[]
  teacher_exclusions: string[]
}

export type OrganizationRunCreated = { run_id: string; candidate_id: string; status: string }

// ── 已发布知识 ────────────────────────────────────────
export type PublishedCard = {
  name: string
  performance_statement: string
  assessable_content: string[]
  scope_boundary: Record<string, unknown>
  cognitive_targets: string[]
  allowed_question_types: string[]
  importance: number
  concept_cluster: string
  answer_proposition: string
  answer_boundary: string
  prompt_material: string[]
}

export type PublishedUnit = {
  unit_id: string
  code: string
  title: string
  performance_statement: string
  exam_point_id: string
  exam_point_code: string
  anchor_key: string
  card_ids: string[]
}

export type PublishedExamPoint = {
  id: string
  code: string
  title: string
  assessment_requirement: string
  anchor_key: string
  weight_value: number
  weight_source: string
  cognitive_targets: string[]
  allowed_question_types: string[]
  operational_detail_policy: string
}

export type PublishedKnowledge = {
  catalog_version_id: string
  framework_version_id: string
  exam_points: PublishedExamPoint[]
  units: PublishedUnit[]
  knowledge_cards: Record<string, PublishedCard>
}

// ── 蓝图 / 合同 / 出题 ────────────────────────────────
export type TypeRule = {
  count: number
  score: number
  difficulty_distribution: Record<string, number>
}

export type BlueprintSettings = {
  total_score: number
  type_rules: Record<string, TypeRule>
  chapter_weights: Record<string, number>
}

export type ContractSlot = {
  item_index: number
  question_type: string
  score: number
  difficulty: string
  cognitive_level: string
  assessment_mode: string
  exam_point_id: string
  anchor_key: string
  unit_id: string
  card_id: string
  coverage_atom: string
  answer_boundary: string
  performance_statement: string
  prompt_material: string[]
  scope_boundary: Record<string, unknown>
  preferred_terms: string[]
  forbidden_context: { atoms: string[]; answer_cores: string[] }
  comprehensive_archetype?: string | null
  material_form?: string | null
  cognitive_sequence: string[]
  subquestion_count_range?: number[] | null
  subquestion_actions: string[]
  answer_boundaries: string[]
}

export type ContractConflict = {
  code: string
  exam_point_id: string
  message: string
  detail: Record<string, unknown>
}

export type PaperContract = {
  total_score: number
  slots: ContractSlot[]
  conflicts: ContractConflict[]
  audit_summary: {
    exam_points: { exam_point_id: string; weight: number; question_count: number; proportion: number }[]
    type_counts: Record<string, number>
    difficulty_counts: Record<string, number>
  }
}

export type Subquestion =
  | string
  | {
      id?: string
      question?: string
      action?: string
      prompt?: string
      answer_boundary?: string
      answer?: string
      rubric?: string[]
      score?: number
    }

export type Question = {
  item_index?: number
  question_type: string
  score: number
  stem?: string
  options?: string[] | Record<string, string>
  answer?: unknown
  explanation?: string
  rubric?: Array<{ point?: string; score?: number }>
  subquestions?: Subquestion[]
  comprehensive_archetype?: string | null
  quality?: { status?: string; code?: string; message?: string; issues?: string[] }
  needs_review?: boolean
  exam_point_id?: string
  coverage_atom?: string
}

export type GenerationRunResult = {
  status: string
  questions: Question[]
  final_check: { passed?: boolean; checks?: Array<{ code: string; passed: boolean; detail?: Record<string, unknown> }> }
  model_call_count: number
  model: string
}

// ── 常量 ──────────────────────────────────────────────
export const QUESTION_TYPE_LABELS: Record<string, string> = {
  single_choice: '单选题',
  true_false: '判断题',
  short_answer: '简答题',
  comprehensive: '综合题',
}

export const ARCHETYPE_LABELS: Record<string, string> = {
  fault_diagnosis: '故障诊断',
  code_completion_scenario: '代码补全',
  comparative_decision: '对比决策',
  experiment_analysis: '实验分析',
  scenario_design: '方案设计',
}

export const DIFFICULTY_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// ── 课程空间首页就绪度（Plan 1 地基） ────────────────
export type ExamProjectSummary = {
  id: string
  semester_label: string
  status: 'draft' | 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'
  total_score: number
  question_count: number
  pending_review: number
}

export type CourseReadiness = {
  materialsReady: boolean
  frameworkReady: boolean
  frameworkVersion: string | null
  knowledgeReady: boolean
  knowledgeVersion: string | null
  knowledgeCardCount: number
  knowledgeUngroundedCount: number
  projects: ExamProjectSummary[]
}
