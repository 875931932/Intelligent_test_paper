import { request } from '../http';
import type { ExamProject, PlanItem, TaskRun, ContractSlot, ContractConflict, ContractAuditSummary } from '../../types/api';

export interface ContractSnapshot {
  slots: ContractSlot[];
  total_score?: number;
  conflicts?: ContractConflict[];
  audit_summary?: ContractAuditSummary;
  /** 确认合同时使用的分配种子（None/缺省 = 确定性默认）；退出项目再进入时用于回填「分配方案」下拉 */
  allocation_seed?: number | null;
}

export interface AllocateContractResponse {
  contract_snapshot: ContractSnapshot;
}

/** 当前已确认合同的读取响应：快照持久化在 generation_runs，未确认时为 404。 */
export interface CurrentContractResponse {
  generation_run_id: string;
  contract_snapshot: ContractSnapshot;
}

export interface CreateBlueprintResponse {
  blueprint_version_id: string;
  plan: PlanItem[];
}

export interface GenerateResponse {
  task_run_id: string;
}

export const examProjectsApi = {
  list: (courseId: string, token?: string): Promise<ExamProject[]> =>
    request('/courses/' + courseId + '/exam-projects', undefined, token),
  create: (courseId: string, data: { name: string }, token?: string): Promise<ExamProject> =>
    request('/courses/' + courseId + '/exam-projects', { method: 'POST', body: JSON.stringify(data) }, token),
  remove: (courseId: string, projectId: string, token?: string): Promise<void> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId, { method: 'DELETE' }, token),
  get: (courseId: string, projectId: string, token?: string): Promise<ExamProject> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId, undefined, token),
  updateStatus: (courseId: string, projectId: string, status: string, token?: string): Promise<ExamProject> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId, { method: 'PATCH', body: JSON.stringify({ status }) }, token),
  createBlueprint: (courseId: string, projectId: string, body: Record<string, unknown>, token?: string): Promise<CreateBlueprintResponse> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/blueprints', { method: 'POST', body: JSON.stringify(body) }, token),
  getPlanItems: (courseId: string, projectId: string, token?: string): Promise<PlanItem[]> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/blueprints/current/plan-items', undefined, token),
  confirmBlueprint: (courseId: string, projectId: string, body?: Record<string, unknown>, token?: string): Promise<Record<string, unknown>> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/blueprints/current/confirm', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  allocateContract: (courseId: string, projectId: string, body?: { blueprint_version_id?: string; allocation_seed?: number }, token?: string): Promise<AllocateContractResponse> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/allocate', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  reviseContract: (courseId: string, projectId: string, body: { blueprint_version_id?: string; slot_revisions?: unknown[]; allocation_seed?: number }, token?: string): Promise<Record<string, unknown>> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/revise', { method: 'PATCH', body: JSON.stringify(body) }, token),
  /** 合同槽位 AI 解释（只读建议，异步任务；实际调整仍走 reviseContract/confirmContract） */
  explainContractSlot: (courseId: string, projectId: string, itemIndex: number, body: { allocation_seed?: number; blueprint_version_id?: string; instruction?: string }, token?: string): Promise<{ task_run_id: string }> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contract-slots/' + itemIndex + '/explain', { method: 'POST', body: JSON.stringify(body) }, token),
  confirmContract: (courseId: string, projectId: string, body: { blueprint_version_id?: string; slot_revisions?: unknown[]; allocation_seed?: number }, token?: string): Promise<Record<string, unknown>> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/confirm', { method: 'POST', body: JSON.stringify(body) }, token),
  getCurrentContract: (courseId: string, projectId: string, token?: string): Promise<CurrentContractResponse> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/current', undefined, token),
  startGeneration: (courseId: string, projectId: string, body?: { mock_graph?: boolean }, token?: string): Promise<GenerateResponse> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/generate', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  getTaskRun: (courseId: string, taskRunId: string, token?: string): Promise<TaskRun> =>
    request('/courses/' + courseId + '/exam-projects/task-runs/' + taskRunId, undefined, token),
};
