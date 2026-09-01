import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { RefreshCw, Check, X, ChevronRight, AlertTriangle, Target, Anchor } from 'lucide-react';
import { api } from '@/api/client';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Modal, Select, Badge, Spinner } from '@/components/ui';
import type { FrameworkCandidate, CurrentFrameworkResponse, AssessmentAnchor } from '@/types/api';

type BuildState = 'idle' | 'building' | 'candidate' | 'done';

interface SyllabusOption {
  id: string;
  label: string;
}

export default function FrameworkPage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const courseId = routeCourseId || '';

  const { addToast } = useToastStore();

  const [published, setPublished] = useState<FrameworkCandidate | null>(null);
  const [buildState, setBuildState] = useState<BuildState>('idle');
  const [loading, setLoading] = useState(true);
  const [buildOpen, setBuildOpen] = useState(false);

  const [teachingVersions, setTeachingVersions] = useState<SyllabusOption[]>([]);
  const [assessmentVersions, setAssessmentVersions] = useState<SyllabusOption[]>([]);
  const [teachingVersionId, setTeachingVersionId] = useState('');
  const [assessmentVersionId, setAssessmentVersionId] = useState('');
  const [building, setBuilding] = useState(false);

  const [runId, setRunId] = useState<string | null>(null);
  const [candidate, setCandidate] = useState<FrameworkCandidate | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [rejecting, setRejecting] = useState(false);

  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const clearPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  const loadPublished = useCallback(async () => {
    if (!courseId) return;
    try {
      const data = await api.framework.getCurrent(courseId) as CurrentFrameworkResponse;
      if (data.published === false || !data.payload) {
        setPublished(null);
        setBuildState('idle');
      } else {
        setPublished(data.payload as unknown as FrameworkCandidate);
        setBuildState('done');
      }
    } catch {
      setPublished(null);
      setBuildState('idle');
    } finally {
      setLoading(false);
    }
  }, [courseId]);

  useEffect(() => {
    loadPublished();
    return () => clearPolling();
  }, [loadPublished, clearPolling]);

  const loadSyllabusOptions = useCallback(async () => {
    if (!courseId) return;
    try {
      const data = await api.materials.list(courseId);
      const list = Array.isArray(data) ? data : [];
      const teaching: SyllabusOption[] = [];
      const assessment: SyllabusOption[] = [];
      list.forEach((m) => {
        if (!m.latest_version) return;
        const option: SyllabusOption = {
          id: m.latest_version.id,
          label: m.logical_name + ' (v' + m.latest_version.version_no + ')',
        };
        if (m.material_type === 'teaching_syllabus') teaching.push(option);
        if (m.material_type === 'assessment_syllabus') assessment.push(option);
      });
      setTeachingVersions(teaching);
      setAssessmentVersions(assessment);
    } catch {
      addToast('加载资料列表失败', 'error');
    }
  }, [courseId, addToast]);

  const handleOpenBuild = useCallback(() => {
    setBuildOpen(true);
    setTeachingVersionId('');
    setAssessmentVersionId('');
    loadSyllabusOptions();
  }, [loadSyllabusOptions]);

  const handleBuild = async () => {
    if (!teachingVersionId || !assessmentVersionId) {
      addToast('请选择教学大纲和考核大纲', 'error');
      return;
    }
    try {
      setBuilding(true);
      setBuildState('building');
      const run = await api.framework.createRun(courseId, {
        teaching_material_version_id: teachingVersionId,
        assessment_material_version_id: assessmentVersionId,
      });
      const runObj = await run;
      if (runObj?.run_id) {
        setRunId(runObj.run_id);
      }
      setBuildOpen(false);

      if (runObj?.candidate_id && runObj?.run_id) {
        await loadCandidate(runObj.run_id);
      } else {
        startPolling();
      }
    } catch {
      addToast('构建框架失败', 'error');
      setBuildState('idle');
    } finally {
      setBuilding(false);
    }
  };

  const loadCandidate = useCallback(async (rid: string) => {
    try {
      const candidateData = await api.framework.getCandidate(courseId, rid);
      setCandidate(candidateData);
      setRunId(rid);
      setBuildState('candidate');
    } catch {
      addToast('获取候选框架失败', 'error');
      setBuildState('idle');
    }
  }, [courseId, addToast]);

  const startPolling = useCallback(() => {
    clearPolling();
    pollingRef.current = setInterval(async () => {
      try {
        const latest = await api.framework.getLatest(courseId);
        if (latest.candidate_id) {
          clearPolling();
          await loadCandidate(latest.run_id);
        } else if (latest.status === 'rejected') {
          clearPolling();
          setBuildState('idle');
          addToast('框架已被拒绝', 'info');
        } else if (latest.status === 'failed') {
          clearPolling();
          setBuildState('idle');
          addToast('框架构建失败', 'error');
        }
      } catch {
        // ignore poll errors
      }
    }, 3000);
  }, [courseId, clearPolling, loadCandidate, addToast]);

  const handleConfirm = async () => {
    if (!runId || !candidate) return;
    try {
      setConfirming(true);
      // 回传候选 anchors / exam_points；为每个 open conflict 提供教师确认的 resolution
      const openConflicts = (candidate.conflicts || []).filter((c) => c.status !== 'resolved');
      const conflictResolutions: Record<string, string> = {};
      openConflicts.forEach((c) => { conflictResolutions[c.key] = '教师确认接受'; });
      await api.framework.confirm(courseId, runId, {
        anchors: (candidate.anchors || []).map((a) => ({ ...a })),
        exam_points: candidate.exam_points || [],
        conflict_resolutions: conflictResolutions,
        teacher_exclusions: [],
      });
      addToast('框架已确认发布', 'success');
      setConfirmOpen(false);
      loadPublished();
    } catch {
      addToast('确认失败', 'error');
    } finally {
      setConfirming(false);
    }
  };

  const handleReject = async () => {
    if (!runId) return;
    try {
      setRejecting(true);
      await api.framework.reject(courseId, runId);
      addToast('框架已拒绝', 'success');
      setBuildState('idle');
      setCandidate(null);
    } catch {
      addToast('拒绝失败', 'error');
    } finally {
      setRejecting(false);
    }
  };

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
            <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em' }}>命题框架</h1>
            {buildState === 'done' && <Badge variant="success">已发布</Badge>}
            {buildState === 'candidate' && <Badge variant="info">待确认</Badge>}
            {buildState === 'building' && <Badge variant="warning">构建中</Badge>}
          </div>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)', marginTop: '6px' }}>
            根据教学大纲与考核大纲生成的课程命题规范
          </p>
        </div>
        {buildState !== 'building' && (
          <Button variant="secondary" icon={<RefreshCw size={16} />} onClick={handleOpenBuild}>
            构建新框架
          </Button>
        )}
      </div>

      {/* Building */}
      {buildState === 'building' && (
        <div className="glass-panel" style={{ padding: '64px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px' }}>
          <Spinner size="lg" />
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>正在分析资料并生成框架，请稍候...</p>
        </div>
      )}

      {/* Candidate */}
      {buildState === 'candidate' && candidate && (
        <CandidateView
          candidate={candidate}
          rejecting={rejecting}
          onReject={handleReject}
          onOpenConfirm={() => setConfirmOpen(true)}
        />
      )}

      {/* Published */}
      {buildState === 'done' && published && (
        <PublishedView candidate={published} />
      )}

      {/* Idle */}
      {buildState === 'idle' && (
        <div className="glass-panel" style={{ padding: '64px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px' }}>
          <div style={{ width: 56, height: 56, borderRadius: '18px', background: 'var(--purple-subtle)', color: 'var(--purple)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Target size={28} />
          </div>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>暂无命题框架</h3>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', maxWidth: '420px', textAlign: 'center' }}>
            命题框架是根据教学大纲和考核大纲生成的课程命题规范，包含考核范围锚点和详细考点。
          </p>
          <Button icon={<RefreshCw size={16} />} onClick={handleOpenBuild}>构建新框架</Button>
        </div>
      )}

      {/* Build Dialog */}
      <Modal
        open={buildOpen}
        onClose={() => setBuildOpen(false)}
        title="构建命题框架"
        onConfirm={handleBuild}
        confirmLabel="开始构建"
        loading={building}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            选择教学大纲和考核大纲的版本以生成命题框架。
          </p>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
            <Select
              label="教学大纲"
              value={teachingVersionId}
              onChange={(e) => setTeachingVersionId(e.target.value)}
              options={[
                { value: '', label: '选择版本' },
                ...teachingVersions.map((v) => ({ value: v.id, label: v.label })),
              ]}
            />
            <Select
              label="考核大纲"
              value={assessmentVersionId}
              onChange={(e) => setAssessmentVersionId(e.target.value)}
              options={[
                { value: '', label: '选择版本' },
                ...assessmentVersions.map((v) => ({ value: v.id, label: v.label })),
              ]}
            />
          </div>
          {(teachingVersions.length === 0 || assessmentVersions.length === 0) && (
            <p style={{ fontSize: '0.8125rem', color: 'var(--warning)' }}>
              请先在「资料库」上传并解析教学大纲与考核大纲
            </p>
          )}
        </div>
      </Modal>

      {/* Confirm Dialog */}
      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="确认命题框架"
        onConfirm={handleConfirm}
        confirmLabel="确认发布"
        loading={confirming}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
            确认后命题框架将对外发布，并用于后续的知识目录与命题蓝图阶段。
          </p>
          {candidate && (
            <div style={{ padding: '12px 16px', borderRadius: '10px', background: 'rgba(0,0,0,0.03)', fontSize: '0.875rem', display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <p>锚点数量: <span style={{ fontWeight: 600 }}>{(candidate.anchors || []).length}</span></p>
              <p>考点数量: <span style={{ fontWeight: 600 }}>{(candidate.exam_points || []).length}</span></p>
              {(candidate.conflicts || []).length > 0 && (
                <p style={{ color: 'var(--warning)' }}>警告: 存在 {(candidate.conflicts || []).length} 个冲突项，将按默认方式处理</p>
              )}
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}

// ─── Candidate View ───

function CandidateView({ candidate, rejecting, onReject, onOpenConfirm }: {
  candidate: FrameworkCandidate;
  rejecting: boolean;
  onReject: () => void;
  onOpenConfirm: () => void;
}) {
  const anchors = candidate.anchors || [];
  const points = candidate.exam_points || [];
  const conflicts = (candidate.conflicts || []).filter((c) => c.status !== 'resolved');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {conflicts.length > 0 && (
        <div className="glass-card" style={{ padding: '16px', borderLeft: '4px solid var(--warning)' }}>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '12px', color: 'var(--warning)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertTriangle size={16} /> 检测到 {conflicts.length} 个冲突
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {conflicts.map((c, i) => (
              <div key={i} style={{ padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,149,0,0.06)', fontSize: '0.875rem' }}>
                <p style={{ fontWeight: 500 }}>{c.message || c.key}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
        {/* Anchors */}
        <div className="glass-card" style={{ padding: '16px' }}>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Anchor size={16} style={{ color: 'var(--purple)' }} /> 考核范围
          </h3>
          {anchors.length === 0 ? (
            <p style={{ fontSize: '0.875rem', color: 'var(--text-tertiary)' }}>暂无锚点</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '360px', overflowY: 'auto' }}>
              {anchors.map((anchor, i) => (
                <AnchorItem key={i} anchor={anchor} />
              ))}
            </div>
          )}
        </div>

        {/* Exam points */}
        <div className="glass-card" style={{ padding: '16px' }}>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Target size={16} style={{ color: 'var(--accent)' }} /> 考点详情
          </h3>
          {points.length === 0 ? (
            <p style={{ fontSize: '0.875rem', color: 'var(--text-tertiary)' }}>暂无考点</p>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>编号</th>
                    <th>考点名称</th>
                    <th>权重</th>
                  </tr>
                </thead>
                <tbody>
                  {points.map((pt) => (
                    <tr key={pt.id}>
                      <td style={{ fontSize: '0.8125rem' }}>{pt.code}</td>
                      <td style={{ fontWeight: 500, fontSize: '0.875rem' }}>{pt.title}</td>
                      <td style={{ fontSize: '0.875rem' }}>{pt.weight_value}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Actions */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
        <Button variant="secondary" icon={<X size={16} />} loading={rejecting} onClick={onReject}>
          拒绝
        </Button>
        <Button icon={<Check size={16} />} onClick={onOpenConfirm}>
          确认
        </Button>
      </div>
    </div>
  );
}

function AnchorItem({ anchor }: { anchor: AssessmentAnchor }) {
  return (
    <div style={{ padding: '10px 12px', borderRadius: '10px', background: 'rgba(0,0,0,0.02)', display: 'flex', flexDirection: 'column', gap: '6px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <ChevronRight size={14} style={{ color: 'var(--purple)', flexShrink: 0 }} />
        <p style={{ fontWeight: 500, fontSize: '0.875rem' }}>{anchor.title}</p>
        <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>{anchor.exam_weight}%</span>
      </div>
      {(anchor.ability_requirements || []).length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginLeft: '22px' }}>
          {(anchor.ability_requirements || []).map((sub, j) => (
            <Badge key={j} variant="info">{sub}</Badge>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Published View ───

function PublishedView({ candidate }: { candidate: FrameworkCandidate }) {
  const anchors = candidate.anchors || [];
  const points = candidate.exam_points || [];

  return (
    <div className="glass-card" style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Anchors */}
      {anchors.length > 0 && (
        <div>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Anchor size={16} style={{ color: 'var(--purple)' }} /> 考核范围
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {anchors.map((anchor, i) => (
              <AnchorItem key={i} anchor={anchor} />
            ))}
          </div>
        </div>
      )}

      {/* Exam points */}
      {points.length > 0 && (
        <div>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Target size={16} style={{ color: 'var(--accent)' }} /> 考点详情
          </h3>
          <div style={{ overflowX: 'auto' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>编号</th>
                  <th>考点名称</th>
                  <th>权重</th>
                  <th>关联锚点</th>
                  <th>允许题型</th>
                </tr>
              </thead>
              <tbody>
                {points.map((pt) => (
                  <tr key={pt.id}>
                    <td style={{ fontSize: '0.8125rem' }}>{pt.code}</td>
                    <td style={{ fontWeight: 500, fontSize: '0.875rem' }}>{pt.title}</td>
                    <td style={{ fontSize: '0.875rem' }}>{pt.weight_value}%</td>
                    <td style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{pt.anchor_key || '-'}</td>
                    <td style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{(pt.allowed_question_types || []).join(', ') || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
