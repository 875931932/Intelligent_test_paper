import { useState, useEffect, Fragment, type ReactNode } from 'react';
import {
  ChevronRight, ArrowLeft, ArrowRight, RefreshCw, Check, PlayCircle,
  ClipboardList, FileText,
} from 'lucide-react';
import { api } from '@/api/client';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge, ProgressPanel, type ProgressStatus } from '@/components/ui';
import { useNameMaps, type NameMaps } from '@/hooks/useNameMaps';
import { clabel, dlabel, qlabel } from '@/lib/examDisplay';
import type { ContractSnapshot } from '@/api/domains/examProjects';
import type { ExamProject, ExamRules, PlanItem, TaskRun } from '@/types/api';

// ─── Stage pipeline ───
// 流水线只负责「出题」：蓝图 → 合同 → 生成。审核/编辑/定稿/导出属于同一页面
// 的「试卷」页签（PaperPanel），不占流水线阶段。
type StageKey = 'blueprint' | 'contract' | 'generate';
type ToastType = 'success' | 'error' | 'info';
type ToastFn = (message: string, type?: ToastType) => void;

const STAGE_ORDER: StageKey[] = ['blueprint', 'contract', 'generate'];

const STAGE_META: Record<StageKey, { label: string; icon: ReactNode; color: string }> = {
  blueprint: { label: '蓝图', icon: <ClipboardList size={16} />, color: '#0071e3' },
  contract:  { label: '合同', icon: <FileText size={16} />, color: '#5856d6' },
  generate:  { label: '生成', icon: <PlayCircle size={16} />, color: '#34c759' },
};

const STATUS_TO_STAGE: Record<string, StageKey> = {
  draft: 'blueprint',
  blueprint: 'blueprint',
  contract: 'contract',
  generating: 'generate',
  // 后端项目状态仍保留 review/exported（试卷已生成）；归一到生成阶段，
  // 后续查看/审核在「试卷」页签进行，流水线到此为止。
  review: 'generate',
  exported: 'generate',
};

function stageFromStatus(status: string): StageKey {
  return STATUS_TO_STAGE[status] ?? 'blueprint';
}

