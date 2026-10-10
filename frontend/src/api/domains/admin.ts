import { request } from '../http';

/** 后台管理 API（/api/v1/admin/**）：概览、组织账号、模型管理、调用监控。 */

// ── 概览 ──
export interface AdminOverview {
  users: { total: number; active: number; by_role: Record<string, number> };
  organizations: { total: number };
  model_calls_24h: { total: number; failed: number };
}

// ── 组织 ──
export type OrgKind = 'school' | 'college';
export type OrgStatus = 'active' | 'disabled';

export interface AdminOrganization {
  id: string;
  parent_id: string | null;
  kind: OrgKind;
  name: string;
  code: string | null;
  status: OrgStatus;
  children?: AdminOrganization[];
}

export interface OrgCreatePayload {
  name: string;
  kind: OrgKind;
  parent_id?: string;
  code?: string;
}

export interface OrgUpdatePayload {
  name?: string;
  code?: string;
  status?: OrgStatus;
}

// ── 账号 ──
export type AdminRole = 'admin' | 'org_admin' | 'teacher';

export interface AdminUser {
  id: string;
  username: string;
  name: string;
  role: AdminRole;
  organization_id: string | null;
  organization_name: string | null;
  title: string | null;
  employee_no: string | null;
  status: 'active' | 'disabled';
  last_login_at: string | null;
}

export interface UserListPage {
  items: AdminUser[];
  total: number;
  page: number;
  page_size: number;
}

export interface UserCreatePayload {
  username: string;
  password: string;
  name: string;
  role: AdminRole;
  organization_id?: string;
  title?: string;
  employee_no?: string;
}

export interface UserUpdatePayload {
  name?: string;
  role?: AdminRole;
  organization_id?: string;
  title?: string;
  employee_no?: string;
}

// ── 模型管理 ──
export interface LlmProvider {
  id: string;
  name: string;
  base_url: string;
  /** 掩码形态（sk-***xxxx），明文永不出后端 */
  api_key_masked: string | null;
  api_key_env: string | null;
  status: 'active' | 'disabled';
  remark: string | null;
}

export interface LlmProviderPayload {
  name?: string;
  base_url?: string;
  api_key?: string;
  api_key_env?: string;
  remark?: string;
}

export interface LlmModel {
  id: string;
  provider_id: string;
  provider_name?: string | null;
  model_id: string;
  display_name: string;
  profile: Record<string, unknown>;
  price_input_per_mtk: number | null;
  price_output_per_mtk: number | null;
  status: 'active' | 'disabled';
}

export interface LlmModelPayload {
  provider_id?: string;
  model_id?: string;
  display_name?: string;
  profile?: Record<string, unknown>;
  price_input_per_mtk?: number;
  price_output_per_mtk?: number;
}

export type LlmPurpose = 'default' | 'extract' | 'classify' | 'consolidate' | 'generation';

export interface LlmBinding {
  purpose: LlmPurpose;
  source: 'db' | 'env' | 'none';
  /** 该用途回落 default 绑定的标记 */
  via?: string;
  updated_at?: string | null;
  model?: { id?: string; model_id: string; display_name: string } | null;
  provider?: { id?: string; name: string; base_url?: string } | null;
}

export interface ProbeResult {
  ok: boolean;
  latency_ms: number;
  error?: string;
}

// ── 模型调用监控 ──
export interface MonitorRange {
  from?: string;
  to?: string;
}

export interface MonitorOverview {
  range: { from: string; to: string };
  total_calls: number;
  succeeded: number;
  failed: number;
  success_rate: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_estimate: number;
  avg_duration_ms: number | null;
  p50_duration_ms: number | null;
  p95_duration_ms: number | null;
}

export interface MonitorBucket {
  bucket: string;
  calls: number;
  succeeded: number;
  failed: number;
  success_rate: number;
  input_tokens: number;
  output_tokens: number;
}

export type MonitorGroupBy = 'model' | 'stage' | 'status' | 'error_code';

export interface MonitorGroup {
  key: string;
  calls: number;
  succeeded: number;
  success_rate: number;
  input_tokens: number;
  output_tokens: number;
  cost_estimate: number;
  avg_duration_ms: number | null;
}

export interface MonitorCall {
  id: string;
  stage: string;
  provider: string;
  model: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  course_id: string;
  created_at: string;
}

export interface MonitorCallDetail extends MonitorCall {
  request_id: string | null;
  prompt_hash: string | null;
  /** 已脱敏投影（§10.4）：永不含 response / raw_response_snapshot */
  details: Record<string, unknown>;
}

export interface MonitorCallPage {
  items: MonitorCall[];
  total: number;
  page: number;
  page_size: number;
}

/** URL 安全的 query 构建：URLSearchParams 会正确转义 ISO 时间里的 `+`。 */
function buildQuery(params: object): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') query.set(k, String(v));
  });
  const qs = query.toString();
  return qs ? `?${qs}` : '';
}

