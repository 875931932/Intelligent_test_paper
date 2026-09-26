import { useState, useCallback, useMemo, useRef, memo } from 'react';
import {
  AlertTriangle, BookOpen, CheckCircle2, ChevronDown, ChevronRight, Circle,
  Folder, Sparkles,
} from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { formatConfidence } from '@/lib/format';
import { Badge, Button, Modal, Spinner } from '@/components/ui';
import type { KnowledgeCandidatePayload, CandidateKnowledgeTopic } from '@/types/api';
import { EvidenceRoleLabel } from './knowledgeShared';
import type { SupplementSource } from './knowledgeShared';

export function CandidatePanel({ candidate, courseId, runId, supplementOps, onSupplementChange, teacherExclusions, onTeacherExclusionsChange, onPublish, publishing, onReset }: {
  candidate: KnowledgeCandidatePayload | null;
  courseId: string;
  runId: string | null;
  supplementOps: Array<{ operation: string; target_code: string; value: string }>;
  onSupplementChange: (ops: Array<{ operation: string; target_code: string; value: string }>) => void;
  teacherExclusions: string[];
  onTeacherExclusionsChange: (codes: string[]) => void;
  onPublish: (autoSupplement?: boolean) => void;
  publishing: boolean;
  onReset: () => void;
}) {
  const topics = useMemo(() => candidate?.topics || [], [candidate]);
  const coverage = useMemo(() => candidate?.coverage || [], [candidate]);
  const evidenceSources = useMemo(
    () => candidate?.evidence_sources || [],
    [candidate]
  );
  const totalUnits = topics.reduce((acc, t) => acc + (t.units?.length || 0), 0);
  const totalCards = topics.reduce((acc, t) => acc + (t.units || []).reduce((a, u) => a + (u.cards?.length || 0), 0), 0);
  const needsReview = topics.filter((t) => t.status !== 'active').length
    + topics.reduce((acc, t) => acc + (t.units || []).filter((u) => u.status !== 'active').length, 0);

  // 按考点索引候选证据，避免每次渲染对全部证据源做线性扫描。
  // 只允许可改判为 direct 的证据：仅 supporting（含真实 support_claim）。
  // background 的 support_claim 是"（未提供说明）"占位符、不含可考核知识，
  // 改判后卡片无支撑事实必过不了发布质量闸；out_of_scope 与考点无关、空
  // content 多为解析失败脏块，三者都不该作为补证据候选展示或下发模型。
  const sourcesByPoint = useMemo(() => {
    const map = new Map<string, SupplementSource[]>();
    evidenceSources.forEach((s) => {
      if (s.relevance_class !== 'supporting') return;
      if (!(s.content || '').trim()) return;
      const list = map.get(s.exam_point_code);
      if (list) list.push(s);
      else map.set(s.exam_point_code, [s]);
    });
    return map;
  }, [evidenceSources]);

  const coverageByCode = useMemo(
    () => new Map((coverage || []).map((c) => [c.exam_point_code, c])),
    [coverage]
  );
  // 展示所有覆盖不足考点（含"归并失败"这类无间接证据可补但可排除的考点），
  // 让教师能针对性的补证据或排除。
  const insufficientPoints = useMemo(
    () => (coverage || [])
      .filter((c) => c.status !== 'sufficient')
      .map((c) => c.exam_point_code),
    [coverage]
  );

  // 为覆盖不足的考点，找可读标题：优先取候选考核单元标题，其次回退到
  // 框架考点元信息（覆盖不足考点常无候选单元，只有裸编码无从判断）。
  const examPointLabels = useMemo(() => candidate?.exam_point_labels || {}, [candidate]);
  const pointLabels = useMemo(() => {
    const map = new Map<string, string>();
    topics.forEach((t) => {
      (t.units || []).forEach((u) => {
        if (!map.has(u.exam_point_code)) {
          map.set(u.exam_point_code, (t.name || t.code) + ' · ' + u.title);
        }
      });
    });
    // 兜底：无候选单元的覆盖不足考点，用框架标题。
    Object.entries(examPointLabels).forEach(([code, meta]) => {
      if (!map.has(code) && meta?.title) map.set(code, meta.title);
    });
    return map;
  }, [topics, examPointLabels]);

  // 考点考核要求摘要，列表里让教师明确该考点考什么。
  const pointRequirement = useMemo(() => {
    const map = new Map<string, string>();
    Object.entries(examPointLabels).forEach(([code, meta]) => {
      if (meta?.assessment_requirement) map.set(code, meta.assessment_requirement);
    });
    return map;
  }, [examPointLabels]);

  const [suppOpen, setSuppOpen] = useState(false);
  const [suppPoint, setSuppPoint] = useState<string>('');
  const [suppChunk, setSuppChunk] = useState<string>('');
  // 排除考点的二次确认：点击某考点"排除"后弹窗，确认后加入 teacherExclusions。
  const [excludeConfirmOpen, setExcludeConfirmOpen] = useState(false);
  const [excludePoint, setExcludePoint] = useState<string>('');
  // AI 推荐：打开弹窗时自动请求该考点的推荐改判（模型预选，教师确认）。
  const [suppRecoLoading, setSuppRecoLoading] = useState(false);
  const [suppRecoError, setSuppRecoError] = useState<string>('');
  const [suppRecoByChunk, setSuppRecoByChunk] = useState<Map<string, string>>(new Map());
  const suppRecoSeq = useRef(0);

  const supplementable = useMemo(
    () => (suppPoint ? sourcesByPoint.get(suppPoint) || [] : []),
    [suppPoint, sourcesByPoint]
  );
  // 下拉选项：等级+置信度+claim 摘要，让教师能区分"有依据的间接证据"与"弱背景"。
  const suppOptions = useMemo(
    () => supplementable.map((s) => {
      const level = s.relevance_class === 'supporting' ? '支持' : '背景';
      const conf = typeof s.confidence === 'number' ? `置信 ${formatConfidence(s.confidence)}` : '';
      const claim = (s.support_claim || '').replace(/\s+/g, ' ').slice(0, 48);
      return {
        id: s.evidence_chunk_id,
        label: `${level} · ${conf}${conf ? ' · ' : ''}${claim}${(s.support_claim || '').length > 48 ? '…' : ''}`,
      };
    }),
    [supplementable]
  );
  // 当前选中证据的原文预览：教师确认前能看到将改判的直接证据内容。
  const suppPreview = useMemo(
    () => supplementable.find((s) => s.evidence_chunk_id === suppChunk) || null,
    [supplementable, suppChunk]
  );
  const suppPreviewPage = useMemo(() => {
    const page = suppPreview?.locator?.page_index;
    return typeof page === 'number' && page >= 0 ? page + 1 : null;
  }, [suppPreview]);

  const openSupplement = useCallback((code: string) => {
    setSuppPoint(code);
    const candidates = sourcesByPoint.get(code) || [];
    // 默认选置信度最高的支持证据；AI 推荐返回后再覆盖为推荐条目。
    const best = [...candidates].sort((a, b) => (b.confidence || 0) - (a.confidence || 0))[0];
    setSuppChunk(best?.evidence_chunk_id || '');
    setSuppOpen(true);
    setSuppRecoError('');
    setSuppRecoByChunk(new Map());
    // 异步请求 AI 推荐（不阻塞弹窗渲染；温度 0 + 后端 503/模型错误均降级为空推荐）。
    const seq = ++suppRecoSeq.current;
    setSuppRecoLoading(true);
    api.knowledge
      .recommendSupplements(courseId, runId || '', code)
      .then((res) => {
        if (seq !== suppRecoSeq.current) return;
        const recommended = Array.isArray(res.recommended) ? res.recommended : [];
        const map = new Map<string, string>();
        recommended.forEach((r: Record<string, unknown>) => {
          const id = String(r.evidence_chunk_id || '');
          if (id) map.set(id, String(r.reason || ''));
        });
        setSuppRecoByChunk(map);
        // 推荐存在时预选第一条推荐，教师无需改动即可采纳。
        if (map.size > 0) {
          const first = sourcesByPoint.get(code)?.find((s) => map.has(s.evidence_chunk_id));
          if (first) setSuppChunk(first.evidence_chunk_id);
        }
      })
      .catch((err) => {
        if (seq !== suppRecoSeq.current) return;
        setSuppRecoError(getErrorMessage(err));
      })
      .finally(() => {
        if (seq === suppRecoSeq.current) setSuppRecoLoading(false);
      });
  }, [sourcesByPoint, courseId, runId]);

  const closeSupplement = useCallback(() => setSuppOpen(false), []);

  const addSupplement = useCallback(() => {
    if (!suppPoint || !suppChunk) return;
    const next = supplementOps.filter((op) => !(op.target_code === suppPoint && op.value === suppChunk));
    next.push({ operation: 'supplement_direct_evidence', target_code: suppPoint, value: suppChunk });
    onSupplementChange(next);
    setSuppOpen(false);
  }, [suppPoint, suppChunk, supplementOps, onSupplementChange]);

  // 已补充的操作数（按考点），行内展示待发布状态，避免"点了没反应"。
  const supplementedCountByPoint = useMemo(() => {
    const map = new Map<string, number>();
    supplementOps.forEach((op) => {
      map.set(op.target_code, (map.get(op.target_code) || 0) + 1);
    });
    return map;
  }, [supplementOps]);
  const totalSupplementOps = supplementOps.length;

  // 排除考点：确认后加入集合；再次点击取消排除（从集合移除）。
  const toggleExclusion = useCallback((code: string) => {
    if (teacherExclusions.includes(code)) {
      onTeacherExclusionsChange(teacherExclusions.filter((c) => c !== code));
    } else {
      setExcludePoint(code);
      setExcludeConfirmOpen(true);
    }
  }, [teacherExclusions, onTeacherExclusionsChange]);

  const confirmExclusion = useCallback(() => {
    if (excludePoint) {
      onTeacherExclusionsChange([...teacherExclusions, excludePoint]);
    }
    setExcludeConfirmOpen(false);
    setExcludePoint('');
  }, [excludePoint, teacherExclusions, onTeacherExclusionsChange]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* 顶部操作区 */}
      <div className="glass-card" style={{ padding: '20px', display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
        <div style={{ width: 44, height: 44, borderRadius: '14px', background: 'var(--info-subtle)', color: 'var(--info)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          <CheckCircle2 size={24} />
        </div>
        <div style={{ flex: '1 1 240px', minWidth: 0 }}>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>知识目录构建完成，待确认</h3>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            {topics.length} 个主题 · {totalUnits} 个考核单元 · {totalCards} 张知识卡
            {needsReview > 0 && <span style={{ color: 'var(--warning)' }}> · {needsReview} 项需审阅</span>}
            {insufficientPoints.length > 0 && <span style={{ color: 'var(--warning)' }}> · {insufficientPoints.length} 考点可补证据</span>}
          </p>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <Button variant="secondary" onClick={onReset} disabled={publishing}>放弃并取消</Button>
          {insufficientPoints.length > 0 && (
            <Button variant="primary" onClick={() => onPublish(true)} loading={publishing} disabled={publishing}>
              一键补证据并发布
            </Button>
          )}
          <Button onClick={() => onPublish(false)} loading={publishing} disabled={publishing}>
            {publishing ? '发布中…' : '确认并发布'}
          </Button>
        </div>
      </div>

      {/* 证据不足考点：单点补证据入口（醒目列表，无需展开树） */}
      {insufficientPoints.length > 0 && (
        <div className="glass-card" style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertTriangle size={16} style={{ color: 'var(--warning)' }} />
            <h4 style={{ fontSize: '0.9375rem', fontWeight: 600 }}>证据不足的考点（{insufficientPoints.length}）</h4>
            <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>一键按 AI 推荐自动补充后发布，或逐点手动补充 / 排除</span>
            {totalSupplementOps > 0 && (
              <Badge variant="success">待发布补充 {totalSupplementOps} 项</Badge>
            )}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {insufficientPoints.map((code) => {
              const cov = coverageByCode.get(code);
              return (
                <div
                  key={code}
                  className="reveal-hover"
                  style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 12px', borderRadius: '10px', background: 'rgba(0,0,0,0.02)', flexWrap: 'wrap' }}
                >
                  <div style={{ flex: '1 1 260px', minWidth: 0 }}>
                    <div style={{ fontSize: '0.875rem', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{pointLabels.get(code) || code}</div>
                    <div className="reveal-target" style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{code}</div>
                    {pointRequirement.has(code) && (
                      <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', lineHeight: 1.5, marginTop: 2, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                        {pointRequirement.get(code)}
                      </div>
                    )}
                  </div>
                  {cov && <CoverageBadge status={cov.status} />}
                  {supplementedCountByPoint.has(code) && (
                    <Badge variant="success">已补 {supplementedCountByPoint.get(code)} 项</Badge>
                  )}
                  {teacherExclusions.includes(code) && (
                    <Badge variant="danger">已排除</Badge>
                  )}
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    {(sourcesByPoint.get(code)?.length || 0) > 0 && (
                      <Button
                        variant="secondary"
                        style={{ padding: '4px 12px', fontSize: '0.8125rem', height: 'auto', minHeight: 0 }}
                        onClick={() => openSupplement(code)}
                        disabled={teacherExclusions.includes(code)}
                      >
                        补证据
                      </Button>
                    )}
                    <Button
                      variant={teacherExclusions.includes(code) ? 'secondary' : 'danger'}
                      style={{ padding: '4px 12px', fontSize: '0.8125rem', height: 'auto', minHeight: 0 }}
                      onClick={() => toggleExclusion(code)}
                    >
                      {teacherExclusions.includes(code) ? '取消排除' : '排除'}
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 预览树 */}
      <div className="glass-card" style={{ padding: '16px', overflow: 'hidden' }}>
        <CandidateTreePreview topics={topics} coverage={coverage} onSupplement={openSupplement} />
      </div>

      {/* 补充证据弹窗 */}
      <Modal
        open={suppOpen}
        onClose={closeSupplement}
        title={`补充直接证据 · ${suppPoint ? pointLabels.get(suppPoint) || suppPoint : ''}`}
        maxWidth="560px"
        footer={
          <>
            <Button variant="secondary" onClick={closeSupplement}>取消</Button>
            <Button disabled={!suppPoint || !suppChunk} onClick={addSupplement}>确认补充</Button>
          </>
        }
      >
        {suppPoint && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div>
              <div style={{ fontSize: '0.9375rem', fontWeight: 600 }}>
                {pointLabels.get(suppPoint) || suppPoint}
              </div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                {suppPoint} · 以下间接证据将被改判为该考点的直接证据参与出卷
              </div>
            </div>
            {supplementable.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--text-tertiary)' }}>
                该考点暂无可用于补充的间接证据。建议直接排除该考点，或重新构建知识目录。
              </p>
            ) : (
              <>
                {suppRecoLoading && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
                    <Spinner size={14} /> AI 正在判读 {suppOptions.length} 条候选证据…
                  </div>
                )}
                {!suppRecoLoading && suppRecoError && (
                  <div style={{ fontSize: '0.8125rem', color: 'var(--warning)' }}>
                    AI 推荐暂不可用（{suppRecoError}），可参考下方信息手动选择。
                  </div>
                )}
                {!suppRecoLoading && !suppRecoError && suppRecoByChunk.size > 0 && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                    <Sparkles size={14} style={{ color: 'var(--accent)' }} />
                    AI 已判读全部候选，推荐 {suppRecoByChunk.size} 条可改判（已高亮并预选），最终由你确认
                  </div>
                )}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {suppOptions.map((opt) => {
                    const reason = suppRecoByChunk.get(opt.id);
                    const selected = suppChunk === opt.id;
                    return (
                      <div
                        key={opt.id}
                        onClick={() => setSuppChunk(opt.id)}
                        style={{
                          display: 'flex', flexDirection: 'column', gap: '4px',
                          padding: '10px 12px', borderRadius: '10px', cursor: 'pointer',
                          border: selected ? '2px solid var(--accent)' : '1px solid var(--border)',
                          background: selected ? 'var(--accent-subtle)' : (reason ? 'rgba(52,199,89,0.06)' : 'var(--surface)'),
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                          {reason && <Badge variant="success">AI 推荐</Badge>}
                          <span style={{ fontSize: '0.8125rem', fontWeight: selected ? 600 : 400, flex: '1 1 200px', minWidth: 0 }}>
                            {opt.label}
                          </span>
                          {selected && <Badge variant="info">已选</Badge>}
                        </div>
                        {reason && (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                            {reason}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
                {suppPreview && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '12px', borderRadius: '10px', background: 'rgba(0,0,0,0.03)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                      <Badge variant={suppPreview.relevance_class === 'supporting' ? 'info' : 'warning'}>
                        {suppPreview.relevance_class === 'supporting' ? '支持证据' : '背景证据'}
                      </Badge>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                        置信度 {formatConfidence(suppPreview.confidence)}
                        {suppPreviewPage !== null && ` · 第 ${suppPreviewPage} 页`}
                        {suppPreview.evidence_role && ` · ${EvidenceRoleLabel(suppPreview.evidence_role)}`}
                      </span>
                    </div>
                    {(suppPreview.support_claim || '').trim() && (
                      <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                        <span style={{ fontWeight: 600 }}>改判依据：</span>{suppPreview.support_claim}
                      </div>
                    )}
                    <div style={{ fontSize: '0.8125rem', color: 'var(--text)', lineHeight: 1.7, maxHeight: 180, overflowY: 'auto', whiteSpace: 'pre-wrap' }}>
                      {(suppPreview.content || '').trim() || '（原文未注入，请以改判依据为准）'}
                    </div>
                  </div>
                )}
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
                  确认后操作会暂存，点击顶部「确认并发布」时才提交生效；每条补充会在考点行标注「已补 N 项」。
                </p>
              </>
            )}
          </div>
        )}
      </Modal>
      <Modal
        open={excludeConfirmOpen}
        onClose={() => { setExcludeConfirmOpen(false); setExcludePoint(''); }}
        title="排除该考点？"
        maxWidth="480px"
        footer={
          <>
            <Button variant="secondary" onClick={() => { setExcludeConfirmOpen(false); setExcludePoint(''); }}>
              取消
            </Button>
            <Button variant="danger" onClick={confirmExclusion}>
              确认排除
            </Button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ fontSize: '0.9375rem', fontWeight: 600 }}>{excludePoint && (pointLabels.get(excludePoint) || excludePoint)}</div>
          <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            排除后该考点将不参与出卷覆盖，也不会进入最终发布的考查范围。
            {sourcesByPoint.get(excludePoint)?.length ? ' 可先尝试「补证据」后再决定是否排除。' : ''}
            排除操作可在此处撤销。
          </div>
        </div>
      </Modal>
    </div>
  );
}

// 候选树渲染面大（实测 142 卡 / 1.2MB 候选 JSON）：补证据弹窗开合、
// supplementOps 变化都不应触发整棵树重渲染，memo 隔离。
const CandidateTreePreview = memo(function CandidateTreePreview({ topics, coverage, onSupplement }: {
  topics: CandidateKnowledgeTopic[];
  coverage: KnowledgeCandidatePayload['coverage'];
  onSupplement: (code: string) => void;
}) {
  const [expandedTopics, setExpandedTopics] = useState<Set<string>>(new Set());
  const [expandedUnits, setExpandedUnits] = useState<Set<string>>(new Set());

  const coverageByCode = useMemo(
    () => new Map((coverage || []).map((c) => [c.exam_point_code, c])),
    [coverage]
  );

  if (topics.length === 0) {
    return (
      <div style={{ padding: '48px 0', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        <BookOpen size={40} style={{ margin: '0 auto 12px', opacity: 0.4 }} />
        <p style={{ fontSize: '0.875rem' }}>候选知识目录为空</p>
      </div>
    );
  }

  const toggleTopic = (code: string) => setExpandedTopics((prev) => {
    const next = new Set(prev);
    if (next.has(code)) next.delete(code); else next.add(code);
    return next;
  });
  const toggleUnit = (key: string) => setExpandedUnits((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      {topics.map((topic) => {
        const isExp = expandedTopics.has(topic.code);
        const units = topic.units || [];
        return (
          <div key={topic.code}>
            <div
              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', cursor: 'pointer', background: 'rgba(0,0,0,0.02)' }}
              onClick={() => toggleTopic(topic.code)}
            >
              <span style={{ color: 'var(--text-tertiary)' }}>{isExp ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
              <span style={{ color: '#5856d6' }}><Folder size={14} /></span>
              <span style={{ fontWeight: 600, fontSize: '0.875rem' }}>{topic.name || topic.code}</span>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>({topic.code})</span>
              {topic.status !== 'active' && <Badge variant="warning">需审阅</Badge>}
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>{units.length} 单元</span>
            </div>
            {isExp && (
              <div style={{ marginLeft: '24px', marginTop: '4px' }}>
                {units.map((unit) => {
                  const unitKey = topic.code + '::' + unit.code;
                  const isUExp = expandedUnits.has(unitKey);
                  const cov = coverageByCode.get(unit.exam_point_code);
                  return (
                    <div key={unitKey}>
                      <div
                        style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 12px', borderRadius: '10px', cursor: 'pointer' }}
                        onClick={() => toggleUnit(unitKey)}
                      >
                        <span style={{ color: 'var(--text-tertiary)' }}>{isUExp ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
                        <span style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{unit.code}</span>
                        <span style={{ fontSize: '0.875rem', fontWeight: 500 }}>{unit.title}</span>
                        {unit.status !== 'active' && <Badge variant="warning">需审阅</Badge>}
                        {cov && <CoverageBadge status={cov.status} />}
                        {cov && cov.status !== 'sufficient' && (
                          <Button
                            variant="secondary"
                            style={{ padding: '2px 8px', fontSize: '0.75rem', height: 'auto', minHeight: 0 }}
                            onClick={(e) => { e.stopPropagation(); onSupplement(unit.exam_point_code); }}
                          >
                            补证据
                          </Button>
                        )}
                        <span style={{ fontSize: '0.8125rem', marginLeft: 'auto', color: 'var(--text-tertiary)' }}>{(unit.cards || []).length}卡</span>
                      </div>
                      {isUExp && (unit.cards || []).length > 0 && (
                        <div style={{ marginLeft: '24px' }}>
                          {(unit.cards || []).map((card, ci) => (
                            <div
                              key={card.name + ci}
                              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 12px', borderRadius: '8px' }}
                            >
                              <span style={{ color: card.status === 'active' ? '#34c759' : '#ff9500' }}>
                                <Circle size={8} fill="currentColor" />
                              </span>
                              <span style={{ fontSize: '0.875rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{card.name}</span>
                              {card.status !== 'active' && <Badge variant="warning">需审阅</Badge>}
                              <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{card.importance || 1}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
});

function CoverageBadge({ status }: { status: string }) {
  if (status === 'sufficient') return <Badge variant="success">覆盖充足</Badge>;
  if (status === 'conflicting') return <Badge variant="error">覆盖冲突</Badge>;
  return <Badge variant="warning">覆盖不足</Badge>;
}
