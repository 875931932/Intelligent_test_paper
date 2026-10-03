import { create } from 'zustand';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { executeProposalAction, RELAY_TOOLS } from '@/lib/assistantProposalExecution';
import type {
  AssistantMessage,
  AssistantProposalStatus,
  AssistantSession,
  GenerationProgressSnapshot,
} from '@/types/api';
import { useAuthStore } from './auth';
import { useToastStore } from './toast';

/**
 * AI 助手对话状态（一域一 store）：会话列表 + 消息时间线 + 在途轮次的流式状态。
 *
 * 恢复语义对齐 BlueprintSuggestPanel 的防重复模式（restoredForRef/startedRef）：
 * 挂载只拉一次会话与历史；末条是 user 消息即视为上一轮仍在途，重连 SSE 续读；
 * 页面卸载只关流不丢状态，回来后续读（任务在 worker 里照常跑完落库）。
 *
 * v3 多会话：一条会话 = 一条时间线与记忆边界。单在途约束——同一时刻只有一轮
 * 在流式（sending 全局禁用发送），切会话只关流不取消、切回续读（streamSessionId 归属）。
 */
interface AssistantState {
  /** 当前课程作用域；切课整体重置（消息/卡片全部带 course_id 隔离） */
  courseId: string | null;
  /** 会话列表（按最近活跃倒序，v3 多会话） */
  sessions: AssistantSession[];
  /** 当前会话（null = 该课程尚无会话）；按课程持久化在 localStorage */
  activeSessionId: string | null;
  messages: AssistantMessage[];
  /** 历史是否已恢复（防重复拉取） */
  restored: boolean;
  restoring: boolean;
  /** 在途轮次：POST turns 后到 done/error 收口前为 true（输入框据此禁用） */
  sending: boolean;
  streamTaskId: string | null;
  /** 在途轮次归属的会话（切会话关流、切回续读的判据） */
  streamSessionId: string | null;
  /** 打字机累积文本（done 刷新拿到落库消息后清空） */
  streamText: string;
  /** 思考模型推理累积（与正文分通道；done 后由消息 thinking 列接管展示） */
  streamThink: string;
  /** 流过程提示（正在思考 / 生成卡片 / 连接中断等待） */
  streamHint: string | null;
  /** 已收口轮次 id（防「只剩提问」的会话在每次进入时反复重连，内存态即可） */
  settledTaskIds: string[];

  /** 切课重置（关流、清时间线与会话态） */
  reset: (courseId: string) => void;
  /** 挂载恢复：拉会话与历史；末条为 user（轮次在途）则重连 SSE 续读 */
  restore: (courseId: string) => Promise<void>;
  /** 以服务端 GET messages 为权威刷新当前会话时间线 */
  refresh: () => Promise<void>;
  /** 发新一轮：无会话先建 → POST turns → 立即开 SSE 收流 */
  send: (message: string) => Promise<void>;
  /** 页面卸载：关流（在途任务照常落库，重进时 restore 续读） */
  stop: () => void;
  /** 停止生成（v3）：POST cancel 取消任务 → 关流 → 刷新见「已停止」部分正文 */
  cancelTurn: () => Promise<void>;
  /** 新建会话并置为当前（空时间线） */
  createSession: (title?: string) => Promise<AssistantSession | null>;
  /** 切换会话：关当前流（任务照跑）→ 拉该会话消息 → 在途则续读 */
  switchSession: (sessionId: string) => Promise<void>;
  /** 重命名会话（失败抛错，由页面提示） */
  renameSession: (sessionId: string, title: string) => Promise<void>;
  /** 删除会话及消息（在途 409 由页面提示先停止；删的是当前会话则切到最近会话） */
  deleteSession: (sessionId: string) => Promise<void>;
  /** 提案卡状态回写（只记账，执行走既有业务 API，由调用方先行调用） */
  patchProposal: (
    messageId: string,
    status: AssistantProposalStatus,
    receipt?: string,
  ) => Promise<void>;
  /** 「执行中断」卡片的手动重试：状态已是 executing（认领过），直接再跑一次 */
  retryProposal: (messageId: string) => void;
  /**
   * 自动执行中的进度文案（messageId → 文案，本地态，不落库）。
   * 进度不走卡片状态机：认领必须是独占的（executing → executing 被后端拒绝），
   * 长步骤（蓝图建议任务）的过程只能靠这份本地态呈现。
   */
  autoProgress: Record<string, string>;
  /**
   * 生成进度快照（project_id → 快照）：出卷是最长的一步，卡片里要给真实进度。
   * 轮询挂在 store 上（不是组件 effect）——教师切到试卷页/切会话也不断，
   * 回来即看到最新进度，生成完成后卡片原地变「进入审核」。
   */
  generationProgress: Record<string, GenerationProgressSnapshot>;
}

