import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeftRight,
  ArrowRight,
  ArrowUpRight,
  Bot,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Clock,
  FolderOpen,
  LibraryBig,
  LogOut,
  PanelLeft,
  PanelRight,
  Pencil,
  Plus,
  Send,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  X,
} from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button, Input, MarkdownText, Modal } from '@/components/ui';
import { CourseAssistantPanel } from '@/pages/assistant';
import type { AssistantHistoryItem } from '@/api/domains/assistant';
import type {
  KnowledgeMessage,
  KnowledgeSessionDetail,
  KnowledgeSpace,
} from '@/api/domains/knowledgeBase';
import type { CourseResponse } from '@/types/api';

type Range =
  | { kind: 'course'; courseId: string; name: string }
  | { kind: 'knowledge'; spaceIds: string[]; names: string[] }
  | null;

type DomainFilter = 'all' | 'course' | 'knowledge';

const FILTERS: Array<{ value: DomainFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'course', label: '课程' },
  { value: 'knowledge', label: '共享' },
];

const QUICK_TASKS: Array<{
  label: string;
  hint: string;
  kind: 'course' | 'knowledge';
  icon: typeof BookOpen;
  bg: string;
  fg: string;
  /** 选完范围后自动发送的首条问题（快捷任务不带问题会只剩"跳转到旧会话"） */
  prompt?: string;
}> = [
  {
    label: '问课程资料',
    hint: '基于课程已解析资料回答',
    kind: 'course',
    icon: BookOpen,
    bg: 'var(--brand-subtle)',
    fg: 'var(--brand-ink)',
    prompt: '这门课的资料主要讲了什么？',
  },
  {
    label: '问共享资料',
    hint: '检索已发布共享库并给出引用',
    kind: 'knowledge',
    icon: LibraryBig,
    bg: 'var(--purple-subtle)',
    fg: 'var(--purple-ink)',
  },
  {
    label: '查看课程进度',
    hint: '资料、框架、试卷的当前状态',
    kind: 'course',
    icon: Clock,
    bg: 'var(--info-subtle)',
    fg: 'var(--info-ink)',
    prompt: '课程现在进行到哪一步了？',
  },
  {
    label: '帮我推进出卷',
    hint: '从考核规则到生成逐卡确认',
    kind: 'course',
    icon: Sparkles,
    bg: 'var(--warning-subtle)',
    fg: 'var(--warning-ink)',
    prompt: '帮我推进出卷，下一步该做什么？',
  },
];

const iconButton = {
  border: 'none',
  background: 'transparent',
  color: 'var(--text-tertiary)',
  cursor: 'pointer',
  padding: 3,
  borderRadius: 6,
  display: 'inline-flex',
} as const;

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function historyTargetLabel(item: AssistantHistoryItem): string {
  if (item.target.kind === 'course') return item.target.name;
  if (!item.target.names.length) return '共享范围已失效';
  return item.target.names.join('、') + (item.target.complete ? '' : '（部分失效）');
}

