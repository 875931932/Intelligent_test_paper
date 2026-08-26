// ============================================================
// API Types - 完整对应后端所有响应模型
// ============================================================

// ── Health ──
export interface HealthResponse {
  api: string;
  postgresql: string;
  redis: string;
  mineru: string;
  deepseek: string;
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
  id: string;
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
  payload?: Record<string, unknown>;
}

// ── Knowledge ──
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
}

export interface OrganizationRunResponse {
  run_id: string;
  candidate_id: string;
  status: string;
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

export interface ContractAuditSummary {
  exam_points: ExamPointProportion[];
  type_counts: Record<string, number>;
  difficulty_counts: Record<string, number>;
}

export interface PaperContract {
  total_score: number;
  slots: ContractSlot[];
  conflicts: ContractConflict[];
  audit_summary: ContractAuditSummary;
}

export interface UnitCoverage {
  unit_id: string;
  exam_point_id: string;
  anchor_key: string;
  card_ids: string[];
  allowed_assessment_modes?: AssessmentMode[];
  operational_detail_policy?: string;
  core?: boolean;
}

export interface ContractRequest {
  total_score: number;
  type_rules: Record<string, Record<string, number>>;
  chapter_weights: Record<string, number>;
  units: UnitCoverage[];
  card_question_types?: Record<string, string[]>;
  card_semantic_profiles?: Record<string, unknown>;
}

// ── Generation ──
export interface GeneratedQuestion {
  item_index: number;
  question_type: string;
  stem: string;
  options?: Record<string, string>;
  answer: string;
  explanation: string;
  score: number;
  difficulty: string;
  cognitive_level: string;
  assessment_mode: string;
  exam_point_id: string;
  card_id: string;
  coverage_atom: string;
  model_call_count?: number;
}

export interface GenerationResult {
  status: 'candidate';
  questions: GeneratedQuestion[];
  final_check: Record<string, unknown>;
  model_call_count: number;
  model: string;
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
  active_paper_version_id?: string;
  model?: string;
  total_score?: number;
  item_count?: number;
  created_at: string;
  updated_at: string;
}

// ── Paper Versons ──
export interface PaperVersionItem {
  item_index: number;
  question_type: string;
  stem: string;
  options: Record<string, string>;
  answer: string;
  explanation: string;
  scoring_detail: string;
  needs_review: boolean;
  needs_review_reasons: string[];
  traceability: Record<string, string>;
  teacher_override_patch: Record<string, unknown>;
  teacher_override_at: string;
}

export interface PaperVersion {
  id: string;
  exam_project_id: string;
  version_no: number;
  total_score: number;
  status: string;
  items: PaperVersionItem[];
  created_at: string;
}

export interface NeedsReviewItem {
  item_index: number;
  question_type: string;
  stem_preview: string;
  reasons: string[];
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

// ── Auth (mock) ──
export interface LoginCredentials {
  username: string;
  password: string;
}
export interface AuthState {
  user: { id: string; username: string; name: string; role: string } | null;
  token: string | null;
  isAuthenticated: boolean;
}
