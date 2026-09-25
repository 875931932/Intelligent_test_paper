import { ArrowLeft, RefreshCw, Check, PlayCircle, Sparkles } from 'lucide-react';
import { api } from '@/api/client';
import { Button, Badge, FloatingPanel } from '@/components/ui';
import type { NameMaps } from '@/hooks/useNameMaps';
import { qlabel, dlabel } from '@/lib/examDisplay';
import { formatScore } from '@/lib/format';
import { ContractExplainPanel } from '../ContractExplainPanel';
import type { ContractSnapshot } from '@/api/domains/examProjects';
import type { ExamProject, PlanItem, TaskRun } from '@/types/api';
import { StageHeading } from './StageHeading';
import { examPointLabel, cardLabel, type StageKey, type ToastFn } from './stageShared';

export function renderContract({
  sp, courseId, setStep, contractVariant, setContractVariant,
  contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming,
  contractAllocating, setContractAllocating, setTaskRun, addToast, maps, planItems, onProjectChanged,
  explainItem, setExplainItem,
}: {
  sp: ExamProject; courseId: string; setStep: (s: StageKey) => void;
  contractVariant: number; setContractVariant: (n: number) => void;
  contractSnapshot: ContractSnapshot | null; setContractSnapshot: (s: ContractSnapshot | null) => void;
  contractConfirming: boolean; setContractConfirming: (b: boolean) => void;
  contractAllocating: boolean; setContractAllocating: (b: boolean) => void;
  /** 确认合同换 run，旧任务进度随之作废 */
  setTaskRun: (tr: TaskRun | null) => void;
  addToast: ToastFn;
  maps: NameMaps;
  planItems: PlanItem[];
  /** 确认合同会推进项目状态，父级据此刷新页头徽章 */
  onProjectChanged: () => void;
  /** AI 解释面板：当前打开的槽位题位号（null = 关闭） */
  explainItem: number | null; setExplainItem: (i: number | null) => void;
}) {
  // 无蓝图时合同无从分配（后端要读蓝图题位），先拦一道，别让教师点出 404。
  if (!sp.active_blueprint_version_id && !contractSnapshot) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <StageHeading title="分配合同" />
        <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
          合同按蓝图题位分配，当前项目还没有蓝图。请先回到蓝图阶段创建蓝图。
        </p>
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button onClick={() => setStep('blueprint')}><ArrowLeft size={16} /> 返回蓝图</Button>
        </div>
      </div>
    );
  }
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
      // 换了方案，旧解释对应旧分配：一并收起，避免"解释与表格对不上"
      setExplainItem(null);
      addToast(`合同已分配（方案第 ${variant} 版）`, 'success');
    } catch (e) {
      addToast('分配失败: ' + (e as Error).message, 'error');
    } finally {
      setContractAllocating(false);
    }
  };
  // 确认合同：后端会写回 status='generating'，父级项目摘要是打开项目时取的快照，
  // 不刷新的话页头徽章会一直停在「合同阶段」。
  const confirmContract = async (onDone: () => void) => {
    setContractConfirming(true);
    try {
      await api.examProjects.confirmContract(courseId, sp.id, {
        blueprint_version_id: sp.active_blueprint_version_id,
        slot_revisions: [],
        allocation_seed: contractVariant - 1,
      });
      addToast('合同已确认', 'success');
      // 新 run 覆盖了旧 run：任务进度属于旧 run，留在面板上会显示上一版的完成态
      setTaskRun(null);
      onProjectChanged();
      onDone();
    } catch (e) {
      addToast('确认失败: ' + (e as Error).message, 'error');
    } finally {
      setContractConfirming(false);
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
                <li key={b.item_index}>第 {b.item_index} 题：{examPointLabel(maps, b.from_exam_point_id)} → {examPointLabel(maps, b.to_exam_point_id)}（原考点答案域容量不足，改派同章富余考点）</li>
              ))}
            </ul>
          </div>
        )}
        {/* AI 解释：只读建议，锚定悬浮在被点行的下方（不占表格上方空间） */}
        {explainItem !== null && (
          <FloatingPanel
            title={<>AI 解释 · 题位 {explainItem} <Badge variant="purple">只读建议</Badge></>}
            pillLabel={`AI 解释 · 题位 ${explainItem}`}
            anchorSelector={`[data-contract-slot="${explainItem}"]`}
            onClose={() => setExplainItem(null)}
          >
            <ContractExplainPanel
              key={explainItem}
              courseId={courseId}
              projectId={sp.id}
              itemIndex={explainItem}
              allocationSeed={contractVariant - 1}
              blueprintVersionId={sp.active_blueprint_version_id}
            />
          </FloatingPanel>
        )}
        <div className="table-wrapper">
          <table className="data-table">
            <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>考点</th><th>知识卡</th><th>操作</th></tr></thead>
            <tbody>
              {contractSnapshot.slots.map((s) => (
                <tr key={s.item_index} data-contract-slot={s.item_index}>
                  {/* item_index 与 plan_items 同源 1 起、与蓝图表同号，勿 +1 */}
                  <td>{s.item_index}</td>
                  <td>{qlabel(s.question_type)}</td>
                  <td><strong>{formatScore(s.score)}</strong></td>
                  <td>{dlabel(s.difficulty)}</td>
                  <td title={s.exam_point_id || undefined}>{examPointLabel(maps, s.exam_point_id)}</td>
                  <td style={{ maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.card_id ? (maps.cards[s.card_id] || s.card_id) : undefined}>{cardLabel(maps, s.card_id)}</td>
                  <td>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setExplainItem(s.item_index)}
                      icon={<Sparkles size={14} />}
                    >
                      AI 解释
                    </Button>
                  </td>
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
            onClick={() => { void confirmContract(() => setStep('generate')); }}
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
