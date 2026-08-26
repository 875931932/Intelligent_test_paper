import { request } from '../http';
import type { KnowledgeTreeConfirmation } from '../../types/api';

export const knowledgePublishApi = {
  publish: (
    courseId: string,
    runId: string,
    data: KnowledgeTreeConfirmation,
    token?: string,
  ): Promise<Record<string, unknown>> =>
    request('/courses/' + courseId + '/organization-runs/' + runId + '/publish',
      { method: 'POST', body: JSON.stringify(data) }, token),
};