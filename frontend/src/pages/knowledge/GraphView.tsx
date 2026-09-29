import { useState, useMemo, useCallback, useRef, memo, useEffect } from 'react';
import { Network } from 'lucide-react';
import type { FrameworkExamPoint, AssessmentUnit, KnowledgeCard } from '@/types/api';
import { truncate } from './knowledgeShared';
import { formatPercent } from '@/lib/format';

// ─── Radial Knowledge Graph（径向层级图谱）───
//
// 旧版「星空」把 500+ 节点撒在 4000×3000 画布上，螺线距离不编码语义，
// 1400 颗星尘纯属噪音。新版语义优先：
//   角度 = 章节命题权重（扇区大小即考纲占比）
//   环层 = 层级：内环章节、外环考点
//   弧线 = 考点间关系（先修 / 对比 / 等价，取卡片 relation_edges 归并）
//   颜色 = 落地状态（全落地绿 / 部分落地橙 / 未落地玫瑰虚线）
//   知识卡不占节点——收编进考点，hover 弹出该考点的卡片清单。
// 布局全确定性（无力图、无随机），同数据永远同构图。

const W = 1160;
const H = 700;
const CX = W / 2;
const CY = H / 2 - 10;
const RC = 132; // 章节环半径
const RP = 292; // 考点环半径
const GAP_DEG = 1.6; // 章节扇区间隔（度）

/** 章节色板（kit 色系，固定顺序分配，保证同章节同色） */
const CHAPTER_COLORS = ['#3b82f6', '#8b5cf6', '#ec4899', '#f97316', '#22c55e', '#06b6d4', '#eab308', '#64748b'];

/** 关系边样式：先修/特化 实线，对比 虚线，等价 粗线 */
const RELATION_STYLES: Record<string, { color: string; width: number; dash?: string }> = {
  requires: { color: '#3b82f6', width: 1.4 },
  specializes: { color: '#3b82f6', width: 1.4 },
  contrasts: { color: '#8b5cf6', width: 1.2, dash: '5 4' },
  equivalent: { color: '#22c55e', width: 2.2 },
};

interface ChapterArc {
  key: string;
  title: string;
  weight: number;
  points: FrameworkExamPoint[];
  a0: number; // 扇区起始角（弧度）
  a1: number; // 扇区结束角
  color: string;
}

interface PointNode {
  point: FrameworkExamPoint;
  angle: number;
  x: number;
  y: number;
  chapter: ChapterArc;
  cardCount: number;
  groundedCount: number;
  r: number;
}

const polar = (cx: number, cy: number, r: number, angle: number) => ({
  x: cx + r * Math.cos(angle),
  y: cy + r * Math.sin(angle),
});

/** 扇区路径（环带：r0→r1，a0→a1） */
function sectorPath(cx: number, cy: number, r0: number, r1: number, a0: number, a1: number): string {
  const p0 = polar(cx, cy, r1, a0);
  const p1 = polar(cx, cy, r1, a1);
  const p2 = polar(cx, cy, r0, a1);
  const p3 = polar(cx, cy, r0, a0);
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return [
    `M ${p0.x.toFixed(1)} ${p0.y.toFixed(1)}`,
    `A ${r1} ${r1} 0 ${large} 1 ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`,
    `L ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`,
    `A ${r0} ${r0} 0 ${large} 0 ${p3.x.toFixed(1)} ${p3.y.toFixed(1)}`,
    'Z',
  ].join(' ');
}

