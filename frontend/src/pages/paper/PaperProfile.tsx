import {
  Check, ClipboardList, Eye, FileJson, FileSearch, FileText, KeySquare,
  RefreshCw, RotateCcw,
} from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import {
  EXAM_PROJECT_STATUS_META, PAPER_STATUS_META, QUESTION_TYPE_ORDER, dlabel, qlabel,
} from '@/lib/examDisplay';
import { formatScore } from '@/lib/format';
import type { ExamProject, PaperVersion } from '@/types/api';
import { type ExportKind, normalizeAnswer } from './questionShared';

// ─── 试卷档案卡 ───

export function PaperProfile({
  pv, project, examPointCount, onExport, onPreview, onFinalize, onRevert, onRegenerate, onReview,
}: {
  pv: PaperVersion;
  project?: ExamProject;
  examPointCount: number;
  onExport: (kind: ExportKind) => void;
  onPreview: () => void;
  onFinalize: () => void;
  onRevert: () => void;
  onRegenerate: () => void;
  onReview: () => void;
}) {
  const questions = pv.questions;
  const typeAcc = new Map<string, { score: number; count: number }>();
  const diffAcc = new Map<string, number>();
  questions.forEach((q) => {
    const t = typeAcc.get(q.question_type) ?? { score: 0, count: 0 };
    t.score += q.score || 0;
    t.count += 1;
    typeAcc.set(q.question_type, t);
    const dk = q.difficulty || 'medium';
    diffAcc.set(dk, (diffAcc.get(dk) || 0) + 1);
  });
  const pending = questions.filter((q) => q.needs_review || q.needs_review_reason).length;
  const overridden = questions.filter((q) => q.has_override).length;
  const missing = questions.filter((q) => !normalizeAnswer(q.answer)).length;
  const psm = PAPER_STATUS_META[pv.status] ?? { label: pv.status, variant: 'default' as const };
  const orderedTypes = [...typeAcc.keys()].sort(
    (a, b) => QUESTION_TYPE_ORDER.indexOf(a) - QUESTION_TYPE_ORDER.indexOf(b),
  );

  return (
    <div className="glass-card" style={{ padding: '24px' }}>
      <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div style={{ minWidth: 104 }}>
          <div style={{ fontSize: '2rem', fontWeight: 700, lineHeight: 1, letterSpacing: '-0.03em' }}>{pv.total_score}</div>
          <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: 5 }}>
            总分 · {questions.length} 题 · v{pv.version_no}
          </div>
          <div style={{ marginTop: '8px' }}><Badge variant={psm.variant}>{psm.label}</Badge></div>
        </div>

        <div style={{ flex: 1, minWidth: 240, display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {orderedTypes.map((t) => {
              const v = typeAcc.get(t)!;
              return (
                <span key={t} style={{
                  padding: '4px 10px', borderRadius: 999, fontSize: '0.76rem', fontWeight: 600,
                  background: 'var(--accent-subtle)', color: 'var(--accent)',
                }}>
                  {qlabel(t)} {formatScore(v.score)}分·{v.count}题
                </span>
              );
            })}
          </div>
          <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            <span>难度：{['easy', 'medium', 'hard'].map((d) => `${dlabel(d)} ${diffAcc.get(d) ?? 0}`).join(' · ')}</span>
            <span>覆盖 {examPointCount} 个考点</span>
            {overridden > 0 && <span>已修改 {overridden} 题</span>}
            {missing > 0 && <span style={{ color: 'var(--error)', fontWeight: 600 }}>缺答案 {missing} 题</span>}
            {pending > 0 && <span style={{ color: 'var(--warning)', fontWeight: 600 }}>待审核 {pending} 题</span>}
          </div>
          {project && (
            <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              所属项目：{project.name} · {(EXAM_PROJECT_STATUS_META[project.status] ?? { label: project.status }).label}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <Button variant="secondary" size="sm" onClick={onRegenerate} icon={<RefreshCw size={14} />} title="按当前合同重新生成，会创建新版本的试卷">
            重新生成
          </Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('student')} icon={<FileText size={14} />}>学生卷</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('card')} icon={<ClipboardList size={14} />}>答题卡</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('answer')} icon={<KeySquare size={14} />}>答卷</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('json')} icon={<FileJson size={14} />}>答案细则</Button>
          <Button variant="secondary" size="sm" onClick={onPreview} icon={<Eye size={14} />}>整体预览</Button>
          {/* 只读评审对定稿卷同样可用：不按 readonly/finalized 收起 */}
          <Button variant="secondary" size="sm" onClick={onReview} icon={<FileSearch size={14} />} title="AI 对整份试卷稿出一份只读质量评审报告（不含学生答卷评分）">
            AI 质量评审
          </Button>
          {pv.status === 'finalized' ? (
            <Button variant="secondary" size="sm" onClick={onRevert} icon={<RotateCcw size={14} />}>撤销定稿</Button>
          ) : (
            <Button size="sm" onClick={onFinalize} icon={<Check size={14} />}>确认定稿</Button>
          )}
        </div>
      </div>
    </div>
  );
}