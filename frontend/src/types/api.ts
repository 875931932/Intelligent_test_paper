// ============================================================
// API Types - 完整对应后端所有响应模型
// ============================================================

// ── Health ──
export interface HealthResponse {
  api: string;
  postgresql: string;
  redis: string;
  mineru: string;
  llm: string;
}

// ── Auth ──
export interface UserProfile {
  id: string;
  username: string;
  name: string;
  role: string;
}
export interface LoginResponse {
  token: string;
  user: UserProfile;
}

// ── Course ──
export interface CourseCreate {
  name: string;
  slug?: string;
  description?: string;
}
export interface CourseUpdate {
  name?: string;
  slug?: string;
  description?: string;
}
export interface CourseResponse {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  description: string | null;
}

// ── Material ──
export type MaterialType = 'teaching_syllabus' | 'assessment_syllabus' | 'teaching_material' | 'exercise';

export interface MaterialVersionResponse {
  id: string;
  material_id: string;
  status: string;
  version_no: number;
  sha256: string;
  mime_type: string;
  size_bytes: number;
}

export interface MaterialResponse {
  id: string;
  course_id: string;
  logical_name: string;
  material_type: MaterialType;
  status: string;
  latest_version: MaterialVersionResponse | null;
  parse_status: MaterialParseStatus;
  created_at: string;
}

export interface MaterialParseStatus {
  id: string;
  status: string;
  error_code?: string;
  error_summary?: string;
}

export interface UploadSessionResponse {
  session_id: string;
  object_key: string;
  upload_url: string;
  expires_at: string;
  headers: Record<string, string>;
}

export interface UploadSessionCreate {
  filename: string;
  material_type: string;
  size_bytes: number;
  sha256: string;
  mime_type: string;
  existing_material_id?: string;
}

// ── Framework ──
export interface FrameworkConflict {
  key: string;
  kind: string;
  message: string;
  status: 'open' | 'resolved';
  /** blocking=必须裁决；advisory=以考核大纲为准的提示，不阻塞发布 */
  severity?: 'blocking' | 'advisory';
}

export interface AssessmentAnchor {
  key: string;
  title: string;
  exam_weight: number;
  ability_requirements: string[];
  allowed_question_types: string[];
  excluded_content: string[];
  alignment_keys: string[];
}

export interface AssessmentOutline {
  anchors: AssessmentAnchor[];
  exam_points: FrameworkExamPoint[];
  final_exam_rules: Record<string, unknown>;
}

export interface FrameworkExamPoint {
  id: string;
  code: string;
  anchor_key: string;
  title: string;
  assessment_requirement: string;
  weight_value: number;
  weight_source: string;
  cognitive_targets: string[];
  allowed_question_types: string[];
  operational_detail_policy: string;
}

export interface FrameworkCandidate {
  anchors: AssessmentAnchor[];
  exam_points: FrameworkExamPoint[];
  teaching_topics: any[];
  conflicts: FrameworkConflict[];
  final_exam_rules: Record<string, unknown>;
  /** 考核大纲的考试规则，由 /framework-versions/current 顶层挂载（payload 里叫 final_exam_rules） */
  exam_rules?: ExamRules;
}

export interface FrameworkConfirmation {
  anchors: Array<{
    key: string;
    title: string;
    exam_weight: number;
    ability_requirements: string[];
    allowed_question_types: string[];
    excluded_content: string[];
    alignment_keys: string[];
  }>;
  exam_points: FrameworkExamPoint[];
  conflict_resolutions: Record<string, string>;
  teacher_exclusions: string[];
}

export interface FrameworkRunCreate {
  teaching_material_version_id: string;
  assessment_material_version_id: string;
}

export interface FrameworkBuildRun {
  run_id: string;
  course_id: string;
  status: string;
  candidate_id?: string;
  error_code?: string;
  error_message?: string;
  created_at: string;
}

export interface FrameworkVersion {
  id: string;
  course_id: string;
  version_no: number;
  status: 'draft' | 'published' | 'superseded' | 'rejected';
  created_at: string;
}

export interface CurrentFrameworkResponse {
  published: boolean;
  detail?: string;
  id?: string;
  candidate_id?: string;
  /** 无已发布版本时返回最近未确认候选作为草稿 */
  draft?: boolean;
  /** 草稿对应的构建 run id，用于继续确认/驳回 */
  run_id?: string;
  payload?: Record<string, unknown>;
  /** 考核大纲抽取出的考试规则（题型比例 / 章节命题权重），可查看可修改 */
  exam_rules?: ExamRules;
}

