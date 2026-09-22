import { useCallback, useState } from 'react';
import { api } from '@/api/client';
import type { CurrentFrameworkResponse, PublishedKnowledgeResponse } from '@/types/api';

/** 后端多数接口只返回 id，展示层需要 id → 中文名 的映射。 */
export interface NameMaps {
  examPoints: Record<string, string>;
  anchors: Record<string, string>;
  cards: Record<string, string>;
}

const EMPTY_MAPS: NameMaps = { examPoints: {}, anchors: {}, cards: {} };

export function buildNameMaps(
  knowledge?: PublishedKnowledgeResponse,
  framework?: CurrentFrameworkResponse,
): NameMaps {
  const examPoints: Record<string, string> = {};
  (knowledge?.exam_points || []).forEach((p) => {
    const name = p.title || p.code || p.id;
    if (p.id) examPoints[p.id] = name;
    // 兼容历史数据用 code 作为 exam_point_id 的情况
    if (p.code && p.code !== p.id) examPoints[p.code] = name;
  });
  const cards: Record<string, string> = {};
  Object.entries(knowledge?.knowledge_cards || {}).forEach(([cid, card]) => {
    cards[cid] = card?.name || cid;
  });
  const anchors: Record<string, string> = {};
  const payloadAnchors = (framework?.payload as { anchors?: Array<{ key?: string; title?: string }> } | undefined)?.anchors;
  (payloadAnchors || []).forEach((a) => {
    if (a?.key) anchors[a.key] = a.title || a.key;
  });
  return { examPoints, anchors, cards };
}

/**
 * 试卷项目与试卷中心共用：拉取已发布知识目录 + 当前框架，构建名称映射。
 * 任一接口失败只丢对应映射，界面回退展示原始 id，不影响主流程。
 * preloadedKnowledge 供调用方复用已取过的数据，避免重复请求。
 */
export function useNameMaps(courseId: string | undefined) {
  const [maps, setMaps] = useState<NameMaps>(EMPTY_MAPS);

  const reload = useCallback(
    async (preloadedKnowledge?: PublishedKnowledgeResponse) => {
      if (!courseId) return;
      const [k, f] = await Promise.allSettled([
        preloadedKnowledge ?? api.knowledge.getPublished(courseId),
        api.framework.getCurrent(courseId),
      ]);
      setMaps(
        buildNameMaps(
          k.status === 'fulfilled' ? k.value : undefined,
          f.status === 'fulfilled' ? f.value : undefined,
        ),
      );
    },
    [courseId],
  );

  return { maps, reload };
}