export const useAssistantStore = create<AssistantState>()((set, get) => {
  let closeStream: (() => void) | null = null;
  let pollTimer: number | null = null;

  const stopStream = () => {
    if (closeStream) {
      closeStream();
      closeStream = null;
    }
  };

  const stopPolling = () => {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  const clearTurnState = () =>
    set({
      sending: false,
      streamTaskId: null,
      streamSessionId: null,
      streamText: '',
      streamThink: '',
      streamHint: null,
    });

  // ---------------------------------------------------------------------------
  // 自动执行队列：出卷主线上的非里程碑提案（label/impact/auto 来自后端注册表）
  // 不显示「确认执行」，直接调既有业务 API 并在气泡里回报执，然后自动接力。
  // ---------------------------------------------------------------------------

  /** 同步闩锁：拦住「看到卡片 → claim 落库」之间的同帧重入（重挂载/StrictMode） */
  const autoInFlight = new Set<string>();
  /** claim 就没落上的卡片：记为跳过，避免同一张卡反复重试 */
  const autoSkipped = new Set<string>();
  /** 串行队列：sending 只保护 send，不保护并发的业务写 */
  let autoChain: Promise<void> = Promise.resolve();

  const isAutoPending = (m: AssistantMessage) =>
    m.role === 'assistant'
    && m.action?.kind === 'proposal'
    && m.action.status === 'auto'
    && !autoInFlight.has(m.id)
    && !autoSkipped.has(m.id);

  /**
   * 执行一张自动卡片：claim（先落库：中断即停在 executing，不再自动重放非幂等写）
   * → 调既有业务 API → 回写 executed → 自动追问「继续」，让下一张卡自动弹出。
   */
  const runAutoCard = async (m: AssistantMessage, sessionId: string | null) => {
    const courseId = get().courseId;
    if (!courseId) return;
    if (m.action.status !== 'executing') {
      try {
        await get().patchProposal(m.id, 'executing');
      } catch {
        autoSkipped.add(m.id);
        return;
      }
    }
    const token = useAuthStore.getState().token ?? undefined;
    try {
      const { receipt, relay } = await executeProposalAction(m, {
        courseId,
        token,
        // 长步骤（蓝图建议任务）的过程走本地进度态：状态机只认「认领」这一跳
        onProgress: (text) => {
          if (get().courseId === courseId) {
            set((s) => ({ autoProgress: { ...s.autoProgress, [m.id]: text } }));
          }
        },
      });
      if (get().courseId !== courseId) return; // 已切课：结果以服务端为准，不再回写
      await get().patchProposal(m.id, 'executed', receipt);
      useToastStore.getState().addToast(receipt, 'success');
      // 教师切走会话就不再替他接力：链停在原会话，卡片仍是已执行
      const onLadder = RELAY_TOOLS.has(m.action.tool ?? '');
      // 发起生成**不立即接力**：出卷要跑几分钟，此刻追问只能得到「进度 0%，
      // 请稍候」这种没有下文的话。改为等生成真正结束，再由 relayAfterGeneration
      // 追问一次，让助手的收尾回答落在「已生成 N 道题」上（教师诉求）。
      const isGeneration = m.action.tool === 'start_generation';
      if (relay && onLadder && !isGeneration && get().activeSessionId === sessionId) {
        await get().send('继续');
      }
    } catch (err) {
      useToastStore.getState().addToast('自动执行失败: ' + getErrorMessage(err), 'error');
      // 卡片停在 executing：给教师可见的「重试执行」，绝不自动重放
    } finally {
      clearProgress(m.id);
      // 生成是长任务：卡片执行完立即挂上进度轮询（轮询在 store 上，切页不断）
      if (m.action.tool === 'start_generation') {
        const projectId = m.action.payload?.project_id;
        if (projectId) watchGeneration(projectId);
      }
    }
  };

  /** 清掉本地进度文案（执行结束；消息的最终回执以落库的 receipt 为准） */
  const clearProgress = (messageId: string) => {
    set((s) => {
      if (!(messageId in s.autoProgress)) return {};
      const next = { ...s.autoProgress };
      delete next[messageId];
      return { autoProgress: next };
    });
  };

  /** 入队（串行）并保持队列自驱：执行完再找下一张 */
  const enqueueAutoRun = (m: AssistantMessage, sessionId: string | null) => {
    if (autoInFlight.has(m.id)) return;
    autoInFlight.add(m.id);
    autoChain = autoChain
      .then(() => runAutoCard(m, sessionId))
      .catch(() => {
        /* 单张失败不拖垮队列：失败态已由 runAutoCard 收口 */
      })
      .finally(() => {
        autoInFlight.delete(m.id);
        window.setTimeout(drainAutoQueue, 0);
      });
  };

  /** 找出下一张待自动执行的卡片（在途轮次结束后才动，避免与流式抢状态） */
  function drainAutoQueue() {
    const s = get();
    if (!s.courseId || s.sending) return;
    const next = s.messages.find(isAutoPending);
    if (!next) return;
    enqueueAutoRun(next, s.activeSessionId);
  }

  // ---------------------------------------------------------------------------
  // 生成进度轮询：出卷是唯一要跑几分钟的一步，卡片里要有真实进度
  // ---------------------------------------------------------------------------

  /** project_id → 定时器（每个项目一条；到终态即停，避免空转打库） */
  const genTimers = new Map<string, number>();
  /** 本次挂载里亲眼见过 running 的项目——完成后接力的准入条件（见 pollGeneration） */
  const genSawRunning = new Set<string>();
  const GEN_POLL_INTERVAL_MS = 2500;

  const stopGenerationWatch = (projectId: string) => {
    const timer = genTimers.get(projectId);
    if (timer !== undefined) {
      window.clearInterval(timer);
      genTimers.delete(projectId);
    }
  };

  const stopAllGenerationWatches = () => {
    genTimers.forEach((timer) => window.clearInterval(timer));
    genTimers.clear();
  };

  /** 生成任务是否已到终态（任务终态或项目已推进到审核/导出） */
  const generationSettled = (snap: GenerationProgressSnapshot) =>
    snap.task_status === 'succeeded'
    || snap.task_status === 'failed'
    || snap.task_status === 'cancelled'
    || snap.project_status === 'review'
    || snap.project_status === 'exported';

  const pollGeneration = async (projectId: string) => {
    const courseId = get().courseId;
    if (!courseId) {
      stopGenerationWatch(projectId);
      return;
    }
    try {
      const p = await api.examProjects.get(courseId, projectId);
      if (get().courseId !== courseId) return; // 已切课：丢弃本次结果
      const snap: GenerationProgressSnapshot = {
        project_id: projectId,
        progress: p.generation_progress ?? null,
        task_status: p.generation_task_status ?? null,
        stage: p.generation_stage ?? null,
        project_status: p.status,
        item_count: p.item_count ?? null,
        total_score: p.total_score ?? null,
        paper_version_id: p.paper_version_id ?? null,
        error: p.generation_error ?? null,
      };
      set((s) => ({
        generationProgress: { ...s.generationProgress, [projectId]: snap },
      }));
      // 必须**亲眼见过**它跑起来（queued/running）才有资格触发「完成后接力」：
      // 否则刷新时遇到一个早就生成完的历史项目，会平白替教师追问一次。
      if (snap.task_status === 'queued' || snap.task_status === 'running') {
        genSawRunning.add(projectId);
      }
      if (generationSettled(snap)) {
        stopGenerationWatch(projectId);
        if (genSawRunning.delete(projectId)) void relayAfterGeneration(projectId);
      }
    } catch {
      /* 瞬时错误忽略：下一轮重试（终态由任务自身收敛） */
    }
  };

  /**
   * 生成结束后的收尾：**请后端落一条助手消息**（不产生用户气泡）。
   *
   * 教师这一阶段只是等待、没有任何操作——若沿用自动追问，聊天里会冒出一条
   * 教师自己发的「继续」，很突兀。改为调后端通报端点：文案与幂等都在后端，
   * 成功给「已生成 N 道题 + 去审核」，失败给原因与重新生成引导。
   */
  const relayAfterGeneration = async (projectId: string) => {
    const s = get();
    const courseId = s.courseId;
    const sessionId = s.activeSessionId;
    if (!courseId || !sessionId) return;
    // 教师已经不看这条链了（切走会话/时间线里没有该项目卡片）→ 不打扰
    const linked = s.messages.some(
      (m) => m.action?.tool === 'start_generation'
        && m.action.payload?.project_id === projectId,
    );
    if (!linked) return;
    try {
      const token = useAuthStore.getState().token ?? undefined;
      const res = await api.assistant.reportGeneration(
        courseId, sessionId, projectId, token,
      );
      if (res.reported && get().activeSessionId === sessionId) await get().refresh();
    } catch {
      /* 播报失败不打扰：卡片已显示生成完成/失败态，教师仍能进试卷页 */
    }
  };

  const watchGeneration = (projectId: string) => {
    if (genTimers.has(projectId)) return;
    void pollGeneration(projectId); // 立即取一次，不等第一个间隔
    genTimers.set(
      projectId,
      window.setInterval(() => void pollGeneration(projectId), GEN_POLL_INTERVAL_MS),
    );
  };

  /**
   * 扫描时间线里「已发起的生成」，为每个项目挂上进度轮询。
   * 幂等：已在轮询的项目不重复挂；已到终态的卡片不再唤醒（避免无谓打库）。
   */
  const scanGenerationWatches = () => {
    const s = get();
    for (const m of s.messages) {
      if (m.action?.kind !== 'proposal' || m.action.tool !== 'start_generation') continue;
      if (m.action.status !== 'executed') continue; // 只跟真正发起过的
      const projectId = m.action.payload?.project_id;
      if (!projectId || genTimers.has(projectId)) continue;
      const known = s.generationProgress[projectId];
      if (known && generationSettled(known)) continue; // 已收口
      watchGeneration(projectId);
    }
  };

  // 当前会话按课程持久化（切课/刷新后回到上次所在会话；隐私模式不可用则退化为内存态）
  const storageKey = (courseId: string) => `assistant:${courseId}:session`;
  const persistActive = (courseId: string, sid: string | null) => {
    try {
      if (sid) window.localStorage.setItem(storageKey(courseId), sid);
      else window.localStorage.removeItem(storageKey(courseId));
    } catch {
      /* localStorage 不可用：仅内存态 */
    }
  };
  const readPersisted = (courseId: string): string | null => {
    try {
      return window.localStorage.getItem(storageKey(courseId));
    } catch {
      return null;
    }
  };

  /**
   * 进入某会话后接上它的在途流（restore 与 switchSession 共用）：
   * - 本端在途（sending 且归属本会话）→ 续读（重挂载/切回时流已关，幂等先关旧再开新）；
   * - 本端无在途 → 末条 user 且未收口过 → 恢复该轮（换设备/刷新后的在途轮次）。
   *   settledTaskIds 跳过「只剩提问」的已收口轮次（如停止于产出前），避免反复重连。
   */
  const attachSessionStream = (messages: AssistantMessage[]) => {
    const s = get();
    const courseId = s.courseId;
    const sid = s.activeSessionId;
    if (!courseId || !sid) return;
    if (s.sending) {
      // 流式通道是全局的（在途轮次可能属于别的会话）：只在通道断开时续上
      // （页面重挂载时 stop() 关过流），页面按 streamSessionId 归属决定是否
      // 显示流式区。开着就不重开——让 done 持续可达，否则收口丢失、sending 卡死。
      if (s.streamTaskId && !closeStream) openStream(courseId, s.streamTaskId);
      return; // 在途（含 POST 未返回的瞬态）：不抢流，输入保持禁用（单在途约束）
    }
    const last = messages[messages.length - 1];
    if (last && last.role === 'user' && !s.settledTaskIds.includes(last.task_run_id)) {
      set({
        sending: true,
        streamTaskId: last.task_run_id,
        streamSessionId: sid,
        streamText: '',
        streamThink: '',
        streamHint: '正在思考…',
      });
      openStream(courseId, last.task_run_id);
    }
  };

  /** done/error 收口：先拉权威消息再清流式状态（先落库后发 done，后端已保序） */
  const finishTurn = async () => {
    stopStream();
    stopPolling();
    // 记账已收口轮次：末条只剩 user 的轮次（停止于产出前）不再被重连
    const settled = get().streamTaskId;
    if (settled) {
      set((s) => ({ settledTaskIds: [...s.settledTaskIds.slice(-49), settled] }));
    }
    const courseId = get().courseId;
    try {
      if (courseId) {
        const sid = get().activeSessionId;
        const messages = sid ? await api.assistant.listMessages(courseId, sid) : [];
        set({ messages, restored: true });
      }
    } catch {
      useToastStore
        .getState()
        .addToast('对话刷新失败，内容以服务端为准，请稍后刷新页面', 'error');
    } finally {
      clearTurnState();
      // 收口后接着跑自动执行队列：接力产出的下一张卡在这一拍落地
      drainAutoQueue();
      // 与自动执行同理：历史里的生成卡片在这一拍挂上进度轮询
      scanGenerationWatches();
    }
  };

  /**
   * 断线重连也失败（give-up）后的兜底：轮询消息直到本轮 assistant 回包。
   * 任务在 worker 里照常执行，丢的只是打字机过程——消息以落库为准。
   */
  const startPolling = () => {
    stopPolling();
    set({ streamHint: '连接中断，正在等待任务完成…' });
    let attempts = 0;
    pollTimer = window.setInterval(() => {
      attempts += 1;
      const taskRunId = get().streamTaskId;
      if (!taskRunId) {
        stopPolling();
        return;
      }
      void get()
        .refresh()
        .then(() => {
          const cur = get();
          const last = cur.messages[cur.messages.length - 1];
          if (last && last.role === 'assistant' && last.task_run_id === taskRunId) {
            stopPolling();
            clearTurnState();
          }
        })
        .catch(() => {
          /* 瞬时轮询错误忽略，下一轮重试 */
        });
      if (attempts >= 40) {
        // ~2 分钟仍无回包：放开输入，教师可重发或稍后刷新查看
        stopPolling();
        useToastStore
          .getState()
          .addToast('任务迟迟未返回，已恢复输入；稍后刷新可查看结果', 'error');
        clearTurnState();
      }
    }, 3000);
  };

  const openStream = (courseId: string, taskRunId: string) => {
    stopStream();
    closeStream = api.assistant.stream(
      courseId,
      taskRunId,
      (event, data) => {
        if (event === 'delta') {
          const text = typeof data.text === 'string' ? data.text : '';
          set((s) => ({ streamText: s.streamText + text, streamHint: null }));
          return;
        }
        if (event === 'think') {
          // 思考增量：独立通道累积，绝不进 streamText（正式气泡只收正文）
          const text = typeof data.text === 'string' ? data.text : '';
          if (text) set((s) => ({ streamThink: s.streamThink + text }));
          return;
        }
        if (event === 'card') {
          set({
            streamHint:
              data.kind === 'proposal'
                ? '正在生成提案卡…'
                : data.kind === 'sources'
                  ? '正在检索资料内容…'
                  : '正在汇总查询结果…',
          });
          return;
        }
        if (event === 'done') {
          void finishTurn();
          return;
        }
        if (event === 'error') {
          const msg = typeof data.message === 'string' ? data.message : '处理失败';
          useToastStore.getState().addToast('AI 助手：' + msg, 'error');
          // error 事件先于失败消息落库发布（publish → persist），等一拍再刷新，
          // 确保时间线能看到「这一轮处理失败」的失败消息
          window.setTimeout(() => void finishTurn(), 500);
        }
      },
      (reason) => {
        if (reason === 'give-up') startPolling();
        // terminal：done/error 已在 onEvent 处理，无需重复收口
      },
    );
  };

  return {
    courseId: null,
    sessions: [],
    activeSessionId: null,
    messages: [],
    restored: false,
    restoring: false,
    sending: false,
    streamTaskId: null,
    streamSessionId: null,
    streamText: '',
    streamThink: '',
    streamHint: null,
    settledTaskIds: [],
    autoProgress: {},
    generationProgress: {},

    reset: (courseId) => {
      stopStream();
      stopPolling();
      // 切课等于换一套数据：在跑的自动执行队列与进度轮询随之作废
      autoInFlight.clear();
      autoSkipped.clear();
      stopAllGenerationWatches();
      set({
        courseId,
        sessions: [],
        activeSessionId: null,
        messages: [],
        restored: false,
        restoring: false,
        sending: false,
        streamTaskId: null,
        streamSessionId: null,
        streamText: '',
        streamThink: '',
        streamHint: null,
        settledTaskIds: [],
        autoProgress: {},
        generationProgress: {},
      });
    },

    restore: async (courseId) => {
      if (get().courseId !== courseId) get().reset(courseId);

      // 防重复拉取（restoredForRef 同款：check 与置位同一同步块，无竞态）。
      // 已恢复过的重挂载只负责续上本会话在途流。
      if (get().restored || get().restoring) {
        const s = get();
        if (s.sending && s.streamTaskId && s.courseId) {
          attachSessionStream(s.messages);
        }
        return;
      }
      set({ restoring: true });
      try {
        const sessions = await api.assistant.listSessions(courseId);
        const persisted = readPersisted(courseId);
        const active =
          persisted && sessions.some((x) => x.id === persisted)
            ? persisted
            : (sessions[0]?.id ?? null);
        set({ sessions, activeSessionId: active });
        if (active) persistActive(courseId, active);
        const messages = active ? await api.assistant.listMessages(courseId, active) : [];
        set({ messages, restored: true, restoring: false });
        attachSessionStream(messages);
        drainAutoQueue();
        scanGenerationWatches();
      } catch (err) {
        set({ restoring: false });
        throw err;
      }
    },

    refresh: async () => {
      const courseId = get().courseId;
      if (!courseId) return;
      const sid = get().activeSessionId;
      const messages = sid ? await api.assistant.listMessages(courseId, sid) : [];
      set({ messages, restored: true });
      drainAutoQueue();
      scanGenerationWatches();
    },

    send: async (message) => {
      const text = message.trim();
      const courseId = get().courseId;
      if (!courseId || !text || get().sending) return;
      // 乐观落泡：用户消息与思考提示同帧渲染，绝不等 POST 往返——后端再慢，
      // 「继续」/提问气泡也必须立刻出现（气泡先于「正在思考…」同帧可见）。
      // POST 返回后对齐服务端 id；失败整条撤下并复位在途态。
      const optimisticId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const optimisticUser: AssistantMessage = {
        id: optimisticId,
        task_run_id: '',
        role: 'user',
        content: text,
        action: {},
        stream_status: 'complete',
        created_at: new Date().toISOString(),
      };
      set((s) => ({
        sending: true,
        streamTaskId: null,
        streamSessionId: null,
        streamText: '',
        streamThink: '',
        streamHint: '正在思考…',
        messages: [...s.messages, optimisticUser],
        restored: true,
      }));
      try {
        // 无会话先建（单路径：不在 UI 上设禁用态，缺省标题由首条消息自动改题）
        let sid = get().activeSessionId;
        if (!sid) {
          const created = await api.assistant.createSession(courseId);
          // messages 不动：乐观气泡已在其中（新会话本无历史，清空会把它冲掉）
          set((s) => ({ sessions: [created, ...s.sessions], activeSessionId: created.id }));
          persistActive(courseId, created.id);
          sid = created.id;
        }
        const turn = await api.assistant.createTurn(courseId, text, sid);
        set((s) => ({
          messages: s.messages.map((m) =>
            m.id === optimisticId
              ? { ...m, id: turn.user_message_id, task_run_id: turn.task_run_id }
              : m,
          ),
          streamTaskId: turn.task_run_id,
          streamSessionId: turn.session_id,
        }));
        openStream(courseId, turn.task_run_id);
      } catch (err) {
        set((s) => ({ messages: s.messages.filter((m) => m.id !== optimisticId) }));
        clearTurnState();
        throw err;
      }
    },

    stop: () => {
      stopStream();
      // 轮询兜底不随卸载停：store 是全局的，任务收口仍要靠它把消息刷进来
    },

    cancelTurn: async () => {
      const courseId = get().courseId;
      const taskRunId = get().streamTaskId;
      if (!courseId || !taskRunId) return;
      const res = await api.assistant.cancelTurn(courseId, taskRunId);
      const stopped = res.status === 'cancelled';
      useToastStore
        .getState()
        .addToast(
          stopped ? '已停止生成（已生成的内容会保留）' : '本轮已结束，未受影响',
          'info',
        );
      if (stopped) {
        // 流仍开着 → worker 检查点落库后发 done 自然收口（收口时刷新可见
        // 部分正文 +「已停止」徽标）；流已关（罕见）→ 等一拍手动收口，
        // 给检查点留落库时间。
        if (!closeStream) {
          window.setTimeout(() => void finishTurn(), 700);
        }
      } else {
        await finishTurn(); // 已终态（跑完了）：按正常收口刷新
      }
    },

    createSession: async (title) => {
      const courseId = get().courseId;
      if (!courseId) return null;
      const created = await api.assistant.createSession(courseId, title);
      set((s) => ({
        sessions: [created, ...s.sessions],
        activeSessionId: created.id,
        messages: [],
        restored: true,
      }));
      persistActive(courseId, created.id);
      return created;
    },

    switchSession: async (sessionId) => {
      const { courseId, activeSessionId, messages: prevMessages } = get();
      if (!courseId || sessionId === activeSessionId) return;
      // 在途流不关（收口依赖它收 done）：切走后页面按 streamSessionId 归属
      // 隐藏流式区，任务在 worker 照跑；切回原会话原样续显（不取消）。
      const prev = activeSessionId;
      set({ activeSessionId: sessionId, messages: [], restored: false });
      persistActive(courseId, sessionId);
      try {
        const messages = await api.assistant.listMessages(courseId, sessionId);
        set({ messages, restored: true });
        attachSessionStream(messages);
        drainAutoQueue();
        scanGenerationWatches();
      } catch (err) {
        // 失败回滚到原会话与原时间线：否则停在空时间线且同 id 点击会被
        // 早退拦掉，无法重试
        set({ activeSessionId: prev, messages: prevMessages, restored: true });
        persistActive(courseId, prev);
        useToastStore.getState().addToast('会话内容加载失败，请重试', 'error');
        throw err;
      }
    },

    renameSession: async (sessionId, title) => {
      const courseId = get().courseId;
      if (!courseId) return;
      const updated = await api.assistant.renameSession(courseId, sessionId, title);
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === sessionId ? updated : x)) }));
    },

    deleteSession: async (sessionId) => {
      const courseId = get().courseId;
      if (!courseId) return;
      await api.assistant.deleteSession(courseId, sessionId);
      set((s) => {
        const sessions = s.sessions.filter((x) => x.id !== sessionId);
        const removedActive = s.activeSessionId === sessionId;
        return {
          sessions,
          ...(removedActive
            ? {
                activeSessionId: sessions[0]?.id ?? null,
                messages: [],
                restored: true,
              }
            : {}),
        };
      });
      persistActive(courseId, get().activeSessionId);
    },

    patchProposal: async (messageId, status, receipt) => {
      const courseId = get().courseId;
      if (!courseId) return;
      const updated = await api.assistant.patchAction(
        courseId,
        messageId,
        { action_status: status, receipt: receipt ?? '' },
      );
      set((s) => ({
        messages: s.messages.map((m) => (m.id === messageId ? updated : m)),
      }));
    },

    retryProposal: (messageId) => {
      const s = get();
      const m = s.messages.find((x) => x.id === messageId);
      if (!m || autoInFlight.has(messageId)) return;
      // executing 已经是「被认领」的状态：重试不再 claim，直接再跑一次
      enqueueAutoRun(m, s.activeSessionId);
    },
  };
});
