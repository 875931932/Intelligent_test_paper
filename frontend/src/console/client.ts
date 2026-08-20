/** 教师控制台 API 客户端：课程 → 资料 → 框架 → 知识 → 命题全链路。 */

import { api } from '../api'
import type {
  Course,
  ExamProjectDetail,
  EvidenceLink,
  FrameworkCandidate,
  FrameworkConfirmation,
  FrameworkRunCreated,
  GenerationRunResult,
  KnowledgeTreeCandidate,
  KnowledgeTreeConfirmation,
  Material,
  MaterialType,
  OrganizationRunCreated,
  PaperContract,
  PublishedKnowledge,
  UploadSession,
} from './types'

const base = (courseId: string) => `/api/v1/courses/${courseId}`

// ── 课程 ──────────────────────────────────────────────
export const coursesApi = {
  list: () => api<Course[]>('/api/v1/courses'),
  create: (payload: { name: string; slug: string; description?: string }) =>
    api<Course>('/api/v1/courses', { method: 'POST', body: JSON.stringify(payload) }),
}

// ── 资料：上传 + 解析 ─────────────────────────────────
export const materialsApi = {
  list: (courseId: string) => api<Material[]>(`${base(courseId)}/materials`),

  /** 三步直传：创建会话 → PUT 文件到预签名地址 → 完成归档。 */
  async upload(courseId: string, file: File, materialType: MaterialType, existingMaterialId?: string) {
    const sha256 = await sha256Hex(file)
    const session = await api<UploadSession>(`${base(courseId)}/upload-sessions`, {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        material_type: materialType,
        size_bytes: file.size,
        sha256,
        mime_type: file.type || guessMime(file.name),
        ...(existingMaterialId ? { existing_material_id: existingMaterialId } : {}),
      }),
    })
    const put = await fetch(session.upload_url, {
      method: 'PUT',
      headers: session.headers,
      body: file,
    })
    if (!put.ok) throw new Error(`文件上传失败（${put.status}）`)
    return api<Material['latest_version']>(`${base(courseId)}/upload-sessions/${session.session_id}/complete`, {
      method: 'POST',
    })
  },

  remove: (courseId: string, materialId: string) =>
    api<void>(`${base(courseId)}/materials/${materialId}`, { method: 'DELETE' }),

  updateType: (courseId: string, materialId: string, materialType: string) =>
    api<Material>(`${base(courseId)}/materials/${materialId}/type?material_type=${materialType}`, { method: 'PATCH' }),

  startParse: (courseId: string, materialId: string) =>
    api<{ run_id: string; status: string; reused: boolean }>(`${base(courseId)}/materials/${materialId}/parse`, {
      method: 'POST',
    }),

  pollParse: (courseId: string, materialId: string) =>
    api<{ run_id: string; status: string; error_code?: string | null; error_summary?: string | null }>(
      `${base(courseId)}/materials/${materialId}/parse/poll`,
      { method: 'POST' },
    ),
}

async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

function guessMime(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() ?? ''
  const table: Record<string, string> = {
    pdf: 'application/pdf',
    doc: 'application/msword',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    ppt: 'application/vnd.ms-powerpoint',
    pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    xls: 'application/vnd.ms-excel',
    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    txt: 'text/plain',
    md: 'text/markdown',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    png: 'image/png',
    gif: 'image/gif',
    webp: 'image/webp',
    bmp: 'image/bmp',
  }
  return table[ext] ?? 'application/octet-stream'
}

// ── 考纲框架 ──────────────────────────────────────────
export const frameworkApi = {
  createRun: (courseId: string, teachingVersionId: string, assessmentVersionId: string) =>
    api<FrameworkRunCreated>(`${base(courseId)}/framework-runs`, {
      method: 'POST',
      body: JSON.stringify({
        teaching_material_version_id: teachingVersionId,
        assessment_material_version_id: assessmentVersionId,
      }),
    }),

  getLatestRun: (courseId: string) =>
    api<{ id: string; status: string } | null>(`${base(courseId)}/framework-runs/latest`),

  getCandidate: (courseId: string, runId: string) =>
    api<FrameworkCandidate>(`${base(courseId)}/framework-runs/${runId}/candidate`),

  confirm: (courseId: string, runId: string, confirmation: FrameworkConfirmation) =>
    api<Record<string, unknown>>(`${base(courseId)}/framework-runs/${runId}/confirm`, {
      method: 'POST',
      body: JSON.stringify(confirmation),
    }),

  reject: (courseId: string, runId: string) =>
    api<Record<string, unknown>>(`${base(courseId)}/framework-runs/${runId}/reject`, { method: 'POST' }),

  getCurrent: (courseId: string) =>
    api<Record<string, unknown>>(`${base(courseId)}/framework-versions/current`),
}

