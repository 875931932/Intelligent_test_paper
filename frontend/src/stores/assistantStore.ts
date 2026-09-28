import { create } from 'zustand';
import { api } from '@/api/client';
import type { AssistantMessage, AssistantProposalStatus } from '@/types/api';
import { useToastStore } from './toast';

/**
 * AI 助手对话状态（一域一 store）：消息时间线 + 在途轮次的流式状态。
 *
 * 恢复语义对齐 BlueprintSuggestPanel 的防重复模式（restoredForRef/startedRef）：
 * 挂载只拉一次历史；末条是 user 消息即视为上一轮仍在途，重连 SSE 续读；
 * 页面卸载只关流不丢状态，回来后续读（任务在 worker 里照常跑完落库）。
 */
interface AssistantState {
  /** 当前课程作用域；切课整体重置（消息/卡片全部带 course_id 隔离） */
  courseId: string | null;
  messages: AssistantMessage[];
  /** 历史是否已恢复（防重复拉取） */
  restored: boolean;
  restoring: boolean;
  /** 在途轮次：POST turns 后到 done/error 收口前为 true（输入框据此禁用） */
  sending: boolean;
  streamTaskId: string | null;
  /** 打字机累积文本（done 刷新拿到落库消息后清空） */
  streamText: string;
  /** 流过程提示（正在思考 / 生成卡片 / 连接中断等待） */
  streamHint: string | null;

  /** 切课重置（关流、清时间线） */
  reset: (courseId: string) => void;
  /** 挂载恢复：拉历史；若末条为 user（轮次在途）则重连 SSE 续读 */
  restore: (courseId: string) => Promise<void>;
  /** 以服务端 GET messages 为权威刷新时间线 */
  refresh: () => Promise<void>;
  /** 发新一轮：POST turns → 立即开 SSE 收流 */
  send: (message: string) => Promise<void>;
  /** 页面卸载：关流（在途任务照常落库，重进时 restore 续读） */
  stop: () => void;
  /** 提案卡状态回写（只记账，执行走既有业务 API，由页面先行调用） */
  patchProposal: (
    messageId: string,
    status: AssistantProposalStatus,
    receipt?: string,
  ) => Promise<void>;
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
    set({ sending: false, streamTaskId: null, streamText: '', streamHint: null });

  /** done/error 收口：先拉权威消息再清流式状态（先落库后发 done，后端已保序） */
  const finishTurn = async () => {
    stopStream();
    stopPolling();
    const courseId = get().courseId;
    try {
      if (courseId) {
        const messages = await api.assistant.listMessages(courseId);
        set({ messages, restored: true });
      }
    } catch {
      useToastStore
        .getState()
        .addToast('对话刷新失败，内容以服务端为准，请稍后刷新页面', 'error');
    } finally {
      clearTurnState();
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
    messages: [],
    restored: false,
    restoring: false,
    sending: false,
    streamTaskId: null,
    streamText: '',
    streamHint: null,

    reset: (courseId) => {
      stopStream();
      stopPolling();
      set({
        courseId,
        messages: [],
        restored: false,
        restoring: false,
        sending: false,
        streamTaskId: null,
        streamText: '',
        streamHint: null,
      });
    },

    restore: async (courseId) => {
      if (get().courseId !== courseId) get().reset(courseId);

      // 页面重挂载：在途流已随卸载关闭，重新接上续读（幂等，先关旧再开新）
      const taskRunId = get().streamTaskId;
      if (get().sending && taskRunId && !closeStream) {
        openStream(courseId, taskRunId);
      }

      // 防重复拉取（restoredForRef 同款：check 与置位同一同步块，无竞态）
      if (get().restored || get().restoring) return;
      set({ restoring: true });
      try {
        const messages = await api.assistant.listMessages(courseId);
        set({ messages, restored: true, restoring: false });
        // 末条是 user → 上一轮仍在途 → 重连 SSE 续读
        const last = messages[messages.length - 1];
        if (last && last.role === 'user') {
          set({
            sending: true,
            streamTaskId: last.task_run_id,
            streamText: '',
            streamHint: '正在思考…',
          });
          openStream(courseId, last.task_run_id);
        }
      } catch (err) {
        set({ restoring: false });
        throw err;
      }
    },

    refresh: async () => {
      const courseId = get().courseId;
      if (!courseId) return;
      const messages = await api.assistant.listMessages(courseId);
      set({ messages, restored: true });
    },

    send: async (message) => {
      const text = message.trim();
      const courseId = get().courseId;
      if (!courseId || !text || get().sending) return;
      set({ sending: true, streamTaskId: null, streamText: '', streamHint: '正在思考…' });
      try {
        const turn = await api.assistant.createTurn(courseId, text);
        const localUser: AssistantMessage = {
          id: turn.user_message_id,
          task_run_id: turn.task_run_id,
          role: 'user',
          content: text,
          action: {},
          stream_status: 'complete',
          created_at: new Date().toISOString(),
        };
        set((s) => ({
          messages: [...s.messages, localUser],
          restored: true,
          streamTaskId: turn.task_run_id,
        }));
        openStream(courseId, turn.task_run_id);
      } catch (err) {
        clearTurnState();
        throw err;
      }
    },

    stop: () => {
      stopStream();
      // 轮询兜底不随卸载停：store 是全局的，任务收口仍要靠它把消息刷进来
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
  };
});
