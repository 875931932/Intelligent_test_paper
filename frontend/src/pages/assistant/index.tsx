import { useEffect, useMemo, useRef, useState, type CSSProperties, type FC } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowUpRight, Bot, Check, ChevronDown, ExternalLink, Eye, Pencil, Plus, Send, Sparkles, Square, Trash2, X } from 'lucide-react';
import { getErrorMessage } from '@/api/errors';
import { executeProposalAction, RELAY_TOOLS } from '@/lib/assistantProposalExecution';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { useAssistantStore } from '@/stores/assistantStore';
import { Badge, Button, MarkdownText, Modal } from '@/components/ui';
import { formatScore } from '@/lib/format';
import {
  ASSESSMENT_MODE_LABELS,
  PAPER_STATUS_META,
  dlabel,
  projectStatusMeta,
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
  AssistantOverviewSection,
  AssistantPlanPreview,
  GenerationProgressSnapshot,
} from '@/types/api';

/**
 * 结果卡视图 → 标题与同源页面跳转（CTA）。六个状态切片已合并进
 * `course_overview`，用 section 选视图；卡片按 section 取这里的元数据。
 */
const SECTION_META: Record<AssistantOverviewSection, { label: string; nav: string; cta: string }> = {
  overview: { label: '课程概览', nav: '', cta: '打开概览' },
  materials: { label: '资料清单', nav: 'materials', cta: '打开资料库' },
  framework: { label: '命题框架状态', nav: 'framework', cta: '打开命题框架' },
  blueprint: { label: '蓝图状态', nav: 'paper', cta: '打开试卷页' },
  contract: { label: '合同状态', nav: 'paper', cta: '打开试卷页' },
  paper: { label: '试卷状态', nav: 'paper', cta: '打开试卷页' },
  projects: { label: '试卷项目', nav: 'paper', cta: '打开试卷页' },
};

/** 使用引导卡（无 section 概念，单独一份元数据） */
const GUIDE_META = { label: '使用引导', nav: '', cta: '打开概览' };

/**
 * 历史卡片兜底：合并前的旧只读工具名 → 现在的 section。
 * 改造前落库的消息 action.tool 仍是旧名，刷新后要照样能渲染。
 */
const LEGACY_TOOL_SECTIONS: Record<string, AssistantOverviewSection> = {
  list_materials: 'materials',
  framework_status: 'framework',
  blueprint_status: 'blueprint',
  contract_status: 'contract',
  paper_status: 'paper',
  list_exam_projects: 'projects',
};

