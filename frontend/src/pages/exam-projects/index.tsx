import { useState, useEffect, useRef, Fragment, type ReactNode } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import {
  Plus, ChevronRight, ArrowLeft, ArrowRight, RefreshCw, Check, PlayCircle,
  ClipboardList, FileText,
} from 'lucide-react';
import { api } from '@/api/client';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge, Input, ProgressPanel, type ProgressStatus } from '@/components/ui';
import { SkeletonCardGrid } from '@/components/ui/Skeleton';
import { useNameMaps, type NameMaps } from '@/hooks/useNameMaps';
import { clabel, dlabel, EXAM_PROJECT_STATUS_META, PAPER_STATUS_META, qlabel } from '@/lib/examDisplay';
import type { ContractSnapshot } from '@/api/domains/examProjects';
import type { ExamProject, PlanItem, TaskRun } from '@/types/api';

// ─── Stage pipeline ───
// 流水线只负责「出题」：蓝图 → 合同 → 生成。审核/编辑/定稿/导出已独立为
// 「试卷中心」（/courses/:courseId/paper-center），不再占流水线阶段。
type StageKey = 'blueprint' | 'contract' | 'generate';
type ToastType = 'success' | 'error' | 'info';
type ToastFn = (message: string, type?: ToastType) => void;

const STAGE_ORDER: StageKey[] = ['blueprint', 'contract', 'generate'];

const STAGE_META: Record<StageKey, { label: string; icon: ReactNode; color: string }> = {
  blueprint: { label: '蓝图', icon: <ClipboardList size={16} />, color: '#0071e3' },
  contract:  { label: '合同', icon: <FileText size={16} />, color: '#5856d6' },
  generate:  { label: '生成', icon: <PlayCircle size={16} />, color: '#34c759' },
};

// 项目状态徽标与试卷中心同源（lib/examDisplay），避免两页口径不一致
const STATUS_META = EXAM_PROJECT_STATUS_META;

const STATUS_TO_STAGE: Record<string, StageKey> = {
  draft: 'blueprint',
  blueprint: 'blueprint',
  contract: 'contract',
  generating: 'generate',
  // 后端项目状态仍保留 review/exported（用于列表徽标）；这里归一到生成阶段，
  // 因为审核与导出已迁至独立的「试卷中心」，流水线不再有对应阶段。
  review: 'generate',
  exported: 'generate',
};

function stageFromStatus(status: string): StageKey {
  return STATUS_TO_STAGE[status] ?? 'blueprint';
}

// 生成阶段的阶段性文案。后端任务只上报「开始 5%」与「完成 100%」两档，
// 中间没有细分百分比，所以这里用轮换文案 + 已等待时长表达推进感，
// 而不是伪造一个会跳变的假进度条。
const GENERATION_MESSAGES = [
  '正在按合同生成题目…',
  '正在进行答案与解析质检…',
  '正在校验收分与题型…',
  '正在整理试卷版本…',
];

const GENERATION_QUEUED_MESSAGES = ['等待 Celery worker 接管任务…'];

// queued 超过该时长提示排查 worker：worker 未启动或繁忙时任务会一直排队，
// 用户侧只看到一个"排队中"无法区分是正常等待还是卡死。
const QUEUED_HINT_SECONDS = 60;

// 名称映射：把考点 / 章节 / 知识卡的 id 换成真实名称，未命中时回退原始值
// （NameMaps 定义与构建逻辑见 hooks/useNameMaps，与试卷中心共用）
function examPointLabel(maps: NameMaps, id: string): string {
  return maps.examPoints[id] || id;
}

function anchorLabel(maps: NameMaps, key: string): string {
  return maps.anchors[key] || key;
}

