/**
 * Centralized runtime configuration for the frontend.
 *
 * 集中所有环境相关配置，前端其它模块一律从这里读取，
 * 避免 API base URL / 超时等散落在各处硬编码。
 */
export const config = {
  /**
   * API 基础路径。默认走 Vite dev proxy（`/api` → 127.0.0.1:8000）。
   * 生产或独立部署时可通过 `VITE_API_BASE` 覆盖。
   */
  apiBase: import.meta.env.VITE_API_BASE ?? '/api/v1',

  /** 上传直传（S3/MinIO PUT）是否可用，由 upload-sessions 返回的 upload_url 决定 */

  /** 默认请求超时（毫秒）。0 表示不超时。 */
  requestTimeoutMs: Number(import.meta.env.VITE_API_TIMEOUT_MS ?? 0),

  /** 3xx/4xx 是否跟随（与鉴权 cookie 相关） */
  credentials: (import.meta.env.VITE_API_CREDENTIALS ?? 'include') as RequestCredentials,
  /** 演示模式：后端未连接时使用内置静态数据渲染页面，后端恢复后设为 false 切回真实接口。 */
  enableMock: (import.meta.env.VITE_ENABLE_MOCK ?? 'true') !== 'false',
} as const;

export type Config = typeof config;