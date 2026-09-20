import { useState, useEffect, useCallback, useMemo, useRef, useDeferredValue, memo } from 'react';
import { useParams } from 'react-router-dom';
import {
  GitBranch, Search, Network, TreePine, ChevronRight, ChevronDown, Circle,
  AlertTriangle, Eye, RefreshCw, Plus, BookOpen, Target, CheckCircle2, Layers, Folder,
  Sparkles,
} from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Select, Badge, Spinner, ProgressPanel } from '@/components/ui';
import type {
  PublishedKnowledgeResponse, KnowledgeCard, AssessmentUnit, EvidenceChunk,
  FrameworkExamPoint, KnowledgeCandidatePayload, CandidateKnowledgeTopic,
} from '@/types/api';

// 补证据可选项（来自候选 payload 的 evidence_sources，仅 supporting/background）
interface SupplementSource {
  evidence_chunk_id: string;
  exam_point_code: string;
  relevance_class: string;
  support_claim: string;
  evidence_role: string;
  confidence: number;
  locator: Record<string, unknown>;
  content?: string;
}

type ViewMode = 'tree' | 'graph';
type BuildState = 'idle' | 'building' | 'candidate' | 'published';

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
  const [publishing, setPublishing] = useState(false);
  const [teacherExclusions, setTeacherExclusions] = useState<string[]>([]);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [selectableVersions, setSelectableVersions] = useState<MaterialVersionOption[]>([]);
  const [versionIds, setVersionIds] = useState<string[]>([]);
  const [building, setBuilding] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [candidatePayload, setCandidatePayload] = useState<KnowledgeCandidatePayload | null>(null);
  const [reviewedTopicCodes, setReviewedTopicCodes] = useState<string[]>([]);
  const [reviewedExamPointCodes, setReviewedExamPointCodes] = useState<string[]>([]);
  const [supplementOps, setSupplementOps] = useState<Array<{ operation: string; target_code: string; value: string }>>([]);

  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceChunk[]>([]);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [expandedPoints, setExpandedPoints] = useState<Set<string>>(new Set());
  const [expandedUnits, setExpandedUnits] = useState<Set<string>>(new Set());

  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Derived（用 useMemo 固定引用，保证下游 memo/记忆化真正生效）
  const examPoints: FrameworkExamPoint[] = useMemo(
    () => knowledge?.exam_points || [],
    [knowledge]
  );
  const units: AssessmentUnit[] = useMemo(() => knowledge?.units || [], [knowledge]);
  const cardsDict: Record<string, KnowledgeCard> = useMemo(
    () => knowledge?.knowledge_cards || {},
    [knowledge]
  );

  // 搜索输入延迟渲染：输入框立即回显，繁重的树/图谱按浏览器空闲时更新，
  // 避免大目录下每敲一个字符全页卡顿。
  const deferredSearch = useDeferredValue(searchQuery);

  const clusters = useMemo(() => {
    const set = new Set<string>();
    Object.values(cardsDict).forEach((c) => set.add(c.concept_cluster));
    return Array.from(set).sort();
  }, [cardsDict]);

  const filteredCards = useMemo(() => {
    let cards = Object.values(cardsDict) as KnowledgeCard[];
    if (deferredSearch.trim()) {
      const q = deferredSearch.toLowerCase();
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
  }, [cardsDict, deferredSearch, filterCluster, filterGrounded]);

  const filteredCardIds = useMemo(() => new Set(filteredCards.map((c) => c.id)), [filteredCards]);

  // 候选就绪：拉取候选、同步已审阅的 topic / exam point，进入待确认态
  const loadCandidate = useCallback(async (rid: string) => {
    try {
      const candidate = await api.knowledge.getCandidate(courseId, rid);
      const payload = (candidate as Record<string, unknown>).payload as Record<string, unknown> | undefined;
      if (payload) {
        // 保存完整候选数据供预览渲染（topics → units → cards）
        setCandidatePayload(payload as unknown as KnowledgeCandidatePayload);
        const topics = (payload.topics || []) as Array<{ code: string; status: string }>;
        topics.forEach((t) => {
          if (t.status === 'active') {
            setReviewedTopicCodes((prev) => prev.includes(t.code) ? prev : [...prev, t.code]);
          }
        });
        const coverage = (payload.coverage || []) as Array<{ exam_point_code: string; status: string }>;
        coverage.forEach((c) => {
          if (c.status === 'sufficient') {
            setReviewedExamPointCodes((prev) => prev.includes(c.exam_point_code) ? prev : [...prev, c.exam_point_code]);
          }
        });
      }
      setBuildState('candidate');
      setBuildOpen(false);
    } catch {
      addToast('获取候选知识目录失败', 'error');
      setBuildState('idle');
    }
  }, [courseId, addToast]);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  const startPolling = useCallback((rid: string) => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    pollingRef.current = setInterval(async () => {
      try {
        const runData = await api.knowledge.getRun(courseId, rid);
        if (runData?.status === 'awaiting_teacher_confirmation') {
          stopPolling();
          setRunId(rid);
          await loadCandidate(rid);
          addToast('知识目录构建完成，请确认', 'success');
        } else if (runData?.status === 'failed') {
          stopPolling();
          setBuildState('idle');
          setBuildOpen(false);
          addToast((runData.error_message as string) || '构建失败', 'error');
        }
      } catch { /* ignore */ }
    }, 3000);
  }, [courseId, addToast, loadCandidate, stopPolling]);

  // Load published（并恢复进行中 / 待确认的构建，刷新后进度不丢失）
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
    try {
      const latest = await api.knowledge.getLatest(courseId);
      if (latest) {
        if (latest.status === 'running' || latest.status === 'queued') {
          setBuildState('building');
          startPolling(latest.run_id);
        } else if (latest.status === 'awaiting_teacher_confirmation') {
          setRunId(latest.run_id);
          await loadCandidate(latest.run_id);
        }
      }
    } catch {
      // 无历史 run 时忽略
    }
  }, [courseId, startPolling, loadCandidate]);

  useEffect(() => {
    loadPublished();
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, [loadPublished]);

  // Build flow
  const handleOpenBuild = useCallback(async () => {
    // 知识目录基于「已发布命题框架」组织考点，先校验框架已发布。
    // 兼容后端两种返回：显式 published 字段，或旧版返回的 status='published'。
    try {
      const framework = await api.framework.getCurrent(courseId) as { published?: boolean; status?: string };
      const isPublished = framework?.published || framework?.status === 'published';
      if (!isPublished) {
        addToast('请先构建并发布命题框架，再构建知识目录', 'error');
        return;
      }
    } catch {
      addToast('请先构建并发布命题框架，再构建知识目录', 'error');
      return;
    }
    setBuildOpen(true);
    try {
      const data = await api.materials.list(courseId);
      const list = Array.isArray(data) ? data : [];
      const versions: MaterialVersionOption[] = [];
      list.forEach((m) => {
        // 知识目录的考点/锚点来自已发布命题框架，教学资料只作为证据来源。
        // 因此只允许选择教学资料/习题，排除教学大纲与考核大纲（大纲已用于生成框架）。
        if (m.material_type !== 'teaching_material' && m.material_type !== 'exercise') return;
        if (!m.latest_version) return;
        if (m.parse_status?.status !== 'ready') return;
        versions.push({
          id: m.latest_version.id,
          name: m.logical_name || '未命名',
          type: m.material_type,
          version: 'v' + m.latest_version.version_no,
        });
      });
      setSelectableVersions(versions);
    } catch {
      addToast('加载资料列表失败', 'error');
    }
    setVersionIds([]);
  }, [courseId, addToast]);

  const handleBuild = useCallback(async () => {
    if (versionIds.length === 0) {
      addToast('请至少选择一个资料版本', 'error');
      return;
    }
    try {
      setBuilding(true);
      setBuildState('building');
      // 点击确认后立即关闭弹窗，回到主界面查看构建进度
      setBuildOpen(false);
      const run = await api.knowledge.createOrganizationRun(courseId, {
        material_version_ids: versionIds,
      });
      setRunId(run.run_id);
      if (run.status === 'awaiting_teacher_confirmation' && run.candidate_id) {
        await loadCandidate(run.run_id);
        addToast('知识目录构建完成，请确认', 'success');
      } else {
        startPolling(run.run_id);
        addToast('知识目录构建中，请稍候...', 'info');
      }
    } catch {
      addToast('启动构建失败', 'error');
      setBuildState('idle');
    } finally {
      setBuilding(false);
    }
  }, [courseId, versionIds, startPolling, loadCandidate, addToast]);

  const handlePublish = useCallback(async (autoSupplement = false) => {
    if (!runId) return;
    setPublishing(true);
    try {
      await api.knowledge.publish(courseId, runId, {
        operations: supplementOps,
        reviewed_topic_codes: reviewedTopicCodes,
        // 补证据的考点由教师逐点挑选并确认过证据，视为已完成审阅。
        reviewed_exam_point_codes: [
          ...new Set([...reviewedExamPointCodes, ...supplementOps.map((op) => op.target_code)]),
        ],
        teacher_exclusions: teacherExclusions,
        // 一键补证据：后端为所有覆盖不足考点自动应用 AI 推荐的证据改判。
        auto_supplement_direct_evidence: autoSupplement,
      });
      addToast(
        autoSupplement && supplementOps.length === 0
          ? '已按 AI 推荐自动补充证据并发布'
          : teacherExclusions.length > 0
            ? `知识目录已发布（已排除 ${teacherExclusions.length} 个考点）`
            : '知识目录已发布',
        'success'
      );
      setBuildState('published');
      setCandidatePayload(null);
      setSupplementOps([]);
      setTeacherExclusions([]);
      loadPublished();
    } catch (err) {
      addToast(`发布失败：${getErrorMessage(err)}`, 'error');
    } finally {
      setPublishing(false);
    }
  }, [courseId, runId, loadPublished, addToast, reviewedTopicCodes, reviewedExamPointCodes, supplementOps, teacherExclusions]);

  const handleReject = useCallback(async () => {
    if (!runId) return;
    setRejecting(true);
    try {
      await api.knowledge.reject(courseId, runId);
      addToast('已放弃该知识目录，候选已标记为驳回', 'success');
      setBuildState('idle');
      setRunId(null);
      setCandidatePayload(null);
      setReviewedTopicCodes([]);
      setReviewedExamPointCodes([]);
    } catch (err) {
      addToast(`取消失败：${getErrorMessage(err)}`, 'error');
    } finally {
      setRejecting(false);
      setRejectOpen(false);
    }
  }, [courseId, runId, addToast]);

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
  const selectedUnit = selectedCardId ? units.find((u) => u.card_ids.includes(selectedCardId)) : undefined;
  const selectedPoint = selectedUnit?.exam_point_id
    ? examPoints.find((p) => p.id === selectedUnit.exam_point_id)
    : undefined;

  const assessableText = useMemo(() => {
    if (!selectedCard) return '';
    const v = selectedCard.assessable_content;
    if (Array.isArray(v)) return v.join('\n');
    return typeof v === 'string' ? v : '';
  }, [selectedCard]);

  const scopeText = useMemo(() => {
    if (!selectedCard) return '';
    const sb = selectedCard.scope_boundary;
    if (!sb || typeof sb !== 'object') return '';
    const parts: string[] = [];
    Object.entries(sb).forEach(([k, val]) => {
      if (val === null || val === undefined || val === '' || (Array.isArray(val) && val.length === 0)) return;
      parts.push(Array.isArray(val) ? String(k) + '：' + val.join('、') : String(k) + '：' + String(val));
    });
    return parts.join('；');
  }, [selectedCard]);

  const cardCognitiveTargets = (selectedCard?.cognitive_targets || []).length > 0
    ? selectedCard!.cognitive_targets
    : (selectedPoint?.cognitive_targets || []);
  const cardQuestionTypes = (selectedCard?.allowed_question_types || []).length > 0
    ? selectedCard!.allowed_question_types
    : (selectedPoint?.allowed_question_types || []);

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
        <CandidatePanel
          candidate={candidatePayload}
          courseId={courseId}
          runId={runId}
          supplementOps={supplementOps}
          onSupplementChange={setSupplementOps}
          teacherExclusions={teacherExclusions}
          onTeacherExclusionsChange={setTeacherExclusions}
          onPublish={handlePublish}
          publishing={publishing}
          onReset={() => setRejectOpen(true)}
        />
      )}

      <Modal
        open={rejectOpen}
        onClose={() => { if (!rejecting) setRejectOpen(false); }}
        title="放弃本次知识目录构建？"
        maxWidth="480px"
        footer={
          <>
            <Button variant="secondary" disabled={rejecting} onClick={() => setRejectOpen(false)}>
              返回
            </Button>
            <Button variant="danger" disabled={rejecting} onClick={handleReject}>
              {rejecting ? '正在取消...' : '确认放弃'}
            </Button>
          </>
        }
      >
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
          放弃后，本次知识目录候选将被标记为「已驳回」，无法再次发布；如需重新生成需重新构建。
          该操作不会删除教学资料。
        </p>
      </Modal>
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
            知识目录将基于已发布的命题框架组织考点，以下教学资料/习题作为证据来源（教学大纲与考核大纲无需重复选择，可多选）：
          </p>
          {selectableVersions.length === 0 ? (
            <p style={{ fontSize: '0.875rem', color: 'var(--text-tertiary)', padding: '24px 0', textAlign: 'center' }}>
              暂无已解析的教学资料，请先前往「资料库」上传并解析教学资料或习题。
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
                  已选 {versionIds.length} / {selectableVersions.length}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    const all = selectableVersions.every((v) => versionIds.includes(v.id));
                    setVersionIds(all ? [] : selectableVersions.map((v) => v.id));
                  }}
                  style={{
                    background: 'none', border: 'none', cursor: 'pointer',
                    fontSize: '0.8125rem', fontWeight: 500, color: 'var(--accent)',
                    padding: '4px 6px', borderRadius: '6px',
                  }}
                >
                  {selectableVersions.length > 0 && selectableVersions.every((v) => versionIds.includes(v.id))
                    ? '取消全选'
                    : '全选'}
                </button>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '300px', overflowY: 'auto' }}>
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
            {/* 归属上下文 */}
            {(selectedUnit || selectedPoint) && (
              <div style={{
                padding: '12px 14px', borderRadius: '12px',
                background: 'rgba(0,113,227,0.05)', border: '1px solid rgba(0,113,227,0.12)',
                display: 'flex', flexDirection: 'column', gap: '5px',
              }}>
                <span style={{ fontSize: '0.7rem', fontWeight: 600, color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  归属位置
                </span>
                {selectedPoint && (
                  <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    考点 <span style={{ fontWeight: 600, color: 'var(--text)' }}>{selectedPoint.code}</span> · {selectedPoint.title}
                    {selectedPoint.assessment_requirement && (
                      <span style={{ display: 'block', marginTop: '2px', color: 'var(--text-tertiary)', fontSize: '0.75rem' }}>
                        {truncate(selectedPoint.assessment_requirement, 90)}
                      </span>
                    )}
                  </p>
                )}
                {selectedUnit && (
                  <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    单元 <span style={{ fontWeight: 600, color: 'var(--text)' }}>{selectedUnit.code}</span> · {selectedUnit.title}
                  </p>
                )}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <Badge variant={selectedCard.grounded ? 'success' : 'error'}>
                {selectedCard.grounded ? '已着陆' : '未着陆'}
              </Badge>
              {!!selectedCard.importance && (
                <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>重要性: {selectedCard.importance}</span>
              )}
              {!!selectedCard.concept_cluster && (
                <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>簇: {selectedCard.concept_cluster}</span>
              )}
            </div>
            {selectedCard.performance_statement && (
              <FieldBlock label="性能表述" content={selectedCard.performance_statement} />
            )}
            {assessableText ? (
              <FieldBlock label="可考核内容" content={assessableText} />
            ) : (
              <FieldBlock muted label="可考核内容" content="（暂无明确可考核内容，可基于性能表述与证据链推导命题范围）" />
            )}
            {selectedCard.answer_proposition ? (
              <FieldBlock label="答案命题" content={selectedCard.answer_proposition} />
            ) : (
              <FieldBlock muted label="答案命题" content="（暂未生成独立答案命题，出题时以可考核内容与性能表述为准）" />
            )}
            {scopeText && <FieldBlock label="范围边界" content={scopeText} />}
            {cardCognitiveTargets.length > 0 && (
              <div>
                <h4 style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '8px' }}>认知目标</h4>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                  {cardCognitiveTargets.map((t, i) => (
                    <Badge key={i} variant="info">{t}</Badge>
                  ))}
                </div>
              </div>
            )}
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
              允许题型: {cardQuestionTypes.join(', ') || '不限'}
            </p>
            {(selectedCard.prompt_material || []).length > 0 && (
              <FieldBlock label="命题素材" content={(selectedCard.prompt_material || []).join('\n')} />
            )}
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

function FieldBlock({ label, content, muted }: { label: string; content: string; muted?: boolean }) {
  return (
    <div>
      <h4 style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '6px' }}>{label}</h4>
      <p style={{
        fontSize: '0.875rem', lineHeight: 1.6, whiteSpace: 'pre-wrap',
        color: muted ? 'var(--text-tertiary)' : 'var(--text)',
        fontStyle: muted ? 'italic' : undefined,
      }}>{content}</p>
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
    <ProgressPanel
      title="正在构建知识目录，请稍候…"
      messages={[
        '正在组织资料与考点…',
        '正在检索证据与落地关系…',
        '正在生成知识卡片…',
        '正在校验目录一致性…',
      ]}
    />
  );
}

