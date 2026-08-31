import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams } from 'react-router-dom';
import {
  GitBranch, Search, Network, TreePine, ChevronRight, ChevronDown, Circle,
  AlertTriangle, Eye, RefreshCw, Plus, BookOpen, Target, CheckCircle2, Layers,
} from 'lucide-react';
import { api } from '@/api/client';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Select, Badge, Spinner } from '@/components/ui';
import type {
  PublishedKnowledgeResponse, KnowledgeCard, AssessmentUnit, EvidenceChunk,
  FrameworkExamPoint,
} from '@/types/api';

type ViewMode = 'tree' | 'graph';
type BuildState = 'idle' | 'building' | 'candidate' | 'published';

const CLUSTER_COLORS = [
  '#0071e3', '#34c759', '#ff9500', '#af52de',
  '#ff3b30', '#5ac8fa', '#ff2d55', '#30b0c7',
];

function EvidenceRoleLabel(role: string): string {
  const labels: Record<string, string> = {
    direct: '直接证据', supporting: '支持证据',
    background: '背景证据', out_of_scope: '超出范围',
  };
  return labels[role] || role;
}

function EvidenceRoleVariant(role: string): string {
  const variants: Record<string, string> = {
    direct: 'success', supporting: 'warning',
    background: 'default', out_of_scope: 'default',
  };
  return variants[role] || 'default';
}

interface MaterialVersionOption {
  id: string;
  name: string;
  type: string;
  version: string;
}