/** 考核大纲里的结构化考试规则 */
export interface ExamRules {
  exam_form?: string;
  duration_minutes?: number | null;
  total_score?: number | null;
  question_type_ratios: ExamRuleTypeRatio[];
  chapter_weights: ExamRuleChapterWeight[];
}

export interface ExamRuleTypeRatio {
  question_type: string;
  ratio: number;
}

export interface ExamRuleChapterWeight {
  anchor_key: string;
  weight: number;
}

// ── Knowledge ──
export interface AnswerRelation {
  source: string;
  target: string;
  relation: string;
  confidence: number;
}

export interface KnowledgeCard {
  id: string;
  name: string;
  performance_statement: string;
  assessable_content: string[] | string;
  scope_boundary: Record<string, unknown>;
  cognitive_targets: string[];
  allowed_question_types: string[];
  importance: number;
  concept_cluster: string;
  answer_proposition: string;
  answer_boundary: string;
  prompt_material: string[];
  relation_edges: AnswerRelation[];
  grounded: boolean;
}

export interface AssessmentUnit {
  unit_id: string;
  code: string;
  title: string;
  performance_statement: string;
  exam_point_id: string;
  exam_point_code: string;
  anchor_key: string;
  card_ids: string[];
}

export interface PublishedKnowledgeResponse {
  published: boolean;
  catalog_version_id: string;
  framework_version_id: string;
  exam_points: FrameworkExamPoint[];
  units: AssessmentUnit[];
  knowledge_cards: Record<string, KnowledgeCard>;
}

export interface EvidenceChunk {
  evidence_role: string;
  confidence: number;
  content: string;
  locator: string;
  material_version_id: string;
}

export interface KnowledgeTreeConfirmation {
  operations: Array<{
    operation: string;
    target_code: string;
    value?: string;
  }>;
  reviewed_topic_codes: string[];
  reviewed_exam_point_codes: string[];
  teacher_exclusions: string[];
  auto_supplement_direct_evidence?: boolean;
}

// ── 知识目录候选（待确认）嵌套结构 ──
export interface CandidateKnowledgeCard {
  name: string;
  performance_statement: string;
  assessable_content: string[];
  cognitive_targets: string[];
  allowed_question_types: string[];
  importance: number;
  concept_cluster: string;
  answer_proposition: string;
  evidence_chunk_ids: string[];
  status: string;
}

export interface CandidateAssessmentUnit {
  code: string;
  title: string;
  performance_statement: string;
  exam_point_code: string;
  cards: CandidateKnowledgeCard[];
  status: string;
  origin: string;
}

export interface CandidateKnowledgeTopic {
  code: string;
  name: string;
  framework_anchor_key: string;
  units: CandidateAssessmentUnit[];
  status: string;
}

export interface CandidateCoverage {
  exam_point_code: string;
  direct_count: number;
  supporting_count: number;
  background_count: number;
  out_of_scope_count: number;
  status: string;
  reasons: string[];
}

export interface KnowledgeCandidatePayload {
  framework_version_id: string;
  topics: CandidateKnowledgeTopic[];
  coverage: CandidateCoverage[];
  relevance_counts?: Record<string, number>;
  evidence_sources?: Array<{
    evidence_chunk_id: string;
    exam_point_code: string;
    material_version_id: string;
    locator: Record<string, unknown>;
    relevance_class: string;
    support_claim: string;
    evidence_role: string;
    confidence: number;
    content?: string;
  }>;
  exam_point_labels?: Record<string, {
    title: string;
    assessment_requirement: string;
  }>;
}

export interface OrganizationRunResponse {
  run_id: string;
  candidate_id?: string;
  status: string;
  error_code?: string;
  error_message?: string;
  created_at?: string;
  updated_at?: string;
}

// ── Blueprint / Contract ──
export type AssessmentMode = 'theory_recall' | 'conceptual' | 'application' | 'problem_solving' | 'practical_operation';

export interface PlanItem {
  item_index: number;
  question_type: string;
  score: number;
  anchor_key: string;
  exam_point_id: string;
  unit_id: string;
  card_id: string;
  difficulty: string;
  cognitive_level: string;
  assessment_mode: AssessmentMode;
  concept_cluster: string;
  answer_proposition: string;
  required_propositions: string[];
  relation_edges: any[];
  instance_carriers: string[];
}

