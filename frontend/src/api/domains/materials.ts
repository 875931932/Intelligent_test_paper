import { request } from '../http';
import type { MaterialContentResponse, MaterialResponse, MaterialVersionResponse, UploadSessionCreate, UploadSessionResponse } from '../../types/api';

export const materialsApi = {
  list: (courseId: string, token?: string): Promise<MaterialResponse[]> =>
    request('/courses/' + courseId + '/materials', undefined, token),
  get: (courseId: string, materialId: string, token?: string): Promise<MaterialResponse> =>
    request('/courses/' + courseId + '/materials/' + materialId, undefined, token),
  /** 解析内容预览：分页浏览（offset/limit），或 blockId 定位该片段±上下文（助手来源核对） */
  getContent: (
    courseId: string,
    materialId: string,
    opts?: { blockId?: string; offset?: number; limit?: number },
    token?: string,
  ): Promise<MaterialContentResponse> => {
    const q = new URLSearchParams();
    if (opts?.blockId) q.set('block_id', opts.blockId);
    q.set('offset', String(opts?.offset ?? 0));
    q.set('limit', String(opts?.limit ?? 30));
    return request('/courses/' + courseId + '/materials/' + materialId + '/content?' + q.toString(), undefined, token);
  },
  createUploadSession: (courseId: string, data: UploadSessionCreate, token?: string): Promise<UploadSessionResponse> =>
    request('/courses/' + courseId + '/upload-sessions', { method: 'POST', body: JSON.stringify(data) }, token),
  completeUpload: (courseId: string, sessionId: string, token?: string): Promise<MaterialVersionResponse> =>
    request('/courses/' + courseId + '/upload-sessions/' + sessionId + '/complete', { method: 'POST' }, token),
  parse: (courseId: string, materialId: string, token?: string): Promise<unknown> =>
    request('/courses/' + courseId + '/materials/' + materialId + '/parse', { method: 'POST' }, token),
  pollParse: (courseId: string, materialId: string, token?: string): Promise<unknown> =>
    request('/courses/' + courseId + '/materials/' + materialId + '/parse/poll', { method: 'POST' }, token),
  updateType: (courseId: string, materialId: string, materialType: string, token?: string): Promise<MaterialResponse> =>
    request('/courses/' + courseId + '/materials/' + materialId + '/type?material_type=' + encodeURIComponent(materialType), { method: 'PATCH' }, token),
  delete: (courseId: string, materialId: string, token?: string): Promise<void> =>
    request('/courses/' + courseId + '/materials/' + materialId, { method: 'DELETE' }, token),
};