/** 结果卡这次要呈现哪一部分：新卡片读 payload.section，历史卡片按 tool 名兜底 */
function readSection(message: AssistantMessage): AssistantOverviewSection {
  const section = message.action.payload?.section;
  if (section) return section;
  return LEGACY_TOOL_SECTIONS[message.action.tool ?? ''] ?? 'overview';
}

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
  create_exam_project: {
    label: '创建试卷项目',
    impact: '在当前课程新增一个试卷项目；不影响既有项目的数据。',
  },
  update_exam_rules: {
    label: '修改考核规则',
    impact: '覆盖题型比例 / 章节权重 / 考试侧重点；蓝图创建时按新规则确定性生成，已存在的蓝图不受影响。',
  },
  create_blueprint: {
    label: '创建草稿蓝图',
    impact: '按考核规则与知识目录确定性生成题位计划（含难度分布）；已有合同将随蓝图重建失效。',
  },
  confirm_blueprint: {
    label: '确认蓝图（里程碑）',
    impact: '确认后蓝图题位计划冻结，作为合同分配依据；要调整需新建蓝图版本或先发起 AI 建议。',
  },
  enqueue_blueprint_suggest: {
    label: '发起蓝图 AI 建议',
    impact: '创建调整建议任务；建议仍需你在试卷页逐条确认后应用。',
  },
  confirm_contract: {
    label: '确认合同（分配落库）',
    impact: '由既有确定性分配算法落库并冻结合同；冻结后只能新建版本继续修改。',
  },
  start_generation: {
    label: '发起 AI 生成',
    impact: '按已确认合同分批生成题目；生成期间可在试卷页查看进度。',
  },
  enqueue_paper_review: {
    label: '发起整卷 AI 评审',
    impact: '创建只读质量评审任务（不修改任何数据）；报告生成后在试卷页查看。',
  },
  enqueue_framework_review: {
    label: '发起框架 AI 评审',
    impact: '创建只读评审任务（不修改任何数据）；报告生成后在命题框架页查看。',
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

/**
 * 提案卡详情弹窗的跳转目标：蓝图/合同类走试卷页阶段深链（?stage=），
 * 考核规则回命题框架页——「详情看完了，去真实页面继续编辑」一键直达。
 */
const DETAIL_TARGETS: Record<string, { label: string; stage?: 'blueprint' | 'contract' | 'generate' }> = {
  create_blueprint: { label: '去蓝图页编辑', stage: 'blueprint' },
  confirm_blueprint: { label: '去蓝图页编辑', stage: 'blueprint' },
  confirm_contract: { label: '去合同页编辑', stage: 'contract' },
  start_generation: { label: '去合同页查看', stage: 'contract' },
};

/** 详情弹窗标题（与气泡里的 chip 文案同源） */
const DETAIL_TITLES: Record<string, string> = {
  update_exam_rules: '考核规则逐项对比',
  create_blueprint: '蓝图生成依据',
  confirm_blueprint: '蓝图题位计划',
  confirm_contract: '合同槽位明细',
  start_generation: '确认合同槽位（生成依据）',
};

/**
 * 消息时间戳：今天只给时分，更早带上月-日——气泡下的近距表达，减少噪音。
 * （会话列表的绝对时间仍走 formatDateTime）
 */
function msgTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (d.toDateString() === new Date().toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`;
}

/** 综合题原型词表（与后端 ARCHETYPE_CONTRACTS 同键；裸英文不进卡片） */
const ARCHETYPE_LABELS: Record<string, string> = {
  code_completion_scenario: '代码补全场景',
  case_analysis: '案例分析',
  fault_diagnosis: '故障诊断',
  comparative_decision: '比较决策',
  solution_design: '方案设计',
  process_optimization: '流程优化',
  critique_correction: '评析纠错',
  integrated_explanation: '综合阐释',
};

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

/** 结果卡正文：按 section 渲染确定性查询结果（不依赖模型文本，数据即真相） */
function ResultBody({
  section,
  payload,
}: {
  section: AssistantOverviewSection;
  payload: AssistantActionPayload;
}) {
  switch (section) {
    case 'materials': {
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
    case 'overview': {
      const mat = payload.materials;
      // 新卡片恒为资料行数组，历史卡片可能是 {count, parse_status} 摘要
      const count = Array.isArray(mat) ? mat.length : (mat?.count ?? 0);
      const dist = Array.isArray(mat)
        ? mat.reduce<Record<string, number>>((acc, m) => {
            const key = m.parse_status;
            if (key) acc[key] = (acc[key] ?? 0) + 1;
            return acc;
          }, {})
        : (mat?.parse_status ?? {});
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
                  ? projects.map((p) => `${p.name}（${projectStatusMeta(p).label}）`).join('、')
                  : '暂无',
              ],
            ]}
          />
        </>
      );
    }
    case 'framework': {
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
    case 'blueprint': {
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
    case 'contract': {
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
    case 'paper': {
      const projects = payload.projects ?? [];
      return (
        <GridTable
          header={['项目', '试卷', '题量', '题型分布']}
          rows={projects.map((p) => {
            const pv = p.paper;
            if (!pv || !pv.exists) return [p.name, '未生成', '—', '—'];
            const text = `v${pv.version_no}（${PAPER_STATUS_META[pv.status ?? '']?.label ?? pv.status ?? '—'}），待复核 ${pv.needs_review_count ?? 0} 题`;
            // 成卷口径（实际落卷）：与蓝图「计划题位」不是一个数时以这里为准
            const byType = Object.entries(pv.by_type ?? {})
              .map(([t, v]) => `${qlabel(t)}×${v.count}`)
              .join('、');
            return [
              p.name,
              text,
              `${pv.item_count ?? 0} 题 · ${pv.total_score ?? 0} 分`,
              byType || '—',
            ];
          })}
        />
      );
    }
    case 'projects': {
      const projects = payload.projects ?? [];
      if (projects.length === 0) return <p style={muted}>暂无试卷项目，到试卷页新建。</p>;
      return (
        <GridTable
          header={['项目', '状态']}
          rows={projects.map((p) => [
            p.name,
            projectStatusMeta(p).label,
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

/** 结构化卡片的内布局（外观由 .chat-card 承载：白底/描边/柔影/hover 动效） */
const cardBox: CSSProperties = {
  padding: '14px 16px',
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  minWidth: 0,
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
  const isGuide = tool === 'usage_guide';
  const section = readSection(message);
  const meta = isGuide ? GUIDE_META : SECTION_META[section];
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
    <div className="chat-card" style={cardBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="info">{meta?.label ?? '查询结果'}</Badge>
        <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          {message.content}
        </span>
      </div>
      <ResultBody section={section} payload={payload} />
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
    <div className="chat-card" style={cardBox}>
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
 * 正文（模型基于片段的回答）由上方 MarkdownText 渲染，本卡只负责可追溯性：
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
    <div className="chat-card" style={cardBox}>
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
    case 'create_exam_project':
      return [['项目名称', typeof body.name === 'string' ? body.name : '—']];
    case 'update_exam_rules':
      // 逐项对比（现值/新值两列 + 变化高亮）由 SpecTable 渲染——
      // 原来把「A、B、C → A、B、C」挤成一行长串，既不好看也没法逐项对比
      return [];
    case 'create_blueprint':
      // 综合题原型与生成依据（考核规则）由 SpecTable 渲染
      return [['项目', payload.project_name || payload.project_id || '—']];
    case 'enqueue_blueprint_suggest':
      return [
        ['项目', payload.project_name || payload.project_id || '—'],
        [
          '要求',
          typeof body.instruction === 'string' && body.instruction ? body.instruction : '（常规检查）',
        ],
      ];
    case 'enqueue_paper_review':
      return [
        ['项目', payload.project_name || payload.project_id || '—'],
        [
          '试卷',
          typeof payload.paper_version_no === 'number' ? `v${payload.paper_version_no}` : '—',
        ],
        [
          '关注点',
          typeof body.instruction === 'string' && body.instruction ? body.instruction : '（常规评审）',
        ],
      ];
    case 'enqueue_framework_review':
      return [
        [
          '框架',
          typeof payload.framework_version_no === 'number'
            ? `v${payload.framework_version_no}（当前）`
            : '当前框架',
        ],
        [
          '关注点',
          typeof body.instruction === 'string' && body.instruction ? body.instruction : '（常规评审）',
        ],
      ];
    case 'confirm_blueprint':
    case 'start_generation':
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
 * 提案卡实物预览：蓝图题位计划 / 已确认合同槽位——教师点「确认」之前，
 * 先在详情弹窗里看到「确认的到底是什么」，而不是只有一行项目名。
 * 数据由后端在提案时现势读库附带（payload.preview），历史卡片没有则不渲染。
 * expanded=true（弹窗内）：表体给到 58vh 限高，超出屏高在表内滚动。
 */
function ProposalPreview({
  tool,
  preview,
  expanded = false,
}: {
  tool: string;
  preview?: AssistantPlanPreview;
  expanded?: boolean;
}) {
  if (!preview || preview.items.length === 0) return null;
  const contract = preview.source === 'contract';
  const ver = preview.version_no != null ? ` v${preview.version_no}` : '';
  const title = contract
    ? '已确认合同槽位'
    : tool === 'confirm_contract'
      ? `蓝图${ver}题位（合同分配基础）`
      : `蓝图${ver}题位计划`;
  const header = contract
    ? ['#', '题型', '分值', '难度', '考点', '知识卡']
    : ['#', '题型', '分值', '难度', '考查方式', '考点'];
  const rows = preview.items.map((it) =>
    contract
      ? [
          String(it.item_index),
          qlabel(it.question_type),
          formatScore(it.score),
          dlabel(it.difficulty),
          it.exam_point || '—',
          it.knowledge_card || '—',
        ]
      : [
          String(it.item_index),
          qlabel(it.question_type),
          formatScore(it.score),
          dlabel(it.difficulty),
          it.assessment_mode
            ? (ASSESSMENT_MODE_LABELS[it.assessment_mode] ?? it.assessment_mode)
            : '—',
          it.exam_point || '—',
        ],
  );
  const diffText = Object.entries(preview.difficulty)
    .map(([k, n]) => `${dlabel(k)} ${n} 题`)
    .join(' · ');
  return (
    <div style={{ margin: '4px 0 2px' }}>
      <div style={{ fontSize: '0.76rem', color: 'var(--text-tertiary)', marginBottom: 4 }}>
        {title} · 共 {preview.item_count} 题 · 总分 {formatScore(preview.total_score)}
        {diffText ? ` · ${diffText}` : ''}
        {tool === 'confirm_contract' ? ' · 确认后逐题锁定考查原子与答案域' : ''}
      </div>
      {/* 题位可能有几十行：限高滚动（弹窗内更高一档），不撑爆气泡/弹窗 */}
      <div className="chat-detail-table" style={expanded ? undefined : { maxHeight: 260 }}>
        <GridTable header={header} rows={rows} />
      </div>
    </div>
  );
}

/**
 * 思考过程块：思考模型推理的独立展示区——与正式回复分离，绝不混入正文。
 * 住在内容列内（与正文同列对齐）；live（流式）始终展开、尾随最新推理；
 * 落库消息默认折叠、点击展开。
 */
function ThinkingBlock({ text, live = false }: { text: string; live?: boolean }) {
  const [open, setOpen] = useState(false);
  const expanded = live || open;
  if (!text) return null;
  return (
    <div style={{ minWidth: 0 }}>
      <button
        type="button"
        onClick={() => !live && setOpen((v) => !v)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          fontSize: '0.72rem',
          color: 'var(--text-tertiary)',
          background: 'none',
          border: 'none',
          padding: 0,
          cursor: live ? 'default' : 'pointer',
        }}
      >
        <Sparkles size={11} style={live ? { animation: 'pulse 1.6s var(--ease-in-out) infinite' } : undefined} />
        {live ? 'AI 思考中…' : '思考过程'}
        {!live && (
          <ChevronDown
            size={11}
            style={{ transform: expanded ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
          />
        )}
      </button>
      {expanded && (
        <div
          style={{
            marginTop: 4,
            background: 'var(--fill)',
            border: '1px solid var(--line)',
            borderRadius: 10,
            padding: '8px 11px',
            fontSize: '0.78rem',
            lineHeight: 1.6,
            color: 'var(--text-secondary)',
            maxHeight: 200,
            overflowY: 'auto',
            // reverse：内容从底部生长，滚动始终锚定最新推理
            display: 'flex',
            flexDirection: 'column-reverse',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          <MarkdownText text={text} />
        </div>
      )}
    </div>
  );
}


/** 规格表行：before=null 表示无对比列（生成依据模式） */
type SpecRow = { group: string; item: string; before: string | null; after: string; changed: boolean };

const specList = (v: unknown): Array<Record<string, unknown>> =>
  Array.isArray(v)
    ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
    : [];

const specNum = (v: unknown): number =>
  typeof v === 'number' && Number.isFinite(v) ? v : Number(v) || 0;

/**
 * 考核规则数组的逐项对比行：按 before 顺序 + after 新增项，label 由 key 派生。
 * before 传 undefined 时退化为单列「值」行（生成依据模式）。
 */
function specDiffRows(
  group: string,
  keyOf: (r: Record<string, unknown>) => string,
  labelOfKey: (k: string) => string,
  fmt: (r: Record<string, unknown>) => string,
  before: unknown,
  after: unknown,
): SpecRow[] {
  const b = specList(before);
  const a = specList(after);
  const bMap = new Map(b.map((r) => [keyOf(r), r]));
  const aMap = new Map(a.map((r) => [keyOf(r), r]));
  const keys: string[] = [];
  for (const r of [...b, ...a]) {
    const k = keyOf(r);
    if (k && !keys.includes(k)) keys.push(k);
  }
  return keys.map((k) => {
    const bv = bMap.has(k) ? fmt(bMap.get(k)!) : '—';
    const av = aMap.has(k) ? fmt(aMap.get(k)!) : '—';
    return {
      group,
      item: labelOfKey(k),
      before: before === undefined ? null : bv,
      after: av,
      changed: bv !== av,
    };
  });
}

/** create_blueprint 生成依据行：综合题原型池（重复=数量）+ 当前考核规则三项 */
function basisRows(payload: AssistantActionPayload): SpecRow[] {
  const body = payload.body ?? {};
  const basis = payload.basis ?? {};
  const rows: SpecRow[] = [];
  const pool = body.comprehensive_archetypes;
  if (Array.isArray(pool) && pool.length > 0) {
    const counts = new Map<string, number>();
    for (const raw of pool) {
      const k = String(raw);
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    for (const [k, n] of counts) {
      rows.push({
        group: '综合题原型',
        item: ARCHETYPE_LABELS[k] ?? k,
        before: null,
        after: `${n} 道`,
        changed: true,
      });
    }
  } else {
    rows.push({ group: '综合题原型', item: '默认轮换池', before: null, after: '按规则轮换', changed: false });
  }
  rows.push(
    ...specDiffRows('题型比例', (r) => String(r.question_type ?? ''), qlabel,
      (r) => `${specNum(r.ratio)}%`, undefined, basis.question_type_ratios),
    ...specDiffRows('考试侧重点', (r) => String(r.assessment_mode ?? ''),
      (k) => ASSESSMENT_MODE_LABELS[k] ?? k, (r) => String(specNum(r.weight)),
      undefined, basis.assessment_focus),
    ...specDiffRows('章节权重', (r) => String(r.anchor_key ?? ''), (k) => k,
      (r) => String(specNum(r.weight)), undefined, basis.chapter_weights),
  );
  return rows;
}

/**
 * 考核规则规格表：
 * - update_exam_rules →「现值 / 新值」逐项对比，变化项高亮——替代原来挤成
 *   一行的「A、B、C → A、B、C」长串，每个侧重点/题型/章节单独一行可对比；
 * - create_blueprint →「生成依据」单列（当前考核规则 + 综合题原型池），
 *   教师确认前看清蓝图将按什么确定性生成，而不是只有一行项目名。
 */
function SpecTable({
  tool,
  payload,
  expanded = false,
}: {
  tool: string;
  payload: AssistantActionPayload;
  expanded?: boolean;
}) {
  const compare = tool === 'update_exam_rules';
  const basisMode = tool === 'create_blueprint';
  if (!compare && !basisMode) return null;

  let rows: SpecRow[];
  let title: string;
  if (compare) {
    const before = payload.before ?? {};
    const body = payload.body ?? {};
    rows = [
      ...specDiffRows('题型比例', (r) => String(r.question_type ?? ''), qlabel,
        (r) => `${specNum(r.ratio)}%`, before.question_type_ratios, body.question_type_ratios),
      ...specDiffRows('考试侧重点', (r) => String(r.assessment_mode ?? ''),
        (k) => ASSESSMENT_MODE_LABELS[k] ?? k, (r) => String(specNum(r.weight)),
        before.assessment_focus, body.assessment_focus),
      ...specDiffRows('章节权重', (r) => String(r.anchor_key ?? ''), (k) => k,
        (r) => String(specNum(r.weight)), before.chapter_weights, body.chapter_weights),
    ];
    if (rows.length === 0) {
      return (
        <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '4px 0' }}>
          （未检测到变化）
        </p>
      );
    }
    const changed = rows.filter((r) => r.changed).length;
    title = `考核规则逐项对比 · ${changed}/${rows.length} 项有变化`;
  } else {
    rows = basisRows(payload);
    title = '生成依据（蓝图将按当前考核规则与原型池确定性生成）';
  }

  const header = compare ? ['类别', '项目', '现值', '新值'] : ['类别', '项目', '值'];
  return (
    <div style={{ margin: '4px 0 2px' }}>
      <div style={{ fontSize: '0.76rem', color: 'var(--text-tertiary)', marginBottom: 4 }}>{title}</div>
      {/* 章节可能几十行：限高滚动（弹窗内更高一档），不撑爆气泡/弹窗 */}
      <div className="chat-detail-table" style={expanded ? undefined : { maxHeight: 260 }}>
        <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: '0.82rem' }}>
          <thead>
            <tr>
              {header.map((h) => (
                <th key={h} style={thStyle}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const hl = compare && r.changed;
              return (
                <tr key={i}>
                  <td style={{ ...tdStyle, whiteSpace: 'nowrap', color: 'var(--text-secondary)' }}>
                    {i === 0 || rows[i - 1].group !== r.group ? r.group : ''}
                  </td>
                  <td style={tdStyle}>{r.item}</td>
                  {compare && (
                    <td style={{ ...tdStyle, color: 'var(--text-tertiary)', textAlign: 'right' }}>
                      {r.before}
                    </td>
                  )}
                  <td
                    style={{
                      ...tdStyle,
                      fontWeight: hl ? 600 : undefined,
                      background: hl ? 'var(--accent-subtle)' : undefined,
                      color: compare ? (r.changed ? 'var(--accent)' : 'var(--text-tertiary)') : undefined,
                      textAlign: 'right',
                    }}
                  >
                    {r.after}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}


/** 生成阶段文案（后端 generation_stage → 教师能看懂的说法） */
const GENERATION_STAGE_LABELS: Record<string, string> = {
  generating: '正在出题',
};

/**
 * 生成进度块：出卷是唯一要跑几分钟的一步，教师必须看得见「还在推进」。
 *
 * 助手在这一步**挂起不收尾**（store 里 start_generation 不立即接力），
 * 由这里承载等待期的反馈：真实进度条（后端按批次折算 5–95%）+ 阶段文案；
 * 生成结束后卡片原地变「已生成 N 道题 · 总分 M」，并给「进入审核」入口，
 * 同时助手才被唤醒补上收尾回答。
 */
function GenerationProgressBlock({
  snap,
  onOpenPaper,
}: {
  snap: GenerationProgressSnapshot;
  onOpenPaper?: (projectId: string) => void;
}) {
  const failed = snap.task_status === 'failed' || snap.task_status === 'cancelled';
  const done =
    snap.task_status === 'succeeded'
    || snap.project_status === 'review'
    || snap.project_status === 'exported';
  const pct = typeof snap.progress === 'number' ? Math.min(100, Math.max(0, snap.progress)) : null;
  const stageText = GENERATION_STAGE_LABELS[snap.stage ?? ''] ?? '正在出题';

  if (failed) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="error">生成失败</Badge>
        <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
          {snap.error || '未知原因'}
        </span>
        <Button
          size="sm"
          variant="secondary"
          icon={<ExternalLink size={13} />}
          onClick={() => onOpenPaper?.(snap.project_id)}
        >
          去试卷页重新生成
        </Button>
      </div>
    );
  }

  if (done) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant="success">生成完成</Badge>
        <span style={{ fontSize: '0.8rem' }}>
          {snap.item_count != null ? `已生成 ${snap.item_count} 道题` : '试卷已生成'}
          {snap.total_score != null ? ` · 总分 ${formatScore(snap.total_score)}` : ''}
        </span>
        <Button
          size="sm"
          icon={<ExternalLink size={13} />}
          onClick={() => onOpenPaper?.(snap.project_id)}
        >
          进入审核
        </Button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <div className="progress-track" style={{ width: '100%' }}>
        <div
          className={`progress-fill${pct === null ? ' progress-indeterminate' : ''}`}
          style={pct === null ? undefined : { width: `${pct}%` }}
        />
      </div>
      <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
        {snap.task_status === 'queued' ? '排队中，等待出题…' : stageText}
        {pct !== null ? ` · ${pct}%` : ''}
        {snap.item_count != null ? ` · 已出 ${snap.item_count} 道` : ''}
      </span>
    </div>
  );
}

/**
 * 提案卡：操作名 + 模型重点 + 关键参数 + 状态区。
 *
 * 卡面只留「结论」——重表（题位计划 / 规则对比）收进详情弹窗，教师点一下
 * 看全套、弹窗里还能一键去真实页面编辑（.chat-detail chip + Modal）。
 *
 * 「要不要教师点确认」由后端注册表决定（随 payload 下发 auto）：出卷主线上的
 * 中间产物 auto=true，卡片不带按钮、由助手自动执行并回报执；只有里程碑
 * （确认蓝图/确认合同）与课程级写操作是 proposed，需要教师点「确认执行」。
 *
 * executing = 已开始、结果未知：中断的卡片停在这里（非幂等写绝不自动重放），
 * 给教师「重试执行」入口。
 */
function ProposalCard({
  message,
  busy,
  progress,
  generation,
  courseId,
  onNavigate,
  onConfirm,
  onDismiss,
  onRetry,
  onOpenPaper,
}: {
  message: AssistantMessage;
  busy: boolean;
  /** 自动执行中的本地进度文案（长步骤；落库的 receipt 只在结束时才有） */
  progress?: string;
  /** 该项目当前的生成进度快照（仅「发起 AI 生成」卡片用；完成前承载等待反馈） */
  generation?: GenerationProgressSnapshot;
  courseId: string;
  /** 详情弹窗「去编辑」：跳转实际承载页面（试卷页阶段深链 / 命题框架页） */
  onNavigate: (to: string) => void;
  onConfirm: (m: AssistantMessage) => void;
  onDismiss: (m: AssistantMessage) => void;
  onRetry: (m: AssistantMessage) => void;
  onOpenPaper?: (projectId: string) => void;
}) {
  const [detailOpen, setDetailOpen] = useState(false);
  const tool = message.action.tool ?? '';
  const payload = message.action.payload ?? {};
  // label/impact 优先用后端注册表下发的（历史卡片没有 → 退回本地表）
  const meta = payload.label
    ? { label: payload.label, impact: payload.impact ?? '' }
    : PROPOSAL_META[tool];
  const status = message.action.status ?? 'proposed';
  const auto = payload.auto === true;
  const rows = proposalParamRows(tool, payload);
  const preview = payload.preview;
  const hasSpec = tool === 'update_exam_rules' || tool === 'create_blueprint';
  const hasDetails = hasSpec || (preview?.items.length ?? 0) > 0;
  const target = DETAIL_TARGETS[tool];
  // 摘要与 chip 文案：不开弹窗也先看到关键数字（题量/分值），点开才看逐项明细
  const detailSummary = hasSpec
    ? tool === 'update_exam_rules'
      ? '题型比例 / 考试侧重点 / 章节权重，逐项对比与变化高亮'
      : '综合题原型池 + 当前考核规则（蓝图按此确定性生成）'
    : preview
      ? `总分 ${formatScore(preview.total_score)}`
      : '';
  const detailChip = hasSpec
    ? tool === 'update_exam_rules' ? '查看逐项对比' : '查看生成依据'
    : `查看题位详情（${preview?.item_count ?? 0} 题）`;
  const editLabel = tool === 'update_exam_rules'
    ? '去考核规则页编辑'
    : (target?.label ?? '去试卷页查看');

  /** 详情弹窗的跳转：考核规则回命题框架页，蓝图/合同走试卷页阶段深链 */
  const goEdit = () => {
    setDetailOpen(false);
    if (tool === 'update_exam_rules') {
      onNavigate(`/courses/${courseId}/framework`);
      return;
    }
    const stage = target?.stage ?? 'blueprint';
    const pid = payload.project_id;
    onNavigate(
      pid ? `/courses/${courseId}/paper?project=${pid}&stage=${stage}` : `/courses/${courseId}/paper`,
    );
  };

  return (
    <div className="chat-card chat-card--proposal" style={cardBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Badge variant={auto ? 'purple' : 'info'}>{auto ? '自动执行' : '待确认提案'}</Badge>
        <strong style={{ fontSize: '0.85rem' }}>{meta?.label ?? tool}</strong>
      </div>
      {/* 模型回复的重点：卡面只留结论与关键数字，重表收进详情弹窗 */}
      {message.content && (
        <p style={{ fontSize: '0.83rem', lineHeight: 1.65, color: 'var(--text-secondary)', margin: 0 }}>
          {message.content}
        </p>
      )}
      <KVTable rows={rows} />
      {hasDetails && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <button type="button" className="chat-detail" onClick={() => setDetailOpen(true)}>
            <Eye size={13} /> {detailChip}
          </button>
          {detailSummary && (
            <span style={{ fontSize: '0.76rem', color: 'var(--text-tertiary)' }}>{detailSummary}</span>
          )}
        </div>
      )}
      <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: 0 }}>
        {meta?.impact ?? '确认后调用既有业务接口执行。'}
      </p>
      {/* 发起生成已执行：只留进度块——「已执行」那行会被进度/完成态取代，避免重复 */}
      {generation && tool === 'start_generation' && status === 'executed' && (
        <GenerationProgressBlock snap={generation} onOpenPaper={onOpenPaper} />
      )}
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
      ) : status === 'auto' ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span className="spinner" style={{ width: 12, height: 12, borderWidth: 2 }} />
          <Badge variant="info">正在自动执行…</Badge>
        </div>
      ) : status === 'executing' ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {progress && <span className="spinner" style={{ width: 12, height: 12, borderWidth: 2 }} />}
          <Badge variant={progress ? 'info' : 'default'}>
            {progress ? '正在自动执行…' : '执行中（中断可重试）'}
          </Badge>
          {(progress || message.action.receipt) && (
            <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
              {progress || message.action.receipt}
            </span>
          )}
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => onRetry(message)}>
            重试执行
          </Button>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => onDismiss(message)}>
            放弃
          </Button>
        </div>
      ) : generation && tool === 'start_generation' ? null : (
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
      {/* 详情弹窗：全套题位/对比表 + 一键去真实页面编辑（蓝图/合同/考核规则） */}
      {hasDetails && (
        <Modal
          open={detailOpen}
          onClose={() => setDetailOpen(false)}
          title={DETAIL_TITLES[tool] ?? '查看详情'}
          maxWidth="880px"
          footer={
            <>
              <Button variant="secondary" onClick={() => setDetailOpen(false)}>关闭</Button>
              <Button icon={<ArrowUpRight size={14} />} onClick={goEdit}>{editLabel}</Button>
            </>
          }
        >
          {hasSpec ? (
            <SpecTable tool={tool} payload={payload} expanded />
          ) : (
            <ProposalPreview tool={tool} preview={preview} expanded />
          )}
        </Modal>
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
  const streamThink = useAssistantStore((s) => s.streamThink);
  const streamHint = useAssistantStore((s) => s.streamHint);
  const restore = useAssistantStore((s) => s.restore);
  const send = useAssistantStore((s) => s.send);
  const stop = useAssistantStore((s) => s.stop);
  const cancelTurn = useAssistantStore((s) => s.cancelTurn);
  const createSession = useAssistantStore((s) => s.createSession);
  const patchProposal = useAssistantStore((s) => s.patchProposal);
  const retryProposal = useAssistantStore((s) => s.retryProposal);
  const relayTurn = useAssistantStore((s) => s.relay);
  const autoProgress = useAssistantStore((s) => s.autoProgress);
  const generationProgress = useAssistantStore((s) => s.generationProgress);

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
  }, [messages.length, streamText, streamThink, streamHint, sending, streamSessionId, activeSessionId]);

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
    setBusyMessageId(m.id);
    try {
      // 执行契约在 lib/assistantProposalExecution 里（自动执行队列同一份实现）
      const { receipt, relay } = await executeProposalAction(m, {
        courseId,
        token: token ?? undefined,
      });
      await patchProposal(m.id, 'executed', receipt);
      addToast(receipt, 'success');
      // 逐级接力：执行成功即推进下一张卡。走内部接力（不写教师消息）——
      // 教师这一步只是点了「确认执行」，替他发一条「继续」会凭空多出气泡。
      if (relay && RELAY_TOOLS.has(m.action.tool ?? '')) {
        // relay 内部已收口失败（toast + 复位在途态），这里不重抛
        void relayTurn(m.id);
      }
    } catch (err) {
      // 执行失败：卡片保持原状态（回写只在成功后发生），教师可重试或取消
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

  // 在途流式区是否可见（只在归属本会话时显示；POST 瞬态 session 未知也显示）
  const streamVisible = sending && (!streamSessionId || streamSessionId === activeSessionId);
  // 流式区紧跟在助手消息后 = 同一回合的续条（不重复出头像，接力线连着走）
  const streamContinues = streamVisible && messages[messages.length - 1]?.role === 'assistant';

  // 助手回合标记：连续助手消息共享一个头像（只在回合首条显示）；
  // 末条决定接力线渐隐；发送中的流式区接在助手消息后时，末条不算回合结束。
  const runFlags = useMemo(
    () =>
      messages.map((m, i) => {
        if (m.role !== 'assistant') return { start: false, end: false, lone: false };
        const start = i === 0 || messages[i - 1].role !== 'assistant';
        const nextAssistant = i < messages.length - 1 && messages[i + 1].role === 'assistant';
        const end = !nextAssistant && !(i === messages.length - 1 && streamContinues);
        return { start, end, lone: start && end };
      }),
    [messages, streamContinues],
  );

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
          <div className="chat-card" style={{ ...cardBox, alignItems: 'flex-start', gap: 10 }}>
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
          <div className="chat-card" style={{ ...cardBox, alignItems: 'flex-start', gap: 10 }}>
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

        {messages.map((m, i) => {
          if (m.role === 'user') {
            return (
              <div
                key={m.id}
                className="msg-in"
                style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}
              >
                <div
                  style={{
                    maxWidth: '78%',
                    background: 'var(--accent)',
                    color: 'var(--text-on-accent)',
                    borderRadius: '18px 18px 6px 18px',
                    padding: '10px 14px',
                    fontSize: '0.875rem',
                    lineHeight: 1.65,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    boxShadow: 'var(--shadow-1)',
                  }}
                >
                  {m.content}
                </div>
                {m.created_at && (
                  <span style={{ fontSize: '0.7rem', color: 'var(--text-tertiary)', marginRight: 4 }}>
                    {msgTime(m.created_at)}
                  </span>
                )}
              </div>
            );
          }
          const f = runFlags[i];
          const runClass = [
            'msg-in',
            'chat-run',
            f.start ? 'is-start' : 'is-cont',
            f.end ? 'is-end' : '',
            f.lone ? 'is-lone' : '',
          ]
            .filter(Boolean)
            .join(' ');
          return (
            <div
              key={m.id}
              className={runClass}
              style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, width: '100%' }}>
                {f.start ? (
                  <span className="chat-avatar" style={{ marginTop: 2 }} aria-hidden>
                    <Bot size={15} />
                  </span>
                ) : (
                  <span className="chat-avatar-spacer" aria-hidden />
                )}
                <div
                  style={{
                    flex: 1,
                    minWidth: 0,
                    maxWidth: 820,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 8,
                  }}
                >
                  {m.thinking && <ThinkingBlock text={m.thinking} />}
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
                      progress={autoProgress[m.id]}
                      generation={
                        m.action.payload?.project_id
                          ? generationProgress[m.action.payload.project_id]
                          : undefined
                      }
                      courseId={courseId}
                      onNavigate={navigate}
                      onConfirm={(msg) => void handleConfirm(msg)}
                      onDismiss={(msg) => void handleDismiss(msg)}
                      onRetry={(msg) => retryProposal(msg.id)}
                      onOpenPaper={(pid) => navigate(`/courses/${courseId}/paper?project=${pid}`)}
                    />
                  ) : m.action.kind === 'sources' ? (
                    <>
                      <div className="chat-text">
                        <MarkdownText text={m.content} />
                      </div>
                      <SourcesCard message={m} courseId={courseId} navigate={navigate} />
                    </>
                  ) : (
                    <div className="chat-text">
                      <MarkdownText text={m.content} />
                    </div>
                  )}
                  {(m.action.kind === undefined || m.action.kind === 'sources') &&
                    m.stream_status === 'failed' && (
                    <div>
                      <Badge variant="warning">网关未走流式，整段返回</Badge>
                    </div>
                  )}
                  {m.stream_status === 'stopped' && (
                    <div>
                      <Badge variant="warning">已停止</Badge>
                    </div>
                  )}
                </div>
              </div>
              {m.created_at && f.end && <span className="chat-time">{msgTime(m.created_at)}</span>}
            </div>
          );
        })}

        {/* 在途轮次的流式占位（只在归属本会话时显示；POST 瞬态 session 未知也显示）
            ——接在助手消息后时算同回合续条：不重复出头像，接力线连着走 */}
        {streamVisible && (
          <div
            className={[
              'msg-in',
              'chat-run',
              streamContinues ? 'is-cont is-end' : 'is-start is-end is-lone',
            ].join(' ')}
            style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}
          >
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, width: '100%' }}>
              {streamContinues ? (
                <span className="chat-avatar-spacer" aria-hidden />
              ) : (
                <span className="chat-avatar" style={{ marginTop: 2 }} aria-hidden>
                  <Bot size={15} />
                </span>
              )}
              <div
                style={{
                  flex: 1,
                  minWidth: 0,
                  maxWidth: 820,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 8,
                }}
              >
                {streamThink && <ThinkingBlock text={streamThink} live />}
                <div className="chat-text">
                  {streamText ? (
                    <>
                      <MarkdownText text={streamText} />
                      <span className="caret">▍</span>
                    </>
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
