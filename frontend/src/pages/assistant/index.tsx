import { useEffect, useRef, useState, type CSSProperties, type FC } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Bot, Check, ExternalLink, Send, Sparkles, X } from 'lucide-react';
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
};

/** 首屏空状态的引导问题（点击即发送） */
const SUGGESTIONS = [
  '课程现在到哪一步了？',
  '资料解析状态怎么样？',
  '有哪些试卷项目？',
];

const thStyle: CSSProperties = {
  border: '1px solid rgba(0, 0, 0, 0.18)',
  padding: '5px 9px',
  textAlign: 'left',
  fontWeight: 600,
  background: 'rgba(0, 0, 0, 0.04)',
  lineHeight: 1.6,
};

const tdStyle: CSSProperties = {
  border: '1px solid rgba(0, 0, 0, 0.18)',
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
  borderRadius: 10,
  border: '1px solid var(--border, #d2d2d7)',
  background: 'var(--surface, #fff)',
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
    <div style={{ ...cardBox, border: '1px dashed rgba(0,113,227,0.45)', background: 'var(--accent-subtle)' }}>
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

const AssistantPage: FC = () => {
  const { courseId = '' } = useParams<{ courseId: string }>();
  const token = useAuthStore((s) => s.token);
  const navigate = useNavigate();
  const addToast = useToastStore((s) => s.addToast);

  const messages = useAssistantStore((s) => s.messages);
  const restoring = useAssistantStore((s) => s.restoring);
  const sending = useAssistantStore((s) => s.sending);
  const streamText = useAssistantStore((s) => s.streamText);
  const streamHint = useAssistantStore((s) => s.streamHint);
  const restore = useAssistantStore((s) => s.restore);
  const send = useAssistantStore((s) => s.send);
  const stop = useAssistantStore((s) => s.stop);
  const patchProposal = useAssistantStore((s) => s.patchProposal);

  const [input, setInput] = useState('');
  const [busyMessageId, setBusyMessageId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

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

  // 新消息 / 流式文本变化时滚到底
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, streamText, streamHint]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput('');
    try {
      await send(text);
    } catch (err) {
      setInput(text); // 回填，不丢教师输入
      addToast('发送失败: ' + getErrorMessage(err), 'error');
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

  const empty = messages.length === 0 && !sending && !restoring;

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
      <div>
        <h1 className="page-title">AI 助手</h1>
        <p className="page-subtitle">
          问答与查询直达结果，写操作生成提案卡——确认后才执行
        </p>
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

        {empty && (
          <div style={{ ...cardBox, alignItems: 'flex-start', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Bot size={18} color="var(--accent)" />
              <strong style={{ fontSize: '0.9rem' }}>你好，我是本课程的 AI 助手</strong>
            </div>
            <p style={muted}>
              我能回答课程进度、资料解析、蓝图/合同/试卷状态的问题；
              修改类操作会生成提案卡，由你确认后执行。试试：
            </p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {SUGGESTIONS.map((s) => (
                <Button key={s} size="sm" variant="secondary" onClick={() => void send(s)}>
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
              style={{ display: 'flex', justifyContent: 'flex-end' }}
            >
              <div
                style={{
                  maxWidth: '78%',
                  background: 'var(--accent)',
                  color: '#fff',
                  borderRadius: '14px 14px 4px 14px',
                  padding: '8px 12px',
                  fontSize: '0.875rem',
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {m.content}
              </div>
            </div>
          ) : (
            <div key={m.id} style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
              <div
                style={{
                  maxWidth: '88%',
                  borderRadius: '14px 14px 14px 4px',
                  padding: '8px 12px',
                  background: 'var(--surface, #fff)',
                  border: '1px solid var(--border, #d2d2d7)',
                  fontSize: '0.875rem',
                  lineHeight: 1.65,
                  color: 'var(--text)',
                  minWidth: 180,
                }}
              >
                {m.action.kind === 'result' ? (
                  <ResultCard message={m} courseId={courseId} navigate={navigate} />
                ) : m.action.kind === 'proposal' ? (
                  <ProposalCard
                    message={m}
                    busy={busyMessageId === m.id}
                    onConfirm={(msg) => void handleConfirm(msg)}
                    onDismiss={(msg) => void handleDismiss(msg)}
                  />
                ) : (
                  <StemBlocks text={m.content} />
                )}
                {m.action.kind === undefined && m.stream_status === 'failed' && (
                  <div style={{ marginTop: 6 }}>
                    <Badge variant="warning">网关未走流式，整段返回</Badge>
                  </div>
                )}
              </div>
              {m.created_at && (
                <span style={{ fontSize: '0.7rem', color: 'var(--text-tertiary)', marginTop: 3, marginLeft: 4 }}>
                  {formatDateTime(m.created_at)}
                </span>
              )}
            </div>
          ),
        )}

        {/* 在途轮次的流式占位 */}
        {sending && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
            <div
              style={{
                maxWidth: '88%',
                borderRadius: '14px 14px 14px 4px',
                padding: '8px 12px',
                background: 'var(--surface, #fff)',
                border: '1px solid var(--border, #d2d2d7)',
                fontSize: '0.875rem',
                lineHeight: 1.65,
                color: 'var(--text)',
                minWidth: 180,
              }}
            >
              {streamText ? (
                <span>
                  <StemBlocks text={streamText} />
                  <span style={{ opacity: 0.55 }}>▍</span>
                </span>
              ) : (
                <span style={{ color: 'var(--text-secondary)' }}>{streamHint ?? '正在思考…'}</span>
              )}
              {streamText && streamHint && (
                <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: 4 }}>
                  {streamHint}
                </div>
              )}
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* 输入区 */}
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexShrink: 0 }}>
        <textarea
          className="input-field"
          style={{ flex: 1, resize: 'none' }}
          rows={2}
          value={input}
          disabled={sending}
          placeholder={sending ? '助手回复中…' : '输入问题，Enter 发送（Shift+Enter 换行）'}
          aria-label="给 AI 助手发消息"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void handleSend();
            }
          }}
        />
        <Button
          onClick={() => void handleSend()}
          disabled={!input.trim() || sending}
          icon={<Send size={16} />}
        >
          发送
        </Button>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.72rem', color: 'var(--text-tertiary)', flexShrink: 0, marginTop: -8 }}>
        <Sparkles size={12} />
        AI 只读查询与提案：所有写操作都需你在提案卡上确认
      </div>
    </div>
  );
};

export default AssistantPage;
