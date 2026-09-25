import { useEffect, useState, type CSSProperties } from 'react';
import { Check, ChevronDown, Sparkles } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import type { AiCreateProposal, AiCreateResult, TaskRun } from '@/types/api';
import { DIFFICULTY_LABELS, QUESTION_TYPE_LABELS, toText } from '@/lib/examDisplay';

/** 任务终态（与后端 task_runs 状态机一致） */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

/** 预设只填进指令框（生成要求要教师补全考查点，不能一点就发） */
const PRESETS = [
  '出一道单选题，考查……',
  '出一道多选题，考查……',
  '出一道判断题，考查……',
  '出一道填空题，考查……',
];

/**
 * 新增题目 AI 生成面板：指令 → 轮询生成任务 → 提案 + 校验徽标 →
 * 「填入下方表单」把提案写进 QuestionEditor 草稿，教师微调（含分值）后
 * 走既有「加入试卷」落库。本面板自己不写任何试卷数据。
 */
export function AiCreatePanel({
  courseId, pvId, onFill,
}: {
  courseId: string;
  pvId: string;
  /** 把通过校验的提案交给父级填进新增表单 */
  onFill: (proposal: AiCreateProposal) => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<AiCreateResult | null>(null);
  const [filled, setFilled] = useState(false);
  // 默认折叠成一条：新增弹窗首屏应是出题表单，AI 生成是可选入口。
  // 折叠只卸载内容块，组件本身不卸载——轮询与已输入的指令都保留。
  const [collapsed, setCollapsed] = useState(true);

  // 轮询生成任务（与 AiRevisePanel 同款：依赖只取 id，终态自停）
  useEffect(() => {
    if (!taskRunId) return;
    const timer = setInterval(async () => {
      try {
        const tr: TaskRun = await api.examProjects.getTaskRun(courseId, taskRunId, token ?? undefined);
        if (!TERMINAL.has(tr.status)) return;
        clearInterval(timer);
        setTaskRunId(null);
        setBusy(false);
        if (tr.status === 'succeeded' && tr.result) {
          setResult(tr.result as unknown as AiCreateResult);
          setFilled(false);
        } else if (tr.status === 'failed') {
          addToast('AI 生成题目失败: ' + (tr.error_message || '未知错误'), 'error');
        }
      } catch {
        /* 瞬时轮询错误忽略，下一轮重试 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [taskRunId, courseId, token, addToast]);

  const submit = async () => {
    const ins = instruction.trim();
    if (!ins) {
      addToast('请先填写出题要求', 'info');
      return;
    }
    setBusy(true);
    setResult(null);
    setFilled(false);
    try {
      const res = await api.paperVersions.aiGenerate(courseId, pvId, ins, token ?? undefined);
      setTaskRunId(res.task_run_id);
    } catch (e) {
      setBusy(false);
      addToast('发起 AI 生成失败: ' + getErrorMessage(e), 'error');
    }
  };

  const running = busy || !!taskRunId;
  const canFill = !!result?.validation.passed && !filled;
  const p = result?.proposal;

  const fill = () => {
    if (!p || !canFill) return;
    onFill(p);
    setFilled(true);
    addToast('提案已填入表单，请核对分值后点「加入试卷」', 'success');
  };

  const rootStyle: CSSProperties = {
    borderLeft: '3px solid var(--purple, #7c5cff)',
    display: 'flex',
    flexDirection: 'column',
    gap: '12px',
  };

  // 折叠条：整条可点。折叠态只渲染这一条（首屏让位给出题表单），
  // 展开态在其下方带出指令区与提案预览
  const toggleBar = (
    <button
      type="button"
      onClick={() => setCollapsed((v) => !v)}
      aria-expanded={!collapsed}
      style={{
        display: 'flex', alignItems: 'center', gap: '8px', width: '100%',
        background: 'none', border: 'none', padding: 0, cursor: 'pointer',
        textAlign: 'left', color: 'var(--text)',
      }}
    >
      <Sparkles size={16} style={{ color: 'var(--purple, #7c5cff)', flexShrink: 0 }} />
      <span style={{ fontWeight: 700, fontSize: '0.9rem' }}>AI 生成题目</span>
      <Badge variant="purple">提案需确认</Badge>
      {running && <Spinner size="sm" />}
      {collapsed && result && <Badge variant="success">已生成提案</Badge>}
      <span style={{ marginLeft: 'auto', fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
        {collapsed ? '展开' : '收起'}
      </span>
      <ChevronDown
        size={14}
        style={{
          color: 'var(--text-tertiary)', flexShrink: 0,
          transform: collapsed ? 'rotate(0deg)' : 'rotate(180deg)',
          transition: 'transform 0.2s',
        }}
      />
    </button>
  );

  if (collapsed) {
    return (
      <div className="glass-card" style={{ ...rootStyle, padding: '10px 14px' }}>
        {toggleBar}
      </div>
    );
  }

  return (
    <div className="glass-card" style={{ ...rootStyle, padding: '16px 24px 16px 22px' }}>
      {toggleBar}

      {/* 指令输入在面板底部（见下），提案预览可任意加长而不挤压输入区 */}

      {result && p && (
        <>
          {/* 校验徽标 + AI 自述 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            {result.validation.passed
              ? <Badge variant="success">校验通过</Badge>
              : (
                  <span title={result.validation.code}>
                    <Badge variant="error">未通过校验</Badge>
                  </span>
                )}
            {!result.validation.passed && (
              <span style={{ fontSize: '0.78rem', color: 'var(--error)' }}>{result.validation.message}</span>
            )}
          </div>
          {result.change_summary && (
            <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.7, margin: 0 }}>
              <span style={{ fontWeight: 600, color: 'var(--purple, #7c5cff)' }}>AI 说明：</span>
              {result.change_summary}
            </p>
          )}

          {/* 提案预览：整题展示（与改题 diff 不同，这里没有原题侧） */}
          <div
            style={{
              display: 'flex', flexDirection: 'column', gap: '8px',
              padding: '10px 12px', borderRadius: 8,
              background: 'var(--success-subtle, rgba(22,163,74,0.08))',
              fontSize: '0.85rem', lineHeight: 1.7, color: 'var(--text-secondary)',
            }}
          >
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <Badge variant="info">{QUESTION_TYPE_LABELS[p.question_type] ?? p.question_type}</Badge>
              {p.difficulty && <Badge variant="default">{DIFFICULTY_LABELS[p.difficulty] ?? p.difficulty}</Badge>}
            </div>
            <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text)' }}>{toText(p.stem)}</div>
            {p.options != null && (
              <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{toText(p.options)}</div>
            )}
            <div>
              <span style={{ fontWeight: 600, fontSize: '0.78rem' }}>答案：</span>
              {toText(p.answer)}
            </div>
            {p.explanation && (
              <div>
                <span style={{ fontWeight: 600, fontSize: '0.78rem' }}>解析：</span>
                {p.explanation}
              </div>
            )}
          </div>

          {/* 确认：把提案写进下方新增表单；落库仍由教师点「加入试卷」完成 */}
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', paddingTop: '10px', borderTop: '1px solid rgba(0,0,0,0.06)' }}>
            <Button size="sm" disabled={!canFill} onClick={fill} icon={<Check size={14} />}>
              填入下方表单
            </Button>
            {filled && <Badge variant="success">已填入表单</Badge>}
            {!result.validation.passed && (
              <span style={{ fontSize: '0.75rem', color: 'var(--warning)' }}>
                校验未通过，不能填入——可调整要求后重新生成
              </span>
            )}
          </div>
        </>
      )}

      {/* 指令输入（悬浮）：置于面板末尾并 sticky 钉住滚动容器（modal-body 所在
          的 .modal-content）底边，提案再长也不会把输入条推出视野；不透明背景 +
          阴影保证浮在预览之上时文字不透出，z-index 低于 modal 遮罩与 toast */}
      <div
        style={{
          display: 'flex', flexDirection: 'column', gap: '8px',
          position: 'sticky', bottom: 12, zIndex: 5,
          padding: '12px 14px', borderRadius: 12,
          background: 'var(--surface-elevated)',
          backdropFilter: 'var(--glass-blur)',
          WebkitBackdropFilter: 'var(--glass-blur)',
          border: '1px solid rgba(0,0,0,0.06)',
          boxShadow: '0 8px 32px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.05)',
        }}
      >
        <textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="告诉 AI 要出什么题（题型 + 考查点，如：出一道单选题，考查进程与线程的区别）"
          rows={2}
          disabled={running}
          style={{
            resize: 'vertical', padding: '8px 10px', borderRadius: 8, fontSize: '0.875rem',
            border: '1px solid rgba(0,0,0,0.12)', background: 'rgba(255,255,255,0.75)',
            fontFamily: 'inherit', lineHeight: 1.6,
          }}
        />
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
          {PRESETS.map((preset) => (
            <button
              key={preset}
              disabled={running}
              onClick={() => setInstruction(preset)}
              style={{
                padding: '3px 10px', borderRadius: 999, fontSize: '0.75rem', fontWeight: 600,
                background: 'rgba(124,92,255,0.08)', color: 'var(--purple, #7c5cff)',
                border: '1px solid rgba(124,92,255,0.25)', cursor: running ? 'default' : 'pointer',
                opacity: running ? 0.5 : 1,
              }}
            >
              {preset}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <Button size="sm" loading={running} onClick={() => void submit()} icon={<Sparkles size={14} />}>
            {running ? 'AI 生成中…（约几秒）' : result ? '再生成一版' : 'AI 生成题目'}
          </Button>
          {result && !running && (
            <Button size="sm" variant="ghost" onClick={() => { setResult(null); setFilled(false); }}>清空提案</Button>
          )}
        </div>
      </div>
    </div>
  );
}
