import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { RefreshCw, Check, X, ChevronRight, ChevronDown, AlertTriangle, Target } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Modal, Badge, Spinner, ProgressPanel } from '@/components/ui';
import type { FrameworkCandidate, CurrentFrameworkResponse, AssessmentAnchor, FrameworkExamPoint } from '@/types/api';

type BuildState = 'idle' | 'building' | 'candidate' | 'done';

interface SyllabusOption {
  id: string;
  label: string;
  /** 大纲该版本未解析完成（status !== 'ready'）时不可选 */
  disabled?: boolean;
}

/**
 * 自定义下拉：未解析(disabled)的选项置灰不可点，hover 时在右侧浮出
 * “未解析”小提示，满足“能查到是否解析、未解析不能选”的要求。
 * 原生 <select> 无法对 disabled option 提供悬停提示，故自绘。
 */
function SyllabusSelect({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: SyllabusOption[];
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((o) => o.id === value);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', position: 'relative', minWidth: 0, maxWidth: '100%', overflow: 'hidden' }}>
      <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px',
          width: '100%', maxWidth: '100%', minWidth: 0, padding: '10px 12px', borderRadius: '10px',
          background: 'var(--surface)', border: '1px solid rgba(0,0,0,0.1)',
          fontSize: '0.875rem', color: selected ? 'var(--text-primary)' : 'var(--text-tertiary)',
          cursor: 'pointer', textAlign: 'left', overflow: 'hidden',
        }}
      >
        <span
          title={selected?.label}
          style={{ flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {selected ? selected.label : '选择版本'}
        </span>
        <ChevronDown size={16} style={{ flexShrink: 0, opacity: 0.6, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 30, marginTop: 4,
          background: 'var(--surface)', border: '1px solid rgba(0,0,0,0.1)',
          borderRadius: '10px', boxShadow: '0 10px 30px rgba(0,0,0,0.12)', padding: 4,
          maxHeight: 220, overflowY: 'auto',
        }}>
          {options.map((o) => (
            <div
              key={o.id}
              onMouseDown={(e) => {
                e.preventDefault();
                if (o.disabled) return;
                onChange(o.id);
                setOpen(false);
              }}
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
                padding: '9px 10px', borderRadius: 8, cursor: o.disabled ? 'not-allowed' : 'pointer',
                background: value === o.id ? 'rgba(0,113,227,0.08)' : 'transparent',
                color: o.disabled ? 'var(--text-tertiary)' : 'var(--text-primary)',
                fontSize: '0.8125rem', opacity: o.disabled ? 0.7 : 1, overflow: 'hidden',
              }}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{o.label}</span>
              {o.disabled && (
                <span
                  title="未解析"
                  style={{
                    flexShrink: 0, fontSize: '0.6875rem', padding: '2px 6px', borderRadius: 6,
                    background: 'rgba(0,0,0,0.06)', color: 'var(--text-tertiary)',
                  }}
                >未解析</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
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

  const loadCandidate = useCallback(async (rid: string) => {
    if (!rid) return;
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
        if (latest.status === 'awaiting_teacher_confirmation' || latest.status === 'published') {
          clearPolling();
          await loadCandidate(latest.run_id);
        } else if (latest.status === 'rejected') {
          clearPolling();
          setBuildState('idle');
          addToast('框架已被拒绝', 'info');
        } else if (latest.status === 'failed' || latest.status === 'cancelled') {
          clearPolling();
          setBuildState('idle');
          addToast('框架构建失败', 'error');
        }
      } catch {
        // ignore poll errors
      }
    }, 3000);
  }, [courseId, clearPolling, loadCandidate, addToast]);

  const loadPublished = useCallback(async () => {
    if (!courseId) return;
    let hasContent = false;
    try {
      const data = await api.framework.getCurrent(courseId) as CurrentFrameworkResponse;
      if (data.payload) {
        hasContent = true;
        if (data.published) {
          setPublished(data.payload as unknown as FrameworkCandidate);
          setBuildState('done');
        } else {
          // 未确认草稿：恢复候选视图，教师可继续确认/驳回，避免重复构建浪费算力
          setCandidate(data.payload as unknown as FrameworkCandidate);
          setRunId(data.run_id ?? null);
          setBuildState('candidate');
        }
      }
    } catch {
      setPublished(null);
    }
    if (!hasContent) {
      setPublished(null);
      setBuildState('idle');
      // 刷新后恢复仍在进行的构建：进度不丢失，继续轮询直到候选就绪
      try {
        const latest = await api.framework.getLatest(courseId);
        if (latest && (latest.status === 'running' || latest.status === 'queued')) {
          setBuildState('building');
          startPolling();
        }
      } catch {
        // 无历史 run 时忽略
      }
    }
    setLoading(false);
  }, [courseId, startPolling]);

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
          // 只有解析完成(ready)的大纲才能用于构建框架，未解析/解析中/失败均不可选
          disabled: m.parse_status?.status !== 'ready',
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
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
      setBuildState('idle');
    } finally {
      setBuilding(false);
    }
  };

  const handleConfirm = async () => {
    if (!runId || !candidate) return;
    try {
      setConfirming(true);
      // 只需裁决 blocking 冲突；advisory（教学深度提示）以考核大纲为准，无需处理
      const blockingConflicts = (candidate.conflicts || []).filter(
        (c) => c.status !== 'resolved' && (c.severity ?? 'blocking') === 'blocking',
      );
      const conflictResolutions: Record<string, string> = {};
      blockingConflicts.forEach((c) => { conflictResolutions[c.key] = '教师确认接受'; });
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
        <ProgressPanel
          title="正在分析资料并生成框架，请稍候…"
          messages={[
            '正在解析教学大纲与考核大纲…',
            '正在提取考核范围锚点…',
            '正在生成考点与能力要求…',
            '正在校验框架一致性…',
          ]}
        />
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
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '12px' }}>
            <SyllabusSelect
              label="教学大纲"
              value={teachingVersionId}
              options={teachingVersions}
              onChange={setTeachingVersionId}
            />
            <SyllabusSelect
              label="考核大纲"
              value={assessmentVersionId}
              options={assessmentVersions}
              onChange={setAssessmentVersionId}
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
  const allConflicts = (candidate.conflicts || []).filter((c) => c.status !== 'resolved');
  const blocking = allConflicts.filter((c) => (c.severity ?? 'blocking') === 'blocking');
  const advisory = allConflicts.filter((c) => (c.severity ?? 'blocking') === 'advisory');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {blocking.length > 0 && (
        <div className="glass-card" style={{ padding: '16px', borderLeft: '4px solid var(--warning)' }}>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '12px', color: 'var(--warning)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertTriangle size={16} /> 需处理的冲突（{blocking.length}）
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {blocking.map((c, i) => (
              <div key={i} style={{ padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,149,0,0.06)', fontSize: '0.875rem' }}>
                <p style={{ fontWeight: 500 }}>{c.message || c.key}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {advisory.length > 0 && (
        <details className="glass-card" style={{ padding: '16px', borderLeft: '4px solid var(--info)' }}>
          <summary style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--info)', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', listStyle: 'none' }}>
            <span>参考提示（{advisory.length}）· 以考核大纲为准，无需处理</span>
          </summary>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '12px' }}>
            {advisory.map((c, i) => (
              <div key={i} style={{ padding: '10px 12px', borderRadius: '10px', background: 'var(--info-subtle)', fontSize: '0.875rem' }}>
                <p>{c.message || c.key}</p>
              </div>
            ))}
          </div>
          <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginTop: '8px' }}>
            命题以考核大纲为准，教学大纲仅用于界定教学覆盖；此类提示在确认发布时会自动以考核大纲为准处理，可折叠忽略。
          </p>
        </details>
      )}

      <FrameworkBreakdown anchors={anchors} points={points} />

      {/* Actions: 固定在视口底部，内容较多时无需滚到页面最下方 */}
      <div style={{
        position: 'sticky', bottom: 0, zIndex: 20,
        display: 'flex', justifyContent: 'flex-end', gap: '8px',
        padding: '12px 16px',
        background: 'var(--sidebar-glass)',
        backdropFilter: 'var(--glass-blur)', WebkitBackdropFilter: 'var(--glass-blur)',
        border: '1px solid rgba(0,0,0,0.08)',
        borderRadius: '14px',
        boxShadow: '0 -4px 20px rgba(0,0,0,0.06)',
      }}>
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