// ── 知识整理 ──────────────────────────────────────────
export const knowledgeApi = {
  createRun: (courseId: string, materialVersionIds: string[]) =>
    api<OrganizationRunCreated>(`${base(courseId)}/organization-runs`, {
      method: 'POST',
      body: JSON.stringify({ material_version_ids: materialVersionIds }),
    }),

  getCandidate: (courseId: string, runId: string) =>
    api<KnowledgeTreeCandidate>(`${base(courseId)}/organization-runs/${runId}/candidate`),

  publish: (courseId: string, runId: string, confirmation: KnowledgeTreeConfirmation) =>
    api<Record<string, unknown>>(`${base(courseId)}/organization-runs/${runId}/publish`, {
      method: 'POST',
      body: JSON.stringify(confirmation),
    }),

  getPublished: (courseId: string) => api<PublishedKnowledge>(`${base(courseId)}/published-knowledge`),

  getEvidence: (courseId: string, cardId: string) =>
    api<EvidenceLink[]>(`${base(courseId)}/published-knowledge/cards/${cardId}/evidence`),
}

// ── 蓝图 / 合同 / 出题 ────────────────────────────────
export const examApi = {
  allocate: (
    courseId: string,
    payload: {
      blueprint: Record<string, unknown>
      knowledge_cards: Record<string, unknown>
      allocation_seed?: number | null
    },
  ) => api<PaperContract>(`${base(courseId)}/blueprints/allocate`, { method: 'POST', body: JSON.stringify(payload) }),

  confirm: (
    courseId: string,
    payload: {
      contract: PaperContract
      slot_revisions: unknown[]
      units: unknown[]
      knowledge_cards: Record<string, unknown>
    },
  ) => api<PaperContract>(`${base(courseId)}/blueprints/confirm`, { method: 'POST', body: JSON.stringify(payload) }),

  generate: (courseId: string, contract: unknown[], knowledgeCards: Record<string, unknown>) =>
    api<GenerationRunResult>(`${base(courseId)}/generation-runs`, {
      method: 'POST',
      body: JSON.stringify({ contract, knowledge_cards: knowledgeCards }),
    }),
}

