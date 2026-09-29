import { useEffect, useRef, useState, type CSSProperties, type FC } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Bot, Check, ChevronDown, ExternalLink, Pencil, Plus, Send, Sparkles, Square, Trash2, X } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useCourseStore } from '@/stores/course';
import { useToastStore } from '@/stores/toast';
import { useAssistantStore } from '@/stores/assistantStore';
import { Badge, Button } from '@/components/ui';
import { StemBlocks } from '@/pages/paper/StemBlocks';
import {
  EXAM_PROJECT_STATUS_META,
  PAPER_STATUS_META,
  qlabel,
} from '@/lib/examDisplay';
import {
  MATERIAL_TYPE_LABELS,
  PARSE_STATUS_LABELS,
  formatDateTime,
} from '@/utils/format';
import type {
  AssistantActionPayload,
  AssistantMaterialRow,
  AssistantMessage,
  CourseCreate,
  CourseUpdate,
} from '@/types/api';

/** 只读工具 → 卡片标题与跳转（结果卡 = 确定性查询，CTA 指向同源页面） */
const READ_TOOL_META: Record<string, { label: string; nav: string; cta: string }> = {
  course_overview: { label: '课程概览', nav: '', cta: '打开概览' },
  list_materials: { label: '资料清单', nav: 'materials', cta: '打开资料库' },
  framework_status: { label: '命题框架状态', nav: 'framework', cta: '打开命题框架' },
  blueprint_status: { label: '蓝图状态', nav: 'paper', cta: '打开试卷页' },
  contract_status: { label: '合同状态', nav: 'paper', cta: '打开试卷页' },
  paper_status: { label: '试卷状态', nav: 'paper', cta: '打开试卷页' },
  list_exam_projects: { label: '试卷项目', nav: 'paper', cta: '打开试卷页' },
};

/** 提案工具 → 操作名与影响说明（确认执行 = 调既有业务 API，无第二套写路径） */
const PROPOSAL_META: Record<string, { label: string; impact: string }> = {
  create_course: {
    label: '新建课程',
    impact: '将在课程空间新增一门课程；不影响当前课程的数据。',
  },
  update_course: {
    label: '修改课程信息',
    impact: '覆盖当前课程的名称 / 别名 / 简介字段。',
  },
  start_parse: {
    label: '发起资料解析',
    impact: '对所选资料执行解析流程；已有解析结果将被覆盖。',
  },
  enqueue_blueprint_suggest: {
    label: '发起蓝图 AI 建议',
    impact: '创建调整建议任务；建议仍需你在试卷页逐条确认后应用。',
  },
  confirm_contract: {
    label: '确认合同（分配落库）',
    impact: '由既有确定性分配算法落库并冻结合同；冻结后只能新建版本继续修改。',
  },
  update_question_type_format: {
    label: '修改题型格式',
    impact: '覆盖该题型的出题格式要求，之后的生成按新格式出题；不影响题型比例/难度/去重。',
  },
};

/** 首屏空状态的引导问题（点击即发送） */
const SUGGESTIONS = [
  '课程现在到哪一步了？',
  '总结教学大纲讲了什么？',
  '有哪些试卷项目？',
];

const thStyle: CSSProperties = {
  border: '1px solid var(--line-strong)',
  padding: '5px 9px',
  textAlign: 'left',
  fontWeight: 600,
  background: 'var(--fill)',
  lineHeight: 1.6,
};

const tdStyle: CSSProperties = {
  border: '1px solid var(--line-strong)',
  padding: '5px 9px',
  lineHeight: 1.6,
};

