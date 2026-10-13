import { request } from '../http';
import type { PublishedKnowledgeResponse, PublishedKnowledgeSummary, EvidenceChunk } from '../../types/api';

export const knowledgeViewApi = {
  getPublished: (courseId: string, token?: string): Promise<PublishedKnowledgeResponse> =>
    request('/courses/' + courseId + '/published-knowledge', undefined, token),

  // 概览页只显示卡片/证据计数：走 summary 模式，避免整树（MB 级 cards 字典）往返
  getPublishedSummary: (courseId: string, token?: string): Promise<PublishedKnowledgeSummary> =>
    request('/courses/' + courseId + '/published-knowledge?summary=true', undefined, token),

  getCardEvidence: (courseId: string, cardId: string, token?: string): Promise<EvidenceChunk[]> =>
    request('/courses/' + courseId + '/published-knowledge/cards/' + cardId + '/evidence', undefined, token),
};