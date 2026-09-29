/**
 * 程序化触发浏览器下载。
 *
 * 导出端点需要 Authorization 头：`window.open` / 裸 `<a href>` 都带不了，
 * 也不允许把 token 拼进 URL（会进浏览器历史与日志）。统一做法是带鉴权拉成
 * Blob → 用 object URL 触发下载，启动后再回收。
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 下载启动后再回收；提前 revoke 会让个别浏览器拿不到文件
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
