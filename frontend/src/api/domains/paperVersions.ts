import { request } from '../http';
import { config } from '@/config';
import type { PaperVersion, NeedsReviewItem } from '../../types/api';

export const paperVersionsApi = {
  getCurrent: (courseId: string, projectId: string, token?: string): Promise<PaperVersion> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/paper-versions/current', undefined, token),
  getNeedsReview: (courseId: string, pvId: string, token?: string): Promise<NeedsReviewItem[]> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/needs-review', undefined, token),
  patchItem: (courseId: string, pvId: string, itemIndex: number, body: { teacher_override_patch?: Record<string, unknown>; clear_needs_review?: boolean }, token?: string) =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/items/' + itemIndex, { method: 'PATCH', body: JSON.stringify(body) }, token),
  confirm: (courseId: string, pvId: string, body?: { force_ignore_needs_review?: boolean }, token?: string) =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/confirm', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  revert: (courseId: string, pvId: string, token?: string) =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/revert', { method: 'POST' }, token),
  exportJson: (courseId: string, projectId: string, pvId: string) =>
    config.apiBase + '/courses/' + courseId + '/exam-projects/' + projectId + '/paper-versions/' + pvId + '/export/json',
  exportStudent: (courseId: string, projectId: string, pvId: string) =>
    config.apiBase + '/courses/' + courseId + '/exam-projects/' + projectId + '/paper-versions/' + pvId + '/export/student',
  exportAnswerKey: (courseId: string, projectId: string, pvId: string) =>
    config.apiBase + '/courses/' + courseId + '/exam-projects/' + projectId + '/paper-versions/' + pvId + '/export/answer-key',
};