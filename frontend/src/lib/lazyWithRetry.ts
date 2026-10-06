import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

/**
 * 路由级懒加载的重试包装。
 *
 * 客户端 ↔ 服务器的链路会偶发连接被掐（浏览器报 `net::ERR_CONNECTION_RESET`
 * 或请求挂起，实测新建连接约 25~30% 失败），表现为
 * `Failed to fetch dynamically imported module: .../assets/paper-xxx.js`，
 * 整页因此白掉，此前只能靠用户手动刷新恢复。这里做两层兜底：
 *   1. 退避重试 2 次——失败后换的是新连接，单次成功率显著提高；
 *   2. 仍失败则整页 reload 一次（等价手动刷新，用 sessionStorage 标记
 *      做 10 分钟窗口去重，避免服务真挂时死循环刷新）。
 */
const RELOAD_FLAG = 'chunk_reload_at';
const RELOAD_WINDOW_MS = 10 * 60 * 1000;
const RETRY_DELAYS_MS = [300, 900];

async function loadWithRetry<T>(loader: () => Promise<T>): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      return await loader();
    } catch (error) {
      lastError = error;
      const delay = RETRY_DELAYS_MS[attempt];
      if (delay === undefined) break;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
  try {
    const last = Number(sessionStorage.getItem(RELOAD_FLAG) ?? 0);
    if (!last || Date.now() - last > RELOAD_WINDOW_MS) {
      sessionStorage.setItem(RELOAD_FLAG, String(Date.now()));
      window.location.reload();
    }
  } catch {
    /* sessionStorage 被禁用：放弃自动重载，把错误交回上层 */
  }
  throw lastError;
}

/**
 * 同 `React.lazy`，但动态 import 失败时自动重试 / 重载。
 *
 * 类型上不做泛型透传：入参放宽到 `ComponentType<Record<string, never>>`
 * （无 props 组件都能赋值进来，含 LoginPage 这类零参函数组件），返回值按
 * 「无 props 组件」擦除——本项目路由页面都是无 props 组件，只需拿到
 * LazyExoticComponent 供 <Route element> 使用。
 */
export function lazyWithRetry(
  loader: () => Promise<{ default: ComponentType<Record<string, never>> }>,
): LazyExoticComponent<ComponentType<Record<string, never>>> {
  return lazy(() => loadWithRetry(loader));
}