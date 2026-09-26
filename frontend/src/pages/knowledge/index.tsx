import { useState, useEffect, useCallback, useMemo, useRef, useDeferredValue } from 'react';
import { useParams } from 'react-router-dom';
import {
  GitBranch, Search, Network, TreePine, AlertTriangle,
  RefreshCw, Plus, BookOpen, Target, CheckCircle2, Layers,
} from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { formatConfidence } from '@/lib/format';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Select, Badge, Spinner, SkeletonTable } from '@/components/ui';
import type {
  PublishedKnowledgeResponse, KnowledgeCard, AssessmentUnit, EvidenceChunk,
  FrameworkExamPoint, KnowledgeCandidatePayload,
} from '@/types/api';
import { EvidenceRoleLabel, EvidenceRoleVariant, formatLocator, truncate } from './knowledgeShared';
import type { ViewMode, BuildState, MaterialVersionOption } from './knowledgeShared';
import { StatsBadge, FieldBlock, ViewToggle, BuildingPanel, IdlePanel } from './knowledgePanels';
import { CandidatePanel } from './CandidatePanel';
import { TreeView } from './TreeView';
import { GraphView } from './GraphView';

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
  // 最近一次「已落定」的状态（published/candidate/idle）：构建失败时回退到它，
  // 立即恢复失败前的界面，免得非得手动刷新才能看到旧目录。
  const preBuildStateRef = useRef<BuildState>('idle');

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
      setBuildState(preBuildStateRef.current);
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
    // 连续查询失败计数：run 行刚发放的瞬间可能尚未落库（短暂 404 属正常），
    // 但持续失败说明查询链路坏了——必须止损回退，否则界面永远卡在「构建中」。
    let consecutiveErrors = 0;
    pollingRef.current = setInterval(async () => {
      try {
        const runData = await api.knowledge.getRun(courseId, rid);
        consecutiveErrors = 0;
        if (runData?.status === 'awaiting_teacher_confirmation') {
          stopPolling();
          setRunId(rid);
          await loadCandidate(rid);
          addToast('知识目录构建完成，请确认', 'success');
        } else if (runData?.status === 'failed') {
          stopPolling();
          setBuildState(preBuildStateRef.current);
          addToast((runData.error_message as string) || '构建失败', 'error');
        }
      } catch (err) {
        consecutiveErrors += 1;
        if (consecutiveErrors < 10) return; // 30 秒容忍窗口（10 次 × 3 秒）
        stopPolling();
        setBuildState(preBuildStateRef.current);
        addToast(`构建状态查询失败：${getErrorMessage(err)}；请稍后刷新查看结果。`, 'error');
      }
    }, 3000);
  }, [courseId, addToast, loadCandidate, stopPolling]);

  // Load published（并恢复进行中 / 待确认的构建，刷新后进度不丢失）
  const loadPublished = useCallback(async () => {
    if (!courseId) return;
    setLoading(true);
    try {
      // 并行拉取已发布目录与最新 run，先判定目标态（published/building/candidate）
      // 再放行渲染：串行写法会先渲染旧 published 树、再等 3MB 候选加载完才切
      // 「待确认」，新页面打开必然先闪旧构建再跳新构建（2026-09-25 反馈根治）。
      const [data, latest] = await Promise.all([
        api.knowledge.getPublished(courseId).catch(() => null),
        api.knowledge.getLatest(courseId).catch(() => null),
      ]);
      if (data && data.published !== false) {
        // 后端 knowledge_cards 是 {id: card} 字典，卡片对象本身不含 id；
        // 树/图谱/详情均依赖 card.id，这里统一注入，避免点击无响应与 key 重复。
        const kc = data.knowledge_cards || {};
        const kcWithId = Object.fromEntries(
          Object.entries(kc).map(([k, v]) => [k, { ...(v as object), id: k }])
        );
        setKnowledge({ ...data, knowledge_cards: kcWithId } as PublishedKnowledgeResponse);
        preBuildStateRef.current = 'published';
      } else {
        setKnowledge(null);
        preBuildStateRef.current = 'idle';
      }
      if (latest?.status === 'running' || latest?.status === 'queued') {
        setBuildState('building');
        startPolling(latest.run_id);
      } else if (latest?.status === 'awaiting_teacher_confirmation') {
        setRunId(latest.run_id);
        // 成功切「待确认」；失败回退 preBuildStateRef（published/idle）
        await loadCandidate(latest.run_id);
      } else {
        // 无历史 run，或 run 已终态（published/failed/rejected）→ 展示基线
        setBuildState(preBuildStateRef.current);
      }
    } finally {
      setLoading(false);
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
    } catch (err) {
      // 启动失败回到基线（已发布目录/空），而不是一刀切回 idle——
      // 已有目录时回 idle 会造成「有统计条、正文却空白」的假死界面。
      setBuildState(preBuildStateRef.current);
      setBuildOpen(false);
      addToast(`启动构建失败：${getErrorMessage(err)}`, 'error');
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
      // 放弃本次候选后回到构建前的基线：旧的已发布目录若在，应立即重新展示。
      setBuildState(preBuildStateRef.current);
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
  // 已有目录（已发布或待确认候选）时，构建入口语义变为「重新构建」。
  const hasExistingCatalog = knowledge !== null || buildState === 'candidate';
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
      <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
        {/* 骨架屏贴合默认「目录树 + 表格」布局，避免加载完布局跳变 */}
        <div className="glass-card" style={{ padding: '24px' }}>
          <SkeletonTable rows={6} />
        </div>
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
          <Button
            variant="primary"
            icon={<Plus size={16} />}
            disabled={buildState === 'building' || building}
            onClick={handleOpenBuild}
          >
            {hasExistingCatalog ? '重新构建知识目录' : '构建知识目录'}
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
        title={hasExistingCatalog ? '重新构建知识目录' : '构建知识目录'}
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
                {selectedCard.grounded ? '已落地' : '未落地'}
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
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{formatConfidence(ev.confidence)}</span>
                      </div>
                      <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>{ev.content}</p>
                      <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{formatLocator(ev.locator)}</p>
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
