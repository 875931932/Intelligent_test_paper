import { request } from '../http';
import type { ExamProject, PlanItem, TaskRun } from '../../types/api';

export const examProjectsApi = {
  list: (courseId: string, token?: string): Promise<ExamProject[]> =>
    request('/courses/' + courseId + '/exam-projects', undefined, token),
  create: (courseId: string, data: { name: string }, token?: string) =>
    request('/courses/' + courseId + '/exam-projects', { method: 'POST', body: JSON.stringify(data) }, token),
  get: (courseId: string, projectId: string, token?: string): Promise<ExamProject> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId, undefined, token),
  updateStatus: (courseId: string, projectId: string, status: string, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId, { method: 'PATCH', body: JSON.stringify({ status }) }, token),
  createBlueprint: (courseId: string, projectId: string, body: Record<string, unknown>, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/blueprints', { method: 'POST', body: JSON.stringify(body) }, token),
  getPlanItems: (courseId: string, projectId: string, token?: string): Promise<PlanItem[]> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/blueprints/current/plan-items', undefined, token),
  patchPlanItem: (planItemId: string, body: Record<string, unknown>, token?: string) => {
    const courseId: any = undefined;
    return request('/courses/' + courseId + '/exam-projects/plan-items/' + planItemId, { method: 'PATCH', body: JSON.stringify(body) }, token);
  },
  confirmBlueprint: (courseId: string, projectId: string, body?: Record<string, unknown>, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/blueprints/current/confirm', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  allocateContract: (courseId: string, projectId: string, body?: { blueprint_version_id?: string; allocation_seed?: number }, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/allocate', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
reviseContract: (courseId: string, projectId: string, body: { blueprint_version_id?: string; slot_revisions?: unknown[]; allocation_seed?: number }, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/revise', { method: 'PATCH', body: JSON.stringify(body) }, token),
  confirmContract: (courseId: string, projectId: string, body: { blueprint_version_id?: string; slot_revisions?: unknown[]; allocation_seed?: number }, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/contracts/confirm', { method: 'POST', body: JSON.stringify(body) }, token),
  startGeneration: (courseId: string, projectId: string, body?: { mock_graph?: boolean }, token?: string) =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/generate', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  getTaskRun: (courseId: string, taskRunId: string, token?: string): Promise<TaskRun> =>
    request('/courses/' + courseId + '/task-runs/' + taskRunId, undefined, token),
};