// 考点表格（同一章节/未归类共用一个渲染）
function PointsTable({ points }: { points: FrameworkExamPoint[] }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>编号</th>
            <th>考点名称</th>
            <th>权重</th>
            <th>认知要求</th>
            <th>允许题型</th>
          </tr>
        </thead>
        <tbody>
          {points.map((pt) => (
            <tr key={pt.id}>
              <td style={{ fontSize: '0.8125rem', whiteSpace: 'nowrap' }}>{pt.code}</td>
              <td style={{ fontWeight: 500, fontSize: '0.875rem' }}>{pt.title}</td>
              <td style={{ fontSize: '0.875rem', whiteSpace: 'nowrap' }}>{pt.weight_value}%</td>
              <td style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                {(pt.cognitive_targets || []).join('、') || '-'}
              </td>
              <td style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                {(pt.allowed_question_types || []).join('、') || '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// 考核范围（章节）与考点合并展示：每个章节作为分组头，其下挂属于该章节的考点。
function FrameworkBreakdown({ anchors, points }: {
  anchors: AssessmentAnchor[];
  points: FrameworkExamPoint[];
}) {
  const anchorKeys = new Set(anchors.map((a) => a.key));
  const groups = anchors.map((anchor) => ({
    anchor,
    points: points.filter((p) => p.anchor_key === anchor.key),
  }));
  const orphanPoints = points.filter((p) => !anchorKeys.has(p.anchor_key));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      {groups.map(({ anchor, points: pts }, gi) => (
        <div key={gi} className="glass-card" style={{ padding: '14px 16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
            <ChevronRight size={14} style={{ color: 'var(--purple)', flexShrink: 0 }} />
            <p style={{ fontWeight: 600, fontSize: '0.9rem' }}>{anchor.title}</p>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginLeft: 'auto', whiteSpace: 'nowrap' }}>
              {anchor.exam_weight}%
            </span>
          </div>
          {(anchor.ability_requirements || []).length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginLeft: '22px', marginBottom: '8px' }}>
              {(anchor.ability_requirements || []).map((sub, j) => (
                <Badge key={j} variant="info">{sub}</Badge>
              ))}
            </div>
          )}
          {pts.length === 0 ? (
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginLeft: '22px', marginTop: '4px' }}>
              本章暂无考点
            </p>
          ) : (
            <PointsTable points={pts} />
          )}
        </div>
      ))}

      {orphanPoints.length > 0 && (
        <div className="glass-card" style={{ padding: '14px 16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
            <ChevronRight size={14} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
            <p style={{ fontWeight: 600, fontSize: '0.9rem', color: 'var(--text-secondary)' }}>未归类考点</p>
          </div>
          <PointsTable points={orphanPoints} />
        </div>
      )}

      {anchors.length === 0 && points.length === 0 && (
        <p style={{ fontSize: '0.875rem', color: 'var(--text-tertiary)' }}>暂无考核范围与考点</p>
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
      <FrameworkBreakdown anchors={anchors} points={points} />
    </div>
  );
}