/** 键值行（概览/框架类信息用） */
function KVTable({ rows }: { rows: Array<[string, string]> }) {
  if (rows.length === 0) return null;
  return (
    <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: '0.82rem', margin: '6px 0' }}>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td style={{ ...tdStyle, width: 110, color: 'var(--text-secondary)' }}>{k}</td>
            <td style={tdStyle}>{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function GridTable({ header, rows }: { header: string[]; rows: string[][] }) {
  if (rows.length === 0) return null;
  return (
    <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: '0.82rem', margin: '6px 0' }}>
      <thead>
        <tr>
          {header.map((h) => (
            <th key={h} style={thStyle}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={i}>
            {row.map((cell, j) => (
              <td key={j} style={tdStyle}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 结果卡正文：按 tool 渲染确定性查询结果（不依赖模型文本，数据即真相） */
function ResultBody({
  tool,
  payload,
}: {
  tool: string;
  payload: AssistantActionPayload;
}) {
  switch (tool) {
    case 'list_materials': {
      const rows: AssistantMaterialRow[] = Array.isArray(payload.materials)
        ? payload.materials
        : [];
      if (rows.length === 0) return <p style={muted}>暂无资料，先到资料库上传。</p>;
      return (
        <GridTable
          header={['名称', '类型', '解析状态']}
          rows={rows.map((m) => [
            m.name,
            MATERIAL_TYPE_LABELS[m.type] ?? m.type,
            m.parse_status ? (PARSE_STATUS_LABELS[m.parse_status] ?? m.parse_status) : '未解析',
          ])}
        />
      );
    }
    case 'course_overview': {
      const mat = payload.materials;
      const count = Array.isArray(mat) ? mat.length : (mat?.count ?? 0);
      const dist = Array.isArray(mat) || !mat ? {} : mat.parse_status;
      const distText =
        Object.entries(dist)
          .map(([st, n]) => `${PARSE_STATUS_LABELS[st] ?? st} ${n}`)
          .join(' · ') || '暂无';
      const fw = payload.framework;
      const cat = payload.catalog;
      const projects = payload.projects ?? [];
      return (
        <>
          <KVTable
            rows={[
              ['课程', payload.course_name ?? '—'],
              ['资料', `${count} 份（${distText}）`],
              ['命题框架', fw ? `v${fw.version_no}（${fw.status}）` : '未构建'],
              ['知识目录', cat ? `v${cat.version_no}（${cat.status}）` : '未发布'],
              [
                '试卷项目',
                projects.length > 0
                  ? projects.map((p) => `${p.name}（${EXAM_PROJECT_STATUS_META[p.status]?.label ?? p.status}）`).join('、')
                  : '暂无',
              ],
            ]}
          />
        </>
      );
    }
    case 'framework_status': {
      const fw = payload.framework;
      if (!fw) return <p style={muted}>尚未构建命题框架，先到命题框架页构建。</p>;
      const rules = fw.exam_rules;
      return (
        <>
          <KVTable
            rows={[
              ['版本', `v${fw.version_no}（${fw.status}）`],
              ['考试形式', rules.exam_form ?? '—'],
              ['时长', rules.duration_minutes ? `${rules.duration_minutes} 分钟` : '—'],
              ['总分', rules.total_score != null ? String(rules.total_score) : '—'],
              [
                '知识目录',
                payload.catalog ? `v${payload.catalog.version_no}（${payload.catalog.status}）` : '未发布',
              ],
            ]}
          />
          <GridTable
            header={['题型', '占比']}
            rows={rules.question_type_ratios.map((r) => [qlabel(r.question_type), `${r.ratio}`])}
          />
        </>
      );
    }
    case 'blueprint_status': {
      const projects = payload.projects ?? [];
      return (
        <GridTable
          header={['项目', '蓝图', '题位', '题型分布']}
          rows={projects.map((p) => {
            const bp = p.blueprint;
            if (!bp) return [p.name, '未生成', '—', '—'];
            const byType = Object.entries(bp.by_type)
              .map(([t, v]) => `${qlabel(t)}×${v.count}`)
              .join('、');
            return [
              p.name,
              `v${bp.version_no}（${bp.confirmed ? '已确认' : bp.status}）`,
              String(bp.item_count),
              byType || '—',
            ];
          })}
        />
      );
    }
    case 'contract_status': {
      const projects = payload.projects ?? [];
      return (
        <GridTable
          header={['项目', '合同']}
          rows={projects.map((p) => {
            const c = p.contract;
            const text = !c || !c.exists
              ? '未生成'
              : c.confirmed
                ? `已确认冻结（${c.slot_count ?? '—'} 题位）`
                : '存在未确认的分配';
            return [p.name, text];
          })}
        />
      );
    }
    case 'paper_status': {
      const projects = payload.projects ?? [];
      return (
        <GridTable
          header={['项目', '试卷']}
          rows={projects.map((p) => {
            const pv = p.paper;
            const text = !pv || !pv.exists
              ? '未生成'
              : `v${pv.version_no}（${PAPER_STATUS_META[pv.status ?? '']?.label ?? pv.status ?? '—'}），待复核 ${pv.needs_review_count ?? 0} 题`;
            return [p.name, text];
          })}
        />
      );
    }
    case 'list_exam_projects': {
      const projects = payload.projects ?? [];
      if (projects.length === 0) return <p style={muted}>暂无试卷项目，到试卷页新建。</p>;
      return (
        <GridTable
          header={['项目', '状态']}
          rows={projects.map((p) => [
            p.name,
            EXAM_PROJECT_STATUS_META[p.status]?.label ?? p.status,
          ])}
        />
      );
    }
    default: {
      // 后端只读工具白名单之外的兜底：如实展示载荷，不编造内容
      return (
        <pre style={{ fontSize: '0.78rem', whiteSpace: 'pre-wrap', color: 'var(--text-secondary)' }}>
          {JSON.stringify(payload, null, 2)}
        </pre>
      );
    }
  }
}

const muted: CSSProperties = { fontSize: '0.82rem', color: 'var(--text-secondary)', margin: '4px 0' };

const cardBox: CSSProperties = {
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--line)',
  background: 'var(--surface-solid)',
  padding: '10px 12px',
  marginTop: 6,
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

/** 结果卡：结构化数据 + 同源页面跳转（教师看完可直接去对应页继续操作） */
function ResultCard({
  message,
  courseId,
  navigate,
}: {
  message: AssistantMessage;
  courseId: string;
  navigate: (to: string) => void;
}) {
  const tool = message.action.tool ?? '';
  const meta = READ_TOOL_META[tool];
  const payload = message.action.payload ?? {};
  // 点名查询的卡片只含一个项目 → CTA 深链到该项目（试卷页支持 ?project= 定位）
  const singleProjectId =
    payload.projects && payload.projects.length === 1 ? payload.projects[0].id : null;
  const go = () => {
    if (!meta || !meta.nav) {
      navigate(`/courses/${courseId}`);
      return;
    }
    const query = meta.nav === 'paper' && singleProjectId ? `?project=${singleProjectId}` : '';
    navigate(`/courses/${courseId}/${meta.nav}${query}`);
  };
  return (
    <div style={cardBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="info">{meta?.label ?? '查询结果'}</Badge>
        <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          {message.content}
        </span>
      </div>
      <ResultBody tool={tool} payload={payload} />
      {meta && (
        <div>
          <Button size="sm" variant="secondary" onClick={go} icon={<ExternalLink size={13} />}>
            {meta.cta}
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * 使用引导卡（usage_guide）：出卷主线六步 + 每步跳转按钮 + 页面导航。
 * 步骤状态（done/current/todo）由后端按课程进度推导，当前步骤高亮；
 * 教师既能看到全流程与自己的位置，也能一键直达对应页面继续操作。
 */
function GuideCard({
  message,
  courseId,
  navigate,
}: {
  message: AssistantMessage;
  courseId: string;
  navigate: (to: string) => void;
}) {
  const payload = message.action.payload ?? {};
  const steps = payload.steps ?? [];
  const pages = payload.pages ?? [];
  const statusMeta: Record<string, { label: string; variant: 'success' | 'info' | 'default' }> = {
    done: { label: '已完成', variant: 'success' },
    current: { label: '进行中', variant: 'info' },
    todo: { label: '未开始', variant: 'default' },
  };
  const nav = (path: string) => navigate(`/courses/${courseId}/${path}`);
  return (
    <div style={cardBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="info">使用引导</Badge>
        <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          {message.content ||
            (payload.current_step ? '出卷全流程与你的当前位置' : '出卷全流程已全部完成')}
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 2 }}>
        {steps.map((s, i) => {
          const meta = statusMeta[s.status] ?? statusMeta.todo;
          const isCurrent = s.status === 'current';
          return (
            <div
              key={s.key}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                flexWrap: 'wrap',
                padding: '8px 12px',
                borderRadius: 'var(--radius-sm)',
                background: isCurrent ? 'var(--accent-subtle)' : 'var(--fill)',
                border: isCurrent ? '1px solid var(--accent-soft)' : '1px solid transparent',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                    {i + 1}.
                  </span>
                  <strong style={{ fontSize: '0.83rem' }}>{s.label}</strong>
                  <Badge variant={meta.variant}>{meta.label}</Badge>
                </div>
                <div style={{ fontSize: '0.76rem', color: 'var(--text-secondary)', marginTop: 2 }}>
                  {s.hint}
                </div>
              </div>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => nav(s.nav)}
                icon={<ExternalLink size={13} />}
              >
                前往
              </Button>
            </div>
          );
        })}
      </div>
      {pages.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
          {pages.map((p) => (
            <Button key={p.label} size="sm" variant="secondary" onClick={() => nav(p.nav)}>
              {p.label}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * 来源引用卡：资料内容问答的命中片段（资料名 + 页/章节 + 摘要）。
 * 正文（模型基于片段的回答）由上方 StemBlocks 渲染，本卡只负责可追溯性：
 * 教师据此可到资料库核对原文；score 不展示（避免把相关度当可信度）。
 */
function SourcesCard({
  message,
  courseId,
  navigate,
}: {
  message: AssistantMessage;
  courseId: string;
  navigate: (to: string) => void;
}) {
  const payload = message.action.payload ?? {};
  const sources = payload.sources ?? [];
  const scope = payload.material_name ? `「${payload.material_name}」` : '课程全部资料';
  return (
    <div style={cardBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="info">资料来源</Badge>
        <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          回答依据 {scope} 的 {sources.length} 处片段
          {payload.mode === 'lexical' ? '（词面匹配）' : ''}
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 2 }}>
        {sources.map((s, i) => {
          const loc = [
            s.material_name,
            s.page_index != null ? `第 ${s.page_index} 页` : '',
            s.heading_path.join(' › '),
          ]
            .filter(Boolean)
            .join(' · ');
          return (
            <div
              key={s.block_id}
              style={{ padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--fill)' }}
            >
              <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                {i + 1}. {loc || '资料片段'}
              </div>
              <p style={{ fontSize: '0.8rem', lineHeight: 1.6, margin: '2px 0 0' }}>
                {s.snippet}
              </p>
            </div>
          );
        })}
      </div>
      <div>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => navigate(`/courses/${courseId}/materials`)}
          icon={<ExternalLink size={13} />}
        >
          打开资料库查看原文
        </Button>
      </div>
    </div>
  );
}

/** 提案参数预览（人话字段名；body 由后端白名单硬校验，展示即执行内容） */
function proposalParamRows(tool: string, payload: AssistantActionPayload): Array<[string, string]> {
  const body = payload.body ?? {};
  switch (tool) {
    case 'create_course':
    case 'update_course': {
      const rows: Array<[string, string]> = [];
      if (typeof body.name === 'string') rows.push(['名称', body.name]);
      if (typeof body.slug === 'string') rows.push(['别名', body.slug]);
      if (typeof body.description === 'string' && body.description) rows.push(['简介', body.description]);
      return rows;
    }
    case 'start_parse':
      return [['资料', payload.material_name || payload.material_id || '—']];
    case 'enqueue_blueprint_suggest':
      return [
        ['项目', payload.project_name || payload.project_id || '—'],
        [
          '要求',
          typeof body.instruction === 'string' && body.instruction ? body.instruction : '（常规检查）',
        ],
      ];
    case 'confirm_contract':
      return [['项目', payload.project_name || payload.project_id || '—']];
    case 'update_question_type_format': {
      const qt = typeof body.question_type === 'string' ? body.question_type : '';
      const template = typeof body.template === 'string' ? body.template : '';
      const brief = (text: string) => (text.length > 60 ? text.slice(0, 60) + '…' : text);
      return [
        ['题型', qlabel(qt)],
        ['现格式', payload.current ? brief(payload.current) : '（系统默认）'],
        ['新格式', template ? brief(template) : '（恢复默认）'],
      ];
    }
    default:
      return Object.entries(body).map(([k, v]) => [k, String(v)]);
  }
}

/**
 * 提案卡：操作名 + 参数预览 + 影响说明 + 确认/取消。
 * 确认后由页面调既有业务 API 执行，成功才回写状态（proposed → executed 单向）；
 * 执行失败卡片保持 proposed，可重试或取消。
 */
function ProposalCard({
  message,
  busy,
  onConfirm,
  onDismiss,
}: {
  message: AssistantMessage;
  busy: boolean;
  onConfirm: (m: AssistantMessage) => void;
  onDismiss: (m: AssistantMessage) => void;
}) {
  const tool = message.action.tool ?? '';
  const meta = PROPOSAL_META[tool];
  const status = message.action.status ?? 'proposed';
  const rows = proposalParamRows(tool, message.action.payload ?? {});
  return (
    <div style={{ ...cardBox, border: '1px dashed var(--accent-soft)', background: 'var(--accent-subtle)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="purple">提案</Badge>
        <strong style={{ fontSize: '0.85rem' }}>{meta?.label ?? tool}</strong>
        <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          {message.content}
        </span>
      </div>
      <KVTable rows={rows} />
      <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '2px 0' }}>
        {meta?.impact ?? '确认后调用既有业务接口执行。'}
      </p>
      {status === 'proposed' ? (
        <div style={{ display: 'flex', gap: 8 }}>
          <Button
            size="sm"
            loading={busy}
            onClick={() => onConfirm(message)}
            icon={<Check size={13} />}
          >
            确认执行
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            onClick={() => onDismiss(message)}
            icon={<X size={13} />}
          >
            取消
          </Button>
        </div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Badge variant={status === 'executed' ? 'success' : 'default'}>
            {status === 'executed' ? '已执行' : '已取消'}
          </Badge>
          {message.action.receipt && (
            <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
              {message.action.receipt}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** 相对时间（会话列表用）：近一天说人话，更早回退到日期时间 */
function relTime(iso: string | null): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const diff = Date.now() - t;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return formatDateTime(iso);
}

const sessionTriggerStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  maxWidth: 240,
  padding: '6px 10px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--line)',
  background: 'var(--surface-solid)',
  color: 'var(--text)',
  fontSize: '0.85rem',
  cursor: 'pointer',
};

const iconBtnStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  flexShrink: 0,
  padding: 4,
  border: 'none',
  borderRadius: 4,
  background: 'transparent',
  color: 'var(--text-secondary)',
  cursor: 'pointer',
};

/**
 * 页头会话选择器（v3 多会话）：下拉列出会话（标题 + 相对时间），
 * 行内改名（Enter/失焦提交、Esc 取消）、删除（行内「确认删除？」是/否），
 * 顶部「新对话」。按 spec §7 不引新组件，全部内联实现。
 */
function SessionBar() {
  const sessions = useAssistantStore((s) => s.sessions);
  const activeSessionId = useAssistantStore((s) => s.activeSessionId);
  const switchSession = useAssistantStore((s) => s.switchSession);
  const createSession = useAssistantStore((s) => s.createSession);
  const renameSession = useAssistantStore((s) => s.renameSession);
  const deleteSession = useAssistantStore((s) => s.deleteSession);
  const addToast = useToastStore((s) => s.addToast);

  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [confirmId, setConfirmId] = useState<string | null>(null);
  // Enter/Esc 提交后编辑框卸载可能再触发 blur：用 ref 挡住第二次提交
  const renameDoneRef = useRef<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  const active = sessions.find((s) => s.id === activeSessionId) ?? null;

  const closeAll = () => {
    setOpen(false);
    setEditingId(null);
    setConfirmId(null);
  };

  // 点击下拉外部关闭（直接操作 setter，不引组件函数进依赖）
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
        setEditingId(null);
        setConfirmId(null);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const handleCreate = () => {
    closeAll();
    void createSession().catch((err) =>
      addToast('新建会话失败: ' + getErrorMessage(err), 'error'),
    );
  };

  const handleSwitch = (sid: string) => {
    closeAll();
    void switchSession(sid).catch(() => {
      /* switchSession 内部已 toast 并回滚 */
    });
  };

  const beginRename = (sid: string, title: string) => {
    renameDoneRef.current = null;
    setConfirmId(null);
    setDraft(title);
    setEditingId(sid);
  };

  const submitRename = (sid: string) => {
    if (renameDoneRef.current === sid) return;
    renameDoneRef.current = sid;
    const title = draft.trim();
    setEditingId(null);
    if (!title) return;
    void renameSession(sid, title).catch((err) =>
      addToast('重命名失败: ' + getErrorMessage(err), 'error'),
    );
  };

  const cancelRename = (sid: string) => {
    renameDoneRef.current = sid;
    setEditingId(null);
  };

  const askDelete = (sid: string) => {
    setEditingId(null);
    setConfirmId(sid);
  };

  const handleDelete = (sid: string) => {
    setConfirmId(null);
    void deleteSession(sid).catch((err) =>
      addToast('删除会话失败: ' + getErrorMessage(err), 'error'),
    );
  };

  return (
    <div ref={boxRef} style={{ position: 'relative', flexShrink: 0 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button size="sm" variant="secondary" onClick={handleCreate} icon={<Plus size={14} />}>
          新对话
        </Button>
        <button
          type="button"
          style={sessionTriggerStyle}
          aria-label="切换会话"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span
            style={{
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {active ? active.title : '无会话'}
          </span>
          <ChevronDown size={14} color="var(--text-secondary)" />
        </button>
      </div>

      {open && (
        <div
          role="listbox"
          aria-label="会话列表"
          style={{
            position: 'absolute',
            right: 0,
            top: 'calc(100% + 6px)',
            width: 300,
            maxHeight: 320,
            overflowY: 'auto',
            zIndex: 30,
            background: 'var(--surface-solid)',
            border: '1px solid var(--line)',
            borderRadius: 'var(--radius-sm)',
            boxShadow: '0 8px 24px rgba(0, 0, 0, 0.12)',
            padding: 6,
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
          }}
        >
          <button
            type="button"
            onClick={handleCreate}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '7px 8px',
              border: 'none',
              borderRadius: 6,
              background: 'transparent',
              color: 'var(--accent)',
              fontSize: '0.82rem',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            <Plus size={14} /> 新对话
          </button>

          {sessions.length === 0 && (
            <p style={{ ...muted, padding: '4px 8px' }}>还没有会话</p>
          )}

          {sessions.map((s) => (
            <div
              key={s.id}
              style={{
                borderRadius: 6,
                background: s.id === activeSessionId ? 'var(--fill)' : 'transparent',
              }}
            >
              {editingId === s.id ? (
                <input
                  className="input-field"
                  style={{ width: '100%', fontSize: '0.82rem', margin: '4px 0' }}
                  value={draft}
                  autoFocus
                  aria-label="会话标题"
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => submitRename(s.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      submitRename(s.id);
                    } else if (e.key === 'Escape') {
                      e.preventDefault();
                      cancelRename(s.id);
                    }
                  }}
                />
              ) : confirmId === s.id ? (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '5px 8px',
                    fontSize: '0.8rem',
                  }}
                >
                  <span style={{ flex: 1, color: 'var(--text-secondary)' }}>确认删除？</span>
                  <Button size="sm" variant="danger" onClick={() => handleDelete(s.id)}>
                    是
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirmId(null)}>
                    否
                  </Button>
                </div>
              ) : (
                <div style={{ display: 'flex', alignItems: 'center', gap: 2, padding: '5px 8px' }}>
                  <button
                    type="button"
                    style={{
                      flex: 1,
                      minWidth: 0,
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'flex-start',
                      gap: 1,
                      textAlign: 'left',
                      border: 'none',
                      background: 'transparent',
                      cursor: 'pointer',
                      padding: 0,
                    }}
                    onClick={() => handleSwitch(s.id)}
                  >
                    <span
                      style={{
                        fontSize: '0.82rem',
                        color: 'var(--text)',
                        maxWidth: '100%',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {s.title}
                    </span>
                    <span style={{ fontSize: '0.7rem', color: 'var(--text-tertiary)' }}>
                      {relTime(s.updated_at ?? s.created_at)}
                    </span>
                  </button>
                  <button
                    type="button"
                    title="重命名"
                    aria-label={`重命名会话「${s.title}」`}
                    style={iconBtnStyle}
                    onClick={() => beginRename(s.id, s.title)}
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    type="button"
                    title="删除"
                    aria-label={`删除会话「${s.title}」`}
                    style={iconBtnStyle}
                    onClick={() => askDelete(s.id)}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const AssistantPage: FC = () => {
  const { courseId = '' } = useParams<{ courseId: string }>();
  const token = useAuthStore((s) => s.token);
  const navigate = useNavigate();
  const addToast = useToastStore((s) => s.addToast);

  const messages = useAssistantStore((s) => s.messages);
  const restored = useAssistantStore((s) => s.restored);
  const activeSessionId = useAssistantStore((s) => s.activeSessionId);
  const streamSessionId = useAssistantStore((s) => s.streamSessionId);
  const restoring = useAssistantStore((s) => s.restoring);
  const sending = useAssistantStore((s) => s.sending);
  const streamText = useAssistantStore((s) => s.streamText);
  const streamHint = useAssistantStore((s) => s.streamHint);
  const restore = useAssistantStore((s) => s.restore);
  const send = useAssistantStore((s) => s.send);
  const stop = useAssistantStore((s) => s.stop);
  const cancelTurn = useAssistantStore((s) => s.cancelTurn);
  const createSession = useAssistantStore((s) => s.createSession);
  const patchProposal = useAssistantStore((s) => s.patchProposal);

  const [input, setInput] = useState('');
  const [busyMessageId, setBusyMessageId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // composer 自适应长高：单行起步（placeholder 垂直居中），输入增长、封顶 120px 后滚动
  const growTa = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  };

  // 挂载恢复（防重复拉取 + 在途续读）；卸载只关流，任务在 worker 里照常跑完
  useEffect(() => {
    if (!courseId) return;
    let cancelled = false;
    void restore(courseId).catch((err) => {
      if (!cancelled) addToast('恢复对话失败: ' + getErrorMessage(err), 'error');
    });
    return () => {
      cancelled = true;
      stop();
    };
  }, [courseId, restore, stop, addToast]);

  // 新消息 / 流式文本变化时滚到底（在途轮次属于别的会话时不动本会话视口）
  useEffect(() => {
    if (sending && streamSessionId && streamSessionId !== activeSessionId) return;
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, streamText, streamHint, sending, streamSessionId, activeSessionId]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput('');
    if (taRef.current) taRef.current.style.height = 'auto'; // 清空回缩一行
    try {
      await send(text);
    } catch (err) {
      setInput(text); // 回填，不丢教师输入
      addToast('发送失败: ' + getErrorMessage(err), 'error');
    }
  };

  /** 停止生成（v3）：协作式取消——POST cancel 后由收口刷新见部分正文与「已停止」徽标 */
  const handleStop = async () => {
    try {
      await cancelTurn();
    } catch (err) {
      addToast('停止失败: ' + getErrorMessage(err), 'error');
    }
  };

  /** 无会话时的「开始新对话」（有会话后走建议问题/输入框，send 自带兜底建会话） */
  const handleNewSession = async () => {
    try {
      await createSession();
    } catch (err) {
      addToast('新建会话失败: ' + getErrorMessage(err), 'error');
    }
  };

  /** 确认提案：助手的写能力上限 = 既有 API 能力，成功后才回写卡片状态 */
  const handleConfirm = async (m: AssistantMessage) => {
    const payload = m.action.payload ?? {};
    const body = payload.body ?? {};
    setBusyMessageId(m.id);
    try {
      let receipt = '';
      switch (m.action.tool) {
        case 'create_course': {
          // body 经后端白名单硬校验（name 必填），运行时形状由 build_proposal_payload 保证
          const created = await api.courses.create(body as unknown as CourseCreate, token ?? undefined);
          useCourseStore.getState().addCourse(created);
          receipt = `已创建课程「${created.name}」`;
          break;
        }
        case 'update_course': {
          const updated = await api.courses.update(
            payload.course_id ?? courseId,
            body as CourseUpdate,
            token ?? undefined,
          );
          // course store 无单条更新动作，就地替换避免侧栏课程名显示陈旧值
          useCourseStore.setState((s) => ({
            courses: s.courses.map((c) => (c.id === updated.id ? updated : c)),
          }));
          receipt = `已更新课程「${updated.name}」`;
          break;
        }
        case 'start_parse': {
          if (!payload.material_id) throw new Error('提案缺少资料 id');
          await api.materials.parse(courseId, payload.material_id, token ?? undefined);
          receipt = '已发起解析，进度见资料库';
          break;
        }
        case 'enqueue_blueprint_suggest': {
          if (!payload.project_id) throw new Error('提案缺少项目 id');
          await api.examProjects.suggestBlueprintAdjustments(
            courseId,
            payload.project_id,
            typeof body.instruction === 'string' ? body.instruction : '',
            token ?? undefined,
          );
          receipt = '已发起蓝图 AI 建议任务，进度见试卷页';
          break;
        }
        case 'confirm_contract': {
          if (!payload.project_id) throw new Error('提案缺少项目 id');
          await api.examProjects.confirmContract(courseId, payload.project_id, {}, token ?? undefined);
          receipt = '合同已确认落库（分配由既有确定性算法执行）';
          break;
        }
        case 'update_question_type_format': {
          if (typeof body.question_type !== 'string') throw new Error('提案缺少题型');
          await api.framework.setQuestionTypeFormat(
            courseId,
            {
              question_type: body.question_type,
              template: typeof body.template === 'string' ? body.template : '',
            },
            token ?? undefined,
          );
          const qtLabel = qlabel(body.question_type);
          receipt = body.template
            ? `已更新「${qtLabel}」出题格式（之后生成生效）`
            : `已恢复「${qtLabel}」系统默认出题格式`;
          break;
        }
        default:
          throw new Error(`不支持的提案操作：${m.action.tool ?? '未知'}`);
      }
      await patchProposal(m.id, 'executed', receipt);
      addToast(receipt, 'success');
    } catch (err) {
      // 执行失败：卡片保持 proposed（回写只在成功后发生），教师可重试或取消
      addToast('执行失败: ' + getErrorMessage(err), 'error');
    } finally {
      setBusyMessageId(null);
    }
  };

  const handleDismiss = async (m: AssistantMessage) => {
    setBusyMessageId(m.id);
    try {
      await patchProposal(m.id, 'dismissed');
    } catch (err) {
      addToast('回写失败: ' + getErrorMessage(err), 'error');
    } finally {
      setBusyMessageId(null);
    }
  };

  // restored 参与判定：切会话拉取的间隙是「已知非空会话加载中」，不闪空状态
  const empty = messages.length === 0 && !sending && !restoring && restored;

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-lg)',
        // 对话页定高：时间线内部滚动，输入框钉在底部
        height: 'calc(100vh - 48px)',
        minHeight: 0,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <div>
          <h1 className="page-title">AI 助手</h1>
          <p className="page-subtitle">
            资料正文问答直达依据片段，查询直达结果，写操作生成提案卡——确认后才执行
          </p>
        </div>
        <SessionBar />
      </div>

      {/* 消息时间线 */}
      <div
        role="log"
        aria-live="polite"
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
          paddingRight: 4,
        }}
      >
        {restoring && messages.length === 0 && (
          <p style={muted}>正在恢复对话…</p>
        )}

        {activeSessionId === null && !restoring && (
          <div style={{ ...cardBox, alignItems: 'flex-start', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="chat-avatar" style={{ width: 32, height: 32 }} aria-hidden>
                <Bot size={17} />
              </span>
              <strong style={{ fontSize: '0.9rem' }}>你好，我是本课程的 AI 助手</strong>
            </div>
            <p style={muted}>
              我能回答课程进度、资料解析、蓝图/合同/试卷状态的问题，也能基于已解析的
              资料正文总结与问答；修改类操作会生成提案卡，由你确认后执行。
            </p>
            <Button onClick={() => void handleNewSession()} icon={<Plus size={15} />}>
              开始新对话
            </Button>
          </div>
        )}

        {activeSessionId !== null && empty && (
          <div style={{ ...cardBox, alignItems: 'flex-start', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="chat-avatar" style={{ width: 32, height: 32 }} aria-hidden>
                <Bot size={17} />
              </span>
              <strong style={{ fontSize: '0.9rem' }}>你好，我是本课程的 AI 助手</strong>
            </div>
            <p style={muted}>
              我能回答课程进度、资料解析、蓝图/合同/试卷状态的问题，也能基于已解析的
              资料正文总结与问答；修改类操作会生成提案卡，由你确认后执行。试试：
            </p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {SUGGESTIONS.map((s) => (
                <Button key={s} size="sm" variant="secondary" className="chat-suggestion" onClick={() => void send(s)}>
                  {s}
                </Button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) =>
          m.role === 'user' ? (
            <div
              key={m.id}
              className="msg-in"
              style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}
            >
              <div
                style={{
                  maxWidth: '78%',
                  background: 'var(--accent)',
                  color: 'var(--surface-solid)',
                  borderRadius: '16px 16px 4px 16px',
                  padding: '9px 13px',
                  fontSize: '0.875rem',
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {m.content}
              </div>
              {m.created_at && (
                <span style={{ fontSize: '0.7rem', color: 'var(--text-tertiary)', marginRight: 4 }}>
                  {formatDateTime(m.created_at)}
                </span>
              )}
            </div>
          ) : (
            <div key={m.id} className="msg-in" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, maxWidth: '88%' }}>
                <span className="chat-avatar" style={{ marginTop: 2 }} aria-hidden>
                  <Bot size={15} />
                </span>
                <div
                  style={{
                    minWidth: 0,
                    borderRadius: '16px 16px 16px 4px',
                    padding: '9px 13px',
                    background: 'var(--surface-solid)',
                    border: '1px solid var(--line)',
                    fontSize: '0.875rem',
                    lineHeight: 1.65,
                    color: 'var(--text)',
                  }}
                >
                {m.action.kind === 'result' ? (
                  m.action.tool === 'usage_guide' ? (
                    <GuideCard message={m} courseId={courseId} navigate={navigate} />
                  ) : (
                    <ResultCard message={m} courseId={courseId} navigate={navigate} />
                  )
                ) : m.action.kind === 'proposal' ? (
                  <ProposalCard
                    message={m}
                    busy={busyMessageId === m.id}
                    onConfirm={(msg) => void handleConfirm(msg)}
                    onDismiss={(msg) => void handleDismiss(msg)}
                  />
                ) : m.action.kind === 'sources' ? (
                  <>
                    <StemBlocks text={m.content} />
                    <SourcesCard message={m} courseId={courseId} navigate={navigate} />
                  </>
                ) : (
                  <StemBlocks text={m.content} />
                )}
                {(m.action.kind === undefined || m.action.kind === 'sources') &&
                  m.stream_status === 'failed' && (
                  <div style={{ marginTop: 6 }}>
                    <Badge variant="warning">网关未走流式，整段返回</Badge>
                  </div>
                )}
                {m.stream_status === 'stopped' && (
                  <div style={{ marginTop: 6 }}>
                    <Badge variant="warning">已停止</Badge>
                  </div>
                )}
                </div>
              </div>
              {m.created_at && (
                <span style={{ fontSize: '0.7rem', color: 'var(--text-tertiary)', marginTop: 3, marginLeft: 4 }}>
                  {formatDateTime(m.created_at)}
                </span>
              )}
            </div>
          ),
        )}

        {/* 在途轮次的流式占位（只在归属本会话时显示；POST 瞬态 session 未知也显示） */}
        {sending && (!streamSessionId || streamSessionId === activeSessionId) && (
          <div className="msg-in" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, maxWidth: '88%' }}>
              <span className="chat-avatar" style={{ marginTop: 2 }} aria-hidden>
                <Bot size={15} />
              </span>
              <div
                style={{
                  minWidth: 0,
                  borderRadius: '16px 16px 16px 4px',
                  padding: '9px 13px',
                  background: 'var(--surface-solid)',
                  border: '1px solid var(--line)',
                  fontSize: '0.875rem',
                  lineHeight: 1.65,
                  color: 'var(--text)',
                }}
              >
                {streamText ? (
                  <span>
                    <StemBlocks text={streamText} />
                    <span className="caret">▍</span>
                  </span>
                ) : (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: 'var(--text-secondary)' }}>
                    <span className="typing-dots">
                      <span className="typing-dot" />
                      <span className="typing-dot" />
                      <span className="typing-dot" />
                    </span>
                    {streamHint ?? '正在思考…'}
                  </span>
                )}
              {streamText && streamHint && (
                <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: 4 }}>
                  {streamHint}
                </div>
              )}
              </div>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* 输入区：composer 白卡承托 + 品牌蓝发送钮（Enter 发送 / Shift+Enter 换行） */}
      <div className="chat-composer" style={{ flexShrink: 0 }}>
        <textarea
          ref={taRef}
          className="input-field"
          style={{ flex: 1, resize: 'none', maxHeight: 120, minHeight: 38 }}
          rows={1}
          value={input}
          disabled={sending}
          placeholder={
            sending
              ? '助手回复中…（点「停止」可中断，已生成内容会保留）'
              : '输入问题，Enter 发送（Shift+Enter 换行）'
          }
          aria-label="给 AI 助手发消息"
          onChange={(e) => { setInput(e.target.value); growTa(e.currentTarget); }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void handleSend();
            }
          }}
        />
        {sending && (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void handleStop()}
            icon={<Square size={14} />}
          >
            停止
          </Button>
        )}
        <button
          type="button"
          className="chat-send"
          onClick={() => void handleSend()}
          disabled={!input.trim() || sending}
          title="发送"
          aria-label="发送消息"
        >
          <Send size={16} />
        </button>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.72rem', color: 'var(--text-tertiary)', flexShrink: 0, marginTop: -8 }}>
        <Sparkles size={12} />
        AI 只读查询与提案：所有写操作都需你在提案卡上确认
      </div>
    </div>
  );
};

export default AssistantPage;
