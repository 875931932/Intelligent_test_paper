/** 教师控制台 API 客户端：课程 → 资料 → 框架 → 知识 → 命题全链路。 */

import { api } from '../api'
import type {
  Course,
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
