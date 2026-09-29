import { request, requestBlob } from '../http';
import type { PaperArchiveDetail, PaperArchiveSummary } from '../../types/api';
import type { PaperExportFile, PaperExportFormat, PaperExportKind } from './paperVersions';

/** 归档四件套导出的路径段（与试卷页导出同名端点，数据源换成归档快照） */
const EXPORT_SUFFIX: Record<PaperExportKind, string> = {
  student: 'student',
  card: 'answer-card',
  answer: 'answer-key',
  json: 'json',
};

/** 后端只给 JSON 带 Content-Disposition，HTML 的下载名由前端命名 */
const HTML_FILENAME: Record<Exclude<PaperExportKind, 'json'>, string> = {
  student: '学生卷.html',
  card: '答题卡.html',
  answer: '答卷_含答案.html',
};

const base = (courseId: string, archiveId: string): string =>
  '/courses/' + courseId + '/paper-archives/' + archiveId;

/** 存回试卷区的结果：新建的一版（version_no 追加、已设为项目当前卷） */
export interface ArchiveRestoreResult {
  paper_version_id: string;
  version_no: number;
  exam_project_id: string;
  item_count: number;
}

export const paperArchivesApi = {
  /** 课程下全部归档（新 → 旧），资料库「试卷」文件夹列表 */
  list: (courseId: string, token?: string): Promise<PaperArchiveSummary[]> =>
    request('/courses/' + courseId + '/paper-archives', undefined, token),
  /** 归档详情：档案 + 快照内全部题目（预览/编辑/导出的输入） */
  get: (courseId: string, archiveId: string, token?: string): Promise<PaperArchiveDetail> =>
    request(base(courseId, archiveId), undefined, token),
  /** 从文件夹里删除（后端真删，不是隐藏） */
  remove: (courseId: string, archiveId: string, token?: string): Promise<void> =>
    request(base(courseId, archiveId), { method: 'DELETE' }, token),
  /** 把一份试卷存进资料库：存快照副本，不受「每项目只留 3 份」约束 */
  archive: (
    courseId: string,
    projectId: string,
    body: { source_paper_version_id?: string; name?: string },
    token?: string,
  ): Promise<PaperArchiveDetail> =>
    request('/courses/' + courseId + '/exam-projects/' + projectId + '/paper-archives', {
      method: 'POST',
      body: JSON.stringify(body),
    }, token),

  // ── P2c 快照编辑（只动归档副本，原试卷不受影响） ──

  /** 改归档名（文件夹里的辨识名） */
  rename: (courseId: string, archiveId: string, name: string, token?: string): Promise<PaperArchiveDetail> =>
    request(base(courseId, archiveId), { method: 'PATCH', body: JSON.stringify({ name }) }, token),
  /** 改归档里的一道题：patch 键域与试卷页编辑器提交字段同口径 */
  patchQuestion: (
    courseId: string,
    archiveId: string,
    itemIndex: number,
    patch: Record<string, unknown>,
    token?: string,
  ): Promise<PaperArchiveDetail> =>
    request(base(courseId, archiveId) + '/questions/' + itemIndex, {
      method: 'PATCH',
      body: JSON.stringify({ patch }),
    }, token),
  /** 删归档里的一道题（其后题号前移） */
  deleteQuestion: (
    courseId: string,
    archiveId: string,
    itemIndex: number,
    token?: string,
  ): Promise<PaperArchiveDetail> =>
    request(base(courseId, archiveId) + '/questions/' + itemIndex, { method: 'DELETE' }, token),
  /** 按给定题号顺序重排（必须恰好覆盖当前全部题号） */
  reorder: (
    courseId: string,
    archiveId: string,
    orderedIndices: number[],
    token?: string,
  ): Promise<PaperArchiveDetail> =>
    request(base(courseId, archiveId) + '/questions/reorder', {
      method: 'PUT',
      body: JSON.stringify({ ordered_indices: orderedIndices }),
    }, token),
  /**
   * 存回试卷区：按快照新建一版并设为项目当前卷（version_no 追加、
   * 走「只留最近 3 份」保留策略）。归档本身不动。
   */
  restore: (courseId: string, archiveId: string, token?: string): Promise<ArchiveRestoreResult> =>
    request(base(courseId, archiveId) + '/restore', { method: 'POST' }, token),

  // ── P2c 四件套导出（带 Authorization 头，token 不进 URL） ──

  /**
   * 拉取归档的单份导出产物。format 对学生卷与答题卡生效（docx 可编辑 Word）；
   * 归档导出与试卷页共用文件名规则。
   */
  fetchExport: async (
    kind: PaperExportKind,
    courseId: string,
    archiveId: string,
    token?: string,
    format?: PaperExportFormat,
  ): Promise<PaperExportFile> => {
    const docx = (kind === 'card' || kind === 'student') && format === 'docx';
    const path = base(courseId, archiveId) + '/export/' + EXPORT_SUFFIX[kind] + (docx ? '?format=docx' : '');
    const blob = await requestBlob(path, {}, token);
    if (docx) return { blob, filename: kind === 'card' ? '答题卡.docx' : '考试卷.docx' };
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
  /** 一键打包：全部导出产物打成 zip；文件名版本号取自归档快照 */
  fetchBundle: async (
    courseId: string,
    archiveId: string,
    versionNo: number,
    token?: string,
  ): Promise<PaperExportFile> => {
    const blob = await requestBlob(base(courseId, archiveId) + '/export/bundle', {}, token);
    return { blob, filename: '试卷包_v' + versionNo + '.zip' };
  },
};