function cardLabel(maps: NameMaps, id: string): string {
  return maps.cards[id] || id;
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
  sp, setStep, bpCreating, handleCreateBlueprint, loadPlanItems, planItems, maps,
}: {
  sp: ExamProject; setStep: (s: StageKey) => void;
  bpCreating: boolean; handleCreateBlueprint: () => Promise<void>;
  loadPlanItems: (p: ExamProject) => void; planItems: PlanItem[];
  maps: NameMaps;
}) {
  if (sp.active_blueprint_version_id) {
    const totalScore = planItems.reduce((s, i) => s + (i.score || 0), 0);
    const typeAcc = new Map<string, { score: number; count: number }>();
    const chapterAcc = new Map<string, number>();
    planItems.forEach((i) => {
      const t = typeAcc.get(i.question_type) ?? { score: 0, count: 0 };
      t.score += i.score || 0; t.count += 1; typeAcc.set(i.question_type, t);
      const ck = i.anchor_key || '未分章';
      chapterAcc.set(ck, (chapterAcc.get(ck) || 0) + (i.score || 0));
    });
    const typeDist = [...typeAcc.entries()];
    const chapterDist = [...chapterAcc.entries()];
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <StageHeading
          title="蓝图规划"
          right={<Button variant="secondary" size="sm" onClick={() => loadPlanItems(sp)} icon={<RefreshCw size={14} />}>刷新</Button>}
        />
        {planItems.length > 0 ? (
          <div>
            <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', alignItems: 'flex-start', marginBottom: '16px' }}>
              <div style={{ minWidth: '120px' }}>
                <div style={{ fontSize: '1.7rem', fontWeight: 700, lineHeight: 1 }}>{totalScore}</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>总分 · {planItems.length} 题</div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', flex: 1, minWidth: '220px' }}>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  {typeDist.map(([t, v]) => (
                    <span key={t} style={{
                      padding: '4px 10px', borderRadius: '999px', fontSize: '0.78rem', fontWeight: 600,
                      background: 'rgba(0,113,227,0.08)', color: '#0071e3',
                    }}>
                      {qlabel(t)} {v.score}分·{v.count}题
                    </span>
                  ))}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 16px' }}>
                  {chapterDist.map(([c, s]) => (
                    <span key={c} style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{anchorLabel(maps, c)}：{s}分</span>
                  ))}
                </div>
              </div>
            </div>
            <div className="table-wrapper">
              <table className="data-table">
                <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>章节</th><th>考点</th><th>认知层级</th></tr></thead>
                <tbody>
                  {planItems.map((item) => (
                    <tr key={item.item_index}>
                      <td>{item.item_index}</td>
                      <td>{qlabel(item.question_type)}</td>
                      <td><strong>{item.score}</strong></td>
                      <td>{dlabel(item.difficulty)}</td>
                      <td>{item.anchor_key ? anchorLabel(maps, item.anchor_key) : '-'}</td>
                      <td>{item.exam_point_id ? examPointLabel(maps, item.exam_point_id) : '-'}</td>
                      <td>{clabel(item.cognitive_level) || '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <p style={{ textAlign: 'center', padding: '24px 0', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>暂无计划项，请点击「刷新」加载</p>
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
  sp, courseId, setStep, contractVariant, setContractVariant,
  contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming,
  contractAllocating, setContractAllocating, addToast, maps, planItems,
}: {
  sp: ExamProject; courseId: string; setStep: (s: StageKey) => void;
  contractVariant: number; setContractVariant: (n: number) => void;
  contractSnapshot: ContractSnapshot | null; setContractSnapshot: (s: ContractSnapshot | null) => void;
  contractConfirming: boolean; setContractConfirming: (b: boolean) => void;
  contractAllocating: boolean; setContractAllocating: (b: boolean) => void;
  addToast: ToastFn;
  maps: NameMaps;
  planItems: PlanItem[];
}) {
  // variantOverride：切换方案时 onChange 已把新版本号拿到手，直接用它发请求，
  // 不等 state 重渲染后再读闭包值，避免切换到第 N 版却按第 N-1 版分配。
  const allocate = async (variantOverride?: number) => {
    const variant = variantOverride ?? contractVariant;
    setContractAllocating(true);
    try {
      const res = await api.examProjects.allocateContract(courseId, sp.id, {
        blueprint_version_id: sp.active_blueprint_version_id,
        allocation_seed: variant - 1,
      });
      setContractSnapshot(res.contract_snapshot);
      addToast(`合同已分配（方案第 ${variant} 版）`, 'success');
    } catch (e) {
      addToast('分配失败: ' + (e as Error).message, 'error');
    } finally {
      setContractAllocating(false);
    }
  };
  // 「分配方案」下拉：同一个版本结果固定、便于与同事讨论同一份卷子；
  // 换一版会生成不同题目排布的方案。内部把第 N 版映射为分配种子 N-1。
  const variantOptions = [1, 2, 3, 4, 5, 6];
  const changeVariant = async (next: number, reload: boolean) => {
    setContractVariant(next);
    // 已有合同快照时换版即重新分配：教师不必再点一次「重新分配」，
    // 少一步手势，也避免"以为换了其实没换"的误解。尚无快照时不发请求，
    // 由教师主动点「分配合同」。
    if (reload) await allocate(next);
  };
  const VariantSelect = ({ compact }: { compact?: boolean }) => (
    <div style={{ minWidth: compact ? '150px' : '200px' }}>
      {!compact && (
        <label style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', display: 'block', marginBottom: '4px' }}>
          分配方案
        </label>
      )}
      <select
        value={contractVariant}
        onChange={(e) => { void changeVariant(Number(e.target.value), !!contractSnapshot); }}
        disabled={contractAllocating}
        className="input-field"
        style={{ width: '100%' }}
      >
        {variantOptions.map((v) => (
          <option key={v} value={v}>第 {v} 版</option>
        ))}
      </select>
    </div>
  );
  if (contractSnapshot) {
    // 实际分配分 vs 蓝图计划分：合同曾因考点答案域容量不足静默丢题（83/100），
    // 这里把缺口、冲突与同章回补全部显式呈现，不再只显示一个总分
    const actualScore = contractSnapshot.total_score ?? contractSnapshot.slots.reduce((s, x) => s + (x.score || 0), 0);
    const plannedScore = planItems.reduce((s, i) => s + (i.score || 0), 0) || sp.total_score || 0;
    const deficit = plannedScore - actualScore;
    const conflicts = contractSnapshot.conflicts ?? [];
    const backfilled = contractSnapshot.audit_summary?.backfilled_points ?? [];
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <StageHeading
          title="合同槽位"
          right={
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <Badge variant="info">总分: {actualScore}</Badge>
              {plannedScore > 0 && deficit > 0 && (
                <Badge variant="warning">应有 {plannedScore} · 缺 {deficit}</Badge>
              )}
              <Badge variant="default">方案第 {contractVariant} 版</Badge>
            </div>
          }
        />
        <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <VariantSelect compact />
          <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: '0 0 6px 0' }}>
            切换方案会自动重新分配；同一版本结果固定，方便与他人讨论同一份卷子。
          </p>
        </div>
        {deficit > 0 && (
          <div style={{ padding: '10px 14px', borderRadius: '8px', background: 'rgba(255,149,0,0.08)', border: '1px solid rgba(255,149,0,0.25)', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
            蓝图计划 <strong>{plannedScore}</strong> 分，实际分配 <strong>{actualScore}</strong> 分，缺 <strong>{deficit}</strong> 分。
            {backfilled.length > 0
              ? ' 部分题位已按同章回补改派到富余考点；仍有缺口说明同章内答案域容量不足，请补充知识卡或调整蓝图。'
              : ' 同章内没有可回补的富余考点，请补充知识卡或调整蓝图后重新分配。'}
          </div>
        )}
        {conflicts.length > 0 && (
          <div style={{ padding: '10px 14px', borderRadius: '8px', background: 'rgba(255,59,48,0.06)', border: '1px solid rgba(255,59,48,0.25)', fontSize: '0.8rem' }}>
            <div style={{ fontWeight: 600, marginBottom: '6px', color: '#ff3b30' }}>合同冲突（{conflicts.length}）</div>
            <ul style={{ margin: 0, paddingLeft: '18px', color: 'var(--text-secondary)' }}>
              {conflicts.map((c, i) => (
                <li key={i}>{c.exam_point_id ? examPointLabel(maps, c.exam_point_id) + '：' : ''}{c.message}</li>
              ))}
            </ul>
          </div>
        )}
        {backfilled.length > 0 && (
          <div style={{ padding: '10px 14px', borderRadius: '8px', background: 'rgba(0,113,227,0.06)', border: '1px solid rgba(0,113,227,0.2)', fontSize: '0.8rem' }}>
            <div style={{ fontWeight: 600, marginBottom: '6px', color: '#0071e3' }}>同章回补（{backfilled.length}）</div>
            <ul style={{ margin: 0, paddingLeft: '18px', color: 'var(--text-secondary)' }}>
              {backfilled.map((b) => (
                <li key={b.item_index}>第 {b.item_index + 1} 题：{examPointLabel(maps, b.from_exam_point_id)} → {examPointLabel(maps, b.to_exam_point_id)}（原考点答案域容量不足，改派同章富余考点）</li>
              ))}
            </ul>
          </div>
        )}
        <div className="table-wrapper">
          <table className="data-table">
            <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>考点</th><th>知识卡</th></tr></thead>
            <tbody>
              {contractSnapshot.slots.map((s) => (
                <tr key={s.item_index}>
                  <td>{s.item_index + 1}</td>
                  <td>{qlabel(s.question_type)}</td>
                  <td><strong>{s.score}</strong></td>
                  <td>{dlabel(s.difficulty)}</td>
                  <td>{examPointLabel(maps, s.exam_point_id)}</td>
                  <td style={{ maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={cardLabel(maps, s.card_id)}>{cardLabel(maps, s.card_id)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
          {/* 不再先 setContractSnapshot(null)：那会让整张表闪回"分配合同"空态，
              再瞬间加载回来。原地刷新快照即可，加载中禁用操作避免重复请求。 */}
          <Button variant="secondary" loading={contractAllocating} onClick={() => { void allocate(); }} icon={<RefreshCw size={16} />}>重新分配</Button>
          <Button
            onClick={async () => {
              setContractConfirming(true);
              try {
                await api.examProjects.confirmContract(courseId, sp.id, {
                  blueprint_version_id: sp.active_blueprint_version_id,
                  slot_revisions: [],
                  allocation_seed: contractVariant - 1,
                });
                addToast('合同已确认', 'success');
                setStep('generate');
              } catch (e) {
                addToast('确认失败: ' + (e as Error).message, 'error');
              }
              setContractConfirming(false);
            }}
            loading={contractConfirming}
            disabled={contractAllocating}
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
        <VariantSelect />
        <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: '0 0 6px 0' }}>
          蓝图不变的前提下，不同版本会生成题目分布不同的合同方案；同一版本结果固定，方便与他人讨论同一份卷子。
        </p>
      </div>
      <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
        <Button variant="secondary" onClick={() => setStep('blueprint')}><ArrowLeft size={16} /> 返回蓝图</Button>
        <Button onClick={() => { void allocate(); }} loading={contractAllocating} icon={<PlayCircle size={16} />}>分配合同</Button>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════
//  生成进度面板（状态感知）
// ═══════════════════════════════════════════
function GenerationProgressPanel({
  taskRun, onRetry, onBack, onOpenPaper,
}: {
  taskRun: TaskRun;
  onRetry: () => void;
  onBack: () => void;
  onOpenPaper: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const inFlight =
    taskRun.status === 'queued' || taskRun.status === 'running' || taskRun.status === 'waiting_external';

  useEffect(() => {
    if (!inFlight) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [inFlight]);

  const startedAt = Date.parse(taskRun.created_at);
  const elapsedSeconds = Number.isFinite(startedAt)
    ? Math.max(0, Math.floor((now - startedAt) / 1000))
    : 0;

  const status: ProgressStatus =
    taskRun.status === 'failed'
      ? 'failed'
      : taskRun.status === 'succeeded'
        ? 'succeeded'
        : taskRun.status === 'queued'
          ? 'queued'
          : 'running';

  // succeeded 后 result 带题目数与 paper_version_id；缺省时退化为通用文案
  const result = (taskRun.result ?? {}) as Record<string, unknown>;
  const resultMessage =
    taskRun.status === 'succeeded'
      ? typeof result.generated_questions === 'number'
        ? `已生成 ${result.generated_questions} 道试题。`
        : '试题已生成完毕。'
      : undefined;

  return (
    <ProgressPanel
      title={
        taskRun.status === 'failed'
          ? '试题生成失败'
          : taskRun.status === 'succeeded'
            ? '试题生成完成'
            : taskRun.status === 'queued'
              ? '任务排队中，等待执行…'
              : '正在生成试题，请稍候…'
      }
      messages={
        taskRun.status === 'succeeded' || taskRun.status === 'failed'
          ? undefined
          : taskRun.status === 'queued'
            ? GENERATION_QUEUED_MESSAGES
            : GENERATION_MESSAGES
      }
      progress={taskRun.progress ?? null}
      stageLabel={taskRun.stage && taskRun.status !== 'succeeded' ? `阶段：${taskRun.stage}` : undefined}
      status={status}
      elapsedSeconds={elapsedSeconds}
      queuedHintAfterSeconds={QUEUED_HINT_SECONDS}
      errorMessage={taskRun.error_message || taskRun.error_code || '未知错误，请重试或联系管理员'}
      resultMessage={resultMessage}
      footer={
        taskRun.status === 'failed' ? (
          <>
            <Button variant="secondary" onClick={onBack}><ArrowLeft size={16} /> 返回合同</Button>
            <Button onClick={onRetry} icon={<RefreshCw size={16} />}>重新生成</Button>
          </>
        ) : taskRun.status === 'succeeded' ? (
          <Button onClick={onOpenPaper} icon={<ArrowRight size={16} />}>进入试卷中心</Button>
        ) : undefined
      }
    />
  );
}

function renderGenerate({
  sp, courseId, token, setStep, taskRun, setTaskRun, generating, setGenerating, addToast, onOpenPaper,
}: {
  sp: ExamProject; courseId: string; token: string | null; setStep: (s: StageKey) => void;
  taskRun: TaskRun | null; setTaskRun: (tr: TaskRun | null) => void;
  generating: boolean; setGenerating: (b: boolean) => void;
  addToast: ToastFn;
  onOpenPaper: () => void;
}) {
  // 首次启动与失败后重试共用同一条链路：拿新 task_run 后立即回填进度面板
  const startGeneration = async () => {
    try {
      setGenerating(true);
      const res = await api.examProjects.startGeneration(courseId, sp.id);
      const tr = await api.examProjects.getTaskRun(courseId, res.task_run_id, token ?? undefined);
      setTaskRun(tr);
      addToast('任务已启动', 'success');
    } catch (e) {
      addToast('生成失败: ' + (e as Error).message, 'error');
    } finally {
      setGenerating(false);
    }
  };
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
              onClick={startGeneration}
              loading={generating}
              icon={<PlayCircle size={16} />}
            >
              开始生成
            </Button>
          </div>
        </div>
      ) : (
        <GenerationProgressPanel
          taskRun={taskRun}
          onRetry={startGeneration}
          onBack={() => setStep('contract')}
          onOpenPaper={onOpenPaper}
        />
      )}
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
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [projects, setProjects] = useState<ExamProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeProject, setActiveProject] = useState<ExamProject | null>(null);
  const [currentStage, setCurrentStage] = useState<StageKey>('blueprint');

  const [planItems, setPlanItems] = useState<PlanItem[]>([]);
  const [contractSnapshot, setContractSnapshot] = useState<ContractSnapshot | null>(null);
  const [taskRun, setTaskRun] = useState<TaskRun | null>(null);
  // 名称映射（id → 中文名）与试卷中心共用同一个 hook，两页口径一致
  const { maps, reload: reloadNameMaps } = useNameMaps(courseId);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [bpCreating, setBpCreating] = useState(false);
  const [contractConfirming, setContractConfirming] = useState(false);
  const [contractAllocating, setContractAllocating] = useState(false);
  // 「分配方案」默认随机一版：每次新建/刷新项目时不再固定回到第 1 版，
  // 否则每套卷子都从同一套搭配起步。历史种子由 hydrate 覆盖回填。
  const [contractVariant, setContractVariant] = useState(
    () => 1 + Math.floor(Math.random() * 6),
  );
  const [generating, setGenerating] = useState(false);
  // ?project=<id> 直达：从试卷中心点「出卷流水线」过来时自动展开该项目，只做一次
  const autoOpenedRef = useRef(false);

  const loadProjects = async () => {
    if (!courseId) return;
    try {
      setLoading(true);
      const res = await api.examProjects.list(courseId);
      setProjects(res);
      const pid = searchParams.get('project');
      const target = pid ? res.find((p) => p.id === pid) : undefined;
      if (target && !autoOpenedRef.current) {
        autoOpenedRef.current = true;
        void openProject(target);
      }
    } catch {
      addToast('加载项目失败', 'error');
    } finally {
      setLoading(false);
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
    if (!taskRun || taskRun.status === 'succeeded' || taskRun.status === 'failed') return;
    const id = setInterval(async () => {
      try {
        const tr = await api.examProjects.getTaskRun(courseId, taskRun.id, token ?? undefined);
        setTaskRun(tr);
        if (tr.status === 'succeeded' || tr.status === 'failed') {
          clearInterval(id);
          setGenerating(false);
          if (tr.status === 'succeeded') {
              addToast('试题生成完成', 'success');
              // 生成完成会落库一张 candidate 试卷版本。项目摘要是在打开项目时取的
              // 快照，此刻 paper_version_id 仍为 null，务必刷新头部才显示题数与
              // “N 分”。
              const refreshed = await api.examProjects
                .get(courseId, activeProject?.id ?? '', token ?? undefined)
                .catch(() => null);
              if (refreshed) setActiveProject(refreshed);
              await loadProjects().catch(() => {});
              // 自动跳转到独立的「试卷中心」：编辑/定稿/导出都在那里完成，
              // 本页流水线不再承载审核或导出。
              navigate('/courses/' + courseId + '/paper-center');
            } else {
            addToast('生成失败: ' + (tr.error_message || '未知错误'), 'error');
          }
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, 2500);
    return () => clearInterval(id);
    // 轮询闭包有意捕获 taskRun 快照；在成功分支读取当前 activeProject 刷新摘要
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskRun, courseId, token, addToast]);

  // 以服务端为权威数据源恢复项目状态。
  // 合同快照持久化在 generation_runs.contract_snapshot，任务进度持久化在
  // task_runs 并由项目摘要归并出 active_task_run_id。二者都与"任务是否仍在
  // 进行中"无关，因此退出项目再进入（或刷新页面）后都不会消失 —— 前端不再
  // 把组件内存态当作事实源。
  const hydrateProjectState = async (proj: ExamProject) => {
    // 1) 合同快照：未 confirm 过合同时后端返回 404，此时才回到"尚无合同"
    try {
      const cur = await api.examProjects.getCurrentContract(courseId, proj.id, token ?? undefined);
      const snap = cur?.contract_snapshot;
      setContractSnapshot(snap && Array.isArray(snap.slots) && snap.slots.length > 0 ? snap : null);
      // 分配方案回填：快照里记下了当初用的种子（前端第 N 版 = 种子 N-1），
      // 退出再进入后下拉必须跟着回来，否则再次确认会静默换成另一套方案。
      // 越界值（旧数据/手工改库）不采用，保留随机默认。
      const seed = snap?.allocation_seed;
      if (typeof seed === 'number' && seed >= 0 && seed <= 5) {
        setContractVariant(seed + 1);
      }
    } catch {
      setContractSnapshot(null);
    }

    // 2) 生成任务进度：无条件按 active_task_run_id 恢复
    if (!proj.active_task_run_id) {
      setTaskRun(null);
      setGenerating(false);
      return;
    }
    try {
      const tr = await api.examProjects.getTaskRun(courseId, proj.active_task_run_id, token ?? undefined);
      if (tr.status === 'succeeded') {
        // 任务已完成：留在生成阶段展示成功面板，由「进入试卷中心」按钮跳转编辑
        setGenerating(false);
        setTaskRun(tr);
        setCurrentStage('generate');
      } else {
        // 失败/取消态恢复错误面板与「重新生成」入口；进行中则继续轮询
        setGenerating(tr.status === 'queued' || tr.status === 'running' || tr.status === 'waiting_external');
        setTaskRun(tr);
      }
    } catch {
      setTaskRun(null);
      setGenerating(false);
    }
  };

  const openProject = async (proj: ExamProject) => {
    setActiveProject(proj);
    setCurrentStage(stageFromStatus(proj.status));
    setContractSnapshot(null);
    setTaskRun(null);
    void reloadNameMaps();
    if (proj.active_blueprint_version_id) {
      await loadPlanItems(proj);
    }
    await hydrateProjectState(proj);
  };

  const handleCreateBlueprint = async () => {
    if (!activeProject) return;
    try {
      setBpCreating(true);
      const data = await api.knowledge.getPublished(courseId);
      if (data?.published === false || !data?.units || data.units.length === 0) {
        addToast('请先发布知识目录', 'error');
        setBpCreating(false);
        return;
      }

      const units = (data.units || []).map((u) => ({
        unit_id: u.unit_id,
        exam_point_id: u.exam_point_id || u.exam_point_code || '',
        anchor_key: u.anchor_key || '',
        card_ids: u.card_ids || [],
      }));

      const chapter_weights: Record<string, number> = {};
      (data.exam_points || []).forEach((p) => {
        const key = p.anchor_key || p.id;
        // 同一章（anchor_key）下可能有多个考点，权重需累加，而不是后者覆盖前者，
        // 否则 chapter_weights 合计远小于 100，蓝图引擎的章节权重校验会失败。
        if (key && p.weight_value != null) {
          chapter_weights[key] = (chapter_weights[key] ?? 0) + p.weight_value;
        }
      });

      const bp = await api.examProjects.createBlueprint(courseId, activeProject.id, {
        framework_version_id: data.framework_version_id,
        catalog_version_id: data.catalog_version_id,
        type_rules: {},
        chapter_weights,
        units,
      });
      addToast('蓝图已生成', 'success');
      // 刚取过知识目录，直接复用刷新名称映射，保证考点/知识卡列显示中文名
      void reloadNameMaps(data);
      // 直接用创建响应的 blueprint_version_id 更新本地项目状态，界面立即展示蓝图
      // 并开放「进入合同阶段」，不依赖 list 接口的返回（后者可能因时序未包含新版本）。
      const updated: ExamProject = {
        ...activeProject,
        active_blueprint_version_id: bp.blueprint_version_id,
        status: 'blueprint',
      };
      setActiveProject(updated);
      setCurrentStage('blueprint');
      if (bp.plan && bp.plan.length > 0) {
        setPlanItems(bp.plan);
      } else {
        await loadPlanItems(updated);
      }
      await loadProjects();
    } catch (e) {
      addToast('蓝图创建失败: ' + (e as Error).message, 'error');
    } finally {
      setBpCreating(false);
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
    const statusMeta = STATUS_META[sp.status] ?? { label: sp.status, variant: 'default' as const };
    return (
      <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <button
          onClick={() => setActiveProject(null)}
          style={{
            background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)',
            fontSize: '0.8125rem', display: 'flex', alignItems: 'center', gap: '4px',
            padding: 0, marginBottom: '-4px',
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
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
              {sp.paper_version_status && (
                <Badge variant={(PAPER_STATUS_META[sp.paper_version_status] ?? { variant: 'default' }).variant}>
                  试卷 {(PAPER_STATUS_META[sp.paper_version_status] ?? { label: sp.paper_version_status }).label}
                  {sp.paper_version_no ? ' v' + sp.paper_version_no : ''}
                </Badge>
              )}
              <Button
                variant="secondary" size="sm"
                onClick={() => navigate('/courses/' + courseId + '/paper-center?project=' + sp.id)}
                icon={<FileText size={14} />}
              >
                查看试卷
              </Button>
            </div>
          </div>
          <StageStepper current={currentStage} onSelect={setCurrentStage} />
        </div>

        <div className="glass-card" style={{ padding: '24px' }}>
          {currentStage === 'blueprint' && renderBlueprint({
            sp, setStep: setCurrentStage, bpCreating, handleCreateBlueprint, loadPlanItems, planItems, maps,
          })}
          {currentStage === 'contract' && renderContract({
            sp, courseId, setStep: setCurrentStage, contractVariant, setContractVariant,
            contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming,
            contractAllocating, setContractAllocating, addToast, maps, planItems,
          })}
          {currentStage === 'generate' && renderGenerate({
            sp, courseId, token, setStep: setCurrentStage, taskRun, setTaskRun, generating, setGenerating, addToast,
            onOpenPaper: () => navigate('/courses/' + courseId + '/paper-center?project=' + sp.id),
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
            蓝图 → 合同 → 生成，AI 驱动的出卷流水线。试卷的查看、审核与导出请前往「试卷中心」
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
            const sm = STATUS_META[p.status] ?? { label: p.status, variant: 'default' as const };
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
