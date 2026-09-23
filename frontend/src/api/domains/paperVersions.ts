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
  reorderItems: (courseId: string, pvId: string, orderedIndices: number[], token?: string): Promise<PaperVersion> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/items/reorder', { method: 'PUT', body: JSON.stringify({ ordered_indices: orderedIndices }) }, token),
  createItem: (courseId: string, pvId: string, body: Record<string, unknown>, token?: string): Promise<PaperVersion> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/items', { method: 'POST', body: JSON.stringify(body) }, token),
  deleteItem: (courseId: string, pvId: string, itemIndex: number, token?: string): Promise<PaperVersion> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/items/' + itemIndex, { method: 'DELETE' }, token),
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
  exportAnswerCard: (courseId: string, projectId: string, pvId: string) =>
    config.apiBase + '/courses/' + courseId + '/exam-projects/' + projectId + '/paper-versions/' + pvId + '/export/answer-card',
};