/** 关系弧：两点间的二次贝塞尔，控制点拉向圆心产生弧感 */
function arcPath(a: PointNode, b: PointNode): string {
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const pull = 0.32;
  const c = { x: CX + (mid.x - CX) * pull, y: CY + (mid.y - CY) * pull };
  return `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} Q ${c.x.toFixed(1)} ${c.y.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
}

export const GraphView = memo(function GraphView(props: {
  examPoints: FrameworkExamPoint[];
  units: AssessmentUnit[];
  cardsDict: Record<string, KnowledgeCard>;
  filteredCardIds: Set<string>;
  onCardClick: (id: string) => void;
}) {
  const { examPoints, units, cardsDict, filteredCardIds, onCardClick } = props;
  const [hoverPoint, setHoverPoint] = useState<string | null>(null);
  // 浮层关闭宽限：鼠标从节点移向浮层时有 14px 空隙，不留宽限会当场卸载
  // （用户根本来不及把光标移上去点卡片）。离开后 150ms 内进入浮层即取消关闭。
  const closeTimer = useRef<number | null>(null);
  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setHoverPoint(null), 150);
  }, [cancelClose]);
  useEffect(() => cancelClose, [cancelClose]);

  // 卡片 → 考点（经单元归组）；统计仅算过滤后可见卡片
  const cardsByPoint = useMemo(() => {
    const unitToPoint = new Map<string, string>();
    const cardToUnit = new Map<string, AssessmentUnit>();
    units.forEach((u) => {
      if (u.exam_point_id) unitToPoint.set(u.unit_id, u.exam_point_id);
      u.card_ids.forEach((cid) => cardToUnit.set(cid, u));
    });
    const m = new Map<string, KnowledgeCard[]>();
    Object.values(cardsDict).forEach((c) => {
      if (!filteredCardIds.has(c.id)) return;
      const u = cardToUnit.get(c.id);
      const pid = (u && u.exam_point_id) || '';
      if (!pid) return;
      const arr = m.get(pid) ?? [];
      arr.push(c);
      m.set(pid, arr);
    });
    return m;
  }, [cardsDict, units, filteredCardIds]);

  // 章节扇区：角度 ∝ 章节权重和（无权重时等分）；考点在扇区内等距铺开
  const { chapters, pointNodes, arcs } = useMemo(() => {
    const byChapter = new Map<string, FrameworkExamPoint[]>();
    examPoints.forEach((p) => {
      const k = p.anchor_key || '未分章';
      const arr = byChapter.get(k) ?? [];
      arr.push(p);
      byChapter.set(k, arr);
    });

    const raw: Array<{ key: string; title: string; weight: number; points: FrameworkExamPoint[] }> = [];
    byChapter.forEach((points, key) => {
      const weight = points.reduce((s, p) => s + (Number(p.weight_value) || 0), 0);
      raw.push({ key, title: key, weight, points: [...points].sort((a, b) => (Number(b.weight_value) || 0) - (Number(a.weight_value) || 0)) });
    });
    raw.sort((a, b) => b.weight - a.weight || a.key.localeCompare(b.key));

    const totalWeight = raw.reduce((s, c) => s + c.weight, 0);
    const useWeight = totalWeight > 0;
    const gap = (GAP_DEG * Math.PI) / 180;
    const usable = raw.length > 1 ? Math.PI * 2 - gap * raw.length : Math.PI * 2;
    const spanOf = (c: (typeof raw)[number]) =>
      useWeight ? (c.weight / totalWeight) * usable : usable / raw.length;

    const chapterArcs: ChapterArc[] = [];
    const nodes: PointNode[] = [];
    let angle = -Math.PI / 2; // 12 点方向起笔

    raw.forEach((c, ci) => {
      const span = spanOf(c);
      const arc: ChapterArc = { ...c, a0: angle, a1: angle + span, color: CHAPTER_COLORS[ci % CHAPTER_COLORS.length] };
      chapterArcs.push(arc);

      const pts = c.points;
      pts.forEach((p, pi) => {
        // 扇区内等距，避开边缘留白
        const inner = span * 0.12;
        const t = pts.length === 1 ? 0.5 : pi / (pts.length - 1);
        const a = angle + inner + span * (1 - inner * 2 / span) * t;
        const { x, y } = polar(CX, CY, RP, a);
        const cards = cardsByPoint.get(p.id) ?? [];
        const grounded = cards.filter((c2) => c2.grounded).length;
        nodes.push({
          point: p,
          angle: a,
          x,
          y,
          chapter: arc,
          cardCount: cards.length,
          groundedCount: grounded,
          r: 5 + Math.min(7, cards.length * 0.35),
        });
      });
      angle += span + (raw.length > 1 ? gap : 0);
    });

    // 关系弧：卡片 relation_edges 归并到考点对，去重（每对保留首个关系类型）
    const nodeById = new Map(nodes.map((n) => [n.point.id, n]));
    const cardToPoint = new Map<string, string>();
    cardsByPoint.forEach((cards, pid) => cards.forEach((c) => cardToPoint.set(c.id, pid)));

    const pairRel = new Map<string, { a: PointNode; b: PointNode; kind: string }>();
    Object.values(cardsDict).forEach((c) => {
      const from = cardToPoint.get(c.id);
      if (!from || !Array.isArray(c.relation_edges)) return;
      c.relation_edges.forEach((edge) => {
        const obj = (edge && typeof edge === 'object' ? edge : {}) as { target?: string; relation?: string; kind?: string };
        const target = (obj.target || (typeof edge === 'string' ? edge : '')).trim();
        const to = cardToPoint.get(target);
        if (!to || to === from) return;
        const na = nodeById.get(from);
        const nb = nodeById.get(to);
        if (!na || !nb) return;
        const key = [from, to].sort().join('|');
        if (pairRel.has(key)) return;
        pairRel.set(key, { a: na, b: nb, kind: (obj.relation || obj.kind || 'requires').toLowerCase() });
      });
    });

    return { chapters: chapterArcs, pointNodes: nodes, arcs: [...pairRel.values()] };
  }, [examPoints, units, cardsByPoint, cardsDict]);

  const totalCards = useMemo(() => [...cardsByPoint.values()].reduce((s, a) => s + a.length, 0), [cardsByPoint]);
  const hovered = hoverPoint ? pointNodes.find((n) => n.point.id === hoverPoint) : null;
  const hoveredCards = hovered ? (cardsByPoint.get(hovered.point.id) ?? []) : [];

  if (examPoints.length === 0 && totalCards === 0) {
    return (
      <div style={{ padding: '48px 0', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        <Network size={40} style={{ margin: '0 auto 12px', opacity: 0.4 }} />
        <p style={{ fontSize: '0.875rem' }}>图谱暂无节点</p>
      </div>
    );
  }

  return (
    <div>
      {/* SVG 独立相对容器：浮层百分比锚定只映射画布，不受下方图例高度影响 */}
      <div style={{ position: 'relative' }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          style={{ width: '100%', maxHeight: 760, display: 'block', background: 'transparent' }}
          role="img"
          aria-label="知识图谱：章节权重扇区与考点关系"
        >
        <defs>
          <marker id="rg-arrow" markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto">
            <path d="M0,0 L6,2.5 L0,5 Z" fill="#3b82f6" />
          </marker>
        </defs>

        {/* 章节扇区（权重角度化） */}
        {chapters.map((c) => (
          <path key={'sec-' + c.key} d={sectorPath(CX, CY, RC - 26, RC + 26, c.a0, c.a1)} fill={c.color} opacity={0.1} />
        ))}

        {/* 章节标签：沿角平分线外置，按方位自动锚点 */}
        {chapters.map((c) => {
          const mid = (c.a0 + c.a1) / 2;
          const p = polar(CX, CY, RC + 44, mid);
          const anchor = Math.cos(mid) > 0.25 ? 'start' : Math.cos(mid) < -0.25 ? 'end' : 'middle';
          return (
            <g key={'lab-' + c.key}>
              <text x={p.x} y={p.y - 4} textAnchor={anchor} fontSize="12.5" fontWeight="600" fill={c.color}>
                {truncate(c.title, 10)}
              </text>
              <text x={p.x} y={p.y + 12} textAnchor={anchor} fontSize="10.5" fill="#a1a1aa">
                {c.points.length} 考点 · {formatPercent(c.weight)}
              </text>
            </g>
          );
        })}

        {/* 中心摘要 */}
        <text x={CX} y={CY - 8} textAnchor="middle" fontSize="15" fontWeight="600" fill="#18181b">
          知识图谱
        </text>
        <text x={CX} y={CY + 14} textAnchor="middle" fontSize="11.5" fill="#8a8a8a">
          {chapters.length} 章 · {pointNodes.length} 考点 · {totalCards} 卡
        </text>
        <text x={CX} y={CY + 32} textAnchor="middle" fontSize="10.5" fill="#a1a1aa">
          扇区角度 = 章节权重 · 弧线 = 考点关系
        </text>

        {/* 关系弧（先画线，节点压上层） */}
        {arcs.map(({ a, b, kind }) => {
          const st = RELATION_STYLES[kind] ?? RELATION_STYLES.requires;
          const dim = hoverPoint && a.point.id !== hoverPoint && b.point.id !== hoverPoint;
          return (
            <path
              key={`arc-${a.point.id}-${b.point.id}-${kind}`}
              d={arcPath(a, b)}
              fill="none"
              stroke={st.color}
              strokeWidth={st.width}
              strokeDasharray={st.dash}
              opacity={dim ? 0.06 : 0.5}
              markerEnd={kind === 'requires' ? 'url(#rg-arrow)' : undefined}
              style={{ transition: 'opacity .2s' }}
            />
          );
        })}

        {/* 考点节点：大小 = 卡片数，颜色 = 落地状态 */}
        {pointNodes.map((n) => {
          const allGrounded = n.cardCount > 0 && n.groundedCount === n.cardCount;
          const someGrounded = n.groundedCount > 0 && n.groundedCount < n.cardCount;
          const hov = hoverPoint === n.point.id;
          const dim = hoverPoint && !hov;
          return (
            <g
              key={n.point.id}
              opacity={dim ? 0.25 : 1}
              onPointerEnter={() => { cancelClose(); setHoverPoint(n.point.id); }}
              onPointerLeave={scheduleClose}
              style={{ cursor: 'pointer', transition: 'opacity .2s' }}
            >
              <circle cx={n.x} cy={n.y} r={n.r + (hov ? 4 : 0)} fill={n.chapter.color} opacity={0.16} />
              <circle
                cx={n.x}
                cy={n.y}
                r={n.r + (hov ? 2 : 0)}
                fill={allGrounded ? n.chapter.color : someGrounded ? '#ffffff' : '#ffffff'}
                fillOpacity={allGrounded ? 0.92 : 1}
                stroke={allGrounded ? n.chapter.color : someGrounded ? '#ea580c' : '#f43f5e'}
                strokeWidth={allGrounded ? 0 : someGrounded ? 1.4 : 1.4}
                strokeDasharray={allGrounded ? undefined : '3 2'}
                style={{ transition: 'r .15s' }}
              />
              {hov && (
                <text x={n.x} y={n.y - n.r - 8} textAnchor="middle" fontSize="10.5" fontWeight="600" fill="#18181b"
                  style={{ paintOrder: 'stroke', stroke: '#ffffff', strokeWidth: 3, strokeLinejoin: 'round', pointerEvents: 'none' }}>
                  {truncate(n.point.title || n.point.code, 12)}
                </text>
              )}
            </g>
          );
        })}
        </svg>

        {/* 考点详情浮层：该考点下的卡片清单（点击卡片进详情抽屉）。
            上下半屏翻转 placement（顶部节点浮层向下，避免被卡片上缘裁剪），
            左右边缘 15% 区域内改边缘锚定（避免横向裁剪）。 */}
        {hovered && (() => {
          const below = hovered.y < H * 0.45;
          const pct = (hovered.x / W) * 100;
          const edge = pct < 18 ? 'left' : pct > 82 ? 'right' : 'center';
          const tx = edge === 'left' ? '0%' : edge === 'right' ? '-100%' : '-50%';
          const ty = below ? '14px' : 'calc(-100% - 14px)';
          return (
            <div
              onPointerEnter={cancelClose}
              onPointerLeave={scheduleClose}
              style={{
                position: 'absolute',
                left: `${pct}%`,
                top: `${(hovered.y / H) * 100}%`,
                transform: `translate(${tx}, ${ty})`,
                width: 260,
                background: 'var(--surface-solid)',
                border: '1px solid var(--line)',
                borderRadius: 'var(--radius-md)',
                boxShadow: 'var(--shadow-3)',
                padding: '12px 14px',
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
                zIndex: 5,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: hovered.chapter.color, flexShrink: 0 }} />
                <strong style={{ fontSize: '0.85rem', lineHeight: 1.35 }}>{hovered.point.title || hovered.point.code}</strong>
              </div>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>
                {hovered.point.code} · {truncate(hovered.chapter.title, 12)} · 权重 {formatPercent(hovered.point.weight_value)} · {hovered.cardCount} 卡（已落地 {hovered.groundedCount}）
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 180, overflowY: 'auto' }}>
                {hoveredCards.length === 0 && (
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>当前过滤下无可见卡片</p>
                )}
                {hoveredCards.slice(0, 20).map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => onCardClick(c.id)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px',
                      border: 'none', borderRadius: 'var(--radius-sm)', background: 'var(--fill)',
                      cursor: 'pointer', textAlign: 'left', fontSize: '0.78rem',
                    }}
                  >
                    <span style={{ width: 6, height: 6, borderRadius: '50%', flexShrink: 0, background: c.grounded ? 'var(--success)' : 'var(--error)' }} />
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.name}</span>
                  </button>
                ))}
              </div>
            </div>
          );
        })()}
      </div>

      {/* 图例 */}
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', padding: '10px 4px 0', fontSize: '0.72rem', color: 'var(--text-secondary)' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#3b82f6' }} /> 全落地考点
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', border: '1.5px solid #ea580c', background: '#ffffff' }} /> 部分落地
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', border: '1.5px dashed #f43f5e', background: '#ffffff' }} /> 未落地
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 18, height: 0, borderTop: '1.5px solid #3b82f6' }} /> 先修 / 特化
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 18, height: 0, borderTop: '1.5px dashed #8b5cf6' }} /> 对比
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 18, height: 0, borderTop: '2.5px solid #22c55e' }} /> 等价
        </span>
      </div>
    </div>
  );
});
