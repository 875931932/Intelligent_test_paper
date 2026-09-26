import { useState, useEffect, useCallback, useMemo, useRef, memo } from 'react';
import { Network } from 'lucide-react';
import type { FrameworkExamPoint, AssessmentUnit, KnowledgeCard } from '@/types/api';
import { truncate } from './knowledgeShared';

// ─── Graph View（星空图谱）───

// 画布比旧版（960×640）扩大十余倍：旧版把 62 考点钉死在 282px 外环、卡片绕单元
// 小环，500+ 节点全糊成一个圆环；星空版摊开到整幅深空画布，靠 fitView 自适应取景。
const GRAPH_W = 4000;
const GRAPH_H = 3000;
const GRAPH_CX = GRAPH_W / 2;
const GRAPH_CY = GRAPH_H / 2;
// 费马螺线间距（密度均匀不重叠）：星空的疏朗度由这三个数控制
// 间距三件套（2026-09-25 反馈「挤成一团」两轮放宽）：
// 考点 NN ≈ 1.7×SECT_SP；单元首环 0.71×UNIT_SP 让开「考点核+标题带」；
// 卡片环受 CARD_RING_MAX 封顶——大单元（17 卡）不封顶时外环 190 会横穿
// 邻座考点核（NN≈146），封顶 96 后最远触达 0.71×84+96=156 < NN-28。
const SECT_SP = 118; // 星座内考点（86 时大单元卡环仍横穿邻座考点）
const UNIT_SP = 84; // 单元绕考点（46 压核上、76 首环标题仍贴考点标题，84 才让开）
const CARD_SP = 46; // 卡片绕单元（32 时首环贴着单元核）
const CARD_RING_MAX = 96; // 卡片螺线半径上限（封顶后按黄金角继续铺开，不会堆点）
// 缩放窗口：上限 12× 保证看清标签（画布 4000 宽缩进 ~1100px 容器后基础比例
// 约 0.28，12× 时考点标签屏显约 30px）；下限允许退回全景。
const ZOOM_MIN = 0.3;
const ZOOM_MAX = 12;
// 深空提亮色板（旧色板在深底上偏暗）
const GRAPH_PALETTE = ['#4da3ff', '#22d3ee', '#34d399', '#fbbf24', '#c084fc', '#fb7185', '#f87171', '#2dd4bf'];

