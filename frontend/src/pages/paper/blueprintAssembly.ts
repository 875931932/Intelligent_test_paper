import { api } from '@/api/client';

/**
 * 蓝图创建请求体组装——试卷页流水线与 AI 助手提案共用的**唯一**实现。
 *
 * 章节权重口径（与考纲硬约束一致）：优先用考核大纲声明的命题权重
 * （框架 payload 里的 exam_rules.chapter_weights），考纲没声明时才回退到
 * 考点权重累加（模型自报）；未声明的锚点补 0，保证章权重覆盖全部考核单元
 * ——蓝图引擎要求一个都不能少。
 */
/** type 字面量（非 interface）：保留对 Record<string, unknown> 的可赋值性 */
export type AssembledBlueprintBody = {
  framework_version_id: string;
  catalog_version_id: string;
  /** 恒为 {}：题型分布由后端按考核规则推导（type_rules_from_ratios） */
  type_rules: Record<string, unknown>;
  chapter_weights: Record<string, number>;
  units: Array<{
    unit_id: string;
    exam_point_id: string;
    anchor_key: string;
    card_ids: string[];
  }>;
};

type PublishedCatalog = Awaited<ReturnType<typeof api.knowledge.getPublished>>;

export type BlueprintAssembly =
  | {
      ok: true;
      body: AssembledBlueprintBody;
      /** 刚拉取的已发布目录：创建成功后就地刷新名称映射用 */
      catalog: PublishedCatalog;
    }
  | { ok: false; reason: 'catalog_unpublished' };

export async function assembleBlueprintRequestBody(
  courseId: string,
): Promise<BlueprintAssembly> {
  // 同时取知识目录与当前框架：后者带考核大纲抽取出的考试规则（题型比例/章节权重）
  const [catalog, framework] = await Promise.all([
    api.knowledge.getPublished(courseId),
    api.framework.getCurrent(courseId).catch(() => null),
  ]);
  if (catalog?.published === false || !catalog?.units || catalog.units.length === 0) {
    return { ok: false, reason: 'catalog_unpublished' };
  }

  const units = (catalog.units || []).map((u) => ({
    unit_id: u.unit_id,
    exam_point_id: u.exam_point_id || u.exam_point_code || '',
    anchor_key: u.anchor_key || '',
    card_ids: u.card_ids || [],
  }));

  const unitAnchors = new Set(units.map((u) => u.anchor_key).filter(Boolean));
  const declared: Record<string, number> = {};
  (framework?.exam_rules?.chapter_weights || []).forEach((c) => {
    if (c.anchor_key) declared[c.anchor_key] = Number(c.weight) || 0;
  });
  let chapter_weights: Record<string, number> = {};
  const declaredKeys = Object.keys(declared);
  if (declaredKeys.length > 0 && declaredKeys.some((k) => declared[k] > 0)) {
    unitAnchors.forEach((a) => {
      chapter_weights[a] = declared[a] ?? 0;
    });
  }
  if (Object.keys(chapter_weights).length === 0) {
    (catalog.exam_points || []).forEach((p) => {
      const key = p.anchor_key || p.id;
      // 同一章（anchor_key）下可能有多个考点，权重需累加，而不是后者覆盖前者，
      // 否则 chapter_weights 合计远小于 100，蓝图引擎的章节权重校验会失败。
      if (key && p.weight_value != null) {
        chapter_weights[key] = (chapter_weights[key] ?? 0) + p.weight_value;
      }
    });
  }

  return {
    ok: true,
    catalog,
    body: {
      framework_version_id: catalog.framework_version_id,
      catalog_version_id: catalog.catalog_version_id,
      type_rules: {},
      chapter_weights,
      units,
    },
  };
}