export const adminApi = {
  overview: (token?: string): Promise<AdminOverview> =>
    request('/admin/overview', undefined, token),

  // 组织
  orgTree: (token?: string): Promise<AdminOrganization[]> =>
    request('/admin/organizations', undefined, token),
  orgScope: (kind?: OrgKind, token?: string): Promise<AdminOrganization[]> =>
    request(`/admin/organizations/scope${kind ? `?kind=${kind}` : ''}`, undefined, token),
  createOrg: (payload: OrgCreatePayload, token?: string): Promise<AdminOrganization> =>
    request('/admin/organizations', { method: 'POST', body: JSON.stringify(payload) }, token),
  updateOrg: (id: string, payload: OrgUpdatePayload, token?: string): Promise<AdminOrganization> =>
    request(`/admin/organizations/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }, token),

  // 账号
  userList: (
    params: {
      q?: string;
      organization_id?: string;
      role?: string;
      status?: string;
      page?: number;
      page_size?: number;
    },
    token?: string,
  ): Promise<UserListPage> => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') query.set(k, String(v));
    });
    const qs = query.toString();
    return request(`/admin/users${qs ? `?${qs}` : ''}`, undefined, token);
  },
  createUser: (payload: UserCreatePayload, token?: string): Promise<AdminUser> =>
    request('/admin/users', { method: 'POST', body: JSON.stringify(payload) }, token),
  updateUser: (id: string, payload: UserUpdatePayload, token?: string): Promise<AdminUser> =>
    request(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }, token),
  setUserStatus: (id: string, status: 'active' | 'disabled', token?: string): Promise<AdminUser> =>
    request(`/admin/users/${id}/status`, { method: 'PUT', body: JSON.stringify({ status }) }, token),
  resetPassword: (id: string, password: string, token?: string): Promise<void> =>
    request(`/admin/users/${id}/password`, { method: 'PUT', body: JSON.stringify({ password }) }, token),

  // 模型管理
  llmProviders: (token?: string): Promise<LlmProvider[]> =>
    request('/admin/llm/providers', undefined, token),
  createLlmProvider: (payload: LlmProviderPayload, token?: string): Promise<LlmProvider> =>
    request('/admin/llm/providers', { method: 'POST', body: JSON.stringify(payload) }, token),
  updateLlmProvider: (id: string, payload: Partial<LlmProviderPayload> & { status?: 'active' | 'disabled' }, token?: string): Promise<LlmProvider> =>
    request(`/admin/llm/providers/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }, token),
  llmModels: (token?: string): Promise<LlmModel[]> =>
    request('/admin/llm/models', undefined, token),
  createLlmModel: (payload: LlmModelPayload, token?: string): Promise<LlmModel> =>
    request('/admin/llm/models', { method: 'POST', body: JSON.stringify(payload) }, token),
  updateLlmModel: (id: string, payload: Partial<LlmModelPayload> & { status?: 'active' | 'disabled' }, token?: string): Promise<LlmModel> =>
    request(`/admin/llm/models/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }, token),
  deleteLlmModel: (id: string, token?: string): Promise<void> =>
    request(`/admin/llm/models/${id}`, { method: 'DELETE' }, token),
  llmBindings: (token?: string): Promise<LlmBinding[]> =>
    request('/admin/llm/bindings', undefined, token),
  setLlmBinding: (purpose: LlmPurpose, modelId: string, token?: string): Promise<LlmBinding> =>
    request(`/admin/llm/bindings/${purpose}`, { method: 'PUT', body: JSON.stringify({ model_id: modelId }) }, token),
  testLlm: (payload: { provider_id?: string; model_id?: string }, token?: string): Promise<ProbeResult> =>
    request('/admin/llm/test', { method: 'POST', body: JSON.stringify(payload) }, token),

  // 模型调用监控
  monitorOverview: (params: MonitorRange, token?: string): Promise<MonitorOverview> =>
    request(`/admin/monitoring/overview${buildQuery(params)}`, undefined, token),
  monitorTimeseries: (
    params: MonitorRange & { bucket: 'hour' | 'day' },
    token?: string,
  ): Promise<MonitorBucket[]> => request(`/admin/monitoring/timeseries${buildQuery(params)}`, undefined, token),
  monitorBreakdown: (
    params: MonitorRange & { group_by: MonitorGroupBy },
    token?: string,
  ): Promise<MonitorGroup[]> => request(`/admin/monitoring/breakdown${buildQuery(params)}`, undefined, token),
  monitorCalls: (
    params: MonitorRange & {
      page?: number;
      page_size?: number;
      status?: string;
      model?: string;
      stage?: string;
      error_code?: string;
    },
    token?: string,
  ): Promise<MonitorCallPage> => request(`/admin/monitoring/calls${buildQuery(params)}`, undefined, token),
  monitorCall: (id: string, token?: string): Promise<MonitorCallDetail> =>
    request(`/admin/monitoring/calls/${id}`, undefined, token),
};