export default function KnowledgePage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const courseId = routeCourseId || '';
  const { addToast } = useToastStore();

  const [viewMode, setViewMode] = useState<ViewMode>('tree');
  const [loading, setLoading] = useState(true);
  const [knowledge, setKnowledge] = useState<PublishedKnowledgeResponse | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterCluster, setFilterCluster] = useState<string>('all');
  const [filterGrounded, setFilterGrounded] = useState<string>('all');

  const [buildState, setBuildState] = useState<BuildState>('idle');
  const [buildOpen, setBuildOpen] = useState(false);
  const [selectableVersions, setSelectableVersions] = useState<MaterialVersionOption[]>([]);
  const [versionIds, setVersionIds] = useState<string[]>([]);
  const [building, setBuilding] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);

  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceChunk[]>([]);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [expandedPoints, setExpandedPoints] = useState<Set<string>>(new Set());
  const [expandedUnits, setExpandedUnits] = useState<Set<string>>(new Set());

  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Derived
  const examPoints: FrameworkExamPoint[] = knowledge?.exam_points || [];
  const units: AssessmentUnit[] = knowledge?.units || [];
  const cardsDict: Record<string, KnowledgeCard> = knowledge?.knowledge_cards || {};

  const clusters = useMemo(() => {
    const set = new Set<string>();
    Object.values(cardsDict).forEach((c) => set.add(c.concept_cluster));
    return Array.from(set).sort();
  }, [cardsDict]);

  const filteredCards = useMemo(() => {
    let cards = Object.values(cardsDict) as KnowledgeCard[];
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      cards = cards.filter((c) =>
        c.name.toLowerCase().includes(q) ||
        c.concept_cluster.toLowerCase().includes(q) ||
        c.performance_statement.toLowerCase().includes(q)
      );
    }
    if (filterCluster !== 'all') {
      cards = cards.filter((c) => c.concept_cluster === filterCluster);
    }
    if (filterGrounded === 'grounded') {
      cards = cards.filter((c) => c.grounded);
    } else if (filterGrounded === 'ungrounded') {
      cards = cards.filter((c) => !c.grounded);
    }
    return cards;
  }, [cardsDict, searchQuery, filterCluster, filterGrounded]);

  const filteredCardIds = useMemo(() => new Set(filteredCards.map((c) => c.id)), [filteredCards]);

  // Load published
  const loadPublished = useCallback(async () => {
    if (!courseId) return;
    try {
      setLoading(true);
      const data = await api.knowledge.getPublished(courseId);
      if (data?.published !== false) {
        setKnowledge(data as PublishedKnowledgeResponse);
        setBuildState('published');
      } else {
        setKnowledge(null);
        setBuildState('idle');
      }
    } catch {
      setKnowledge(null);
      setBuildState('idle');
    } finally {
      setLoading(false);
    }
  }, [courseId]);

  useEffect(() => {
    loadPublished();
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, [loadPublished]);

  // Build flow
  const handleOpenBuild = useCallback(async () => {
    setBuildOpen(true);
    try {
      const data = await api.materials.list(courseId);
      const list = Array.isArray(data) ? data : [];
      const versions: MaterialVersionOption[] = [];
      list.forEach((m) => {
        if (m.latest_version) {
          versions.push({
            id: m.latest_version.id,
            name: m.logical_name || '未命名',
            type: m.material_type,
            version: 'v' + m.latest_version.version_no,
          });
        }
      });
      setSelectableVersions(versions);
    } catch {
      addToast('加载资料列表失败', 'error');
    }
    setVersionIds([]);
  }, [courseId, addToast]);

  const startPolling = useCallback((rid: string) => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    pollingRef.current = setInterval(async () => {
      try {
        const runData = await api.knowledge.getRun(courseId, rid);
        if (runData?.candidate_id) {
          clearInterval(pollingRef.current!);
          pollingRef.current = null;
          setBuildState('candidate');
          setBuildOpen(false);
          addToast('知识目录构建完成，请确认', 'success');
        } else if (runData?.status === 'failed') {
          clearInterval(pollingRef.current!);
          pollingRef.current = null;
          setBuildState('idle');
          setBuildOpen(false);
          addToast((runData.error_message as string) || '构建失败', 'error');
        }
      } catch { /* ignore */ }
    }, 3000);
  }, [courseId, addToast]);

  const handleBuild = useCallback(async () => {
    if (versionIds.length === 0) {
      addToast('请至少选择一个资料版本', 'error');
      return;
    }
    try {
      setBuilding(true);
      setBuildState('building');
      const run = await api.knowledge.createOrganizationRun(courseId, {
        material_version_ids: versionIds,
      });
      setRunId(run.run_id);
      startPolling(run.run_id);
      addToast('知识目录构建中，请稍候...', 'info');
      setBuildOpen(false);
    } catch {
      addToast('启动构建失败', 'error');
      setBuildState('idle');
    } finally {
      setBuilding(false);
    }
  }, [courseId, versionIds, startPolling, addToast]);

  const handlePublish = useCallback(async () => {
    if (!runId) return;
    try {
      await api.knowledge.publish(courseId, runId, {
        operations: [],
        reviewed_topic_codes: [],
        reviewed_exam_point_codes: [],
        teacher_exclusions: [],
      });
      addToast('知识目录已发布', 'success');
      setBuildState('published');
      loadPublished();
    } catch {
      addToast('发布失败', 'error');
    }
  }, [courseId, runId, loadPublished, addToast]);

  // Evidence
  const loadEvidence = useCallback(async (cardId: string) => {
    setEvidenceLoading(true);
    setSelectedCardId(cardId);
    try {
      const data = await api.knowledge.getCardEvidence(courseId, cardId);
      setEvidence(data);
    } catch {
      setEvidence([]);
    } finally {
      setEvidenceLoading(false);
    }
  }, [courseId]);

  const handleCardClick = useCallback((cardId: string) => {
    loadEvidence(cardId);
    setDrawerOpen(true);
  }, [loadEvidence]);

  const togglePoint = useCallback((pid: string) => {
    setExpandedPoints((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid); else next.add(pid);
      return next;
    });
  }, []);

  const toggleUnit = useCallback((uid: string) => {
    setExpandedUnits((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) next.delete(uid); else next.add(uid);
      return next;
    });
  }, []);

  // Stats
  const allCards = useMemo(() => Object.values(cardsDict) as KnowledgeCard[], [cardsDict]);

  const stats = useMemo(() => ({
    totalCards: allCards.length,
    groundedCards: allCards.filter((c) => c.grounded).length,
    ungroundedCards: allCards.filter((c) => !c.grounded).length,
    totalUnits: units.length,
    totalPoints: examPoints.length,
  }), [allCards, units, examPoints]);

  const selectedCard = selectedCardId ? (cardsDict[selectedCardId] as KnowledgeCard | undefined) : undefined;

  if (loading) {
    return (
      <div className="page-enter" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '120px 20px' }}>
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px', flexWrap: 'wrap' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em' }}>知识目录</h1>
            {buildState === 'published' && <Badge variant="success">已发布</Badge>}
            {buildState === 'building' && <Badge variant="warning">构建中</Badge>}
            {buildState === 'candidate' && <Badge variant="info">待确认</Badge>}
          </div>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)', marginTop: '6px' }}>
            {stats.totalCards > 0
              ? stats.totalCards + ' 张知识卡 · ' + stats.totalUnits + ' 个考核单元 · ' + stats.totalPoints + ' 个考点'
              : '结构化知识网络，驱动命题流程'}
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Button variant="secondary" icon={<RefreshCw size={16} />} onClick={loadPublished} />
          <Button variant="primary" icon={<Plus size={16} />} onClick={handleOpenBuild}>
            构建知识目录
          </Button>
        </div>
      </div>

      {/* Stats */}
      {stats.totalCards > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <StatsBadge icon={<BookOpen size={14} />} color="#0071e3" value={stats.totalCards} label="知识卡" />
          <StatsBadge icon={<Target size={14} />} color="#ff9500" value={stats.totalPoints} label="考点" />
          <StatsBadge icon={<GitBranch size={14} />} color="#af52de" value={stats.totalUnits} label="考核单元" />
          <StatsBadge icon={<CheckCircle2 size={14} />} color="#34c759" value={stats.groundedCards} label="已落地" />
          {stats.ungroundedCards > 0 && (
            <StatsBadge icon={<AlertTriangle size={14} />} color="#ff3b30" value={stats.ungroundedCards} label="未落地" />
          )}
        </div>
      )}

      {/* Toolbar */}
      {stats.totalCards > 0 && (
        <div className="glass-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <div style={{ position: 'relative', flex: '1 1 200px', maxWidth: '320px' }}>
            <Search size={16} style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-tertiary)' }} />
            <input
              type="text"
              placeholder="搜索知识卡..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              style={{ width: '100%', paddingLeft: '36px', paddingRight: '12px', height: '36px', fontSize: '0.875rem', borderRadius: '10px', border: '1px solid rgba(0,0,0,0.06)', background: 'rgba(0,0,0,0.03)', outline: 'none' }}
            />
          </div>
          <Select
            value={filterCluster}
            onChange={(e) => setFilterCluster(e.target.value)}
            options={[{ value: 'all', label: '全部簇' }, ...clusters.map((c) => ({ value: c, label: c }))]}
            style={{ width: 'auto', minWidth: '120px' }}
          />
          <Select
            value={filterGrounded}
            onChange={(e) => setFilterGrounded(e.target.value)}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'grounded', label: '已落地' },
              { value: 'ungrounded', label: '未落地' },
            ]}
            style={{ width: 'auto', minWidth: '120px' }}
          />
          <div style={{ flex: 1 }} />
          <div style={{ display: 'flex', alignItems: 'center', background: 'rgba(0,0,0,0.04)', borderRadius: '10px', padding: '3px' }}>
            <ViewToggle mode="tree" current={viewMode} onChange={setViewMode} label="树形" icon={TreePine} />
            <ViewToggle mode="graph" current={viewMode} onChange={setViewMode} label="图谱" icon={Network} />
          </div>
        </div>
      )}

      {/* Content */}
      {buildState === 'building' && <BuildingPanel />}
      {buildState === 'candidate' && (
        <CandidatePanel onPublish={handlePublish} onReset={() => { setBuildState('idle'); setRunId(null); }} />
      )}
      {buildState === 'published' && (
        <div className="glass-card" style={{ padding: '16px', overflow: 'hidden' }}>
          {viewMode === 'tree' && (
            <TreeView
              examPoints={examPoints}
              units={units}
              cardsDict={cardsDict}
              filteredCardIds={filteredCardIds}
              expandedPoints={expandedPoints}
              expandedUnits={expandedUnits}
              togglePoint={togglePoint}
              toggleUnit={toggleUnit}
              onCardClick={handleCardClick}
            />
          )}
          {viewMode === 'graph' && (
            <GraphView
              examPoints={examPoints}
              units={units}
              cards={filteredCards}
              onCardClick={handleCardClick}
            />
          )}
        </div>
      )}
      {buildState === 'idle' && stats.totalCards === 0 && <IdlePanel onBuild={handleOpenBuild} />}

      {/* Build Dialog */}
      <Modal
        open={buildOpen}
        onClose={() => setBuildOpen(false)}
        title="构建知识目录"
        footer={
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <Button variant="secondary" onClick={() => setBuildOpen(false)}>取消</Button>
            <Button loading={building} disabled={versionIds.length === 0} onClick={handleBuild}>
              开始构建
            </Button>
          </div>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            选择用于构建知识目录的资料版本（可多选）：
          </p>
          {selectableVersions.length === 0 ? (
            <p style={{ fontSize: '0.875rem', color: 'var(--text-tertiary)', padding: '24px 0', textAlign: 'center' }}>
              暂无已上传的资料版本，请先前往「资料库」上传并解析资料。
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '320px', overflowY: 'auto' }}>
              {selectableVersions.map((v) => {
                const checked = versionIds.includes(v.id);
                return (
                  <label
                    key={v.id}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '10px',
                      padding: '10px 12px', borderRadius: '10px',
                      border: checked ? '1px solid var(--accent)' : '1px solid rgba(0,0,0,0.06)',
                      background: checked ? 'var(--accent-subtle)' : 'rgba(0,0,0,0.02)',
                      cursor: 'pointer', transition: 'all 0.2s',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        setVersionIds((prev) => checked ? prev.filter((id) => id !== v.id) : [...prev, v.id]);
                      }}
                      style={{ accentColor: 'var(--accent)' }}
                    />
                    <Layers size={15} style={{ color: 'var(--text-tertiary)' }} />
                    <span style={{ fontSize: '0.875rem', fontWeight: 500 }}>{v.name}</span>
                    <Badge variant="default">{v.version}</Badge>
                  </label>
                );
              })}
            </div>
          )}
        </div>
      </Modal>

      {/* Detail Drawer */}
      <Modal
        open={drawerOpen}
        onClose={() => { setDrawerOpen(false); setEvidence([]); setSelectedCardId(null); }}
        title={selectedCard?.name || '知识卡详情'}
        maxWidth="640px"
        footer={
          <Button variant="secondary" onClick={() => { setDrawerOpen(false); setEvidence([]); setSelectedCardId(null); }}>
            关闭
          </Button>
        }
      >
        {selectedCard && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <Badge variant={selectedCard.grounded ? 'success' : 'error'}>
                {selectedCard.grounded ? '已着陆' : '未着陆'}
              </Badge>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>重要性: {selectedCard.importance}</span>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>簇: {selectedCard.concept_cluster}</span>
            </div>
            {selectedCard.performance_statement && (
              <FieldBlock label="性能表述" content={selectedCard.performance_statement} />
            )}
            {selectedCard.answer_proposition && (
              <FieldBlock label="答案命题" content={selectedCard.answer_proposition} />
            )}
            {(selectedCard.cognitive_targets || []).length > 0 && (
              <div>
                <h4 style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '8px' }}>认知目标</h4>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                  {(selectedCard.cognitive_targets || []).map((t, i) => (
                    <Badge key={i} variant="info">{t}</Badge>
                  ))}
                </div>
              </div>
            )}
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
              允许题型: {(selectedCard.allowed_question_types || []).join(', ') || '不限'}
            </p>
            <div>
              <h4 style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '8px' }}>证据链</h4>
              {evidenceLoading ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px 0' }}>
                  <Spinner size="sm" />
                  <span style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>加载中...</span>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '200px', overflowY: 'auto' }}>
                  {evidence.map((ev, i) => (
                    <div key={i} style={{ padding: '10px 12px', borderRadius: '10px', background: 'rgba(0,0,0,0.02)', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <Badge variant={EvidenceRoleVariant(ev.evidence_role)}>{EvidenceRoleLabel(ev.evidence_role)}</Badge>
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{Math.round((ev.confidence || 0) * 100)}%</span>
                      </div>
                      <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>{ev.content}</p>
                      <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ev.locator}</p>
                    </div>
                  ))}
                  {evidence.length === 0 && (
                    <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', textAlign: 'center', padding: '16px 0' }}>暂无证据链</p>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

// ─── Sub-components ───

function StatsBadge({ icon, color, value, label }: { icon: React.ReactNode; color: string; value: number; label: string }) {
  return (
    <div className="glass-card" style={{ padding: '8px 14px', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.875rem' }}>
      <span style={{ color }}>{icon}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
    </div>
  );
}

function FieldBlock({ label, content }: { label: string; content: string }) {
  return (
    <div>
      <h4 style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '6px' }}>{label}</h4>
      <p style={{ fontSize: '0.875rem', color: 'var(--text)', lineHeight: 1.6 }}>{content}</p>
    </div>
  );
}

function ViewToggle({ mode, current, onChange, label, icon: Icon }: {
  mode: ViewMode; current: ViewMode; onChange: (m: ViewMode) => void;
  label: string; icon: React.ComponentType<{ size: number }>;
}) {
  const active = current === mode;
  return (
    <button
      onClick={() => onChange(mode)}
      style={{
        display: 'flex', alignItems: 'center', gap: '6px',
        padding: '6px 12px', borderRadius: '8px',
        fontSize: '0.875rem', fontWeight: 500,
        background: active ? '#fff' : 'transparent',
        boxShadow: active ? '0 1px 4px rgba(0,0,0,0.08)' : 'none',
        color: active ? 'var(--text)' : 'var(--text-tertiary)',
        border: 'none', cursor: 'pointer',
        transition: 'all 0.2s',
      }}
    >
      <Icon size={14} /> {label}
    </button>
  );
}

function BuildingPanel() {
  return (
    <div className="glass-panel" style={{ padding: '64px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px' }}>
      <Spinner size="lg" />
      <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>正在构建知识目录，正在组织资料与考点...</p>
      <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>此过程通常需要 1-3 分钟，请勿关闭页面</p>
    </div>
  );
}

function CandidatePanel({ onPublish, onReset }: { onPublish: () => void; onReset: () => void }) {
  return (
    <div className="glass-panel" style={{ padding: '48px 24px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px', textAlign: 'center' }}>
      <div style={{ width: 56, height: 56, borderRadius: '18px', background: 'var(--info-subtle)', color: 'var(--info)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <CheckCircle2 size={28} />
      </div>
      <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>知识目录构建完成</h3>
      <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', maxWidth: '420px' }}>
        系统已完成知识组织。确认无误后发布，发布后即可用于蓝图与合同命题阶段。
      </p>
      <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
        <Button variant="secondary" onClick={onReset}>暂不发布</Button>
        <Button onClick={onPublish}>确认并发布</Button>
      </div>
    </div>
  );
}

function IdlePanel({ onBuild }: { onBuild: () => void }) {
  return (
    <div className="glass-panel" style={{ padding: '64px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px' }}>
      <div style={{ width: 56, height: 56, borderRadius: '18px', background: 'var(--accent-subtle)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Network size={28} />
      </div>
      <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>尚未构建知识目录</h3>
      <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', maxWidth: '420px', textAlign: 'center' }}>
        知识目录将课程资料组织为以考点为核心的知识卡片与证据链，是命题蓝图与合同的基础。
      </p>
      <Button icon={<Plus size={16} />} onClick={onBuild}>构建知识目录</Button>
    </div>
  );
}

// ─── Tree View ───

function TreeView(props: {
  examPoints: FrameworkExamPoint[];
  units: AssessmentUnit[];
  cardsDict: Record<string, KnowledgeCard>;
  filteredCardIds: Set<string>;
  expandedPoints: Set<string>;
  expandedUnits: Set<string>;
  togglePoint: (id: string) => void;
  toggleUnit: (id: string) => void;
  onCardClick: (id: string) => void;
}) {
  const { examPoints, units, cardsDict, filteredCardIds, expandedPoints, expandedUnits, togglePoint, toggleUnit, onCardClick } = props;

  if (examPoints.length === 0 && Object.keys(cardsDict).length === 0) {
    return (
      <div style={{ padding: '48px 0', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        <BookOpen size={40} style={{ margin: '0 auto 12px', opacity: 0.4 }} />
        <p style={{ fontSize: '0.875rem' }}>暂无知识目录</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '8px 0' }}>
      {examPoints.map((point) => {
        const pointUnits = units.filter((u) => u.exam_point_id === point.id);
        const isExp = expandedPoints.has(point.id);
        return (
          <div key={point.id}>
            <div
              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', cursor: 'pointer', transition: 'background 0.2s' }}
              onClick={() => togglePoint(point.id)}
            >
              <span style={{ color: 'var(--text-tertiary)' }}>{isExp ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
              <span style={{ color: '#0071e3' }}><Target size={14} /></span>
              <span style={{ fontWeight: 600, fontSize: '0.875rem' }}>{point.title || point.code}</span>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>({point.code})</span>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>{point.weight_value}%</span>
            </div>
            {isExp && (
              <div style={{ marginLeft: '24px' }}>
                {pointUnits.map((unit) => {
                  const unitCards = unit.card_ids
                    .map((cid) => cardsDict[cid])
                    .filter((c): c is KnowledgeCard => !!(c && filteredCardIds.has(c.id)));
                  const isUExp = expandedUnits.has(unit.unit_id);
                  const ungrounded = unitCards.some((c) => !c.grounded);
                  return (
                    <div key={unit.unit_id}>
                      <div
                        style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 12px', borderRadius: '10px', cursor: 'pointer', transition: 'background 0.2s' }}
                        onClick={() => toggleUnit(unit.unit_id)}
                      >
                        <span style={{ color: 'var(--text-tertiary)' }}>{isUExp ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
                        <span style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{unit.code}</span>
                        <span style={{ fontSize: '0.875rem', fontWeight: 500 }}>{unit.title}</span>
                        <span style={{ fontSize: '0.8125rem', marginLeft: 'auto', color: ungrounded ? '#ff3b30' : '#34c759' }}>
                          {unitCards.length}卡
                        </span>
                      </div>
                      {isUExp && unitCards.length > 0 && (
                        <div style={{ marginLeft: '24px' }}>
                          {unitCards.map((card) => (
                            <div
                              key={card.id}
                              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', transition: 'background 0.2s' }}
                              onClick={() => onCardClick(card.id)}
                            >
                              <span style={{ color: card.grounded ? '#34c759' : '#ff3b30' }}>
                                <Circle size={8} fill="currentColor" />
                              </span>
                              <span style={{ fontSize: '0.875rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{card.name}</span>
                              <Badge variant={card.grounded ? 'success' : 'error'}>
                                {card.grounded ? '已落地' : '未落地'}
                              </Badge>
                              <Eye size={12} style={{ color: 'var(--text-tertiary)', opacity: 0.6 }} />
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
}

// ─── Graph View ───

function GraphView(props: {
  examPoints: FrameworkExamPoint[];
  units: AssessmentUnit[];
  cards: KnowledgeCard[];
  onCardClick: (id: string) => void;
}) {
  const { examPoints, units, cards, onCardClick } = props;

  const width = 720;
  const height = 520;
  const cx = width / 2;
  const cy = height / 2;

  if (cards.length === 0 && units.length === 0 && examPoints.length === 0) {
    return (
      <div style={{ padding: '40px 0', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        <Network size={40} style={{ margin: '0 auto 12px', opacity: 0.4 }} />
        <p style={{ fontSize: '0.875rem' }}>图谱暂无节点</p>
      </div>
    );
  }

  // Card nodes positioned on inner rings
  const cardNodes = cards.map((card, i) => {
    const angle = ((Math.PI * 2) / Math.max(cards.length, 1)) * i - Math.PI / 2;
    const ring = 110 + (i % 3) * 30 + (card.importance || 0) * 3;
    return {
      id: card.id,
      x: cx + ring * Math.cos(angle),
      y: cy + ring * Math.sin(angle),
      r: 5 + (card.importance || 0) * 1.5,
      color: CLUSTER_COLORS[i % CLUSTER_COLORS.length],
      grounded: card.grounded,
      name: card.name,
    };
  });

  // Unit nodes on middle ring
  const unitNodes = units.map((unit, i) => {
    const angle = ((Math.PI * 2) / Math.max(units.length, 1)) * i - Math.PI / 2;
    const r = 210;
    return {
      id: unit.unit_id,
      x: cx + r * Math.cos(angle),
      y: cy + r * Math.sin(angle),
      code: unit.code,
      count: unit.card_ids?.length || 0,
    };
  });

  // Exam point nodes on outer ring
  const pointNodes = examPoints.map((point, i) => {
    const angle = ((Math.PI * 2) / Math.max(examPoints.length, 1)) * i - Math.PI / 2;
    const r = 290;
    return {
      id: point.id,
      x: cx + r * Math.cos(angle),
      y: cy + r * Math.sin(angle),
      code: point.code,
      weight: point.weight_value,
    };
  });

  // Edges from relation_edges
  const edges: { id: string; x1: number; y1: number; x2: number; y2: number; dashed: boolean }[] = [];
  const nodeById = new Map(cardNodes.map((n) => [n.id, n]));
  cards.forEach((card) => {
    const edgesArr = card.relation_edges;
    if (!Array.isArray(edgesArr)) return;
    edgesArr.forEach((edge, idx) => {
      const targetId = typeof edge === 'string' ? edge : (edge as { target_id?: string }).target_id;
      if (!targetId) return;
      const src = nodeById.get(card.id);
      const tgt = nodeById.get(targetId);
      if (src && tgt) {
        edges.push({ id: card.id + '-' + targetId + '-' + idx, x1: src.x, y1: src.y, x2: tgt.x, y2: tgt.y, dashed: false });
      }
    });
  });

  return (
    <div>
      <div style={{ position: 'relative', userSelect: 'none' }}>
        <svg viewBox={'0 0 ' + width + ' ' + height} style={{ width: '100%', maxHeight: '560px' }}>
          <defs>
            <marker id="gh-arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <path d="M0,0 L8,3 L0,6 Z" fill="rgba(0,0,0,0.2)" />
            </marker>
          </defs>

          {/* Edges */}
          {edges.map((edge) => (
            <line key={edge.id} x1={edge.x1} y1={edge.y1} x2={edge.x2} y2={edge.y2}
              stroke="rgba(0,0,0,0.12)" strokeWidth={1}
              strokeDasharray={edge.dashed ? '4 3' : undefined}
              markerEnd="url(#gh-arrowhead)" />
          ))}

          {/* Exam point nodes */}
          {pointNodes.map((point) => (
            <g key={'ep-' + point.id}>
              <circle cx={point.x} cy={point.y} r="26" fill="#fff" stroke="rgba(0,0,0,0.08)" strokeWidth="1" />
              <circle cx={point.x} cy={point.y} r="26" fill="rgba(0,113,227,0.04)" />
              <text x={point.x} y={point.y - 2} textAnchor="middle" fontSize="10" fontWeight="700" fill="#0071e3">{point.code}</text>
              <text x={point.x} y={point.y + 11} textAnchor="middle" fontSize="7" fill="var(--text-tertiary)">{point.weight}%</text>
            </g>
          ))}

          {/* Unit nodes */}
          {unitNodes.map((unit) => (
            <g key={'unit-' + unit.id}>
              <circle cx={unit.x} cy={unit.y} r="17" fill="rgba(88,86,214,0.08)" stroke="rgba(88,86,214,0.25)" strokeWidth="1" />
              <text x={unit.x} y={unit.y - 1} textAnchor="middle" fontSize="8" fontWeight="600" fill="#5856d6">{unit.code}</text>
              <text x={unit.x} y={unit.y + 9} textAnchor="middle" fontSize="6" fill="var(--text-tertiary)">{unit.count}</text>
            </g>
          ))}

          {/* Card nodes */}
          {cardNodes.map((node) => (
            <g key={node.id} onClick={() => onCardClick(node.id)} style={{ cursor: 'pointer' }}>
              <title>{node.name}</title>
              <circle cx={node.x} cy={node.y} r={node.r}
                fill={node.color} fillOpacity={node.grounded ? 1 : 0.4}
                stroke={node.grounded ? 'none' : '#ff3b30'} strokeWidth={node.grounded ? 0 : 2}
                strokeDasharray={node.grounded ? 'none' : '3 2'} />
            </g>
          ))}
        </svg>
      </div>

      {/* Legend */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', padding: '12px 8px 0', flexWrap: 'wrap' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', border: '2px dashed #ff3b30', background: 'rgba(255,59,48,0.1)' }} /> 未落地
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#34c759' }} /> 已落地
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: 'rgba(0,113,227,0.15)', border: '1px solid rgba(0,113,227,0.4)' }} /> 考点
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: 'rgba(88,86,214,0.15)', border: '1px solid rgba(88,86,214,0.4)' }} /> 单元
        </span>
        <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>点击卡片查看详情</span>
      </div>
    </div>
  );
}
