import { ChevronRight, RefreshCw, PlayCircle } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import type { NameMaps } from '@/hooks/useNameMaps';
import { qlabel, dlabel, clabel } from '@/lib/examDisplay';
import { formatScore } from '@/lib/format';
import type { ExamProject, ExamRules, PlanItem } from '@/types/api';
import { StageHeading } from './StageHeading';
import { examPointLabel, anchorLabel, type StageKey } from './stageShared';

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

export function renderBlueprint({
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
                <div style={{ fontSize: '1.7rem', fontWeight: 700, lineHeight: 1 }}>{formatScore(totalScore)}</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>总分 · {planItems.length} 题</div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', flex: 1, minWidth: '220px' }}>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  {typeDist.map(([t, v]) => (
                    <span key={t} style={{
                      padding: '4px 10px', borderRadius: '999px', fontSize: '0.78rem', fontWeight: 600,
                      background: 'rgba(0,113,227,0.08)', color: '#0071e3',
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
            <div className="table-wrapper">
              <table className="data-table">
                <thead><tr><th>#</th><th>题型</th><th>分值</th><th>难度</th><th>章节</th><th>考点</th><th>认知层级</th></tr></thead>
                <tbody>
                  {planItems.map((item) => (
                    <tr key={item.item_index}>
                      <td>{item.item_index}</td>
                      <td>{qlabel(item.question_type)}</td>
                      <td><strong>{formatScore(item.score)}</strong></td>
                      <td>{dlabel(item.difficulty)}</td>
                      <td title={item.anchor_key || undefined}>{item.anchor_key ? anchorLabel(maps, item.anchor_key) : '-'}</td>
                      <td title={item.exam_point_id || undefined}>{item.exam_point_id ? examPointLabel(maps, item.exam_point_id) : '-'}</td>
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
