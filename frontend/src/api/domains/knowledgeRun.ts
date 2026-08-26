import { request } from '../http';
import type { OrganizationRunResponse } from '../../types/api';

export const knowledgeRunApi = {
  createOrganizationRun: (
    courseId: string,
    data: { material_version_ids: string[] },
    token?: string,
  ): Promise<OrganizationRunResponse> =>
    request('/courses/' + courseId + '/organization-runs',
      { method: 'POST', body: JSON.stringify(data) }, token),

  getRun: (courseId: string, runId: string, token?: string): Promise<Record<string, unknown>> =>
    request('/courses/' + courseId + '/organization-runs/' + runId, undefined, token),

  getCandidate: (courseId: string, runId: string, token?: string): Promise<Record<string, unknown>> =>
    request('/courses/' + courseId + '/organization-runs/' + runId + '/candidate', undefined, token),
};