import { config } from '@/config';
import { useAuthStore } from '@/stores/auth';
import { request } from '../http';
import type {
  AssistantActionPatch,
  AssistantMessage,
  AssistantSession,
  AssistantTurnCreated,
} from '../../types/api';

/** 流结束原因：terminal=收到 done/error 正常收口；give-up=断线重连一次后仍失败 */
export type StreamEndReason = 'terminal' | 'give-up';

/**
 * 打开一轮对话的 SSE 事件流（fetch + ReadableStream；EventSource 带不了 Authorization）。
 *
 * - 按行解析 `id:` / `event:` / `data:`，空行分发一帧；
 * - 收到 done/error 视为终态：关流不再重连；
 * - 网络断开自动重连一次，带上最后一条 `id` 让服务端 XREAD 从该条目后续读
 *   （不重放已收增量）；仍失败 → onEnd('give-up')，由 store 走轮询兜底；
 * - 返回关闭函数（新一轮开始 / 页面卸载时中止读取）。
 */
export function streamTurn(
  courseId: string,
  taskRunId: string,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  onEnd?: (reason: StreamEndReason) => void,
): () => void {
  let closed = false;
  let retried = false;
  let terminal = false;
  let lastId = '';
  let abort: AbortController | null = null;

  const giveUpOrRetry = () => {
    if (closed || terminal) return;
    if (retried) {
      onEnd?.('give-up');
      return;
    }
    retried = true;
    window.setTimeout(() => {
      if (!closed) void connect();
    }, 600);
  };

  const connect = async () => {
    abort = new AbortController();
    const token = useAuthStore.getState().token;
    const qs = lastId ? `?last_id=${encodeURIComponent(lastId)}` : '';
    let res: Response;
    try {
      res = await fetch(
        `${config.apiBase}/courses/${courseId}/assistant/turns/${taskRunId}/stream${qs}`,
        {
          headers: {
            Accept: 'text/event-stream',
            ...(token ? { Authorization: 'Bearer ' + token } : {}),
          },
          credentials: config.credentials,
          signal: abort.signal,
        },
      );
    } catch {
      giveUpOrRetry();
      return;
    }
    if (!res.ok || !res.body) {
      giveUpOrRetry();
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let event = '';
    let id = ''; // id 缓冲按 SSE 规范跨帧保留，直到下一条 id 行覆盖
    const dataLines: string[] = [];

    const dispatch = () => {
      if (!event && dataLines.length === 0) return;
      const raw = dataLines.join('\n');
      let data: Record<string, unknown> = {};
      try {
        const parsed: unknown = JSON.parse(raw);
        data =
          parsed && typeof parsed === 'object'
            ? (parsed as Record<string, unknown>)
            : { text: raw };
      } catch {
        data = { text: raw };
      }
      dataLines.length = 0;
      const name = event;
      event = '';
      if (id) lastId = id;
      if (name === 'done' || name === 'error') terminal = true;
      onEvent(name, data);
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl = buf.indexOf('\n');
        while (nl >= 0) {
          const line = buf.slice(0, nl).replace(/\r$/, '');
          buf = buf.slice(nl + 1);
          if (line === '') {
            dispatch();
          } else if (line.startsWith('event:')) {
            event = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).replace(/^ /, ''));
          } else if (line.startsWith('id:')) {
            id = line.slice(3).trim();
          }
          nl = buf.indexOf('\n');
        }
        if (terminal) break; // 终态帧后服务端即关连接，提前收尾
      }
    } catch {
      /* 读取中断（网络抖动/服务端关闭）→ 走下方收尾 */
    }

    if (closed) return;
    if (terminal) {
      onEnd?.('terminal');
      return;
    }
    giveUpOrRetry();
  };

  void connect();

  return () => {
    closed = true;
    abort?.abort();
  };
}

export const assistantApi = {
  /** 入新一轮：写 user 消息 + assistant_turn 任务，202 返回 task_run_id */
  createTurn: (
    courseId: string,
    message: string,
    sessionId?: string,
    token?: string,
  ): Promise<AssistantTurnCreated> =>
    request('/courses/' + courseId + '/assistant/turns', {
      method: 'POST',
      body: JSON.stringify(sessionId ? { message, session_id: sessionId } : { message }),
    }, token),
  /** 历史恢复（时间序，含卡片与回执；挂载与 done 后刷新都以它为权威）。
   *  sessionId 给定只返回该会话（v3），缺省全课程（向后兼容） */
  listMessages: (courseId: string, sessionId?: string, token?: string): Promise<AssistantMessage[]> =>
    request(
      '/courses/' + courseId + '/assistant/messages' +
        (sessionId ? '?session_id=' + encodeURIComponent(sessionId) : ''),
      undefined,
      token,
    ),
  /** 提案卡状态回写（proposed → executed/dismissed 单向；执行本身走既有业务 API） */
  patchAction: (
    courseId: string,
    messageId: string,
    patch: AssistantActionPatch,
    token?: string,
  ): Promise<AssistantMessage> =>
    request('/courses/' + courseId + '/assistant/messages/' + messageId, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }, token),
  /** 会话列表（按最近活跃倒序，v3 多会话） */
  listSessions: (courseId: string, token?: string): Promise<AssistantSession[]> =>
    request('/courses/' + courseId + '/assistant/sessions', undefined, token),
  /** 新建会话（标题缺省「新会话」，首条消息自动改题） */
  createSession: (courseId: string, title?: string, token?: string): Promise<AssistantSession> =>
    request('/courses/' + courseId + '/assistant/sessions', {
      method: 'POST',
      body: JSON.stringify(title ? { title } : {}),
    }, token),
  /** 重命名会话 */
  renameSession: (
    courseId: string,
    sessionId: string,
    title: string,
    token?: string,
  ): Promise<AssistantSession> =>
    request('/courses/' + courseId + '/assistant/sessions/' + sessionId, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }, token),
  /** 删除会话及全部消息（204）；在途轮次会 409，由调用方提示先停止 */
  deleteSession: (courseId: string, sessionId: string, token?: string): Promise<void> =>
    request('/courses/' + courseId + '/assistant/sessions/' + sessionId, {
      method: 'DELETE',
    }, token),
  /**
   * 停止生成（v3）：纯 DB 改任务状态、零 LLM；worker 检查点中止流式并保留
   * 已流出的部分正文。幂等：已终态原样返回现态（status 非 cancelled 即未停成）。
   */
  cancelTurn: (
    courseId: string,
    taskRunId: string,
    token?: string,
  ): Promise<{ task_run_id: string; status: string }> =>
    request('/courses/' + courseId + '/assistant/turns/' + taskRunId + '/cancel', {
      method: 'POST',
    }, token),
  /** SSE 事件流（delta/card/done/error），见 streamTurn */
  stream: streamTurn,
};
