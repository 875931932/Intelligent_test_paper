/**
 * Typed API error hierarchy + user-facing message mapping.
 *
 * 后端统一错误 shape 为 `{ "detail": string | object }`（见 docs/backend-api.md
 * 状态码约定）。这里把非 2xx 响应包装为带 status / body 的 `ApiError`，
 * 并提供 `getErrorMessage` 将错误映射为可读文案，避免把原始 detail 直接抛给用户。
 */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

/**
 * 将任意错误映射为用户可读文案。
 * 供页面 / toast 使用：`addToast(getErrorMessage(e), 'error')`。
 */
export function getErrorMessage(err: unknown): string {
  if (isApiError(err)) {
    switch (err.status) {
      case 0:
      case 503:
        return '服务暂不可用，请稍后再试。';
      case 401:
        return '登录已过期，请重新登录。';
      case 403:
        return '没有权限执行此操作。';
      case 404:
        return '请求的资源不存在。';
      case 409:
        // 后端 detail 可能携带 { message, item_indices }
        if (err.body && typeof err.body === 'object') {
          const m = (err.body as Record<string, unknown>).message;
          if (typeof m === 'string') return m;
        }
        return '操作冲突，请刷新后重试。';
      case 422:
        return '提交的参数有误，请检查输入。';
      case 410:
        return '上传会话已过期，请重新上传。';
      default:
        break;
    }
    if (err.message) return err.message;
    return '请求失败，请稍后再试。';
  }
  if (err instanceof TypeError && err.message === 'Failed to fetch') {
    return '无法连接服务器，请检查网络。';
  }
  if (err instanceof Error && err.message) return err.message;
  return '发生未知错误，请稍后再试。';
}