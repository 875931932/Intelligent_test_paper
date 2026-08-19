export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options?.headers ?? {}),
      },
    })
  } catch {
    throw new Error('网络请求失败，请检查服务是否可用')
  }
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    let detail = ''
    if (body != null) {
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) {
        // FastAPI 校验错误：取第一条的可读信息
        const first = body.detail[0] as { msg?: string; loc?: unknown[] } | undefined
        detail = first?.msg ?? JSON.stringify(body.detail)
      } else if (Object.keys(body).length > 0) {
        detail = JSON.stringify(body)
      }
    }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`)
  }
  return body as T
}
