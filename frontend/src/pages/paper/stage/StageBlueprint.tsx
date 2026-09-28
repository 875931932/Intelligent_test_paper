import { ChevronRight, RefreshCw, PlayCircle } from 'lucide-react';
import { api } from '@/api/client';
import { Button } from '@/components/ui/Button';
import type { NameMaps } from '@/hooks/useNameMaps';
import { qlabel, dlabel, clabel, mlabel, PLAN_DIFFICULTY_OPTIONS } from '@/lib/examDisplay';
import { formatScore } from '@/lib/format';
import type { ExamProject, ExamRules, PlanItem } from '@/types/api';
import { BlueprintSuggestPanel } from './BlueprintSuggestPanel';
import { StageHeading } from './StageHeading';
import { examPointLabel, anchorLabel, type StageKey, type ToastFn } from './stageShared';

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

/** 下拉必须包含当前值，否则遗留词表（历史数据）会让 select 显示成空白 */
function planDifficultyOptions(current: string) {
  return PLAN_DIFFICULTY_OPTIONS.some((o) => o.value === current)
    ? PLAN_DIFFICULTY_OPTIONS
    : [{ value: current, label: dlabel(current) }, ...PLAN_DIFFICULTY_OPTIONS];
}

export function renderBlueprint({
  sp, courseId, setStep, bpCreating, handleCreateBlueprint, loadPlanItems, planItems, maps, examRules, addToast,
}: {
  sp: ExamProject; courseId: string; setStep: (s: StageKey) => void;
  bpCreating: boolean; handleCreateBlueprint: () => Promise<void>;
  loadPlanItems: (p: ExamProject) => void; planItems: PlanItem[];
  maps: NameMaps;
  examRules: ExamRules | null;
  addToast: ToastFn;
}) {
  if (sp.active_blueprint_version_id) {
    // 题位编辑（难度/分值）：后端只允许 draft 蓝图原地改（已确认 → 409），
    // 0.5 步进与总分合理性由服务端校验；改完已分配的合同需重新分配才生效。
    const patchItem = async (
      item: PlanItem,
      changes: { score?: number; difficulty?: string },
      onFail?: () => void,
    ) => {
      try {
        await api.examProjects.updatePlanItem(courseId, item.id, changes);
        addToast('已更新题位；已分配的合同需重新分配后才会采用新值', 'success');
      } catch (err) {
        addToast(err instanceof Error ? err.message : '保存失败', 'error');
        onFail?.();
      } finally {
        void loadPlanItems(sp);
      }
    };
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
            padding: '12px 14px', borderRadius: 'var(--radius-sm)', fontSize: '0.8rem', lineHeight: 1.7,
            background: 'var(--warning-subtle)', border: '1px solid var(--warning-line)',
          }}>
            <div style={{ fontWeight: 600, color: 'var(--warning)', marginBottom: '4px' }}>
              这份蓝图的题型比例与「考核规则」不一致
            </div>
            <div style={{ color: 'var(--text-secondary)' }}>
              {mismatch.map((m) => `${qlabel(m.question_type)} 考纲 ${formatScore(m.expected)} 分 / 蓝图 ${formatScore(m.actual)} 分`).join('；')}
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
                <div style={{ fontSize: '1.7rem', fontWeight: 600, lineHeight: 1 }}>{formatScore(totalScore)}</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>总分 · {planItems.length} 题</div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', flex: 1, minWidth: '220px' }}>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  {typeDist.map(([t, v]) => (
                    <span key={t} style={{
                      padding: '4px 10px', borderRadius: '999px', fontSize: '0.78rem', fontWeight: 600,
                      background: 'var(--accent-subtle)', color: 'var(--accent)',
                    }}>
                      {qlabel(t)} {formatScore(v.score)}分·{v.count}题
                    </span>
                  ))}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 16px' }}>
                  {chapterDist.map(([c, s]) => (
                    <span key={c} style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{anchorLabel(maps, c)}：{formatScore(s)}分</span>
                  ))}
                </div>
              </div>
            </div>
            <BlueprintSuggestPanel
              courseId={courseId}
              projectId={sp.id}
              planItems={planItems}
              reload={() => loadPlanItems(sp)}
              maps={maps}
              addToast={addToast}
            />
            <div className="table-wrapper">
              <table className="data-table">
                <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>考查方式</th><th>章节</th><th>考点</th><th>认知层级</th></tr></thead>
                <tbody>
                  {planItems.map((item) => (
                    <tr key={item.item_index}>
                      <td>{item.item_index}</td>
                      <td>{qlabel(item.question_type)}</td>
                      <td>
                        <input
                          type="number" step={0.5} min={0.5}
                          aria-label={`第${item.item_index}题分值`}
                          defaultValue={item.score}
                          style={{
                            width: '68px', fontSize: '0.85rem', padding: '3px 6px',
                            borderRadius: 'var(--radius-sm)', border: '1px solid var(--line)',
                            background: 'var(--surface-solid)', color: 'var(--text)',
                          }}
                          onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }}
                          onBlur={(e) => {
                            const el = e.currentTarget;
                            const v = Number(el.value);
                            if (!Number.isFinite(v) || v <= 0 || v === item.score) {
                              el.value = String(item.score);
                              return;
                            }
                            void patchItem(item, { score: v }, () => { el.value = String(item.score); });
                          }}
                        />
                      </td>
                      <td>
                        <select
                          aria-label={`第${item.item_index}题难度`}
                          value={item.difficulty}
                          onChange={(e) => {
                            const v = e.target.value;
                            if (v !== item.difficulty) void patchItem(item, { difficulty: v });
                          }}
                          style={{
                            fontSize: '0.82rem', padding: '3px 4px',
                            borderRadius: 'var(--radius-sm)', border: '1px solid var(--line)',
                            background: 'var(--surface-solid)', color: 'var(--text)',
                          }}
                        >
                          {planDifficultyOptions(item.difficulty).map((o) => (
                            <option key={o.value} value={o.value}>{o.label}</option>
                          ))}
                        </select>
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>{mlabel(item.assessment_mode)}</td>
                      <td title={item.anchor_key || undefined}>{item.anchor_key ? anchorLabel(maps, item.anchor_key) : '-'}</td>
                      <td title={item.exam_point_title || item.exam_point_code || item.exam_point_id || undefined}>
                        {item.exam_point_id ? examPointLabel(maps, item.exam_point_id, item.exam_point_title) : '-'}
                      </td>
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
        {/* 不提供「跳过」：合同分配必须读取蓝图题位，没有蓝图时进入合同阶段
            只会撞一个 allocate 404，是条走不通的死路。 */}
        <Button onClick={handleCreateBlueprint} loading={bpCreating} icon={<PlayCircle size={16} />}>创建蓝图</Button>
      </div>
    </div>
  );
}