// 生成阶段的阶段性文案。后端任务只上报「开始 5%」与「完成 100%」两档，
// 中间没有细分百分比，所以这里用轮旋文案 + 已等待时长表达推进感，
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
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 7,
                background: 'none', border: 'none', padding: 0,
                minWidth: 58, cursor: reachable ? 'pointer' : 'not-allowed',
              }}
            >
              <span style={{
                width: 32, height: 32, borderRadius: '50%',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: active || done ? '#fff' : 'var(--text-tertiary)',
                background: active || done ? meta.color : 'rgba(0,0,0,0.05)',
                boxShadow: active ? '0 0 0 4px ' + meta.color + '30' : 'none',
                transition: 'all 0.25s var(--ease-out-expo)',
              }}>
                {done ? <Check size={16} /> : meta.icon}
              </span>
              <span style={{
                fontSize: '0.7rem', fontWeight: active ? 600 : 400,
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

/**
 * 蓝图题型分布与考核规则的比例差异（按分值，容差 1 分）。
 * 蓝图可能在考核规则解析出来之前创建，或按默认分布生成——这时要能看出来并重建。
 */
function findTypeRatioMismatch(
  planItems: PlanItem[],
  rules: ExamRules | null,
): Array<{ question_type: string; expected: number; actual: number }> {
  const ratios = rules?.question_type_ratios ?? [];
  if (ratios.length === 0 || planItems.length === 0) return [];
  const total = planItems.reduce((s, i) => s + (i.score || 0), 0);
  if (total <= 0) return [];
  const actual = new Map<string, number>();
  planItems.forEach((i) => {
    actual.set(i.question_type, (actual.get(i.question_type) || 0) + (i.score || 0));
  });
  const out: Array<{ question_type: string; expected: number; actual: number }> = [];
  ratios.forEach((r) => {
    const expected = (Number(r.ratio) || 0) / 100 * total;
    const got = actual.get(r.question_type) || 0;
    if (Math.abs(got - expected) > 1) {
      out.push({ question_type: r.question_type, expected, actual: got });
    }
  });
  // 蓝图里存在、但考纲比例里没有的题型同样算不一致
  actual.forEach((score, t) => {
    if (!ratios.some((r) => r.question_type === t) && score > 1) {
      out.push({ question_type: t, expected: 0, actual: score });
    }
  });
  return out;
}

function renderBlueprint({
  sp, setStep, bpCreating, handleCreateBlueprint, loadPlanItems, planItems, maps, examRules,
}: {
  sp: ExamProject; setStep: (s: StageKey) => void;
  bpCreating: boolean; handleCreateBlueprint: () => Promise<void>;
  loadPlanItems: (p: ExamProject) => void; planItems: PlanItem[];
  maps: NameMaps;
  examRules: ExamRules | null;
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
    const mismatch = findTypeRatioMismatch(planItems, examRules);
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <StageHeading
          title="蓝图规划"
          right={<Button variant="secondary" size="sm" onClick={() => loadPlanItems(sp)} icon={<RefreshCw size={14} />}>刷新</Button>}
        />
        {mismatch.length > 0 && (
          <div style={{
            padding: '12px 14px', borderRadius: 10, fontSize: '0.8rem', lineHeight: 1.7,
            background: 'var(--warning-subtle)', border: '1px solid rgba(255,149,0,0.3)',
          }}>
            <div style={{ fontWeight: 600, color: 'var(--warning)', marginBottom: '4px' }}>
              这份蓝图的题型比例与「考核规则」不一致
            </div>
            <div style={{ color: 'var(--text-secondary)' }}>
              {mismatch.map((m) => `${qlabel(m.question_type)} 考纲 ${m.expected.toFixed(0)} 分 / 蓝图 ${m.actual.toFixed(0)} 分`).join('；')}
              。通常是蓝图建在考核规则解析出来之前，或当时按默认分布生成。
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginTop: '10px', flexWrap: 'wrap' }}>
              <Button size="sm" loading={bpCreating} onClick={handleCreateBlueprint} icon={<PlayCircle size={16} />}>
                按考核规则重新生成蓝图
              </Button>
              <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>
                会创建新版本蓝图；教师对题位的手动调整将丢失
              </span>
            </div>
          </div>
        )}
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
              } finally {
                setContractConfirming(false);
              }
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

// ═══════════════════════════════════════════════
//  生成进度面板（状态感知）
// ═══════════════════════════════════════════════
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
          <Button onClick={onOpenPaper} icon={<ArrowRight size={16} />}>查看试卷</Button>
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
//  出卷流水线面板
// ═══════════════════════════════════════════════
export default function PipelinePanel({
  sp, courseId, onOpenPaper, onBlueprintCreated,
}: {
  sp: ExamProject;
  courseId: string;
  /** 生成完成：父级切到「试卷」页签继续查看/审核 */
  onOpenPaper: () => void;
  /** 蓝图创建成功：父级用返回的版本号刷新项目，保证后续阶段立即可用 */
  onBlueprintCreated: (blueprintVersionId: string) => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const { maps, reload: reloadNameMaps } = useNameMaps(courseId);

  const [currentStage, setCurrentStage] = useState<StageKey>(() => stageFromStatus(sp.status));
  const [planItems, setPlanItems] = useState<PlanItem[]>([]);
  const [contractSnapshot, setContractSnapshot] = useState<ContractSnapshot | null>(null);
  const [taskRun, setTaskRun] = useState<TaskRun | null>(null);
  const [bpCreating, setBpCreating] = useState(false);
  const [contractConfirming, setContractConfirming] = useState(false);
  const [contractAllocating, setContractAllocating] = useState(false);
  // 「分配方案」默认随机一版：每次新建/刷新项目时不再固定回到第 1 版，
  // 否则每套卷子都从同一套搭配起步。历史种子由 hydrate 覆盖回填。
  const [contractVariant, setContractVariant] = useState(
    () => 1 + Math.floor(Math.random() * 6),
  );
  const [generating, setGenerating] = useState(false);
  // 考核大纲的题型比例：用来核对已有蓝图是不是按考纲比例生成的
  const [examRules, setExamRules] = useState<ExamRules | null>(null);

  useEffect(() => {
    if (!courseId) return;
    api.framework.getCurrent(courseId)
      .then((fw) => setExamRules(fw?.exam_rules ?? null))
      .catch(() => setExamRules(null));
  }, [courseId]);

  const loadPlanItems = async (proj: ExamProject) => {
    try {
      const items = await api.examProjects.getPlanItems(courseId, proj.id);
      setPlanItems(items);
    } catch {
      addToast('加载计划项失败', 'error');
    }
  };

  // 以服务端为权威数据源恢复流水线状态。
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
        // 任务已完成：留在生成阶段展示成功面板，由「查看试卷」按钮切换到试卷页签
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

  // 切换项目时重置并恢复状态
  useEffect(() => {
    setCurrentStage(stageFromStatus(sp.status));
    setContractSnapshot(null);
    setTaskRun(null);
    void reloadNameMaps();
    if (sp.active_blueprint_version_id) {
      void loadPlanItems(sp);
    }
    void hydrateProjectState(sp);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sp.id]);

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
            // 项目摘要是打开项目时取的快照，此刻 paper_version_id 仍为 null，
            // 父级刷新项目后再切到「试卷」页签，否则那边读不到新试卷。
            onOpenPaper();
          } else {
            addToast('生成失败: ' + (tr.error_message || '未知错误'), 'error');
          }
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, 2500);
    return () => clearInterval(id);
    // 轮询闭包有意捕获 taskRun 快照
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskRun, courseId, token, addToast]);

  const handleCreateBlueprint = async () => {
    try {
      setBpCreating(true);
      // 同时取知识目录与当前框架：后者带考核大纲抽取出的考试规则（题型比例/章节权重）
      const [data, fw] = await Promise.all([
        api.knowledge.getPublished(courseId),
        api.framework.getCurrent(courseId).catch(() => null),
      ]);
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

      // 章节权重：优先用考核大纲声明的命题权重（框架 payload 里的 exam_rules），
      // 那才是考纲的硬约束；考纲没声明时才回退到考点权重累加（模型自报）。
      // 未声明的锚点补 0，保证章权重覆盖全部考核单元——蓝图引擎要求一个都不能少。
      const unitAnchors = new Set(units.map((u) => u.anchor_key).filter(Boolean));
      const declared: Record<string, number> = {};
      (fw?.exam_rules?.chapter_weights || []).forEach((c) => {
        if (c.anchor_key) declared[c.anchor_key] = Number(c.weight) || 0;
      });
      let chapter_weights: Record<string, number> = {};
      const declaredKeys = Object.keys(declared);
      if (declaredKeys.length > 0 && declaredKeys.some((k) => declared[k] > 0)) {
        unitAnchors.forEach((a) => {
          chapter_weights[a] = declared[a] ?? 0;
        });
      }
      if (Object.keys(chapter_weights).length === 0) {
        (data.exam_points || []).forEach((p) => {
          const key = p.anchor_key || p.id;
          // 同一章（anchor_key）下可能有多个考点，权重需累加，而不是后者覆盖前者，
          // 否则 chapter_weights 合计远小于 100，蓝图引擎的章节权重校验会失败。
          if (key && p.weight_value != null) {
            chapter_weights[key] = (chapter_weights[key] ?? 0) + p.weight_value;
          }
        });
      }

      const bp = await api.examProjects.createBlueprint(courseId, sp.id, {
        framework_version_id: data.framework_version_id,
        catalog_version_id: data.catalog_version_id,
        type_rules: {},
        chapter_weights,
        units,
      });
      addToast('蓝图已生成', 'success');
      // 刚取过知识目录，直接复用刷新名称映射，保证考点/知识卡列显示中文名
      void reloadNameMaps(data);
      // 通知父级刷新项目（拿回 active_blueprint_version_id），界面立即展示蓝图
      // 并开放「进入合同阶段」，不依赖 list 接口的返回（可能因时序未包含新版本）。
      onBlueprintCreated(bp.blueprint_version_id);
      setCurrentStage('blueprint');
      if (bp.plan && bp.plan.length > 0) {
        setPlanItems(bp.plan);
      }
    } catch (e) {
      addToast('蓝图创建失败: ' + (e as Error).message, 'error');
    } finally {
      setBpCreating(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageStepper current={currentStage} onSelect={setCurrentStage} />
      {currentStage === 'blueprint' && renderBlueprint({
        sp, setStep: setCurrentStage, bpCreating, handleCreateBlueprint, loadPlanItems, planItems, maps, examRules,
      })}
      {currentStage === 'contract' && renderContract({
        sp, courseId, setStep: setCurrentStage, contractVariant, setContractVariant,
        contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming,
        contractAllocating, setContractAllocating, addToast, maps, planItems,
      })}
      {currentStage === 'generate' && renderGenerate({
        sp, courseId, token, setStep: setCurrentStage, taskRun, setTaskRun, generating, setGenerating, addToast,
        onOpenPaper,
      })}
    </div>
  );
}
