import { request, requestBlob } from '../http';
import type { PaperVersion, NeedsReviewItem } from '../../types/api';

/** 可导出的三份卷面 + 答案细则 JSON；前三种同时也是整体预览的页签 */
export type PaperExportKind = 'student' | 'card' | 'answer' | 'json';

export type PaperExportFile = { blob: Blob; filename: string };

const EXPORT_SUFFIX: Record<PaperExportKind, string> = {
  student: 'student',
  card: 'answer-card',
  answer: 'answer-key',
  json: 'json',
};

/** 三份 HTML 导出后端不带 Content-Disposition，下载文件名由前端命名 */
const HTML_FILENAME: Record<Exclude<PaperExportKind, 'json'>, string> = {
  student: '学生卷.html',
  card: '答题卡.html',
  answer: '答卷_含答案.html',
};

const exportPath = (kind: PaperExportKind, courseId: string, projectId: string, pvId: string): string =>
  '/courses/' + courseId + '/exam-projects/' + projectId +
  '/paper-versions/' + pvId + '/export/' + EXPORT_SUFFIX[kind];

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
  /** 发起单题 AI 改题任务（202）；提案经校验后用 examProjects.getTaskRun 轮询取回 */
  aiRevise: (courseId: string, pvId: string, itemIndex: number, instruction: string, token?: string): Promise<{ task_run_id: string }> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/items/' + itemIndex + '/ai-revise', { method: 'POST', body: JSON.stringify({ instruction }) }, token),
  /** 发起整题 AI 生成任务（202）；提案回填新增表单，教师确认后走既有 createItem 落库 */
  aiGenerate: (courseId: string, pvId: string, instruction: string, token?: string): Promise<{ task_run_id: string }> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/items/ai-generate', { method: 'POST', body: JSON.stringify({ instruction }) }, token),
  /**
   * 发起整卷 AI 质量评审任务（202，纯只读报告，定稿卷也可评审）；
   * 报告用 examProjects.getTaskRun 轮询取回。instruction 可空 = 标准评审。
   */
  aiReview: (courseId: string, pvId: string, instruction: string, token?: string): Promise<{ task_run_id: string }> =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/ai-review', { method: 'POST', body: JSON.stringify({ instruction }) }, token),
  confirm: (courseId: string, pvId: string, body?: { force_ignore_needs_review?: boolean }, token?: string) =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/confirm', { method: 'POST', body: body ? JSON.stringify(body) : undefined }, token),
  revert: (courseId: string, pvId: string, token?: string) =>
    request('/courses/' + courseId + '/paper-versions/' + pvId + '/revert', { method: 'POST' }, token),
  /**
   * 拉取导出产物（带 Authorization 头，token 不进 URL）。
   * 导出下载与整体预览统一走这里：返回 Blob 由调用方生成 object URL。
   * JSON 文件名与后端 Content-Disposition 同规则：answer_detail_v{n}.json。
   */
  fetchExport: async (
    kind: PaperExportKind,
    courseId: string,
    projectId: string,
    pvId: string,
    token?: string,
  ): Promise<PaperExportFile> => {
    const blob = await requestBlob(exportPath(kind, courseId, projectId, pvId), {}, token);
    if (kind !== 'json') return { blob, filename: HTML_FILENAME[kind] };
    let versionNo = 1;
    try {
      const parsed: unknown = JSON.parse(await blob.text());
      if (parsed && typeof parsed === 'object') {
        const v = (parsed as { version_no?: unknown }).version_no;
        if (typeof v === 'number' && Number.isFinite(v)) versionNo = v;
      }
    } catch {
      // 内容异常时退回默认文件名，下载不因命名失败而中断
    }
    return { blob, filename: 'answer_detail_v' + versionNo + '.json' };
  },
};