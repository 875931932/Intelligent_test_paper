import { request } from '../http';
import type { PaperArchiveDetail, PaperArchiveSummary } from '../../types/api';

export const paperArchivesApi = {
  /** 课程下全部归档（新 → 旧），资料库「试卷」文件夹列表 */
  list: (courseId: string, token?: string): Promise<PaperArchiveSummary[]> =>
    request('/courses/' + courseId + '/paper-archives', undefined, token),
  /** 归档详情：档案 + 快照内全部题目（预览/编辑/导出的输入） */
  get: (courseId: string, archiveId: string, token?: string): Promise<PaperArchiveDetail> =>
    request('/courses/' + courseId + '/paper-archives/' + archiveId, undefined, token),
  /** 从文件夹里删除（后端真删，不是隐藏） */
  remove: (courseId: string, archiveId: string, token?: string): Promise<void> =>
    request('/courses/' + courseId + '/paper-archives/' + archiveId, { method: 'DELETE' }, token),
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
};
