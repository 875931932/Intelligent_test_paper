import { useState, useEffect, Fragment, type ReactNode } from 'react';
import { useParams } from 'react-router-dom';
import {
  Plus, ChevronRight, ArrowLeft, RefreshCw, Check, PlayCircle,
  ClipboardList, FileText, Download, Eye, Pencil,
} from 'lucide-react';
import { api } from '@/api/client';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge, Input } from '@/components/ui';
import { SkeletonCardGrid } from '@/components/ui/Skeleton';
import type { ContractSnapshot } from '@/api/domains/examProjects';
import type { ExamProject, PlanItem, PaperVersionItem, TaskRun } from '@/types/api';

// ─── Stage pipeline ───
type StageKey = 'blueprint' | 'contract' | 'generate' | 'review' | 'export';
type ToastType = 'success' | 'error' | 'info';
type ToastFn = (message: string, type?: ToastType) => void;

const STAGE_ORDER: StageKey[] = ['blueprint', 'contract', 'generate', 'review', 'export'];

const STAGE_META: Record<StageKey, { label: string; icon: ReactNode; color: string }> = {
  blueprint: { label: '蓝图', icon: <ClipboardList size={16} />, color: '#0071e3' },
  contract:  { label: '合同', icon: <FileText size={16} />, color: '#5856d6' },
  generate:  { label: '生成', icon: <PlayCircle size={16} />, color: '#34c759' },
  review:    { label: '审核', icon: <Eye size={16} />, color: '#ff9500' },
  export:    { label: '导出', icon: <Download size={16} />, color: '#af52de' },
};

type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'info' | 'purple';

const STATUS_META: Record<string, { label: string; variant: BadgeVariant }> = {
  blueprint:  { label: '蓝图阶段', variant: 'info' },
  contract:   { label: '合同阶段', variant: 'purple' },
  generating: { label: '生成中',   variant: 'warning' },
  review:     { label: '待审核',   variant: 'warning' },
  exported:   { label: '已导出',   variant: 'success' },
};

function stageFromStatus(status: string): StageKey {
  const idx = STAGE_ORDER.indexOf(status as StageKey);
  return idx >= 0 ? STAGE_ORDER[idx] : 'blueprint';
}