function CandidatePanel({ candidate, courseId, runId, supplementOps, onSupplementChange, teacherExclusions, onTeacherExclusionsChange, onPublish, publishing, onReset }: {
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
      const conf = typeof s.confidence === 'number' ? `置信${s.confidence}` : '';
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
                  style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 12px', borderRadius: '10px', background: 'rgba(0,0,0,0.02)', flexWrap: 'wrap' }}
                >
                  <div style={{ flex: '1 1 260px', minWidth: 0 }}>
                    <div style={{ fontSize: '0.875rem', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{pointLabels.get(code) || code}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{code}</div>
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
                        置信度 {suppPreview.confidence}
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

const TreeView = memo(function TreeView(props: {
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
                              <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '2px' }}>
                                <span style={{ fontSize: '0.875rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{card.name}</span>
                                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  {truncate(card.performance_statement || '暂无性能表述', 46)}
                                </span>
                              </div>
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
});

// ─── Graph View ───

const GRAPH_W = 960;
const GRAPH_H = 640;
const GRAPH_CX = GRAPH_W / 2;
const GRAPH_CY = GRAPH_H / 2;
const POINT_R = 282;
const UNIT_R = 188;
const GRAPH_PALETTE = ['#0071e3', '#0ea5e9', '#10b981', '#f59e0b', '#af52de', '#ff2d55', '#ff3b30', '#14b8a6'];

function hashStr(s: string | null | undefined): number {
  if (!s) return 0;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

function truncate(s: string | null | undefined, n: number): string {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
}

interface GraphNode {
  key: string;
  kind: 'point' | 'unit' | 'card';
  x: number;
  y: number;
  angle: number;
  r: number;
  label: string;
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

const GraphView = memo(function GraphView(props: {
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
  const panStart = useRef({ x: 0, y: 0 });
  const movedRef = useRef(false);
  zoomRef.current = zoom;
  panRef.current = pan;

  const unitOfCard = useMemo(() => {
    const m = new Map<string, AssessmentUnit>();
    units.forEach((u) => u.card_ids.forEach((cid) => m.set(cid, u)));
    return m;
  }, [units]);

  // 分层"太阳系"布局：考点外环 → 单元中环（同考点同角度微偏移）→ 卡片簇绕所属单元
  const layout = useMemo(() => {
    const pointAngles = new Map<string, number>();
    const pointNodes: GraphNode[] = [];
    examPoints.forEach((p, i) => {
      const a = -Math.PI / 2 + (Math.PI * 2 * i) / Math.max(examPoints.length, 1);
      pointAngles.set(p.id, a);
      pointNodes.push({
        key: 'p-' + p.id, kind: 'point',
        x: GRAPH_CX + POINT_R * Math.cos(a),
        y: GRAPH_CY + POINT_R * Math.sin(a),
        angle: a, r: 21,
        label: String(p.code || '').replace(/^SK3020-/, ''),
        sub: (p.weight_value ?? 0) + '%',
        color: '#0071e3', grounded: true,
      });
    });
    const unitCountByPoint = new Map<string, number>();
    units.forEach((u) => unitCountByPoint.set(u.exam_point_id, (unitCountByPoint.get(u.exam_point_id) || 0) + 1));
    const unitIdx = new Map<string, number>();
    const unitNodes: GraphNode[] = [];
    units.forEach((u) => {
      const base = pointAngles.get(u.exam_point_id) ?? 0;
      const idx = unitIdx.get(u.exam_point_id) || 0;
      unitIdx.set(u.exam_point_id, idx + 1);
      const total = unitCountByPoint.get(u.exam_point_id) || 1;
      const spread = Math.min(0.2, (Math.PI * 2 / Math.max(examPoints.length, 1)) * 0.8);
      const a = base + (total > 1 ? (idx - (total - 1) / 2) * spread : 0);
      unitNodes.push({
        key: 'u-' + u.unit_id, kind: 'unit',
        x: GRAPH_CX + UNIT_R * Math.cos(a),
        y: GRAPH_CY + UNIT_R * Math.sin(a),
        angle: a, r: 14,
        label: String(u.code || ''), sub: String(u.card_ids?.length || 0) + '卡',
        color: '#af52de', grounded: true, unitId: u.unit_id,
      });
    });
    const unitNodeById = new Map(unitNodes.map((n) => [n.unitId, n]));
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
    const cardNodes: GraphNode[] = [];
    cardsByUnit.forEach((list, uid) => {
      const un = unitNodeById.get(uid);
      if (!un) return;
      const n = list.length;
      const ring = 40 + Math.min(9, n) * 7;
      const spread = Math.min(0.6, (Math.PI * 2) / Math.max(n, 1) * 0.85);
      list.forEach((c, i) => {
        const a = un.angle + (i - (n - 1) / 2) * spread;
        cardNodes.push({
          key: 'c-' + c.id, kind: 'card',
          x: un.x + ring * Math.cos(a),
          y: un.y + ring * Math.sin(a),
          angle: a,
          r: 5 + (c.importance || 1) * 2.4,
          label: String(c.name || ''), sub: '',
          color: c.concept_cluster
            ? GRAPH_PALETTE[hashStr(c.concept_cluster) % GRAPH_PALETTE.length]
            : GRAPH_PALETTE[hashStr(uid) % GRAPH_PALETTE.length],
          grounded: c.grounded, cardId: c.id,
        });
      });
    });
    // 未归属单元的卡片散落在中心区域
    if (orphans.length > 0) {
      orphans.forEach((c, i) => {
        const a = -Math.PI / 2 + (Math.PI * 2 * i) / Math.max(orphans.length, 1);
        cardNodes.push({
          key: 'c-' + c.id, kind: 'card',
          x: GRAPH_CX + 36 * Math.cos(a),
          y: GRAPH_CY + 36 * Math.sin(a),
          angle: a,
          r: 5 + (c.importance || 1) * 2.4,
          label: c.name, sub: '',
          color: c.concept_cluster
            ? GRAPH_PALETTE[hashStr(c.concept_cluster) % GRAPH_PALETTE.length]
            : GRAPH_PALETTE[hashStr(c.id) % GRAPH_PALETTE.length],
          grounded: c.grounded, cardId: c.id,
        });
      });
    }
    return { pointNodes, unitNodes, cardNodes };
  }, [examPoints, units, cards, unitOfCard]);

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
      const rect = svg.getBoundingClientRect();
      const factor = e.deltaY > 0 ? 0.88 : 1.12;
      const z0 = zoomRef.current;
      const z1 = Math.min(2.4, Math.max(0.3, z0 * factor));
      const p0 = panRef.current;
      const m = {
        x: ((e.clientX - rect.left) / rect.width) * GRAPH_W,
        y: ((e.clientY - rect.top) / rect.height) * GRAPH_H,
      };
      const k = z1 / z0;
      setPan({ x: m.x - (m.x - p0.x) * k, y: m.y - (m.y - p0.y) * k });
      setZoom(z1);
    };
    svg.addEventListener('wheel', onWheel, { passive: false });
    return () => svg.removeEventListener('wheel', onWheel);
  }, []);

  const resetView = useCallback(() => {
    setZoom(0.8);
    setPan({ x: 0, y: 0 });
    setDraggedPos({});
  }, []);

  const toCanvas = useCallback((clientX: number, clientY: number) => {
    const rect = svgRef.current!.getBoundingClientRect();
    const vx = ((clientX - rect.left) / rect.width) * GRAPH_W;
    const vy = ((clientY - rect.top) / rect.height) * GRAPH_H;
    const z = zoomRef.current;
    const p = panRef.current;
    return {
      x: (vx - (GRAPH_CX + p.x)) / z + GRAPH_CX,
      y: (vy - (GRAPH_CY + p.y)) / z + GRAPH_CY,
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
      setPan({ x: e.clientX - panStart.current.x, y: e.clientY - panStart.current.y });
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
    panStart.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
    setPanning(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const transform = `translate(${GRAPH_CX + pan.x} ${GRAPH_CY + pan.y}) scale(${zoom}) translate(${-GRAPH_CX} ${-GRAPH_CY})`;

  const allNodes = useMemo(
    () => [...layout.pointNodes, ...layout.unitNodes, ...layout.cardNodes],
    [layout]
  );
  void allNodes;

  const edgeStyle = (kind: string) => {
    if (kind === 'specializes' || kind === 'requires') return { stroke: '#0071e3', width: 1.6, dash: undefined, opacity: 0.75, marker: true };
    if (kind === 'contrasts') return { stroke: '#af52de', width: 1.3, dash: '5 4', opacity: 0.7, marker: false };
    if (kind === 'equivalent') return { stroke: '#10b981', width: 2.6, dash: undefined, opacity: 0.7, marker: false };
    if (kind === 'card-unit') return { stroke: 'rgba(0,0,0,0.10)', width: 1, dash: undefined, opacity: 1, marker: false };
    if (kind === 'unit-point') return { stroke: 'rgba(0,0,0,0.16)', width: 1.2, dash: undefined, opacity: 1, marker: false };
    return { stroke: 'rgba(0,0,0,0.22)', width: 1.2, dash: undefined, opacity: 0.7, marker: false };
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
          style={{ width: '100%', maxHeight: '620px', cursor: panning ? 'grabbing' : 'grab', touchAction: 'none' }}
          onPointerMove={onSvgPointerMove}
          onPointerUp={onSvgPointerUp}
          onPointerCancel={onSvgPointerUp}
          onPointerDown={onBgPointerDown}
        >
          <style>{`
            .gb { animation: gbFloat 3.6s ease-in-out infinite; }
            @keyframes gbFloat { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-2.5px); } }
          `}</style>
          <defs>
            <marker id="gh-arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <path d="M0,0 L8,3 L0,6 Z" fill="#0071e3" />
            </marker>
          </defs>
          <g transform={transform}>
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

            {/* 考点节点 */}
            {layout.pointNodes.map((n) => {
              const p = nodePos(n);
              const dim = isDimmed(n.key);
              return (
                <g
                  key={n.key}
                  opacity={dim ? 0.12 : 1}
                  onPointerEnter={() => setHoverKey(n.key)}
                  onPointerLeave={() => setHoverKey(null)}
                  onPointerDown={(e) => onNodePointerDown(e, n)}
                  style={{ cursor: 'grab' }}
                >
                  <g className="gb" style={{ animationDelay: (hashStr(n.key) % 20) / 10 + 's' }}>
                    <circle cx={p.x} cy={p.y} r={n.r + (hoverKey === n.key ? 3 : 0)} fill="#fff" stroke="#0071e3" strokeWidth={1.6} style={{ transition: 'r 0.15s' }} />
                    <circle cx={p.x} cy={p.y} r={n.r + (hoverKey === n.key ? 3 : 0)} fill="rgba(0,113,227,0.06)" style={{ transition: 'r 0.15s' }} />
                    <text x={p.x} y={p.y + 3} textAnchor="middle" fontSize="9" fontWeight="700" fill="#0071e3" style={{ pointerEvents: 'none' }}>{n.label}</text>
                    <text x={p.x} y={p.y + 15} textAnchor="middle" fontSize="7.5" fill="var(--text-tertiary)" style={{ pointerEvents: 'none' }}>{n.sub}</text>
                  </g>
                </g>
              );
            })}

            {/* 单元节点 */}
            {layout.unitNodes.map((n) => {
              const p = nodePos(n);
              const dim = isDimmed(n.key);
              return (
                <g
                  key={n.key}
                  opacity={dim ? 0.12 : 1}
                  onPointerEnter={() => setHoverKey(n.key)}
                  onPointerLeave={() => setHoverKey(null)}
                  onPointerDown={(e) => onNodePointerDown(e, n)}
                  style={{ cursor: 'grab' }}
                >
                  <g className="gb" style={{ animationDelay: (hashStr(n.key) % 20) / 10 + 's' }}>
                    <circle cx={p.x} cy={p.y} r={n.r + (hoverKey === n.key ? 2.5 : 0)} fill="#fff" stroke="#af52de" strokeWidth={1.4} style={{ transition: 'r 0.15s' }} />
                    <circle cx={p.x} cy={p.y} r={n.r + (hoverKey === n.key ? 2.5 : 0)} fill="rgba(175,82,222,0.07)" style={{ transition: 'r 0.15s' }} />
                    <text x={p.x} y={p.y + 2.5} textAnchor="middle" fontSize="8" fontWeight="600" fill="#af52de" style={{ pointerEvents: 'none' }}>{n.label}</text>
                    <text x={p.x} y={p.y + 12} textAnchor="middle" fontSize="7" fill="var(--text-tertiary)" style={{ pointerEvents: 'none' }}>{n.sub}</text>
                  </g>
                </g>
              );
            })}

            {/* 卡片节点 */}
            {layout.cardNodes.map((n) => {
              const p = nodePos(n);
              const dim = isDimmed(n.key);
              const hovered = hoverKey === n.key;
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
                  <g className="gb" style={{ animationDelay: (hashStr(n.key) % 24) / 10 + 's' }}>
                    <circle
                      cx={p.x} cy={p.y}
                      r={n.r + (hovered ? 3 : 0)}
                      fill={n.color}
                      fillOpacity={n.grounded ? 0.9 : 0.3}
                      stroke={n.grounded ? n.color : '#ff3b30'}
                      strokeWidth={n.grounded ? 0 : 1.8}
                      strokeDasharray={n.grounded ? undefined : '3 2'}
                      style={{ transition: 'r 0.15s, fill-opacity 0.15s' }}
                    />
                    {hovered && (
                      <text x={p.x} y={p.y - n.r - 6} textAnchor="middle" fontSize="8.5" fontWeight="600" fill="var(--text)"
                        style={{ paintOrder: 'stroke', stroke: '#fff', strokeWidth: 3, strokeLinejoin: 'round', pointerEvents: 'none' }}>
                        {truncate(n.label, 16)}
                      </text>
                    )}
                    {!hovered && (
                      <text x={p.x} y={p.y + n.r + 11} textAnchor="middle" fontSize="7.5" fill="var(--text-secondary)" style={{ pointerEvents: 'none' }}>
                        {truncate(n.label, 8)}
                      </text>
                    )}
                  </g>
                </g>
              );
            })}
          </g>
        </svg>

        {/* 缩放控件 */}
        <div style={{ position: 'absolute', right: 10, top: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {[
            { label: '+', fn: () => setZoom((z) => Math.min(2.4, z * 1.25)) },
            { label: '−', fn: () => setZoom((z) => Math.max(0.3, z * 0.8)) },
            { label: '⟲', fn: resetView },
          ].map((b) => (
            <button
              key={b.label}
              onClick={b.fn}
              style={{
                width: 30, height: 30, borderRadius: 8, border: '1px solid rgba(0,0,0,0.08)',
                background: 'rgba(255,255,255,0.9)', boxShadow: '0 1px 4px rgba(0,0,0,0.08)',
                cursor: 'pointer', fontSize: '0.9rem', color: 'var(--text-secondary)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              {b.label}
            </button>
          ))}
        </div>
        <div style={{ position: 'absolute', left: 10, top: 10, fontSize: '0.72rem', color: 'var(--text-tertiary)', background: 'rgba(255,255,255,0.75)', padding: '4px 8px', borderRadius: 6, border: '1px solid rgba(0,0,0,0.05)' }}>
          滚轮缩放 · 拖拽画布平移 · 拖动节点调整 · 点击卡片看详情
        </div>
      </div>

      {/* Legend */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', padding: '12px 8px 0', flexWrap: 'wrap' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', border: '2px dashed #ff3b30', background: 'rgba(255,59,48,0.15)' }} /> 未落地
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#0071e3' }} /> 已落地卡
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#fff', border: '1.5px solid #0071e3' }} /> 考点
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 12, height: 12, borderRadius: '50%', background: '#fff', border: '1.5px solid #af52de' }} /> 单元
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span style={{ width: 18, height: 0, borderTop: '2px solid #0071e3' }} /> 卡片关系
        </span>
      </div>
    </div>
  );
});
