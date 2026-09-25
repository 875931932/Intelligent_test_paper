/**
 * 前端展示层格式化的唯一出口（页面禁止各自 toFixed / 裸拼 '%'）。
 *
 * 背景：后端多个字段是 Float（plan_items.score、exam_points.weight_value、
 * anchors.exam_weight），直接渲染会出现 33.333333333333336% 这类长尾小数；
 * 置信度个别通道会下发 0-1 小数，与 0-100 整数混用。统一在此收敛。
 * 口径镜像后端 paper_version_service._trim_number（整数去尾、浮点短格式）。
 */

/** 分值：试卷分值按 0.5 递增，至多 1 位小数，整数不带尾巴（2 / 2.5）。 */
export function formatScore(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  const r = Math.round(n * 10) / 10;
  return Number.isInteger(r) ? String(r) : r.toFixed(1);
}

/** 百分比：至多 1 位小数（33.3% / 100%）。 */
export function formatPercent(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  const r = Math.round(n * 10) / 10;
  return `${Number.isInteger(r) ? r : r.toFixed(1)}%`;
}

/**
 * 置信度 → 整数百分比。量纲归一规则：整数按 0-100 直读（数据库约束
 * confidence ∈ [0,100] Integer）；非整数且 ≤1 视为 0-1 小数通道换算 ×100。
 */
export function formatConfidence(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  const pct = !Number.isInteger(n) && n > 0 && n <= 1 ? n * 100 : n;
  return `${Math.round(pct)}%`;
}

/**
 * 32 位十六进制 ID（UUID 无连字符形态）判定。这类值对教师毫无意义，
 * 展示层一律不直接渲染，走 friendlyId 降级。
 */
export function looksLikeRawId(value: string): boolean {
  return /^[0-9a-f]{16,}$/i.test(value);
}

/**
 * 名称降级链的最后一环：能读懂的业务 code 原样展示；空值与裸 ID
 * 统一替换为占位文案（真值放 title 悬浮提示，由调用方挂）。
 */
export function friendlyId(value: string | null | undefined, fallback: string): string {
  return value && !looksLikeRawId(value) ? value : fallback;
}
