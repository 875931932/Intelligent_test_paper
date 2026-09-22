import { request } from '../http';
import type { ExamRules, FrameworkBuildRun, FrameworkConfirmation, CurrentFrameworkResponse, FrameworkCandidate } from '../../types/api';

export const frameworkApi = {
  createRun: (courseId: string, data: { teaching_material_version_id: string; assessment_material_version_id: string }, token?: string): Promise<FrameworkBuildRun> =>
    request('/courses/' + courseId + '/framework-runs', { method: 'POST', body: JSON.stringify(data) }, token),
  getLatest: (courseId: string, token?: string): Promise<FrameworkBuildRun> =>
    request('/courses/' + courseId + '/framework-runs/latest', undefined, token),
  getRun: (courseId: string, runId: string, token?: string): Promise<FrameworkBuildRun> =>
    request('/courses/' + courseId + '/framework-runs/' + runId, undefined, token),
  getCandidate: (courseId: string, runId: string, token?: string): Promise<FrameworkCandidate> =>
    request('/courses/' + courseId + '/framework-runs/' + runId + '/candidate', undefined, token),
  confirm: (courseId: string, runId: string, data: FrameworkConfirmation, token?: string) =>
    request('/courses/' + courseId + '/framework-runs/' + runId + '/confirm', { method: 'POST', body: JSON.stringify(data) }, token),
  reject: (courseId: string, runId: string, token?: string) =>
    request('/courses/' + courseId + '/framework-runs/' + runId + '/reject', { method: 'POST' }, token),
  getCurrent: (courseId: string, token?: string): Promise<CurrentFrameworkResponse> =>
    request('/courses/' + courseId + '/framework-versions/current', undefined, token),
  /** 修改考核大纲的考试规则：题型比例与章节命题权重 */
  updateExamRules: (courseId: string, rules: ExamRules, token?: string): Promise<{ status: string; exam_rules: ExamRules }> =>
    request('/courses/' + courseId + '/framework-versions/current/rules', { method: 'PATCH', body: JSON.stringify(rules) }, token),
};