import { request } from '../http';

/** 校级教师共享知识库（P1.2）：空间、成员与共享策略。 */
export type KnowledgeRole = 'owner' | 'editor' | 'publisher' | 'reader';
export type ReadScope = 'members' | 'organization' | 'school';

export interface KnowledgeSpace {
  id: string;
  name: string;
  school_id: string;
  org_id: string;
  org_name?: string | null;
  read_scope: ReadScope;
  read_scope_label: string;
  retrieval_mode: 'hybrid' | 'lexical';
  allow_course_import: boolean;
  subject_tags: string[];
  status: 'active' | 'archived';
  policy_version: number;
  role: KnowledgeRole;
  document_count: number;
  created_at?: string | null;
}

export interface KnowledgeDocumentVersion {
  id: string;
  version_no: number;
  state: string;
  mime_type: string;
  size_bytes: number;
  effective_from?: string | null;
  effective_to?: string | null;
  published_at?: string | null;
}

export interface KnowledgeDocument {
  id: string;
  title: string;
  classification: 'shareable_teaching' | 'limited_teaching';
  status: string;
  published_version_id?: string | null;
  access_version: number;
  updated_at?: string | null;
  version?: KnowledgeDocumentVersion | null;
}

export interface KnowledgeMember {
  user_id: string;
  role: KnowledgeRole;
  name?: string | null;
  username?: string | null;
  status?: string | null;
}

export interface KnowledgeParseStatus {
  id: string;
  state: 'queued' | 'running' | 'ready' | 'failed';
  error_code?: string | null;
  attempts: number;
  updated_at?: string | null;
}

export interface KnowledgeDocumentVersionDetail extends KnowledgeDocumentVersion {
  file_hash: string;
  parse?: KnowledgeParseStatus | null;
}

export interface KnowledgeDocumentDetail {
  id: string;
  title: string;
  classification: 'shareable_teaching' | 'limited_teaching';
  status: string;
  published_version_id?: string | null;
  access_version: number;
  updated_at?: string | null;
  versions: KnowledgeDocumentVersionDetail[];
}

export interface KnowledgeBlock {
  block_index: number;
  block_type: string;
  text: string;
  page_index?: number | null;
  heading_path: string[];
}

export interface KnowledgeVersionContent {
  document_id: string;
  version_id: string;
  version_no: number;
  state: string;
  blocks: KnowledgeBlock[];
}

export interface UploadSessionCreatePayload {
  filename: string;
  size_bytes: number;
  mime_type: string;
  sha256: string;
}

export interface UploadSessionResult {
  session_id: string;
  object_key: string;
  upload_url: string;
  expires_at: string;
  headers: Record<string, string>;
}

export interface UploadCompletePayload {
  title?: string;
  classification?: 'shareable_teaching' | 'limited_teaching';
  document_id?: string;
}

export interface UploadCompleteResult {
  document_id: string;
  version_id: string;
  version_no: number;
  parse_run_id: string;
  job_id: string;
  state: string;
}

export interface KnowledgeJob {
  id: string;
  kind: string;
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  progress: number;
  attempts: number;
  error_code?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface KnowledgeSource {
  source_id: number;
  chunk_id: string;
  space_id: string;
  document_id: string;
  document_title: string;
  version_id: string;
  version_no: number;
  page_index?: number | null;
  heading_path: string[];
  quote: string;
}

export interface KnowledgeMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  state: string;
  sources: KnowledgeSource[];
  dependency_versions: string[];
  retrieval_snapshot: Record<string, unknown>;
  feedback: Record<string, unknown>;
  created_at?: string | null;
}

export interface KnowledgeSessionSummary {
  id: string;
  title: string;
  selected_space_ids: string[];
  updated_at?: string | null;
}

export interface KnowledgeSessionDetail extends KnowledgeSessionSummary {
  created_at?: string | null;
  messages: KnowledgeMessage[];
}

export interface KnowledgeGovernanceSummary {
  scope: { school_id: string; org_id?: string | null };
  spaces: { total: number; active: number; archived: number };
  documents: { total: number; published: number };
  versions: { total: number };
  parsing: { failed: number };
  index: { ready_builds: number; chunks: number };
  jobs: Record<string, number>;
  feedback: Record<string, number>;
  model_cost: { calls: number; spend: string; note: string };
}

export interface KnowledgeMeta {
  read_scopes: Array<{ value: ReadScope; label: string; description: string }>;
  classifications: Array<{ value: string; label: string; description: string }>;
}

export interface PolicyPreview {
  space_id: string;
  current_read_scope: ReadScope;
  new_read_scope: ReadScope;
  requires_confirmation: boolean;
  newly_visible_documents: Array<{
    document_id: string;
    title: string;
    classification: string;
  }>;
  active_school_user_count: number;
  notes: string[];
}

export interface SpaceCreatePayload {
  name: string;
  org_id?: string;
  read_scope?: ReadScope;
  subject_tags?: string[];
  allow_course_import?: boolean;
  owner_id?: string;
}

export interface SpaceUpdatePayload {
  expected_policy_version: number;
  name?: string;
  read_scope?: ReadScope;
  allow_course_import?: boolean;
  subject_tags?: string[];
  confirm_scope_expansion?: boolean;
}

