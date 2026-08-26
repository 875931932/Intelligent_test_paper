import { request } from '../http';
import type { PaperContract, ContractRequest, GenerationResult } from '../../types/api';

export const blueprintsApi = {
  /** 无项目作用域的契约分配（传统路由，见 docs §6.1）。 */
  allocate: (courseId: string, data: ContractRequest, token?: string): Promise<PaperContract> =>
    request('/courses/' + courseId + '/blueprints/allocate',
      { method: 'POST', body: JSON.stringify(data) }, token),
  /** 无项目作用域的契约确认（传统路由，见 docs §6.2）。 */
  confirm: (
    courseId: string,
    data: {
      contract: PaperContract;
      slot_revisions?: unknown[];
      units?: unknown[];
      knowledge_cards?: Record<string, unknown>;
    },
    token?: string,
  ): Promise<PaperContract> =>
    request('/courses/' + courseId + '/blueprints/confirm',
      { method: 'POST', body: JSON.stringify(data) }, token),
};

export const generationApi = {
  /** 同步候选生成（见 docs §7.1）。 */
  generate: (
    courseId: string,
    data: { contract: unknown[]; knowledge_cards: Record<string, unknown> },
    token?: string,
  ): Promise<GenerationResult> =>
    request('/courses/' + courseId + '/generation-runs',
      { method: 'POST', body: JSON.stringify(data) }, token),
};