// ── 试卷项目 CRUD ────────────────────────────────────
export const projectsApi = {
  list: (courseId: string) => api<ExamProjectDetail[]>(`${base(courseId)}/exam-projects`),

  create: (courseId: string, name: string) =>
    api<ExamProjectDetail>(`${base(courseId)}/exam-projects`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  get: (courseId: string, projectId: string) =>
    api<ExamProjectDetail>(`${base(courseId)}/exam-projects/${projectId}`),

  updateStatus: (courseId: string, projectId: string, status: string) =>
    api<ExamProjectDetail>(`${base(courseId)}/exam-projects/${projectId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
}

// ── 蓝图 / 合同 / 生成 / 试卷版本 Pipeline API ─────────
export interface PipelineBlueprintInput {
  framework_version_id: string
  catalog_version_id: string
  type_rules: Record<string, { count: number; score: number; difficulty_distribution?: Record<string, number> }>
  chapter_weights: Record<string, number>
  units: Array<unknown>
  card_semantic_profiles: Record<string, unknown>
  card_question_types: Record<string, string[]>
}

export interface PipelinePlanItem {
  id: string
  item_index: number
  question_type: string
  score: number
  difficulty?: string
  cognitive_level?: string
  assessment_unit_id: string | null
  assessment_unit_title?: string
  knowledge_card_id: string | null
  knowledge_card_name?: string
  exam_point_id?: string | null
}

const projectBase = (courseId: string, projectId: string) => `${base(courseId)}/exam-projects/${projectId}`

export const examPipelineApi = {
  // BLUEPRINT
  createBlueprint: (courseId: string, projectId: string, body: PipelineBlueprintInput) =>
    api<{ blueprint_version_id: string; plan: PipelinePlanItem[] }>(`${projectBase(courseId, projectId)}/blueprints`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getPlanItems: (courseId: string, projectId: string) =>
    api<PipelinePlanItem[]>(`${projectBase(courseId, projectId)}/blueprints/current/plan-items`),

  patchPlanItem: (
    courseId: string,
    planItemId: string,
    changes: Partial<PipelinePlanItem> & { card_id?: string },
  ) =>
    api<PipelinePlanItem>(`${base(courseId)}/plan-items/${planItemId}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }),

  confirmBlueprint: (courseId: string, projectId: string, body?: { blueprint_version_id?: string }) =>
    api<{ status: string; blueprint_version_id: string }>(
      `${projectBase(courseId, projectId)}/blueprints/current/confirm`,
      { method: 'POST', body: body ? JSON.stringify(body) : undefined },
    ),

  // CONTRACT
  allocateContract: (
    courseId: string,
    projectId: string,
    body?: { blueprint_version_id?: string; allocation_seed?: number },
  ) =>
    api<{
      used_threshold: number
      conflicts_history: Array<[number, number]>
      contract_snapshot: { slots: Array<Record<string, unknown>> }
    }>(`${projectBase(courseId, projectId)}/contracts/allocate`, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),

  reviseContract: (
    courseId: string,
    projectId: string,
    body: { slot_revisions: Array<unknown>; blueprint_version_id?: string; allocation_seed?: number },
  ) =>
    api<{ revised_contract_snapshot: unknown }>(`${projectBase(courseId, projectId)}/contracts/revise`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  confirmContract: (
    courseId: string,
    projectId: string,
    body: { slot_revisions: Array<unknown>; blueprint_version_id?: string; allocation_seed?: number },
  ) =>
    api<{ generation_run_id: string; threshold: number; slot_count: number }>(
      `${projectBase(courseId, projectId)}/contracts/confirm`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  // GENERATION
  startGeneration: (courseId: string, projectId: string, body?: { mock_graph?: boolean }) =>
    api<{ task_run_id: string }>(`${projectBase(courseId, projectId)}/generate`, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),

  getTaskRun: (courseId: string, taskRunId: string) =>
    api<{
      id: string
      status: string
      progress: number
      stage: string
      result?: unknown
      payload?: unknown
      error_message?: string
    }>(`${base(courseId)}/task-runs/${taskRunId}`),

  // PAPER VERSIONS
  getCurrentPaperVersion: (courseId: string, projectId: string) =>
    api<unknown>(`${projectBase(courseId, projectId)}/paper-versions/current`),

  listNeedsReview: (courseId: string, pvId: string) =>
    api<Array<Record<string, unknown>>>(`${base(courseId)}/paper-versions/${pvId}/needs-review`),

  patchPaperItem: (
    courseId: string,
    pvId: string,
    itemIndex: number,
    body: { teacher_override_patch: unknown; clear_needs_review?: boolean },
  ) =>
    api<unknown>(`${base(courseId)}/paper-versions/${pvId}/items/${itemIndex}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  confirmPaperVersion: (courseId: string, pvId: string, body?: { force_ignore_needs_review?: boolean }) =>
    api<{ status: string; unresolved: number }>(`${base(courseId)}/paper-versions/${pvId}/confirm`, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),

  revertPaperVersion: (courseId: string, pvId: string) =>
    api<void>(`${base(courseId)}/paper-versions/${pvId}/revert`, { method: 'POST' }),

  // ── 导出 ──
  exportStudentPaperUrl: (courseId: string, projectId: string, pvId: string) =>
    `${base(courseId)}/exam-projects/${projectId}/paper-versions/${pvId}/export/student`,
  exportAnswerKeyUrl: (courseId: string, projectId: string, pvId: string) =>
    `${base(courseId)}/exam-projects/${projectId}/paper-versions/${pvId}/export/answer-key`,
  exportAnswerDetailJsonUrl: (courseId: string, projectId: string, pvId: string) =>
    `${base(courseId)}/exam-projects/${projectId}/paper-versions/${pvId}/export/json`,
}
