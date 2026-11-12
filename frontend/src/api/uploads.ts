import { uploadBinary } from './http';

const MIME: Record<string, string> = {
  md: 'text/markdown', markdown: 'text/markdown', txt: 'text/plain', pdf: 'application/pdf',
  doc: 'application/msword', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  ppt: 'application/vnd.ms-powerpoint', pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  xls: 'application/vnd.ms-excel', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
};

export function fileMime(file: File): string {
  return MIME[file.name.split('.').pop()?.toLowerCase() || ''] || file.type || 'application/octet-stream';
}

/** Local storage uses the API session; signed object-storage URLs never receive its token. */
export async function uploadFileObject(
  session: { upload_url: string; object_key: string; headers?: Record<string, string> },
  file: File,
  sha256: string,
): Promise<void> {
  const mime = session.headers?.['Content-Type'] || fileMime(file);
  const url = new URL(session.upload_url || '/api/v1/_local-storage/' + session.object_key, window.location.origin);
  if (url.origin === window.location.origin && url.pathname.startsWith('/api/v1/_local-storage/')) {
    await uploadBinary('/_local-storage/' + session.object_key, file.slice(0, file.size, mime));
    return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5 * 60 * 1000);
  try {
    const response = await fetch(url, {
      method: 'PUT', body: file, signal: controller.signal,
      headers: { 'Content-Type': mime, 'x-amz-meta-sha256': sha256, ...session.headers },
    });
    if (!response.ok) throw new Error('对象上传失败(' + response.status + ')');
  } finally { clearTimeout(timer); }
}