function hashStr(s: string | null | undefined): number {
  if (!s) return 0;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

interface GraphNode {
  key: string;
  kind: 'point' | 'unit' | 'card';
  x: number;
  y: number;
  angle: number;
  r: number;
  label: string;
  /** 完整可读标题（hover 时展示；label 是截断短名） */
  full?: string;
  sub: string;
  color: string;
  grounded: boolean;
  cardId?: string;
  unitId?: string;
}

interface GraphEdge {
  id: string;
  from: string;
  to: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  kind: string;
}

export const GraphView = memo(function GraphView(props: {
  examPoints: FrameworkExamPoint[];
  units: AssessmentUnit[];
  cards: KnowledgeCard[];
  onCardClick: (id: string) => void;
}) {
  const { examPoints, units, cards, onCardClick } = props;
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [zoom, setZoom] = useState(0.8);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [dragging, setDragging] = useState<{ key: string; dx: number; dy: number } | null>(null);
  const [panning, setPanning] = useState(false);
  const [draggedPos, setDraggedPos] = useState<Record<string, { x: number; y: number }>>({});
  const zoomRef = useRef(zoom);
  const panRef = useRef(pan);
  const panStart = useRef({ cx: 0, cy: 0, px: 0, py: 0 });
  const movedRef = useRef(false);
  zoomRef.current = zoom;
  panRef.current = pan;

  const unitOfCard = useMemo(() => {
    const m = new Map<string, AssessmentUnit>();
    units.forEach((u) => u.card_ids.forEach((cid) => m.set(cid, u)));
    return m;
  }, [units]);

  // ── 星空布局：章节(anchor_key)为「星座」，黄金角费马螺线逐层错落散布 ──
  // 旧版把 62 考点钉死在 282px 外环、卡片绕单元小环，500+ 节点糊成一个圆环；
  // 新版按章节分星座 → 星座内考点螺线 → 单元绕考点 → 卡片绕单元，sqrt 半径
  // 保证密度均匀不重叠，星团自然疏开。
  const layout = useMemo(() => {
    const spiral = (i: number, sp: number) => ({ rad: sp * Math.sqrt(i + 0.5), ang: i * 2.39996323 });

    const sections = new Map<string, FrameworkExamPoint[]>();
    examPoints.forEach((p) => {
      const k = p.anchor_key || '未分章';
      const arr = sections.get(k);
      if (arr) arr.push(p); else sections.set(k, [p]);
    });
    const sectKeys = [...sections.keys()];

    const unitsOfPoint = new Map<string, AssessmentUnit[]>();
    units.forEach((u) => {
      if (!u.exam_point_id) return;
      const arr = unitsOfPoint.get(u.exam_point_id);
      if (arr) arr.push(u); else unitsOfPoint.set(u.exam_point_id, [u]);
    });

    // 星座容量半径 = 考点螺线外沿 + 最大单元数外扩 + 最大卡数外扩
    const sectRadius = (pts: FrameworkExamPoint[]) => {
      let uMax = 1;
      let cMax = 1;
      pts.forEach((p) => {
        const us = unitsOfPoint.get(p.id) || [];
        uMax = Math.max(uMax, us.length);
        us.forEach((u) => { cMax = Math.max(cMax, u.card_ids?.length || 1); });
      });
      return SECT_SP * Math.sqrt(pts.length) + UNIT_SP * Math.sqrt(uMax) + CARD_SP * Math.sqrt(cMax) + 96;
    };
    const globalRS = Math.max(140, ...sectKeys.map((k) => sectRadius(sections.get(k)!)));

    // 星座中心：黄金角螺线摊开（i=0 居中）。间距系数 1.75×半径保证相邻星座
    // 不相切（1.5 时纵向对会压到 0.66 缩放后 < 2RS，挤成一团）；y 压扁 0.78。
    const sectCenter = new Map<string, { x: number; y: number }>();
    sectKeys.forEach((k, i) => {
      const rad = globalRS * 1.75 * Math.sqrt(i);
      const ang = i * 2.39996323;
      sectCenter.set(k, { x: GRAPH_CX + rad * Math.cos(ang), y: GRAPH_CY + rad * Math.sin(ang) * 0.78 });
    });

    const pointNodes: GraphNode[] = [];
    const pointPos = new Map<string, { x: number; y: number }>();
    sectKeys.forEach((sk) => {
      const center = sectCenter.get(sk)!;
      sections.get(sk)!.forEach((p, pi) => {
        const { rad, ang } = spiral(pi, SECT_SP);
        const x = center.x + rad * Math.cos(ang);
        const y = center.y + rad * Math.sin(ang);
        pointPos.set(p.id, { x, y });
        pointNodes.push({
          key: 'p-' + p.id, kind: 'point',
          x, y, angle: ang, r: 21,
          // 节点文字要「看得懂」：显示考点标题而非 CH4-EP01 序号（2026-09-25 反馈）
          label: truncate(p.title || p.code, 6),
          full: p.title || p.code,
          sub: '',
          color: '#4da3ff', grounded: true,
        });
      });
    });
    const unitNodes: GraphNode[] = [];
    const unitPos = new Map<string, { x: number; y: number }>();
    units.forEach((u) => {
      const base = pointPos.get(u.exam_point_id);
      const siblings = unitsOfPoint.get(u.exam_point_id) || [u];
      const idx = Math.max(0, siblings.findIndex((it) => it.unit_id === u.unit_id));
      const { rad, ang } = spiral(idx, UNIT_SP);
      const x = (base?.x ?? GRAPH_CX) + rad * Math.cos(ang);
      const y = (base?.y ?? GRAPH_CY) + rad * Math.sin(ang);
      unitPos.set(u.unit_id, { x, y });
      unitNodes.push({
        key: 'u-' + u.unit_id, kind: 'unit',
        x, y, angle: ang, r: 16,
        // 同考点：标题优先于 code 序号
        label: truncate(u.title || u.code, 6),
        full: u.title || u.code,
        sub: String(u.card_ids?.length || 0) + '卡',
        color: '#c084fc', grounded: true, unitId: u.unit_id,
      });
    });
    const cardsByUnit = new Map<string, KnowledgeCard[]>();
    const orphans: KnowledgeCard[] = [];
    cards.forEach((c) => {
      const u = unitOfCard.get(c.id);
      if (u) {
        const arr = cardsByUnit.get(u.unit_id) || [];
        arr.push(c);
        cardsByUnit.set(u.unit_id, arr);
      } else {
        orphans.push(c);
      }
    });

    const cardColor = (c: KnowledgeCard, salt: string) =>
      c.concept_cluster
        ? GRAPH_PALETTE[hashStr(c.concept_cluster) % GRAPH_PALETTE.length]
        : GRAPH_PALETTE[hashStr(salt) % GRAPH_PALETTE.length];

    const cardNodes: GraphNode[] = [];
    cardsByUnit.forEach((list, uid) => {
      const un = unitPos.get(uid);
      if (!un) return;
      list.forEach((c, i) => {
        const { rad, ang } = spiral(i, CARD_SP);
        const rr = Math.min(rad, CARD_RING_MAX);
        cardNodes.push({
          key: 'c-' + c.id, kind: 'card',
          x: un.x + rr * Math.cos(ang),
          y: un.y + rr * Math.sin(ang),
          angle: ang,
          r: 4.5 + (c.importance || 1) * 2.2,
          label: String(c.name || ''), sub: '',
          color: cardColor(c, uid),
          grounded: c.grounded, cardId: c.id,
        });
      });
    });
    // 未归属单元的卡片作为无主星尘散落在中心
    orphans.forEach((c, i) => {
      const { rad, ang } = spiral(i, CARD_SP + 8);
      const rr = Math.min(rad, CARD_RING_MAX);
      cardNodes.push({
        key: 'c-' + c.id, kind: 'card',
        x: GRAPH_CX + rr * Math.cos(ang),
        y: GRAPH_CY + rr * Math.sin(ang),
        angle: ang,
        r: 4.5 + (c.importance || 1) * 2.2,
        label: c.name, sub: '',
        color: cardColor(c, c.id),
        grounded: c.grounded, cardId: c.id,
      });
    });
    return { pointNodes, unitNodes, cardNodes };
  }, [examPoints, units, cards, unitOfCard]);

  // 背景星尘：确定性伪随机（LCG），三组错相闪烁。
  // 覆盖范围外扩 ±3000：SECT_SP 放宽后节点框远超 4000×3000 画布，星尘须
  // 同步铺到画布外，否则 fitView 取景时画缘出现「无星空带」。
  const bgStars = useMemo(() => {
    let s = 987654321;
    const rnd = () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
    return Array.from({ length: 1400 }, () => {
      const bucket = rnd();
      const warm = rnd();
      return {
        x: rnd() * (GRAPH_W + 6000) - 3000,
        y: rnd() * (GRAPH_H + 6000) - 3000,
        r: 0.5 + rnd() * 1.7,
        big: rnd() < 0.1,
        // 三档静态透明度分层代替旧版循环闪烁：1400 颗常驻星的无限动画是白耗的 GPU 负担
        opacity: bucket < 0.34 ? 0.35 : bucket < 0.67 ? 0.6 : 0.9,
        hue: warm < 0.22 ? '#9ecbff' : warm < 0.4 ? '#ffe9c4' : '#ffffff',
      };
    });
  }, []);

  // 从属边（卡→单元→考点）+ 卡片关系边（relation_edges）
  const { edges, relEdges } = useMemo(() => {
    const out: GraphEdge[] = [];
    const rel: GraphEdge[] = [];
    const nodeByKey = new Map<string, GraphNode>();
    [...layout.cardNodes, ...layout.unitNodes, ...layout.pointNodes].forEach((n) => nodeByKey.set(n.key, n));
    const unitByCardKey = new Map<string, GraphNode>();
    layout.cardNodes.forEach((n) => {
      const u = unitOfCard.get(n.cardId || '');
      if (u) {
        const un = nodeByKey.get('u-' + u.unit_id);
        if (un) unitByCardKey.set(n.key, un);
      }
    });
    layout.cardNodes.forEach((n) => {
      const un = unitByCardKey.get(n.key);
      if (un) out.push({ id: n.key + '->u-' + un.key, from: n.key, to: un.key, x1: n.x, y1: n.y, x2: un.x, y2: un.y, kind: 'card-unit' });
    });
    layout.unitNodes.forEach((n) => {
      const u = units.find((it) => it.unit_id === n.unitId);
      if (u && u.exam_point_id) {
        const pn = nodeByKey.get('p-' + u.exam_point_id);
        if (pn) out.push({ id: n.key + '->p-' + pn.key, from: n.key, to: pn.key, x1: n.x, y1: n.y, x2: pn.x, y2: pn.y, kind: 'unit-point' });
      }
    });
    const cardNodeByCardId = new Map(layout.cardNodes.map((n) => [n.cardId, n]));
    cards.forEach((c) => {
      const arr = c.relation_edges;
      if (!Array.isArray(arr)) return;
      const src = cardNodeByCardId.get(c.id);
      if (!src) return;
      arr.forEach((edge, idx) => {
        const obj = (edge && typeof edge === 'object' ? edge : {}) as { target?: string; relation?: string; kind?: string };
        const target = (obj.target || (typeof edge === 'string' ? edge : '') || '').trim();
        const relKind = (obj.relation || obj.kind || '').toLowerCase();
        const tgt = cardNodeByCardId.get(target);
        if (src && tgt) {
          rel.push({ id: 'r-' + c.id + '-' + target + '-' + idx, from: src.key, to: tgt.key, x1: src.x, y1: src.y, x2: tgt.x, y2: tgt.y, kind: relKind });
        }
      });
    });
    return { edges: out, relEdges: rel };
  }, [layout, units, cards, unitOfCard]);

  // hover 邻接表：高亮节点与其直接关联的节点/边
  const adjacency = useMemo(() => {
    const adj = new Map<string, Set<string>>();
    const add = (a: string, b: string) => {
      if (!adj.has(a)) adj.set(a, new Set());
      if (!adj.has(b)) adj.set(b, new Set());
      adj.get(a)!.add(b);
      adj.get(b)!.add(a);
    };
    [...edges, ...relEdges].forEach((e) => add(e.from, e.to));
    return adj;
  }, [edges, relEdges]);

  const activeSet = useMemo(() => {
    if (!hoverKey) return null;
    const s = adjacency.get(hoverKey) || new Set();
    return new Set([hoverKey, ...s]);
  }, [hoverKey, adjacency]);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      // 光标 → viewBox 走 CTM 反变换：rect 线性映射忽略了 preserveAspectRatio
      // 的 letterbox（元素 4:3 之外的左右留白），会横向偏最多几十 px。
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      const v = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
      const factor = e.deltaY > 0 ? 0.88 : 1.12;
      const z0 = zoomRef.current;
      const z1 = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z0 * factor));
      const p0 = panRef.current;
      const k = z1 / z0;
      // 锚点公式须与 transform 的「绕画布中心缩放」一致：
      //   v = (n−c)·z + c + pan  ⟹  pan₁ = (m−c) − (m−c−pan₀)·k
      // 旧式 m − (m−pan₀)·k 是绕原点缩放的写法，每步多漂 c·(1−k)
      // （约 −240/−180 viewBox 单位），连续滚动会把视图斜着带出内容区。
      const dx = v.x - GRAPH_CX;
      const dy = v.y - GRAPH_CY;
      setPan({ x: dx - (dx - p0.x) * k, y: dy - (dy - p0.y) * k });
      setZoom(z1);
    };
    svg.addEventListener('wheel', onWheel, { passive: false });
    return () => svg.removeEventListener('wheel', onWheel);
  }, []);

  // 自适应取景：把全部节点框进画布并居中（星空画布远大于内容，靠它开局给全景）
  const fitView = useCallback(() => {
    const all = [...layout.pointNodes, ...layout.unitNodes, ...layout.cardNodes];
    if (all.length === 0) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    all.forEach((n) => {
      minX = Math.min(minX, n.x - n.r); maxX = Math.max(maxX, n.x + n.r);
      minY = Math.min(minY, n.y - n.r); maxY = Math.max(maxY, n.y + n.r);
    });
    const pad = 140;
    const z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.min(
      GRAPH_W / (maxX - minX + pad * 2),
      GRAPH_H / (maxY - minY + pad * 2),
    )));
    setZoom(z);
    setPan({
      x: -z * ((minX + maxX) / 2 - GRAPH_CX),
      y: -z * ((minY + maxY) / 2 - GRAPH_CY),
    });
    setDraggedPos({});
  }, [layout]);

  // 数据加载/更新时自动取景一次（不干扰用户随后的拖拽/缩放）
  useEffect(() => { fitView(); }, [fitView]);

  const toCanvas = useCallback((clientX: number, clientY: number) => {
    // CTM 反变换拿精确 viewBox 坐标（含 letterbox 校正），再逆掉内层
    // 缩放平移得到画布坐标——与 onWheel 同一套映射，拖拽/缩放才一致。
    const ctm = svgRef.current?.getScreenCTM();
    if (!ctm) return { x: GRAPH_CX, y: GRAPH_CY };
    const v = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
    const z = zoomRef.current;
    const p = panRef.current;
    return {
      x: (v.x - (GRAPH_CX + p.x)) / z + GRAPH_CX,
      y: (v.y - (GRAPH_CY + p.y)) / z + GRAPH_CY,
    };
  }, []);

  const nodePos = useCallback((n: GraphNode) => draggedPos[n.key] || { x: n.x, y: n.y }, [draggedPos]);

  const onNodePointerDown = (e: React.PointerEvent, n: GraphNode) => {
    e.stopPropagation();
    if (e.button !== 0) return;
    movedRef.current = false;
    const c = toCanvas(e.clientX, e.clientY);
    const p = nodePos(n);
    setDragging({ key: n.key, dx: c.x - p.x, dy: c.y - p.y });
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onSvgPointerMove = (e: React.PointerEvent) => {
    if (dragging) {
      const c = toCanvas(e.clientX, e.clientY);
      const p = { x: c.x - dragging.dx, y: c.y - dragging.dy };
      const prev = draggedPos[dragging.key] || { x: 0, y: 0 };
      if (Math.abs(p.x - prev.x) + Math.abs(p.y - prev.y) > 2) movedRef.current = true;
      setDraggedPos((prev2) => ({ ...prev2, [dragging.key]: p }));
    } else if (panning) {
      // 屏幕像素差换算成 viewBox 单位（÷CTM 缩放），内容才严格跟手；
      // 旧写法把 px 直接当 viewBox 单位，恒慢 ~4× 且缩放级别越高越钝。
      const s = svgRef.current?.getScreenCTM()?.a || 1;
      setPan({
        x: panStart.current.px + (e.clientX - panStart.current.cx) / s,
        y: panStart.current.py + (e.clientY - panStart.current.cy) / s,
      });
    }
  };

  const onSvgPointerUp = (e: React.PointerEvent) => {
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    setDragging(null);
    setPanning(false);
  };

  const onBgPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    movedRef.current = false;
    panStart.current = { cx: e.clientX, cy: e.clientY, px: pan.x, py: pan.y };
    setPanning(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const transform = `translate(${GRAPH_CX + pan.x} ${GRAPH_CY + pan.y}) scale(${zoom}) translate(${-GRAPH_CX} ${-GRAPH_CY})`;

  const allNodes = useMemo(
    () => [...layout.pointNodes, ...layout.unitNodes, ...layout.cardNodes],
    [layout]
  );
  void allNodes;

  // 深空底上的「星座连线」：从属边走半透明星光白，关系边保留彩色但提亮
  const edgeStyle = (kind: string) => {
    if (kind === 'specializes' || kind === 'requires') return { stroke: '#4da3ff', width: 1.6, dash: undefined, opacity: 0.75, marker: true };
    if (kind === 'contrasts') return { stroke: '#c084fc', width: 1.3, dash: '5 4', opacity: 0.7, marker: false };
    if (kind === 'equivalent') return { stroke: '#34d399', width: 2.6, dash: undefined, opacity: 0.7, marker: false };
    if (kind === 'card-unit') return { stroke: 'rgba(158,191,255,0.13)', width: 1, dash: undefined, opacity: 1, marker: false };
    if (kind === 'unit-point') return { stroke: 'rgba(192,132,252,0.20)', width: 1.2, dash: undefined, opacity: 1, marker: false };
    return { stroke: 'rgba(255,255,255,0.25)', width: 1.2, dash: undefined, opacity: 0.7, marker: false };
  };

  const isDimmed = (key: string) => activeSet !== null && !activeSet.has(key);

  if (cards.length === 0 && units.length === 0 && examPoints.length === 0) {
    return (
      <div style={{ padding: '40px 0', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        <Network size={40} style={{ margin: '0 auto 12px', opacity: 0.4 }} />
        <p style={{ fontSize: '0.875rem' }}>图谱暂无节点</p>
      </div>
    );
  }

  return (
    <div>
      <div style={{ position: 'relative', userSelect: 'none' }}>
        <svg
          ref={svgRef}
          viewBox={`0 0 ${GRAPH_W} ${GRAPH_H}`}
          style={{
            width: '100%', maxHeight: '760px',
            background: 'radial-gradient(ellipse at 50% 38%, #0b1530 0%, #070d1f 55%, #04070f 100%)',
            cursor: panning ? 'grabbing' : 'grab', touchAction: 'none',
          }}
          onPointerMove={onSvgPointerMove}
          onPointerUp={onSvgPointerUp}
          onPointerCancel={onSvgPointerUp}
          onPointerDown={onBgPointerDown}
        >
          <defs>
            <marker id="gh-arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <path d="M0,0 L8,3 L0,6 Z" fill="#4da3ff" />
            </marker>
            {/* 节点光晕：径向渐变模拟发光，避免逐节点 SVG 滤镜的性能开销 */}
            {GRAPH_PALETTE.map((c, i) => (
              <radialGradient key={'g' + i} id={'glow-' + i}>
                <stop offset="0%" stopColor={c} stopOpacity="0.75" />
                <stop offset="42%" stopColor={c} stopOpacity="0.22" />
                <stop offset="100%" stopColor={c} stopOpacity="0" />
              </radialGradient>
            ))}
            <radialGradient id="glow-pt">
              <stop offset="0%" stopColor="#8ec9ff" stopOpacity="0.85" />
              <stop offset="45%" stopColor="#4da3ff" stopOpacity="0.25" />
              <stop offset="100%" stopColor="#4da3ff" stopOpacity="0" />
            </radialGradient>
            <radialGradient id="glow-un">
              <stop offset="0%" stopColor="#d8b4fe" stopOpacity="0.8" />
              <stop offset="45%" stopColor="#c084fc" stopOpacity="0.22" />
              <stop offset="100%" stopColor="#c084fc" stopOpacity="0" />
            </radialGradient>
            <radialGradient id="glow-star">
              <stop offset="0%" stopColor="#ffffff" stopOpacity="0.9" />
              <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
            </radialGradient>
          </defs>
          <g transform={transform}>
            {/* 背景星尘：坐标在画布系随缩放平移，三档静态透明度分层 */}
            <g>
              {bgStars.map((st, i) => (
                <g key={'st' + i} opacity={st.opacity}>
                  {st.big && <circle cx={st.x} cy={st.y} r={st.r * 5} fill="url(#glow-star)" opacity={0.5} />}
                  <circle cx={st.x} cy={st.y} r={st.r} fill={st.hue} />
                </g>
              ))}
            </g>
            {/* 边 */}
            <g>
              {[...edges, ...relEdges].map((e) => {
                const dim = activeSet !== null && !(activeSet.has(e.from) && activeSet.has(e.to));
                const st = edgeStyle(e.kind);
                return (
                  <line
                    key={e.id}
                    x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2}
                    stroke={st.stroke}
                    strokeWidth={st.width}
                    strokeDasharray={st.dash}
                    opacity={dim ? 0.06 : st.opacity}
                    markerEnd={st.marker ? 'url(#gh-arrowhead)' : undefined}
                  />
                );
              })}
            </g>

            {/* 考点 = 恒星：星芒 + 蓝白光晕 + 白核 */}
            {layout.pointNodes.map((n) => {
              const p = nodePos(n);
              const dim = isDimmed(n.key);
              const hov = hoverKey === n.key;
              return (
                <g
                  key={n.key}
                  opacity={dim ? 0.12 : 1}
                  onPointerEnter={() => setHoverKey(n.key)}
                  onPointerLeave={() => setHoverKey(null)}
                  onPointerDown={(e) => onNodePointerDown(e, n)}
                  style={{ cursor: 'grab' }}
                >
                  <line x1={p.x - n.r * 2.4} y1={p.y} x2={p.x + n.r * 2.4} y2={p.y} stroke="rgba(255,255,255,0.45)" strokeWidth={1} style={{ pointerEvents: 'none' }} />
                  <line x1={p.x} y1={p.y - n.r * 2.4} x2={p.x} y2={p.y + n.r * 2.4} stroke="rgba(255,255,255,0.45)" strokeWidth={1} style={{ pointerEvents: 'none' }} />
                  <circle cx={p.x} cy={p.y} r={hov ? n.r * 3.1 : n.r * 2.6} fill="url(#glow-pt)" style={{ transition: 'r 0.15s' }} />
                  <circle cx={p.x} cy={p.y} r={n.r + (hov ? 3 : 0)} fill="#f2f8ff" stroke="#4da3ff" strokeWidth={1.6} style={{ transition: 'r 0.15s' }} />
                  {/* 标题挂核外深底：浅字+深描边与深空天然高对比（旧版序号压在
                      同色系光晕上糊成一片）；hover 展开全名；核内留白做纯恒星。 */}
                  <text x={p.x} y={p.y + n.r + (hov ? 6 : 0) + 12} textAnchor="middle" fontSize={hov ? 10.5 : 9} fontWeight="600" fill="#eaf2ff"
                    style={{ paintOrder: 'stroke', stroke: '#050914', strokeWidth: 3, strokeLinejoin: 'round', pointerEvents: 'none' }}>
                    {hov ? n.full : n.label}
                  </text>
                </g>
              );
            })}

            {/* 单元 = 卫星星：紫晕 + 淡紫白核 */}
            {layout.unitNodes.map((n) => {
              const p = nodePos(n);
              const dim = isDimmed(n.key);
              const hov = hoverKey === n.key;
              return (
                <g
                  key={n.key}
                  opacity={dim ? 0.12 : 1}
                  onPointerEnter={() => setHoverKey(n.key)}
                  onPointerLeave={() => setHoverKey(null)}
                  onPointerDown={(e) => onNodePointerDown(e, n)}
                  style={{ cursor: 'grab' }}
                >
                  <circle cx={p.x} cy={p.y} r={hov ? n.r * 2.9 : n.r * 2.4} fill="url(#glow-un)" style={{ transition: 'r 0.15s' }} />
                  <circle cx={p.x} cy={p.y} r={n.r + (hov ? 2.5 : 0)} fill="#f6efff" stroke="#c084fc" strokeWidth={1.4} style={{ transition: 'r 0.15s' }} />
                  {/* 核内只留「N卡」计数；标题挂核外深底（浅字深描边高对比），
                      hover 展开全名。 */}
                  <text x={p.x} y={p.y + 2.5} textAnchor="middle" fontSize="6.5" fontWeight="600" fill="#5b2196"
                    style={{ paintOrder: 'stroke', stroke: '#ffffff', strokeWidth: 1.4, strokeLinejoin: 'round', pointerEvents: 'none' }}>{n.sub}</text>
                  <text x={p.x} y={p.y + n.r + (hov ? 5 : 0) + 11} textAnchor="middle" fontSize={hov ? 10 : 8.5} fontWeight="600" fill="#f3e8ff"
                    style={{ paintOrder: 'stroke', stroke: '#050914', strokeWidth: 3, strokeLinejoin: 'round', pointerEvents: 'none' }}>
                    {hov ? n.full : n.label}
                  </text>
                </g>
              );
            })}

            {/* 卡片 = 星尘：色晕 + 核；默认不挂标签（503 个标签就是旧版糊成团的元凶），hover 才显名 */}
            {layout.cardNodes.map((n) => {
              const p = nodePos(n);
              const dim = isDimmed(n.key);
              const hovered = hoverKey === n.key;
              const gi = GRAPH_PALETTE.indexOf(n.color);
              return (
                <g
                  key={n.key}
                  opacity={dim ? 0.1 : 1}
                  onPointerEnter={() => setHoverKey(n.key)}
                  onPointerLeave={() => setHoverKey(null)}
                  onPointerDown={(e) => onNodePointerDown(e, n)}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (movedRef.current) { movedRef.current = false; return; }
                    if (n.cardId) onCardClick(n.cardId);
                  }}
                  style={{ cursor: 'pointer' }}
                >
                  <circle
                    cx={p.x} cy={p.y}
                    r={hovered ? n.r * 3.4 : n.r * 2.7}
                    fill={`url(#glow-${gi < 0 ? 0 : gi})`}
                    opacity={n.grounded ? 0.95 : 0.4}
                    style={{ transition: 'r 0.15s' }}
                  />
                  <circle
                    cx={p.x} cy={p.y}
                    r={n.r + (hovered ? 3 : 0)}
                    fill={n.color}
                    fillOpacity={n.grounded ? 0.95 : 0.35}
                    stroke={n.grounded ? 'rgba(255,255,255,0.8)' : '#ff5c5c'}
                    strokeWidth={n.grounded ? 0.8 : 1.6}
                    strokeDasharray={n.grounded ? undefined : '3 2'}
                    style={{ transition: 'r 0.15s, fill-opacity 0.15s' }}
                  />
                  {hovered && (
                    <text x={p.x} y={p.y - n.r - 8} textAnchor="middle" fontSize="9.5" fontWeight="600" fill="#eaf2ff"
                      style={{ paintOrder: 'stroke', stroke: '#050914', strokeWidth: 3.5, strokeLinejoin: 'round', pointerEvents: 'none' }}>
                      {truncate(n.label, 18)}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        </svg>

        {/* 缩放控件（深色玻璃，浮在星空上） */}
        <div style={{ position: 'absolute', right: 10, top: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {[
            { label: '+', fn: () => setZoom((z) => Math.min(ZOOM_MAX, z * 1.45)) },
            { label: '−', fn: () => setZoom((z) => Math.max(ZOOM_MIN, z / 1.45)) },
            { label: '⟲', fn: fitView },
          ].map((b) => (
            <button
              key={b.label}
              onClick={b.fn}
              style={{
                width: 30, height: 30, borderRadius: 8, border: '1px solid rgba(255,255,255,0.16)',
                background: 'rgba(13,20,38,0.88)', boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
                cursor: 'pointer', fontSize: '0.9rem', color: '#dce7f7',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              {b.label}
            </button>
          ))}
        </div>
        <div style={{ position: 'absolute', left: 10, top: 10, fontSize: '0.72rem', color: 'rgba(255,255,255,0.72)', background: 'rgba(10,16,32,0.78)', padding: '4px 8px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.12)' }}>
          滚轮缩放 · 拖拽画布平移 · 拖动节点调整 · 点击卡片看详情
        </div>
      </div>

      {/* Legend */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', padding: '12px 8px 0', flexWrap: 'wrap' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', border: '2px dashed #ff5c5c', background: 'rgba(255,92,92,0.15)' }} /> 未落地
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#4da3ff' }} /> 已落地卡
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#f2f8ff', border: '1.5px solid #4da3ff' }} /> 考点（恒星）
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#f6efff', border: '1.5px solid #c084fc' }} /> 单元（卫星）
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 18, height: 0, borderTop: '2px solid rgba(158,191,255,0.7)' }} /> 星座连线
        </span>
      </div>
    </div>
  );
});