// ═══════════════════════════════════════════════
//  玻璃步骤条
// ═══════════════════════════════════════════════
function StageStepper({ current, onSelect }: { current: StageKey; onSelect: (s: StageKey) => void }) {
  const currentIdx = STAGE_ORDER.indexOf(current);
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', marginTop: '18px' }}>
      {STAGE_ORDER.map((key, i) => {
        const done = i < currentIdx;
        const active = i === currentIdx;
        const meta = STAGE_META[key];
        const reachable = i <= currentIdx;
        return (
          <Fragment key={key}>
            {i > 0 && (
              <div style={{
                flex: 1,
                height: 2,
                alignSelf: 'center',
                marginTop: '-20px',
                background: reachable ? meta.color : 'rgba(0,0,0,0.08)',
                borderRadius: 2,
                transition: 'background 0.3s',
                opacity: reachable ? 0.6 : 1,
              }} />
            )}
            <button
              onClick={() => onSelect(key)}
              disabled={!reachable}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 7,
                background: 'none',
                border: 'none',
                padding: 0,
                minWidth: 58,
                cursor: reachable ? 'pointer' : 'not-allowed',
              }}
            >
              <span style={{
                width: 32,
                height: 32,
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: active || done ? '#fff' : 'var(--text-tertiary)',
                background: active || done ? meta.color : 'rgba(0,0,0,0.05)',
                boxShadow: active ? '0 0 0 4px ' + meta.color + '30' : 'none',
                transition: 'all 0.25s var(--ease-out-expo)',
              }}>
                {done ? <Check size={16} /> : meta.icon}
              </span>
              <span style={{
                fontSize: '0.7rem',
                fontWeight: active ? 600 : 400,
                color: active ? meta.color : done ? 'var(--text-secondary)' : 'var(--text-tertiary)',
                whiteSpace: 'nowrap',
              }}>
                {meta.label}
              </span>
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}

// ═══════════════════════════════════════════════
//  阶段渲染（纯函数）
// ═══════════════════════════════════════════════
function StageHeading({ title, right }: { title: string; right?: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
      <h3 style={{ fontWeight: 600, fontSize: '1rem', letterSpacing: '-0.01em' }}>{title}</h3>
      {right}
    </div>
  );
}

function renderBlueprint({
  sp, setStep, bpCreating, handleCreateBlueprint, loadPlanItems, planItems,
}: {
  sp: ExamProject; setStep: (s: StageKey) => void;
  bpCreating: boolean; handleCreateBlueprint: () => Promise<void>;
  loadPlanItems: (p: ExamProject) => void; planItems: PlanItem[];
}) {
  if (sp.active_blueprint_version_id) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <StageHeading
          title="蓝图规划"
          right={<Button variant="secondary" size="sm" onClick={() => loadPlanItems(sp)} icon={<RefreshCw size={14} />}>刷新</Button>}
        />
        {planItems.length > 0 ? (
          <div className="table-wrapper">
            <table className="data-table">
              <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>考点</th><th>认知层级</th></tr></thead>
              <tbody>
                {planItems.map((item) => (
                  <tr key={item.item_index}>
                    <td>{item.item_index}</td>
                    <td>{item.question_type}</td>
                    <td><strong>{item.score}</strong></td>
                    <td>{item.difficulty}</td>
                    <td>{item.exam_point_id}</td>
                    <td>{item.cognitive_level}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p style={{ textAlign: 'center', padding: '24px 0', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>暂无计划项</p>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button onClick={() => setStep('contract')} icon={<ChevronRight size={16} />}>进入合同阶段</Button>
        </div>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageHeading title="创建蓝图规划" />
      <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
        输入蓝图规划参数，系统将根据框架和知识目录生成命题计划。
      </p>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
        <Button variant="secondary" onClick={() => setStep('contract')}>跳过</Button>
        <Button onClick={handleCreateBlueprint} loading={bpCreating} icon={<PlayCircle size={16} />}>创建蓝图</Button>
      </div>
    </div>
  );
}

function renderContract({
  sp, courseId, setStep, contractSeed, setContractSeed,
  contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming, addToast,
}: {
  sp: ExamProject; courseId: string; setStep: (s: StageKey) => void;
  contractSeed: number; setContractSeed: (n: number) => void;
  contractSnapshot: ContractSnapshot | null; setContractSnapshot: (s: ContractSnapshot | null) => void;
  contractConfirming: boolean; setContractConfirming: (b: boolean) => void;
  addToast: ToastFn;
}) {
  const allocate = async () => {
    try {
      const res = await api.examProjects.allocateContract(courseId, sp.id, {
        blueprint_version_id: sp.active_blueprint_version_id,
        allocation_seed: contractSeed,
      });
      setContractSnapshot(res.contract_snapshot);
      addToast('合同已分配', 'success');
    } catch (e) {
      addToast('分配失败: ' + (e as Error).message, 'error');
    }
  };
  if (contractSnapshot) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <StageHeading
          title="合同槽位"
          right={<Badge variant="info">总分: {contractSnapshot.total_score ?? '-'}</Badge>}
        />
        <div className="table-wrapper">
          <table className="data-table">
            <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>考点</th><th>知识卡</th></tr></thead>
            <tbody>
              {contractSnapshot.slots.map((s) => (
                <tr key={s.item_index}>
                  <td>{s.item_index + 1}</td>
                  <td>{s.question_type}</td>
                  <td><strong>{s.score}</strong></td>
                  <td>{s.difficulty}</td>
                  <td>{s.exam_point_id}</td>
                  <td style={{ maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.card_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
          <Button variant="secondary" onClick={async () => { setContractSnapshot(null); await allocate(); }} icon={<RefreshCw size={16} />}>重新分配</Button>
          <Button
            onClick={async () => {
              setContractConfirming(true);
              try {
                await api.examProjects.confirmContract(courseId, sp.id, {
                  blueprint_version_id: sp.active_blueprint_version_id,
                  slot_revisions: [],
                  allocation_seed: contractSeed,
                });
                addToast('合同已确认', 'success');
                setStep('generate');
              } catch (e) {
                addToast('确认失败: ' + (e as Error).message, 'error');
              }
              setContractConfirming(false);
            }}
            loading={contractConfirming}
            icon={<Check size={16} />}
          >
            确认合同
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageHeading title="分配合同" />
      <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
        根据蓝图规划分配具体的题型和分值，形成可执行的合同。
      </p>
      <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div style={{ minWidth: '180px' }}>
          <label style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>分配种子</label>
          <input type="number" value={contractSeed} onChange={(e) => setContractSeed(Number(e.target.value))} className="input-field" style={{ marginTop: '4px' }} />
        </div>
      </div>
      <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
        <Button variant="secondary" onClick={() => setStep('blueprint')}><ArrowLeft size={16} /> 返回蓝图</Button>
        <Button onClick={allocate} icon={<PlayCircle size={16} />}>分配合同</Button>
      </div>
    </div>
  );
}

function renderGenerate({
  sp, courseId, token, setStep, taskRun, setTaskRun, generating, setGenerating, addToast,
}: {
  sp: ExamProject; courseId: string; token: string | null; setStep: (s: StageKey) => void;
  taskRun: TaskRun | null; setTaskRun: (tr: TaskRun | null) => void;
  generating: boolean; setGenerating: (b: boolean) => void;
  addToast: ToastFn;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageHeading title="AI 生成试题" />
      {!taskRun ? (
        <div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '14px' }}>
            AI 将根据合同约定生成试题。生成过程大约需要 30-60 秒。
          </p>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <Button variant="secondary" onClick={() => setStep('contract')}><ArrowLeft size={16} /> 返回合同</Button>
            <Button
              onClick={async () => {
                try {
                  setGenerating(true);
                  const res = await api.examProjects.startGeneration(courseId, sp.id, { mock_graph: true });
                  const tr = await api.examProjects.getTaskRun(courseId, res.task_run_id, token ?? undefined);
                  setTaskRun(tr);
                  addToast('任务已启动', 'success');
                } catch (e) {
                  addToast('生成失败: ' + (e as Error).message, 'error');
                  setGenerating(false);
                }
              }}
              loading={generating}
              icon={<PlayCircle size={16} />}
            >
              开始生成
            </Button>
          </div>
        </div>
      ) : (
        <div style={{ textAlign: 'center', padding: '48px 20px' }}>
          <span className="spinner spinner-lg" />
          <p style={{ marginTop: '16px', fontWeight: 500 }}>正在生成试题...</p>
          <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>
            阶段: {taskRun.stage || '-'} · 进度: {taskRun.progress ?? 0}%
          </p>
        </div>
      )}
    </div>
  );
}

function renderReviewItems(items: PaperVersionItem[], onPatchItem: (idx: number, p: Record<string, unknown>) => Promise<void>) {
  return items.map((item) => {
    const flagged = (item.needs_review_reasons?.length ?? 0) > 0 || item.needs_review;
    const inputId = 'review-input-' + item.item_index;
    return (
      <div
        key={item.item_index}
        className="glass-card"
        style={{
          padding: '16px',
          borderLeft: '3px solid ' + (flagged ? '#ff9500' : 'rgba(0,113,227,0.4)'),
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)' }}>
            #{item.item_index + 1}
          </span>
          <Badge variant="info">{item.question_type}</Badge>
          <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>{item.difficulty}</span>
          <strong style={{ marginLeft: 'auto', fontSize: '0.85rem' }}>{item.score} 分</strong>
        </div>
        <p style={{ fontSize: '0.9rem', lineHeight: 1.6 }}>{item.stem}</p>
        {item.options && Object.keys(item.options).length > 0 && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', marginTop: '10px' }}>
            {Object.entries(item.options).map(([k, v]) => (
              <div key={k} style={{ fontSize: '0.8rem', padding: '6px 10px', borderRadius: '8px', background: 'rgba(0,0,0,0.03)' }}>
                <strong>{k}.</strong> {v as string}
              </div>
            ))}
          </div>
        )}
        <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: '8px' }}>
          答案: {item.answer} · 解析: {item.explanation}
        </div>
        {flagged && (
          <div style={{ fontSize: '0.78rem', color: '#b36b00', marginTop: '6px', fontWeight: 500 }}>
            需审核: {item.needs_review_reasons?.join('; ') || '有修改建议'}
          </div>
        )}
        <div style={{ display: 'flex', gap: '8px', marginTop: '12px', alignItems: 'center' }}>
          <Input id={inputId} placeholder="修正题干 (留空保留)" style={{ flex: 1 }} />
          <Button
            variant="secondary"
            size="sm"
            onClick={async () => {
              const inputEl = document.getElementById(inputId) as HTMLInputElement | null;
              const val = inputEl?.value;
              if (val && val !== item.stem) {
                await onPatchItem(item.item_index, { stem: val });
              }
            }}
          >
            <Pencil size={14} /> 保存
          </Button>
        </div>
      </div>
    );
  });
}

function renderReview({
  setStep, paperVersion, pvId, pvConfirming, handleConfirmReview, handlePatchReviewItem,
}: {
  setStep: (s: StageKey) => void;
  paperVersion: any; pvId: string | undefined;
  pvConfirming: boolean;
  handleConfirmReview: () => Promise<void>;
  handlePatchReviewItem: (idx: number, p: Record<string, unknown>) => Promise<void>;
}) {
  if (!paperVersion && !pvId) {
    return (
      <div style={{ textAlign: 'center', padding: '32px' }}>
        <p style={{ color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>请先在生成阶段完成生成</p>
        <Button variant="secondary" style={{ marginTop: '14px' }} onClick={() => setStep('generate')}>返回生成</Button>
      </div>
    );
  }
  const items: PaperVersionItem[] = paperVersion?.items || [];
  const needsReviewCount = items.filter((i) => i.needs_review).length;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageHeading
        title="试卷审核"
        right={
          <div style={{ display: 'flex', gap: '8px' }}>
            <Badge>总分: {paperVersion?.total_score}</Badge>
            {needsReviewCount > 0 && <Badge variant="warning">待审: {needsReviewCount}</Badge>}
          </div>
        }
      />
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', maxHeight: '600px', overflowY: 'auto', paddingRight: '4px' }}>
        {items.length === 0 ? (
          <p style={{ textAlign: 'center', padding: '32px 0', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>暂无题目</p>
        ) : (
          renderReviewItems(items, handlePatchReviewItem)
        )}
      </div>
      <div style={{ display: 'flex', gap: '8px', justifyContent: 'space-between' }}>
        <Button variant="secondary" onClick={() => setStep('generate')}><ArrowLeft size={16} /> 返回生成</Button>
        <Button onClick={handleConfirmReview} loading={pvConfirming} icon={<Check size={16} />}>确认通过</Button>
      </div>
    </div>
  );
}

function renderExport({
  exportUrls, pvId, setStep,
}: {
  exportUrls: { json?: string; student?: string; answerKey?: string };
  pvId: string | undefined; setStep: (s: StageKey) => void;
}) {
  if (!pvId) {
    return (
      <div style={{ textAlign: 'center', padding: '32px' }}>
        <p style={{ color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>请先确认试卷</p>
        <Button variant="secondary" style={{ marginTop: '14px' }} onClick={() => setStep('review')}>返回审核</Button>
      </div>
    );
  }
  const cards = [
    { name: '答案细则 JSON', desc: '每题详细答案与评分标准，供阅卷端消费', icon: <FileText size={22} style={{ color: '#0071e3' }} />, url: exportUrls.json, label: '下载 JSON' },
    { name: '学生卷 HTML', desc: '不含答案，可打印为 PDF', icon: <Eye size={22} style={{ color: '#34c759' }} />, url: exportUrls.student, label: '打开预览', external: true },
    { name: '答卷 HTML', desc: '含答案与评分标准', icon: <ClipboardList size={22} style={{ color: '#ff9500' }} />, url: exportUrls.answerKey, label: '打开预览', external: true },
  ];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageHeading title="导出试卷" />
      <div className="card-grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))' }}>
        {cards.map((ex) => (
          <div key={ex.name} className="glass-card" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {ex.icon}
            <h4 style={{ fontWeight: 600, fontSize: '0.9rem' }}>{ex.name}</h4>
            <p style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', flex: 1 }}>{ex.desc}</p>
            <a href={ex.url} target="_blank" rel="noopener" download={!ex.external} style={{ display: 'flex' }}>
              <Button variant="secondary" size="sm" style={{ width: '100%' }}>{ex.label}</Button>
            </a>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="secondary" onClick={() => setStep('review')}><ArrowLeft size={16} /> 返回审核</Button>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════
//  主组件
// ═══════════════════════════════════════════════
export default function ExamProjectsPage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const courseId = routeCourseId || '';
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [projects, setProjects] = useState<ExamProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeProject, setActiveProject] = useState<ExamProject | null>(null);
  const [currentStage, setCurrentStage] = useState<StageKey>('blueprint');

  const [planItems, setPlanItems] = useState<PlanItem[]>([]);
  const [contractSnapshot, setContractSnapshot] = useState<ContractSnapshot | null>(null);
  const [paperVersion, setPaperVersion] = useState<any>(null);
  const [taskRun, setTaskRun] = useState<TaskRun | null>(null);
  const [exportUrls, setExportUrls] = useState<{ json?: string; student?: string; answerKey?: string }>({});

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [bpCreating, setBpCreating] = useState(false);
  const [contractSeed, setContractSeed] = useState(0);
  const [contractConfirming, setContractConfirming] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [pvConfirming, setPvConfirming] = useState(false);

  const loadProjects = async () => {
    if (!courseId) return;
    try {
      setLoading(true);
      const res = await api.examProjects.list(courseId);
      setProjects(res);
    } catch {
      addToast('加载项目失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const loadPaperVersion = async () => {
    if (!activeProject?.active_paper_version_id || !token) return;
    try {
      const pv = await api.paperVersions.getCurrent(courseId, activeProject.id, token);
      setPaperVersion(pv);
    } catch {
      addToast('加载试卷版本失败', 'error');
    }
  };

  const loadPlanItems = async (proj: ExamProject) => {
    try {
      const items = await api.examProjects.getPlanItems(courseId, proj.id);
      setPlanItems(items);
    } catch {
      addToast('加载计划项失败', 'error');
    }
  };

  // 轮询生成任务
  useEffect(() => {
    if (!taskRun || taskRun.status === 'completed' || taskRun.status === 'failed') return;
    const id = setInterval(async () => {
      try {
        const tr = await api.examProjects.getTaskRun(courseId, taskRun.id, token ?? undefined);
        setTaskRun(tr);
        if (tr.status === 'completed' || tr.status === 'failed') {
          clearInterval(id);
          setGenerating(false);
          if (tr.status === 'completed') {
            addToast('试题生成完成', 'success');
          } else {
            addToast('生成失败: ' + (tr.error_message || '未知错误'), 'error');
          }
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, 2500);
    return () => clearInterval(id);
  }, [taskRun, courseId, token, addToast]);

  // 生成完成后加载试卷版本
  useEffect(() => {
    if (taskRun?.status === 'completed' && activeProject?.active_paper_version_id) {
      loadPaperVersion();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskRun?.status, activeProject]);

  // 进入 review/export 阶段时加载版本 / 导出地址
  useEffect(() => {
    if (currentStage === 'review' && !paperVersion && activeProject?.active_paper_version_id) {
      loadPaperVersion();
    }
    if (currentStage === 'export' && activeProject?.active_paper_version_id && exportUrls.json === undefined) {
      const pvId = activeProject.active_paper_version_id;
      setExportUrls({
        json: api.paperVersions.exportJson(courseId, activeProject.id, pvId),
        student: api.paperVersions.exportStudent(courseId, activeProject.id, pvId),
        answerKey: api.paperVersions.exportAnswerKey(courseId, activeProject.id, pvId),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStage, paperVersion, activeProject]);

  const openProject = async (proj: ExamProject) => {
    setActiveProject(proj);
    setCurrentStage(stageFromStatus(proj.status));
    setContractSnapshot(null);
    setPaperVersion(null);
    setTaskRun(null);
    setExportUrls({});
    if (proj.active_blueprint_version_id) {
      await loadPlanItems(proj);
    }
  };

  const handleCreateBlueprint = async () => {
    if (!activeProject) return;
    try {
      setBpCreating(true);
      const units = JSON.parse('[{"unit_id":"default","exam_point_id":"default","card_ids":[]}]');
      await api.examProjects.createBlueprint(courseId, activeProject.id, {
        framework_version_id: String(1),
        catalog_version_id: String(1),
        type_rules: {},
        chapter_weights: {},
        units: units as never,
      });
      addToast('蓝图已生成', 'success');
      setCurrentStage('contract');
      await loadProjects();
    } catch (e) {
      addToast('蓝图创建失败: ' + (e as Error).message, 'error');
    } finally {
      setBpCreating(false);
    }
  };

  const handleConfirmReview = async () => {
    if (!activeProject?.active_paper_version_id) return;
    setPvConfirming(true);
    try {
      await api.paperVersions.confirm(courseId, activeProject.active_paper_version_id, {});
      addToast('试卷确认通过', 'success');
      await loadProjects();
      setCurrentStage('export');
      const pvId = activeProject.active_paper_version_id;
      setExportUrls({
        json: api.paperVersions.exportJson(courseId, activeProject.id, pvId),
        student: api.paperVersions.exportStudent(courseId, activeProject.id, pvId),
        answerKey: api.paperVersions.exportAnswerKey(courseId, activeProject.id, pvId),
      });
    } catch {
      addToast('确认失败', 'error');
    } finally {
      setPvConfirming(false);
    }
  };

  const handlePatchReviewItem = async (itemIndex: number, patch: Record<string, unknown>) => {
    if (!activeProject?.active_paper_version_id) return;
    try {
      await api.paperVersions.patchItem(courseId, activeProject.active_paper_version_id, itemIndex, patch);
      addToast('题目已更新', 'success');
      loadPaperVersion();
    } catch {
      addToast('修正失败', 'error');
    }
  };

  const handleCreateProject = async () => {
    const name = newName.trim();
    if (!name || !courseId) return;
    try {
      const proj = await api.examProjects.create(courseId, { name });
      setProjects((s) => [...s, proj]);
      setCreateOpen(false);
      setNewName('');
      addToast('项目创建成功', 'success');
    } catch {
      addToast('创建失败', 'error');
    }
  };

  useEffect(() => {
    loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  // ── 加载态：骨架屏 ──
  if (loading) {
    return (
      <div className="page-enter">
        <SkeletonCardGrid count={4} />
      </div>
    );
  }

  // ── 项目详情视图 ──
  if (activeProject) {
    const sp = activeProject;
    const statusMeta = STATUS_META[sp.status] ?? { label: sp.status, variant: 'default' as BadgeVariant };
    return (
      <div className="page-enter">
        <button
          onClick={() => setActiveProject(null)}
          style={{
            background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)',
            fontSize: '0.8125rem', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '10px',
            padding: 0,
          }}
        >
          <ArrowLeft size={16} /> 返回项目列表
        </button>

        <div className="glass-card" style={{ padding: '24px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <h1 style={{ fontWeight: 700, fontSize: '1.3rem', letterSpacing: '-0.02em' }}>{sp.name}</h1>
              <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                {sp.total_score ? sp.total_score + ' 分 · ' + (sp.item_count || 0) + ' 题' : '尚未生成试卷'}
              </p>
            </div>
            <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
          </div>
          <StageStepper current={currentStage} onSelect={setCurrentStage} />
        </div>

        <div className="glass-card" style={{ padding: '24px' }}>
          {currentStage === 'blueprint' && renderBlueprint({
            sp, setStep: setCurrentStage, bpCreating, handleCreateBlueprint, loadPlanItems, planItems,
          })}
          {currentStage === 'contract' && renderContract({
            sp, courseId, setStep: setCurrentStage, contractSeed, setContractSeed,
            contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming, addToast,
          })}
          {currentStage === 'generate' && renderGenerate({
            sp, courseId, token, setStep: setCurrentStage, taskRun, setTaskRun, generating, setGenerating, addToast,
          })}
          {currentStage === 'review' && renderReview({
            setStep: setCurrentStage, paperVersion, pvId: paperVersion?.id,
            pvConfirming, handleConfirmReview, handlePatchReviewItem,
          })}
          {currentStage === 'export' && renderExport({
            exportUrls, pvId: sp.active_paper_version_id, setStep: setCurrentStage,
          })}
        </div>
      </div>
    );
  }

  // ── 项目列表 ──
  return (
    <div className="page-enter">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '6px' }}>试卷项目</h1>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>
            蓝图 → 合同 → 生成 → 审核 → 导出，AI 驱动的完整试卷生产流程
          </p>
        </div>
        <Button onClick={() => { setNewName(''); setCreateOpen(true); }} icon={<Plus size={16} />}>新建项目</Button>
      </div>

      {projects.length === 0 ? (
        <div className="glass-card" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '72px 20px', gap: '16px' }}>
          <div style={{ width: 60, height: 60, borderRadius: '18px', background: 'var(--accent-subtle)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <ClipboardList size={30} />
          </div>
          <h3 style={{ fontWeight: 600, fontSize: '1.05rem' }}>暂无试卷项目</h3>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>点击「新建项目」开始您的第一次出卷</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {projects.map((p) => {
            const sm = STATUS_META[p.status] ?? { label: p.status, variant: 'default' as BadgeVariant };
            return (
              <div
                key={p.id}
                className="glass-card"
                style={{ padding: '16px 20px', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
                onClick={() => openProject(p)}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
                  <div style={{
                    width: 42, height: 42, borderRadius: '12px',
                    background: 'rgba(0,113,227,0.08)', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <ClipboardList size={20} style={{ color: '#0071e3' }} />
                  </div>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: '0.9rem' }}>{p.name}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '3px' }}>
                      {p.total_score ? p.total_score + ' 分 · ' + (p.item_count || 0) + ' 题' : '待生成'}
                    </div>
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <Badge variant={sm.variant}>{sm.label}</Badge>
                  <ChevronRight size={18} style={{ color: 'var(--text-tertiary)' }} />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {createOpen && (
        <div className="modal-overlay" onClick={() => setCreateOpen(false)}>
          <div className="modal-content" style={{ maxWidth: '420px' }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="modal-title">新建项目</h3>
            </div>
            <div className="modal-body">
              <Input label="项目名称" placeholder="请输入项目名称" value={newName} onChange={(e) => setNewName(e.target.value)} autoFocus />
            </div>
            <div className="modal-footer">
              <Button variant="secondary" onClick={() => setCreateOpen(false)}>取消</Button>
              <Button onClick={handleCreateProject}>创建</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
