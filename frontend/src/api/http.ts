import { config } from '@/config';
import { useAuthStore } from '@/stores/auth';
import { ApiError } from './errors';
const resolveToken = (t?: string): string | undefined =>
  t ?? useAuthStore.getState().token ?? undefined;

function detail(body: unknown): string {
  if (!body || typeof body !== 'object') return '';
  const d = (body as Record<string, unknown>).detail;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object') {
    const m = (d as Record<string, unknown>).message;
    if (typeof m === 'string') return m;
  }
  return '';
}

async function parseBody(res: Response): Promise<unknown> {
  try {
    const ct = res.headers.get('content-type') ?? '';
    return ct.includes('application/json') ? await res.json() : await res.text();
  } catch {
    return undefined;
  }
}

async function fetchWithAuth(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<Response> {
  const controller =
    config.requestTimeoutMs > 0 ? new AbortController() : undefined;
  const timer = controller
    ? setTimeout(() => controller.abort(), config.requestTimeoutMs)
    : undefined;
  let res: Response;
  try {
    res = await fetch(config.apiBase + path, {
      ...options,
      signal: controller ? controller.signal : undefined,
      credentials: config.credentials,
      headers: {
        'Content-Type': 'application/json',
        ...(resolveToken(token)
          ? { Authorization: 'Bearer ' + resolveToken(token) }
          : {}),
        ...(options.headers ?? {}),
      },
    });
  } catch (e) {
    const err = e as Error;
    if (err && err.name === 'AbortError') {
      throw new ApiError(0, '请求超时，请稍后再试');
    }
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (!res.ok) {
    const body = await parseBody(res);
    const msg =
      detail(body) || res.statusText || '请求失败(' + res.status + ')';
    throw new ApiError(res.status, msg, body);
  }
  return res;
}

/** 统一 JSON 请求：自动鉴权/超时；204→undefined；非 2xx 抛 ApiError。 */
export async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const res = await fetchWithAuth(path, options, token);
  if (res.status === 204) return undefined as T;
  const isJson = (res.headers.get('content-type') ?? '').includes('application/json');
  return (isJson ? await res.json() : await res.blob()) as T;
}

/**
 * 统一附件/文档请求：自动鉴权/超时，非 2xx 抛 ApiError，成功返回 Blob。
 * 导出与整体预览专用——iframe/window.open 带不了 Authorization 头，
 * 必须先带鉴权拉成 Blob 再用 object URL 打开（token 不进 URL）。
 */
export async function requestBlob(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<Blob> {
  const res = await fetchWithAuth(path, options, token);
  return res.blob();
}

/** 本地存储兜底的上传（PUT 二进制），详见 docs/backend-api.md §1.2。 */
export function uploadBinary(
  path: string,
  body: Blob,
  token?: string,
): Promise<void> {
  return request<void>(
    path,
    { method: 'PUT', headers: { 'Content-Type': body.type || 'application/octet-stream' }, body },
    token,
  );
}