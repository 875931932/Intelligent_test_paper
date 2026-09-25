import { useCallback, useState } from 'react';
import { api } from '@/api/client';
import type { CurrentFrameworkResponse, PublishedKnowledgeResponse } from '@/types/api';

/**
 * 后端多数接口只返回 id，展示层需要 id → 中文名 的映射。
 * 数据源双保险：已发布知识目录 + 当前框架 payload（framework_service 也会
 * 写 exam_points）——知识目录未发布/接口失败时，框架侧仍能解析出考点名。
 */
export interface NameMaps {
  examPoints: Record<string, string>;
  anchors: Record<string, string>;
  cards: Record<string, string>;
}

const EMPTY_MAPS: NameMaps = { examPoints: {}, anchors: {}, cards: {} };

/** 框架 payload 里与知识目录同构的考点快照（只要 id/code/title 三个展示字段） */
interface PayloadExamPoint {
  id?: string;
  code?: string;
  title?: string;
}

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
  // 框架 payload 兜底（只补空缺，不覆盖已发布目录的名字）：目录未发布时它是唯一来源
  const payloadPoints = (framework?.payload as { exam_points?: PayloadExamPoint[] } | undefined)?.exam_points;
  (payloadPoints || []).forEach((p) => {
    if (!p) return;
    const name = p.title || p.code || p.id;
    if (!name) return;
    if (p.id && !examPoints[p.id]) examPoints[p.id] = name;
    if (p.code && p.code !== p.id && !examPoints[p.code]) examPoints[p.code] = name;
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
 * 试卷模块内共用（出卷流水线与试卷页签）：拉取已发布知识目录 + 当前框架，构建名称映射。
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
      // 名称映射失败只降级展示（页面回退占位文案），但必须可观测，
      // 否则「考点显示裸 ID」这类问题无从定位
      if (k.status === 'rejected') console.warn('[useNameMaps] 知识目录名称映射加载失败：', k.reason);
      if (f.status === 'rejected') console.warn('[useNameMaps] 框架名称映射加载失败：', f.reason);
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