/** 历史按"7 天内 / 30 天内 / 年月"分组（对齐对话产品的常见时间分组） */
function groupHistory(items: AssistantHistoryItem[]): Array<{
  label: string;
  items: AssistantHistoryItem[];
}> {
  const groups: Array<{ label: string; items: AssistantHistoryItem[] }> = [];
  const push = (label: string) => {
    let group = groups.find((g) => g.label === label);
    if (!group) {
      group = { label, items: [] };
      groups.push(group);
    }
    return group;
  };
  const now = Date.now();
  for (const item of items) {
    const time = new Date(item.updated_at).getTime();
    const days = Number.isNaN(time) ? Number.POSITIVE_INFINITY : (now - time) / 86_400_000;
    if (days <= 7) {
      push('7 天内').items.push(item);
    } else if (days <= 30) {
      push('30 天内').items.push(item);
    } else if (Number.isNaN(time)) {
      push('更早').items.push(item);
    } else {
      const date = new Date(time);
      push(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`).items.push(item);
    }
  }
  return groups;
}

function channelLabel(message: KnowledgeMessage): string | null {
  const channel = message.retrieval_snapshot.channel;
  if (channel === 'model') return '模型综合回答';
  if (channel === 'extractive') {
    return message.retrieval_snapshot.degraded
      ? '资料摘录（模型不可用，已降级）'
      : '资料摘录';
  }
  return null;
}

export default function HomeAssistantPage() {
  const navigate = useNavigate();
  const logout = useAuthStore((s) => s.logout);
  const user = useAuthStore((s) => s.user);
  const addToast = useToastStore((s) => s.addToast);

  const [courses, setCourses] = useState<CourseResponse[]>([]);
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]);
  const [history, setHistory] = useState<AssistantHistoryItem[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [filter, setFilter] = useState<DomainFilter>('all');

  const [range, setRange] = useState<Range>(null);
  const [rangeOpen, setRangeOpen] = useState(false);
  const [pickerKind, setPickerKind] = useState<'course' | 'knowledge'>('course');
  const [pickerCourse, setPickerCourse] = useState('');
  const [pickerSpaces, setPickerSpaces] = useState<string[]>([]);

  // 首屏只保留大屏 + 输入框；选范围或打开历史后才进入聊天布局。
  const [landingDraft, setLandingDraft] = useState('');
  // 输入框聚焦时才展开预设问题卡片，避免首屏被占满
  const [quickOpen, setQuickOpen] = useState(false);
  const [pendingPrompt, setPendingPrompt] = useState('');
  const [courseInitialMessage, setCourseInitialMessage] = useState('');
  // 大屏/快捷任务选课程进入 = 新话题：要求助手面板开新会话（不复用上次会话）
  const [courseStartFresh, setCourseStartFresh] = useState(false);

  const [courseSessionId, setCourseSessionId] = useState<string | undefined>();
  const [ksession, setKsession] = useState<KnowledgeSessionDetail | null>(null);
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [scopeChanged, setScopeChanged] = useState(false);

  const [sideOpen, setSideOpen] = useState(false);
  const [rightSideOpen, setRightSideOpen] = useState(false);
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [renameTarget, setRenameTarget] = useState<AssistantHistoryItem | null>(null);
  const [renameTitle, setRenameTitle] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<AssistantHistoryItem | null>(null);
  const [busy, setBusy] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const sessionPollRef = useRef<number | null>(null);

  const stopSessionPolling = useCallback(() => {
    if (sessionPollRef.current !== null) {
      window.clearInterval(sessionPollRef.current);
      sessionPollRef.current = null;
    }
  }, []);

  const startSessionPolling = useCallback(
    (sessionId: string) => {
      stopSessionPolling();
      let attempts = 0;
      sessionPollRef.current = window.setInterval(async () => {
        attempts += 1;
        try {
          const detail = await api.knowledgeBase.getSession(sessionId);
          setKsession((prev) => (prev?.id === detail.id ? detail : prev));
          if (!detail.messages.some((message) => message.state === 'pending')) {
            stopSessionPolling();
          }
        } catch {
          stopSessionPolling();
        }
        // 与后端 5 分钟超时对齐；后端会把超时消息标为 failed，轮询随后停止。
        if (attempts >= 100) stopSessionPolling();
      }, 3000);
    },
    [stopSessionPolling]
  );

  useEffect(() => () => stopSessionPolling(), [stopSessionPolling]);

  useEffect(() => {
    void api.courses
      .list()
      .then(setCourses)
      .catch(() => addToast('课程加载失败', 'error'));
    void api.knowledgeBase
      .listSpaces()
      .then((list) => setSpaces(list.filter((space) => space.status === 'active')))
      .catch(() => undefined);
  }, [addToast]);

  const refreshHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const page = await api.assistant.listAllSessions({
        domain: filter === 'all' ? undefined : filter,
        limit: 20,
      });
      setHistory(page.items);
      setHistoryCursor(page.next_cursor);
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
    } finally {
      setHistoryLoading(false);
    }
  }, [filter, addToast]);

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  const loadMoreHistory = async () => {
    if (!historyCursor || historyLoading) return;
    setHistoryLoading(true);
    try {
      const page = await api.assistant.listAllSessions({
        domain: filter === 'all' ? undefined : filter,
        limit: 20,
        cursor: historyCursor,
      });
      setHistory((prev) => [...prev, ...page.items]);
      setHistoryCursor(page.next_cursor);
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
    } finally {
      setHistoryLoading(false);
    }
  };

  const openPicker = (kind: 'course' | 'knowledge') => {
    setPickerKind(kind);
    setPickerCourse(range?.kind === 'course' ? range.courseId : '');
    setPickerSpaces(
      range?.kind === 'knowledge'
        ? range.spaceIds.filter((id) => spaces.some((space) => space.id === id))
        : []
    );
    setRangeOpen(true);
  };

  const toggleSpace = (spaceId: string) => {
    if (pickerSpaces.includes(spaceId)) {
      setPickerSpaces(pickerSpaces.filter((id) => id !== spaceId));
      return;
    }
    if (pickerSpaces.length >= 5) {
      addToast('一次最多选择 5 个共享库', 'info');
      return;
    }
    setPickerSpaces([...pickerSpaces, spaceId]);
  };

  const confirmRange = async () => {
    stopSessionPolling();
    let nextRange: Range = null;
    if (pickerKind === 'course') {
      const course = courses.find((item) => item.id === pickerCourse);
      if (!course) {
        addToast('请选择一门课程', 'info');
        return;
      }
      nextRange = { kind: 'course', courseId: course.id, name: course.name };
      setRange(nextRange);
      setCourseSessionId(undefined);
      // 从大屏/快捷任务选课程进入 = 新话题：让助手面板开新会话，
      // 不能沿用上次会话（否则旧对话会继续被当成上下文）
      setCourseStartFresh(true);
      setKsession(null);
    } else {
      if (!pickerSpaces.length) {
        addToast('请至少选择一个共享库', 'info');
        return;
      }
      const names = pickerSpaces
        .map((id) => spaces.find((space) => space.id === id)?.name ?? '')
        .filter(Boolean);
      nextRange = { kind: 'knowledge', spaceIds: pickerSpaces, names };
      setRange(nextRange);
      setKsession(null);
      setCourseSessionId(undefined);
    }
    setScopeChanged(false);
    setRangeOpen(false);

    const prompt = pendingPrompt.trim();
    setPendingPrompt('');
    if (!nextRange) return;
    if (nextRange.kind === 'course') {
      // 无首条问题也照常进对话页：面板按 startFresh 开一条空的新会话
      setLandingDraft('');
      setCourseInitialMessage(prompt);
      return;
    }
    if (!prompt) return;
    try {
      const current = await api.knowledgeBase.createSession(
        nextRange.spaceIds,
        `${nextRange.names[0] ?? '共享资料'}问答`
      );
      const updated = await api.knowledgeBase.ask(current.id, prompt);
      setKsession(updated);
      if (updated.messages.some((message) => message.state === 'pending')) {
        startSessionPolling(updated.id);
      }
      void refreshHistory();
    } catch (err) {
      if ((err as { status?: number }).status === 409) setScopeChanged(true);
      addToast(getErrorMessage(err), 'error');
    }
  };

  const goHome = useCallback(() => {
    stopSessionPolling();
    setRange(null);
    setKsession(null);
    setCourseSessionId(undefined);
    setScopeChanged(false);
    setLandingDraft('');
    setPendingPrompt('');
    setCourseInitialMessage('');
    setCourseStartFresh(false);
    setSideOpen(false);
    setRightSideOpen(false);
  }, [stopSessionPolling]);

  const consumeCourseMessage = useCallback(() => setCourseInitialMessage(''), []);

  const submitLanding = () => {
    const text = landingDraft.trim();
    if (!text) return;
    setPendingPrompt(text);
    openPicker('course');
  };

  const openHistory = async (item: AssistantHistoryItem) => {
    setSideOpen(false);
    if (item.target.kind === 'course') {
      stopSessionPolling();
      setRange({ kind: 'course', courseId: item.target.course_id, name: item.target.name });
      setCourseSessionId(item.session_id);
      setKsession(null);
      setScopeChanged(false);
      return;
    }
    try {
      const detail = await api.knowledgeBase.getSession(item.session_id);
      setRange({
        kind: 'knowledge',
        spaceIds: detail.selected_space_ids,
        names: item.target.names,
      });
      setKsession(detail);
      setScopeChanged(!item.target.complete);
      if (detail.messages.some((message) => message.state === 'pending')) {
        startSessionPolling(detail.id);
      }
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
    }
  };

  const sendKnowledge = async () => {
    const text = question.trim();
    if (!text || asking || range?.kind !== 'knowledge') return;
    setAsking(true);
    try {
      let current = ksession;
      if (!current) {
        current = await api.knowledgeBase.createSession(
          range.spaceIds,
          `${range.names[0] ?? '共享资料'}问答`
        );
      }
      const updated = await api.knowledgeBase.ask(current.id, text);
      setKsession(updated);
      setQuestion('');
      setScopeChanged(false);
      if (updated.messages.some((message) => message.state === 'pending')) {
        startSessionPolling(updated.id);
      }
      void refreshHistory();
      window.setTimeout(() => threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight }), 50);
    } catch (err) {
      if ((err as { status?: number }).status === 409) setScopeChanged(true);
      addToast(getErrorMessage(err), 'error');
    } finally {
      setAsking(false);
    }
  };

  const submitFeedback = async (messageId: string, rating: 'helpful' | 'unhelpful') => {
    if (!ksession) return;
    try {
      await api.knowledgeBase.submitFeedback(ksession.id, messageId, { rating });
      setKsession((prev) =>
        prev
          ? {
              ...prev,
              messages: prev.messages.map((message) =>
                message.id === messageId
                  ? { ...message, feedback: { ...message.feedback, rating } }
                  : message
              ),
            }
          : prev
      );
      addToast('反馈已记录', 'success');
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
    }
  };

  const openRename = (item: AssistantHistoryItem) => {
    setRenameTarget(item);
    setRenameTitle(item.title);
  };

  const submitRename = async () => {
    if (!renameTarget) return;
    const title = renameTitle.trim();
    if (!title) {
      addToast('会话标题不能为空', 'info');
      return;
    }
    setBusy(true);
    try {
      if (renameTarget.target.kind === 'course') {
        await api.assistant.renameSession(
          renameTarget.target.course_id,
          renameTarget.session_id,
          title
        );
      } else {
        await api.knowledgeBase.renameSession(renameTarget.session_id, title);
      }
      setRenameTarget(null);
      await refreshHistory();
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      if (deleteTarget.target.kind === 'course') {
        await api.assistant.deleteSession(
          deleteTarget.target.course_id,
          deleteTarget.session_id
        );
      } else {
        await api.knowledgeBase.deleteSession(deleteTarget.session_id);
      }
      if (deleteTarget.session_id === ksession?.id) {
        stopSessionPolling();
        setKsession(null);
      }
      if (deleteTarget.session_id === courseSessionId) setCourseSessionId(undefined);
      setDeleteTarget(null);
      await refreshHistory();
      addToast('会话已删除，共享资料不受影响', 'success');
    } catch (err) {
      addToast(getErrorMessage(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  const emptyKnowledge = !ksession || ksession.messages.length === 0;

  const activeSessionId = useMemo(() => {
    if (range?.kind === 'course') return courseSessionId ?? null;
    return ksession?.id ?? null;
  }, [range, courseSessionId, ksession]);

  const chatMode = Boolean(range || ksession || courseSessionId);

  const groupedHistory = useMemo(() => groupHistory(history), [history]);

  /** 顶栏标题：优先显示当前会话标题（与左侧栏选中项一致），否则回落到范围名 */
  const activeTitle = useMemo(() => {
    if (!range) return '新对话';
    const hit = history.find((item) => item.session_id === activeSessionId);
    if (hit) return hit.title;
    if (range.kind === 'knowledge') return ksession?.title || range.names[0] || '共享资料问答';
    return range.name;
  }, [range, history, activeSessionId, ksession]);

  return (
    <div className="home-shell">
      <div
        className={`home-backdrop ${sideOpen || rightSideOpen ? 'is-open' : ''}`}
        onClick={() => {
          setSideOpen(false);
          setRightSideOpen(false);
        }}
      />

      {/* 左侧栏默认收起：顶部保留展开按钮，按需滑出 */}
      <aside className={`home-side ${sideOpen ? 'is-open' : ''}`}>
        <div className="home-side-top">
          <span className="home-brand-mark">
            <Bot size={18} />
          </span>
          <span className="home-brand-word">PaperPact</span>
          <button
            type="button"
            className="home-icon-btn"
            title="收起侧栏"
            onClick={() => setSideOpen(false)}
          >
            <X size={16} />
          </button>
        </div>

        <button type="button" className="home-new-chat" onClick={goHome}>
          <Plus size={15} />
          新建对话
        </button>

        <div className="home-filter-row">
          {FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`home-filter ${filter === item.value ? 'is-active' : ''}`}
              onClick={() => setFilter(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="home-history">
          {groupedHistory.map((group) => {
            const collapsed = Boolean(collapsedGroups[group.label]);
            return (
              <div key={group.label} className="home-history-group">
                <button
                  type="button"
                  className="home-history-group-head"
                  onClick={() =>
                    setCollapsedGroups((prev) => ({ ...prev, [group.label]: !prev[group.label] }))
                  }
                >
                  <span>{group.label}</span>
                  {collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                </button>
                {!collapsed &&
                  group.items.map((item) => {
                    const active = item.session_id === activeSessionId;
                    return (
                      <div
                        key={`${item.domain}-${item.session_id}`}
                        role="button"
                        tabIndex={0}
                        title={`${historyTargetLabel(item)} · ${formatTime(item.updated_at)}`}
                        className={`home-history-item ${active ? 'is-active' : ''}`}
                        onClick={() => void openHistory(item)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            void openHistory(item);
                          }
                        }}
                      >
                        <span className="home-history-title">{item.title}</span>
                        <div className="home-history-actions">
                          <button
                            type="button"
                            style={iconButton}
                            title="重命名"
                            onClick={(event) => {
                              event.stopPropagation();
                              openRename(item);
                            }}
                          >
                            <Pencil size={13} />
                          </button>
                          <button
                            type="button"
                            style={iconButton}
                            title="删除"
                            onClick={(event) => {
                              event.stopPropagation();
                              setDeleteTarget(item);
                            }}
                          >
                            <Trash2 size={13} />
                          </button>
                        </div>
                      </div>
                    );
                  })}
              </div>
            );
          })}
          {!history.length && !historyLoading && (
            <p className="home-history-empty">暂无历史会话。选个范围开始第一个问题。</p>
          )}
          {historyLoading && <p className="home-history-empty">加载中…</p>}
          {historyCursor && !historyLoading && (
            <button type="button" className="home-history-more" onClick={() => void loadMoreHistory()}>
              加载更多
            </button>
          )}
        </div>

        <div className="home-side-foot">
          <span className="home-user-avatar">
            {(user?.name || user?.username || '师').slice(0, 1)}
          </span>
          <span className="home-user-name">{user?.name || user?.username || '教师'}</span>
          <button type="button" className="home-icon-btn" title="退出登录" onClick={logout}>
            <LogOut size={15} />
          </button>
        </div>
      </aside>

        <main className="home-main">
          <header className="home-main-head">
            <div className="home-main-head-left">
              <div className="home-title-row">
                <button
                  type="button"
                  className="home-side-toggle"
                  title="展开侧栏"
                  aria-label="展开侧栏"
                  onClick={() => setSideOpen(true)}
                >
                  <PanelLeft size={16} />
                </button>
                <div className="home-main-title">{chatMode ? activeTitle : '新对话'}</div>
              </div>
              {chatMode && (
                <div className="home-main-sub">
                  {range ? (
                    <span className="home-scope-chip">
                      {range.kind === 'course'
                        ? `课程：${range.name}`
                        : `共享资料：${range.names.join('、') || '范围已失效'}`}
                    </span>
                  ) : (
                    <span>尚未选择课程或共享资料</span>
                  )}
                  {scopeChanged && (
                    <span style={{ color: 'var(--error-ink, #b91c1c)' }}>
                      部分共享库已不可访问，请重新选择范围
                    </span>
                  )}
                  <button
                    type="button"
                    className="home-pill-btn"
                    onClick={() => openPicker(range?.kind === 'knowledge' ? 'knowledge' : 'course')}
                  >
                    <ArrowLeftRight size={12} />
                    {range ? '更换范围' : '选择范围'}
                  </button>
                  {range?.kind === 'course' && (
                    <button
                      type="button"
                      className="home-pill-btn"
                      onClick={() => navigate(`/courses/${range.courseId}`)}
                    >
                      <ArrowUpRight size={12} />
                      课程工作台
                    </button>
                  )}
                </div>
              )}
            </div>
          </header>

          {range?.kind === 'course' ? (
            <div className="home-chat-shell">
              <CourseAssistantPanel
                key={range.courseId}
                courseId={range.courseId}
                embedded
                startFresh={courseStartFresh}
                initialSessionId={courseSessionId}
                initialMessage={courseInitialMessage}
                onInitialMessageConsumed={consumeCourseMessage}
              />
            </div>
          ) : range?.kind === 'knowledge' ? (
            <div className="home-chat-shell">
              <div className="home-conversation" ref={threadRef}>
                {emptyKnowledge && (
                  <div className="home-empty">
                    <Bot size={26} color="var(--brand-ink)" />
                    <div>
                      <div style={{ fontWeight: 650, color: 'var(--text)' }}>基于共享资料回答问题</div>
                      <div style={{ fontSize: '0.8125rem', marginTop: 4 }}>
                        当前回答会标注「模型综合回答」或「资料摘录」，并给出固定版本、页码或章节来源。
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'center' }}>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => setQuestion('这些资料的核心概念和适用场景是什么？')}
                      >
                        核心概念与适用场景
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => setQuestion('这些资料之间有哪些相互矛盾或互补的观点？')}
                      >
                        对比资料观点
                      </Button>
                    </div>
                  </div>
                )}

                {ksession?.messages.map((message) =>
                  message.role === 'user' ? (
                    <div key={message.id} className="home-message is-user">
                      <div className="home-bubble">{message.content}</div>
                    </div>
                  ) : (
                    <div key={message.id} className="home-message">
                      <span
                        className="chat-avatar"
                        style={{ marginTop: 2, flexShrink: 0 }}
                        aria-hidden
                      >
                        <Bot size={15} />
                      </span>
                      <div className="home-bubble">
                        {message.state === 'pending' ? (
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: 8,
                              color: 'var(--text-secondary)',
                            }}
                          >
                            <span className="typing-dots">
                              <span className="typing-dot" />
                              <span className="typing-dot" />
                              <span className="typing-dot" />
                            </span>
                            正在综合回答…
                          </span>
                        ) : (
                          <>
                            {channelLabel(message) && (
                              <div
                                style={{
                                  fontSize: '0.68rem',
                                  fontWeight: 600,
                                  color: 'var(--purple-ink)',
                                  marginBottom: 6,
                                }}
                              >
                                {channelLabel(message)}
                              </div>
                            )}
                            <MarkdownText text={message.content} />
                          </>
                        )}
                        {message.sources.length > 0 && (
                          <div className="home-sources">
                            {message.sources.map((source) => (
                              <button
                                key={source.chunk_id}
                                type="button"
                                className="home-source-chip"
                                title={source.quote}
                                onClick={() =>
                                  navigate(
                                    `/knowledge/${source.space_id}?document=${source.document_id}` +
                                      `&sourceVersion=${source.version_id}`
                                  )
                                }
                              >
                                <LibraryBig size={11} />
                                {source.document_title} · v{source.version_no}
                                {source.page_index != null ? ` · p${source.page_index + 1}` : ''}
                              </button>
                            ))}
                          </div>
                        )}
                        {message.state === 'complete' && (
                          <div style={{ display: 'flex', gap: 4, marginTop: 8 }}>
                            <button
                              type="button"
                              style={iconButton}
                              title="有帮助"
                              onClick={() => void submitFeedback(message.id, 'helpful')}
                            >
                              <ThumbsUp
                                size={13}
                                color={
                                  message.feedback.rating === 'helpful'
                                    ? 'var(--brand-ink)'
                                    : undefined
                                }
                              />
                            </button>
                            <button
                              type="button"
                              style={iconButton}
                              title="没帮助"
                              onClick={() => void submitFeedback(message.id, 'unhelpful')}
                            >
                              <ThumbsDown
                                size={13}
                                color={
                                  message.feedback.rating === 'unhelpful'
                                    ? 'var(--warning-ink, #b45309)'
                                    : undefined
                                }
                              />
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  )
                )}
              </div>

              <div className="home-composer-wrap">
                <div className="chat-composer">
                  <textarea
                    className="input-field"
                    style={{ resize: 'none', maxHeight: 120, minHeight: 24 }}
                    rows={1}
                    value={question}
                    disabled={asking}
                    placeholder={
                      scopeChanged
                        ? '当前范围已变化，请先重新选择资料范围'
                        : asking
                          ? '正在检索资料…'
                          : '基于所选共享库提问'
                    }
                    aria-label="向共享资料提问"
                    onChange={(event) => setQuestion(event.target.value)}
                    onKeyDown={(event) => {
                      if (
                        event.key === 'Enter' &&
                        !event.shiftKey &&
                        !event.nativeEvent.isComposing &&
                        !asking &&
                        !scopeChanged
                      ) {
                        event.preventDefault();
                        void sendKnowledge();
                      }
                    }}
                  />
                  <div className="chat-composer-bar">
                    <span className="chat-composer-note">Enter 发送 · Shift+Enter 换行</span>
                    <div className="chat-composer-actions">
                      <button
                        type="button"
                        className="chat-send"
                        onClick={() => void sendKnowledge()}
                        disabled={!question.trim() || asking || scopeChanged}
                        title="发送"
                      >
                        <Send size={16} />
                      </button>
                    </div>
                  </div>
                </div>
                <p className="chat-disclaimer">内容由 AI 生成，请核对引用来源</p>
              </div>
            </div>
          ) : (
            <div className="home-landing">
              <span className="home-landing-glow" aria-hidden />
              <div className={`home-landing-inner ${quickOpen ? 'is-quick' : ''}`}>
                <div className="home-landing-intro">
                  <span className="home-landing-mark">
                    <Bot size={28} />
                  </span>
                  <div className="home-landing-copy">
                    <h1>从一个问题开始</h1>
                    <p>
                      先选择一门课程或 1～5 个共享库。课程范围使用完整课程助手；
                      共享范围使用资料摘录或模型综合回答。
                    </p>
                  </div>
                </div>
                <div className={`home-quick-grid ${quickOpen ? 'is-open' : ''}`}>
                  {QUICK_TASKS.map((task) => {
                    const Icon = task.icon;
                    return (
                      <button
                        key={task.label}
                        type="button"
                        className="home-quick-card"
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => {
                          // 大屏已输入优先；否则用快捷任务自带的首条问题
                          const text = landingDraft.trim() || task.prompt || '';
                          if (text) setPendingPrompt(text);
                          setQuickOpen(false);
                          openPicker(task.kind);
                        }}
                      >
                        <span
                          className="home-quick-icon"
                          style={{ background: task.bg, color: task.fg }}
                        >
                          <Icon size={18} />
                        </span>
                        <span className="home-quick-copy">
                          <strong>{task.label}</strong>
                          <small>{task.hint}</small>
                        </span>
                        <ArrowRight className="home-quick-arrow" size={16} />
                      </button>
                    );
                  })}
                </div>
                <div className="home-landing-composer">
                  <div className="chat-composer">
                    <textarea
                      className="input-field"
                      style={{ resize: 'none', maxHeight: 160, minHeight: 52 }}
                      rows={2}
                      value={landingDraft}
                      placeholder="输入问题，发送时选择课程或共享资料…"
                      aria-label="输入问题"
                      onChange={(event) => setLandingDraft(event.target.value)}
                      onFocus={() => setQuickOpen(true)}
                      onBlur={() => window.setTimeout(() => setQuickOpen(false), 120)}
                      onKeyDown={(event) => {
                        if (
                          event.key === 'Enter' &&
                          !event.shiftKey &&
                          !event.nativeEvent.isComposing
                        ) {
                          event.preventDefault();
                          void submitLanding();
                        }
                      }}
                    />
                    <div className="chat-composer-bar">
                      <span className="chat-composer-note">发送时选择对话范围</span>
                      <div className="chat-composer-actions">
                        <button
                          type="button"
                          className="chat-send"
                          onClick={() => void submitLanding()}
                          disabled={!landingDraft.trim()}
                          title="发送并选择范围"
                        >
                          <Send size={16} />
                        </button>
                      </div>
                    </div>
                  </div>
                  <div className="home-landing-hint">
                    未选择范围前不会调用模型，也不会写入业务数据
                  </div>
                </div>
              </div>
            </div>
          )}
        </main>

      {/* 悬浮入口：贴在右侧边缘中部，点击展开右侧工作空间栏 */}
      <button
        type="button"
        className={`home-fab ${rightSideOpen ? 'is-hidden' : ''}`}
        onClick={() => setRightSideOpen(true)}
        title="工作空间"
        aria-label="打开工作空间"
      >
        <PanelRight size={18} />
        <span className="home-fab-label">工作空间</span>
      </button>

      {/* 右侧栏：工作空间预览（课程 + 共享库），点悬浮按钮滑出 */}
      <aside className={`home-right-side ${rightSideOpen ? 'is-open' : ''}`}>
        <div className="home-right-head">
          <span className="home-right-title">工作空间</span>
          <button
            type="button"
            className="home-icon-btn"
            title="关闭"
            onClick={() => setRightSideOpen(false)}
          >
            <X size={16} />
          </button>
        </div>

        <div className="home-right-body">
          <section className="home-right-section">
            <div className="home-right-section-head">
              <FolderOpen size={15} />
              <span>课程空间</span>
              <button
                type="button"
                className="home-right-enter-all"
                onClick={() => {
                  setRightSideOpen(false);
                  navigate('/courses');
                }}
              >
                全部 <ArrowUpRight size={12} />
              </button>
            </div>
            <div className="home-right-list">
              {courses.length ? (
                courses.slice(0, 6).map((course) => (
                  <button
                    key={course.id}
                    type="button"
                    className="home-right-item"
                    onClick={() => {
                      setRightSideOpen(false);
                      navigate(`/courses/${course.id}`);
                    }}
                  >
                    <span className="home-right-item-name" title={course.name}>
                      {course.name}
                    </span>
                    <span className="home-right-item-enter">进入</span>
                  </button>
                ))
              ) : (
                <p className="home-right-empty">暂无课程</p>
              )}
            </div>
          </section>

          <section className="home-right-section">
            <div className="home-right-section-head">
              <LibraryBig size={15} />
              <span>资料与知识中心</span>
              <button
                type="button"
                className="home-right-enter-all"
                onClick={() => {
                  setRightSideOpen(false);
                  navigate('/knowledge');
                }}
              >
                全部 <ArrowUpRight size={12} />
              </button>
            </div>
            <div className="home-right-list">
              {spaces.length ? (
                spaces.slice(0, 6).map((space) => (
                  <button
                    key={space.id}
                    type="button"
                    className="home-right-item"
                    onClick={() => {
                      setRightSideOpen(false);
                      navigate(`/knowledge/${space.id}`);
                    }}
                  >
                    <span className="home-right-item-copy">
                      <span className="home-right-item-name" title={space.name}>
                        {space.name}
                      </span>
                      <span className="home-right-item-meta">
                        {space.read_scope_label} · {space.document_count} 份资料
                      </span>
                    </span>
                    <span className="home-right-item-enter">进入</span>
                  </button>
                ))
              ) : (
                <p className="home-right-empty">暂无可访问的共享库</p>
              )}
            </div>
          </section>
        </div>
      </aside>

      <Modal
        open={rangeOpen}
        onClose={() => {
          setRangeOpen(false);
          setPendingPrompt('');
        }}
        title="选择对话范围"
        onConfirm={confirmRange}
        confirmLabel="确定"
        maxWidth="620px"
      >
        {pendingPrompt && (
          <div
            style={{
              marginBottom: 12,
              padding: '10px 12px',
              borderRadius: 10,
              background: 'var(--brand-subtle)',
              color: 'var(--brand-ink)',
              fontSize: '0.8125rem',
            }}
          >
            选择范围后将继续发送：{pendingPrompt}
          </div>
        )}
        <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
          <Button
            size="sm"
            variant={pickerKind === 'course' ? 'primary' : 'secondary'}
            onClick={() => setPickerKind('course')}
          >
            课程
          </Button>
          <Button
            size="sm"
            variant={pickerKind === 'knowledge' ? 'primary' : 'secondary'}
            onClick={() => setPickerKind('knowledge')}
          >
            共享资料
          </Button>
        </div>

        {pickerKind === 'course' ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {courses.length ? (
              courses.map((course) => (
                <label
                  key={course.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '10px 12px',
                    border: '1px solid var(--border)',
                    borderRadius: 10,
                    cursor: 'pointer',
                    background:
                      pickerCourse === course.id ? 'var(--brand-subtle)' : 'var(--surface)',
                  }}
                >
                  <input
                    type="radio"
                    name="range-course"
                    checked={pickerCourse === course.id}
                    onChange={() => setPickerCourse(course.id)}
                  />
                  <span style={{ fontSize: '0.875rem', fontWeight: 600 }}>{course.name}</span>
                </label>
              ))
            ) : (
              <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
                暂无可选课程。请先在课程空间创建课程。
              </p>
            )}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginBottom: 4 }}>
              最多选择 5 个共享库；切换集合会创建新会话，历史不会串用。
            </p>
            {spaces.length ? (
              spaces.map((space) => (
                <label
                  key={space.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '10px 12px',
                    border: '1px solid var(--border)',
                    borderRadius: 10,
                    cursor: 'pointer',
                    background:
                      pickerSpaces.includes(space.id) ? 'var(--brand-subtle)' : 'var(--surface)',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={pickerSpaces.includes(space.id)}
                    onChange={() => toggleSpace(space.id)}
                  />
                  <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <span style={{ fontSize: '0.875rem', fontWeight: 600 }}>{space.name}</span>
                    <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>
                      {space.read_scope_label} · 已发布 {space.document_count} 份
                    </span>
                  </span>
                </label>
              ))
            ) : (
              <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
                暂无可访问的共享库。请联系空间负责人授予阅读权限。
              </p>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={Boolean(renameTarget)}
        onClose={() => setRenameTarget(null)}
        title="重命名会话"
        onConfirm={submitRename}
        confirmLabel="保存"
        loading={busy}
      >
        <Input
          value={renameTitle}
          onChange={(event) => setRenameTitle(event.target.value)}
          maxLength={120}
          placeholder="会话标题"
          autoFocus
        />
      </Modal>

      <Modal
        open={Boolean(deleteTarget)}
        onClose={() => setDeleteTarget(null)}
        title="删除会话"
        onConfirm={confirmDelete}
        confirmLabel="删除"
        loading={busy}
        danger
      >
        <p style={{ fontSize: '0.875rem', lineHeight: 1.7 }}>
          确定删除「{deleteTarget?.title}」吗？只删除本人对话，不影响共享资料或课程内容。
          删除后该会话的详情、提问与反馈均不可访问。
        </p>
      </Modal>
    </div>
  );
}