export const knowledgeBaseApi = {
  meta: (): Promise<KnowledgeMeta> => request('/knowledge/meta'),
  listSpaces: (subjectTag?: string): Promise<KnowledgeSpace[]> =>
    request(
      '/knowledge/spaces' +
        (subjectTag ? '?subject_tag=' + encodeURIComponent(subjectTag) : '')
    ),
  createSpace: (payload: SpaceCreatePayload): Promise<KnowledgeSpace> =>
    request('/knowledge/spaces', { method: 'POST', body: JSON.stringify(payload) }),
  getSpace: (spaceId: string): Promise<KnowledgeSpace> =>
    request('/knowledge/spaces/' + spaceId),
  updateSpace: (spaceId: string, payload: SpaceUpdatePayload): Promise<KnowledgeSpace> =>
    request('/knowledge/spaces/' + spaceId, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  previewPolicy: (spaceId: string, newReadScope: ReadScope): Promise<PolicyPreview> =>
    request('/knowledge/spaces/' + spaceId + '/policy-preview', {
      method: 'POST',
      body: JSON.stringify({ new_read_scope: newReadScope }),
    }),
  archiveSpace: (spaceId: string): Promise<KnowledgeSpace> =>
    request('/knowledge/spaces/' + spaceId + '/archive', { method: 'POST' }),
  listDocuments: (spaceId: string): Promise<KnowledgeDocument[]> =>
    request('/knowledge/spaces/' + spaceId + '/documents'),
  getDocument: (spaceId: string, documentId: string): Promise<KnowledgeDocumentDetail> =>
    request('/knowledge/spaces/' + spaceId + '/documents/' + documentId),
  getVersionContent: (
    spaceId: string,
    documentId: string,
    versionId: string
  ): Promise<KnowledgeVersionContent> =>
    request(
      '/knowledge/spaces/' +
        spaceId +
        '/documents/' +
        documentId +
        '/versions/' +
        versionId +
        '/content'
    ),
  createUploadSession: (
    spaceId: string,
    payload: UploadSessionCreatePayload
  ): Promise<UploadSessionResult> =>
    request('/knowledge/spaces/' + spaceId + '/upload-sessions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  completeUpload: (
    spaceId: string,
    sessionId: string,
    payload: UploadCompletePayload
  ): Promise<UploadCompleteResult> =>
    request(
      '/knowledge/spaces/' + spaceId + '/upload-sessions/' + sessionId + '/complete',
      { method: 'POST', body: JSON.stringify(payload) }
    ),
  runJob: (spaceId: string, jobId: string): Promise<KnowledgeJob> =>
    request('/knowledge/spaces/' + spaceId + '/jobs/' + jobId + '/run', {
      method: 'POST',
    }),
  getJob: (spaceId: string, jobId: string): Promise<KnowledgeJob> =>
    request('/knowledge/spaces/' + spaceId + '/jobs/' + jobId),
  publishDocument: (
    spaceId: string,
    documentId: string,
    payload: { expected_access_version?: number; effective_to?: string }
  ): Promise<KnowledgeDocumentDetail> =>
    request('/knowledge/spaces/' + spaceId + '/documents/' + documentId + '/publish', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  withdrawDocument: (spaceId: string, documentId: string): Promise<KnowledgeDocumentDetail> =>
    request('/knowledge/spaces/' + spaceId + '/documents/' + documentId + '/withdraw', {
      method: 'POST',
    }),
  importToCourse: (
    spaceId: string,
    documentId: string,
    versionId: string,
    payload: { course_id: string; material_type?: string; title?: string }
  ): Promise<{
    state: string;
    idempotent: boolean;
    material_version_id: string;
    logical_name?: string;
  }> =>
    request(
      '/knowledge/spaces/' +
        spaceId +
        '/documents/' +
        documentId +
        '/versions/' +
        versionId +
        '/import-to-course',
      { method: 'POST', body: JSON.stringify(payload) }
    ),
  listSessions: (): Promise<KnowledgeSessionSummary[]> => request('/knowledge/sessions'),
  adminSummary: (): Promise<KnowledgeGovernanceSummary> => request('/knowledge/admin/summary'),
  createSession: (
    selectedSpaceIds: string[],
    title?: string
  ): Promise<KnowledgeSessionDetail> =>
    request('/knowledge/sessions', {
      method: 'POST',
      body: JSON.stringify({ selected_space_ids: selectedSpaceIds, title }),
    }),
  getSession: (sessionId: string): Promise<KnowledgeSessionDetail> =>
    request('/knowledge/sessions/' + sessionId),
  ask: (sessionId: string, question: string): Promise<KnowledgeSessionDetail> =>
    request('/knowledge/sessions/' + sessionId + '/ask', {
      method: 'POST',
      body: JSON.stringify({ question }),
    }),
  submitFeedback: (
    sessionId: string,
    messageId: string,
    payload: { rating?: string; comment?: string; reason?: string }
  ): Promise<unknown> =>
    request(
      '/knowledge/sessions/' + sessionId + '/messages/' + messageId + '/feedback',
      { method: 'POST', body: JSON.stringify(payload) }
    ),
  listMembers: (spaceId: string): Promise<KnowledgeMember[]> =>
    request('/knowledge/spaces/' + spaceId + '/members'),
  upsertMember: (spaceId: string, userId: string, role: KnowledgeRole): Promise<unknown> =>
    request('/knowledge/spaces/' + spaceId + '/members/' + encodeURIComponent(userId), {
      method: 'PUT',
      body: JSON.stringify({ role }),
    }),
  removeMember: (spaceId: string, userId: string): Promise<void> =>
    request('/knowledge/spaces/' + spaceId + '/members/' + encodeURIComponent(userId), {
      method: 'DELETE',
    }),
  transferOwner: (
    spaceId: string,
    targetUserId: string,
    expectedPolicyVersion: number
  ): Promise<unknown> =>
    request('/knowledge/spaces/' + spaceId + '/transfer-owner', {
      method: 'POST',
      body: JSON.stringify({
        target_user_id: targetUserId,
        expected_policy_version: expectedPolicyVersion,
      }),
    }),
};
