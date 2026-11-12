import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '@/api/client';
import type { KnowledgeSpace } from '@/api/domains/knowledgeBase';
import { Button, Modal, Select } from '@/components/ui';
import { useToastStore } from '@/stores/toast';

export interface ShareMaterialSource {
  course_id: string;
  version_id: string;
  title: string;
  version_no: number;
}

interface ShareModalProps {
  source: ShareMaterialSource | null;
  targetSpaceId?: string | null;
  onClose: () => void;
  onShared?: () => void;
}

export function ShareCourseMaterialModal(props: ShareModalProps) {
  if (!props.source) return null;
  return <ShareModalContent key={`${props.source.version_id}:${props.targetSpaceId || ''}`} {...props} />;
}

function ShareModalContent({ source, targetSpaceId, onClose, onShared }: ShareModalProps) {
  const navigate = useNavigate();
  const addToast = useToastStore((s) => s.addToast);
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]);
  const [spaceId, setSpaceId] = useState('');
  const [classification, setClassification] = useState<'shareable_teaching' | 'limited_teaching'>('shareable_teaching');
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ spaceId: string; documentId: string } | null>(null);
  const versionId = source?.version_id;

  useEffect(() => {
    if (!versionId) return;
    let cancelled = false;
    api.knowledgeBase.listSpaces().then((items) => {
      if (cancelled) return;
      const allowed = items.filter((item) => item.status === 'active' && item.role !== 'reader');
      setSpaces(allowed);
      setSpaceId(allowed.find((item) => item.id === targetSpaceId)?.id || allowed[0]?.id || '');
    }).catch((err: unknown) => {
      if (!cancelled) setError(err instanceof Error ? err.message : '加载知识库失败');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [versionId, targetSpaceId]);

  const space = spaces.find((item) => item.id === spaceId);
  const submit = async () => {
    if (!source || !space || !confirmed) return;
    setBusy(true);
    setError('');
    try {
      const value = await api.knowledgeBase.importFromCourse(space.id, {
        course_id: source.course_id,
        material_version_id: source.version_id,
        expected_policy_version: space.policy_version,
        confirm_shareable: confirmed,
        classification,
      });
      setResult({ spaceId: space.id, documentId: value.document_id });
      addToast(
        value.idempotent
          ? value.state === 'ready'
            ? '此版本已就绪，无需重复解析'
            : '此版本已添加，未重复创建'
          : value.state === 'ready'
            ? '已添加到共享库，可直接预览并发布'
            : '已添加到共享库，解析后可预览并发布',
        'success'
      );
      onShared?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : '添加失败');
    } finally { setBusy(false); }
  };

  return (
    <Modal open={Boolean(source)} onClose={() => { if (!busy) onClose(); }} title="发布课程资料到共享库"
      footer={<>
        <Button variant="secondary" disabled={busy} onClick={onClose}>{result ? '关闭' : '取消'}</Button>
        {result ? <Button onClick={() => { navigate(`/knowledge/${result.spaceId}?fromCourse=${source?.course_id}&document=${result.documentId}&sourceVersion=${source?.version_id}`); onClose(); }}>前往预览与发布</Button>
          : <Button loading={busy} disabled={loading || !space || !confirmed || busy} onClick={() => void submit()}>添加待发布版本</Button>}
      </>}>
      <div style={{ display: 'grid', gap: 16 }}>
        <div style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{source?.title} · v{source?.version_no}</div>
        {error && <p role="alert" style={{ color: 'var(--error-ink)' }}>{error}</p>}
        {result ? <p>资料已保留来源与固定版本。共享库发布后，授权教师才能搜索、提问或导入自己的课程。</p> : <>
          {loading ? <p>正在读取可编辑的共享库…</p> : spaces.length ? <>
            <Select label="目标共享库" value={spaceId} options={spaces.map((item) => ({ value: item.id, label: item.name }))}
              onChange={(e) => { setSpaceId(e.target.value); setConfirmed(false); setClassification('shareable_teaching'); }} />
            <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>发布后读者范围：{space?.read_scope_label}</p>
            <Select label="资料分类" value={classification}
              options={space?.read_scope === 'members' ? [
                { value: 'shareable_teaching', label: '可共享教学资料' },
                { value: 'limited_teaching', label: '限指定教师资料' },
              ] : [{ value: 'shareable_teaching', label: '可共享教学资料' }]}
              onChange={(e) => { setClassification(e.target.value as typeof classification); setConfirmed(false); }} />
            <label style={{ display: 'flex', gap: 10, alignItems: 'flex-start', fontSize: '0.875rem', lineHeight: 1.7 }}>
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} style={{ marginTop: 6 }} />
              我确认拥有分享权限，资料不含未公开试卷、答案或考试保密内容，可供上述范围使用。
            </label>
          </> : <p>暂无可编辑的共享库。请先在资料中心创建知识库，或由负责人授予编辑权限。</p>}
          <p style={{ fontSize: '0.8125rem', lineHeight: 1.7, color: 'var(--text-tertiary)' }}>直接使用已上传文件，无需重新上传。同一课程资料再次添加到同一库时追加版本；原课程与已发布版本在新版本确认发布前保持不变。</p>
        </>}
      </div>
    </Modal>
  );
}
