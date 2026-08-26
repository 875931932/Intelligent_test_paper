import { request } from '../http';
import type { PublishedKnowledgeResponse, EvidenceChunk } from '../../types/api';

export const knowledgeViewApi = {
  getPublished: (courseId: string, token?: string): Promise<PublishedKnowledgeResponse> =>
    request('/courses/' + courseId + '/published-knowledge', undefined, token),

  getCardEvidence: (courseId: string, cardId: string, token?: string): Promise<EvidenceChunk[]> =>
    request('/courses/' + courseId + '/published-knowledge/cards/' + cardId + '/evidence', undefined, token),
};