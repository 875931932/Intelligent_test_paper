import { useEffect, useRef, useState } from 'react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import type { MaterialContentResponse } from '@/types/api';

const PAGE_SIZE = 30;

const locText = (page?: number | null, heading?: string[]): string =>
  [page != null ? `第 ${page + 1} 页` : '', (heading ?? []).join(' › ')]
    .filter(Boolean)
    .join(' · ');

/**
 * 资料解析内容预览弹窗（资料卡「查看」与 AI 助手来源卡「查看原文」共用）。
 *
 * - 带 focusBlockId：定位模式——该片段±上下文窗口、焦点高亮；旧引用
 *   （资料重解析后块 id 失效）如实提示并自动回落全文，不装作定位成功；
 * - 不带：分页浏览整份资料；未解析（total=0）提示先解析，不是错误。
 * 数据来自 GET materials/{id}/content，与助手 RAG 同语料。
 */
export function MaterialContentPreview({
  open,
  onClose,
  courseId,
  materialId,
  materialName,
  focusBlockId,
}: {
  open: boolean;
  onClose: () => void;
  courseId: string;
  materialId: string;
  materialName?: string;
  focusBlockId?: string | null;
}) {
  const token = useAuthStore((s) => s.token);
  const [data, setData] = useState<MaterialContentResponse | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  // 内部焦点态：定位模式里可切回全文；旧引用失效时也回落到这里
  const [focusId, setFocusId] = useState<string | null>(focusBlockId ?? null);
  const [staleNote, setStaleNote] = useState(false);
  const focusRef = useRef<HTMLDivElement | null>(null);

  // 每次打开/切换目标重置到请求方给定的模式与页码
  useEffect(() => {
    if (!open) return;
    setFocusId(focusBlockId ?? null);
    setOffset(0);
    setStaleNote(false);
    setData(null);
  }, [open, materialId, focusBlockId]);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    setError('');
    api.materials
      .getContent(
        courseId,
        materialId,
        focusId ? { blockId: focusId } : { offset, limit: PAGE_SIZE },
        token ?? undefined,
      )
      .then((d) => {
        if (!alive) return;
        setData(d);
        if (focusId && d.focus && d.focus.index == null) {
          // 旧引用失效：记提示并自动回落全文首页（focusId 变化触发本轮重拉）
          setStaleNote(true);
          setFocusId(null);
        }
      })
      .catch((e) => { if (alive) setError(getErrorMessage(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [open, courseId, materialId, focusId, offset, token]);

  // 定位模式：焦点块滚入视野（弹窗内容区可能需要滚动）
  useEffect(() => {
    if (data?.focus && data.focus.index != null) {
      focusRef.current?.scrollIntoView({ block: 'center' });
    }
  }, [data]);

  const isFocusMode = focusId != null;
  const total = data?.total ?? 0;
  const blocks = data?.blocks ?? [];
  const canPrev = offset > 0 && !loading;
  const canNext = offset + (data?.returned ?? 0) < total && !loading;

  const blockCard = (b: (typeof blocks)[number], i: number) => {
    const focused = isFocusMode && b.id === focusId;
    return (
      <div
        key={b.id}
        ref={focused ? focusRef : undefined}
        style={{
          padding: '10px 12px',
          borderRadius: 'var(--radius-sm)',
          background: focused ? 'var(--accent-subtle)' : 'var(--fill)',
          border: focused ? '1px solid var(--accent-soft)' : '1px solid transparent',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>
            {(data?.offset ?? 0) + i + 1}. {locText(b.page_index, b.heading_path) || '解析片段'}
          </span>
          {focused && <Badge variant="info">引用片段</Badge>}
        </div>
        <p style={{ fontSize: '0.83rem', lineHeight: 1.7, margin: '4px 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {b.content}
        </p>
      </div>
    );
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`资料预览：${data?.material_name || materialName || ''}`}
      maxWidth="720px"
      footer={
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', flexWrap: 'wrap' }}>
          {isFocusMode && (
            <Button size="sm" variant="secondary" onClick={() => setFocusId(null)}>
              浏览全部（{total} 段）
            </Button>
          )}
          {!isFocusMode && total > 0 && (
            <>
              <Button size="sm" variant="secondary" disabled={!canPrev} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                上一页
              </Button>
              <Button size="sm" variant="secondary" disabled={!canNext} onClick={() => setOffset(offset + PAGE_SIZE)}>
                下一页
              </Button>
              <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>
                第 {offset + 1}–{offset + (data?.returned ?? 0)} / {total} 段
              </span>
            </>
          )}
        </div>
      }
    >
      {error && (
        <p style={{ fontSize: '0.83rem', color: 'var(--error)', margin: 0 }}>{error}</p>
      )}
      {!error && loading && !data && (
        <p style={{ fontSize: '0.83rem', color: 'var(--text-tertiary)', margin: 0 }}>加载解析内容…</p>
      )}
      {!error && data && total === 0 && (
        <p style={{ fontSize: '0.83rem', color: 'var(--text-secondary)', margin: 0, lineHeight: 1.7 }}>
          该资料尚无解析内容——请先在资料库点「解析」，完成后即可查看。
        </p>
      )}
      {!error && staleNote && total > 0 && (
        <p style={{ fontSize: '0.83rem', color: 'var(--warning)', margin: '0 0 8px', lineHeight: 1.7 }}>
          该片段已不在当前解析结果中（资料可能已重新解析）。下面是这份资料的最新内容，可自行定位。
        </p>
      )}
      {!error && data && total > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {isFocusMode && (
            <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: 0 }}>
              显示命中片段的上下文（全文共 {total} 段）
            </p>
          )}
          {blocks.length === 0 && !loading && (
            <p style={{ fontSize: '0.83rem', color: 'var(--text-tertiary)', margin: 0 }}>没有更多片段了。</p>
          )}
          {blocks.map((b, i) => blockCard(b, i))}
        </div>
      )}
      {loading && data && (
        <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: '8px 0 0' }}>加载中…</p>
      )}
    </Modal>
  );
}