export interface ForbiddenContext {
  atoms: string[];
  answer_cores: string[];
}

export interface ContractSlot {
  item_index: number;
  question_type: string;
  score: number;
  difficulty: string;
  cognitive_level: string;
  assessment_mode: string | AssessmentMode;
  exam_point_id: string;
  anchor_key: string;
  unit_id: string;
  card_id: string;
  coverage_atom: string;
  answer_boundary: string;
  performance_statement?: string;
  prompt_material?: string[];
  scope_boundary?: Record<string, unknown>;
  preferred_terms?: string[];
  forbidden_context?: ForbiddenContext;
  comprehensive_archetype?: string | null;
  material_form?: string | null;
  cognitive_sequence?: string[];
  subquestion_count_range?: [number, number] | null;
  subquestion_actions?: string[];
  answer_boundaries?: string[];
}

export interface ContractConflict {
  code: 'atom_pool_insufficient' | 'cluster_exhausted' | 'missing_exam_point' | string;
  exam_point_id: string;
  message: string;
  detail: Record<string, unknown>;
}

export interface ExamPointProportion {
  exam_point_id: string;
  weight: number;
  question_count: number;
  proportion: number;
}

export interface BackfilledPoint {
  item_index: number;
  from_exam_point_id: string;
  to_exam_point_id: string;
  anchor_key: string;
}

export interface ContractAuditSummary {
  exam_points: ExamPointProportion[];
  type_counts: Record<string, number>;
  difficulty_counts: Record<string, number>;
  backfilled_points?: BackfilledPoint[];
}

// ── Exam Projects ──
export interface ExamProjectCreate {
  name: string;
}
export interface ExamProject {
  id: string;
  course_id: string;
  name: string;
  status: string;
  active_blueprint_version_id?: string;
  /** 已确认合同所在的 generation_run（exam_projects.active_generation_run_id）；未确认合同时为 null */
  active_generation_run_id?: string | null;
  active_paper_version_id?: string;
  /** 后端摘要解析出的“当前可审核版本”（未定稿 candidate 优先），仅用于展示门禁 */
  paper_version_id?: string | null;
  paper_version_no?: number | null;
  paper_version_status?: string | null;
  model?: string;
  total_score?: number | null;
  item_count?: number | null;
  active_task_run_id?: string | null;
  generation_task_status?: string | null;
  generation_progress?: number | null;
  generation_stage?: string | null;
  generation_error?: string | null;
  created_at: string;
  updated_at: string;
}

// ── Paper Versons ──
export interface PaperVersionItem {
  item_index: number;
  plan_item_id?: string | null;
  knowledge_card_id?: string | null;
  exam_point_id?: string | null;
  question_type: string;
  stem: string;
  options?: Record<string, string> | string[];
  /** 判断题（true_false）答案在后端是布尔值且没有 options 字段，其余题型为字符串 */
  answer: string | boolean;
  /** 模型产出，部分题型为 null，前端需降级 */
  explanation?: string | null;
  /** 综合题分问（含每问 prompt/score/answer），非综合题为空数组；导出与答题卡依赖它 */
  subquestions?: Array<Record<string, unknown>>;
  score: number;
  difficulty?: string;
  cognitive_level?: string;
  needs_review: boolean;
  /** 理由串（；连接，最长 200 字），非数组 */
  needs_review_reason?: string | null;
  teacher_override: Record<string, unknown>;
  has_override: boolean;
  finalized_text?: Record<string, unknown> | null;
  quality_audit: Record<string, unknown>;
}

export interface PaperVersion {
  id: string;
  exam_project_id: string;
  generation_run_id?: string | null;
  version_no: number;
  total_score: number;
  status: string;
  project_status?: string;
  /** 逐题数组，按题号升序；item_index 与 PATCH items/{item_index} 同源 */
  questions: PaperVersionItem[];
  created_at: string;
  confirmed_at?: string | null;
  finalized_at?: string | null;
}

export interface NeedsReviewItem {
  item_index: number;
  question_type: string;
  needs_review_reason: string;
  quality_message: string;
  exam_point_id?: string | null;
  card_id?: string | null;
}

export interface TaskRun {
  id: string;
  course_id: string;
  task_type: string;
  status: string;
  stage: string;
  progress: number;
  attempt: number;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error_code?: string;
  error_message?: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}
