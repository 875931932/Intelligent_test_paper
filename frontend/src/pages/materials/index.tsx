import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { Upload, RefreshCw, Trash2, FileText, Loader2 } from 'lucide-react';
import { api } from '@/api/client';
import { useCourseStore } from '@/stores/course';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Input, Select, Badge, Spinner } from '@/components/ui';
import { computeSha256 } from '@/lib/sha256';
import type { MaterialResponse } from '@/types/api';

const MATERIAL_TYPE_LABELS: Record<string, string> = {
  teaching_syllabus: '教学大纲',
  assessment_syllabus: '考核大纲',
  teaching_material: '教材',
  exercise: '习题',
};

const MATERIAL_TYPE_VARIANTS: Record<string, string> = {
  teaching_syllabus: 'info',
  assessment_syllabus: 'success',
  teaching_material: 'default',
  exercise: 'warning',
};

const PARSE_STATUS_LABELS: Record<string, string> = {
  pending: '待解析',
  parsing: '解析中',
  completed: '已完成',
  failed: '失败',
};

const PARSE_STATUS_VARIANTS: Record<string, string> = {
  pending: 'default',
  parsing: 'warning',
  completed: 'success',
  failed: 'error',
};

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

export default function MaterialsPage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const { activeCourseId } = useCourseStore();
  const courseId = routeCourseId || activeCourseId || '';

  const { addToast } = useToastStore();

  const [materials, setMaterials] = useState<MaterialResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFilename, setUploadFilename] = useState('');
  const [uploadType, setUploadType] = useState('teaching_material');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);

  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [parsingIds, setParsingIds] = useState<Set<string>>(new Set());

  const pollingRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());

  const loadMaterials = useCallback(async () => {
    if (!courseId) return;
    try {
      setLoading(true);
      const data = await api.materials.list(courseId);
      setMaterials(Array.isArray(data) ? data : []);
    } catch {
      addToast('加载资料列表失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [courseId, addToast]);

  useEffect(() => {
    loadMaterials();
    return () => {
      pollingRef.current.forEach((timer) => clearInterval(timer));
      pollingRef.current.clear();
    };
  }, [loadMaterials]);

  const handleUpload = async () => {
    if (!courseId || !uploadFile || !uploadFilename.trim()) {
      addToast('请填写完整信息', 'error');
      return;
    }
    try {
      setUploading(true);
      const sha256 = await computeSha256(uploadFile);
      const session = await api.materials.createUploadSession(courseId, {
        filename: uploadFilename,
        material_type: uploadType,
        size_bytes: uploadFile.size,
        sha256,
        mime_type: uploadFile.type || 'application/octet-stream',
      });

      if (!session?.upload_url) {
        throw new Error('未获取到上传地址');
      }

      await fetch(session.upload_url, {
        method: 'PUT',
        body: uploadFile,
        headers: { 'Content-Type': uploadFile.type || 'application/octet-stream' },
      });

      await api.materials.completeUpload(courseId, session.session_id);

      addToast('资料上传成功', 'success');
      setUploadOpen(false);
      setUploadFilename('');
      setUploadFile(null);
      setUploadType('teaching_material');
      loadMaterials();
    } catch {
      addToast('上传失败，请重试', 'error');
    } finally {
      setUploading(false);
    }
  };

  const handleParse = async (material: MaterialResponse) => {
    const materialId = material.id;
    try {
      await api.materials.parse(courseId, materialId);
      setParsingIds((prev) => new Set(prev).add(materialId));
      addToast('开始解析，请稍候...', 'info');

      if (pollingRef.current.has(materialId)) {
        clearInterval(pollingRef.current.get(materialId)!);
      }

      const timer = setInterval(async () => {
        try {
          const updated = await api.materials.pollParse(courseId, materialId);
          const status = (updated as Record<string, unknown>)?.parse_status;
          const statusValue = (status as Record<string, unknown>)?.status as string | undefined;
          if (statusValue === 'completed' || statusValue === 'failed') {
            clearInterval(timer);
            pollingRef.current.delete(materialId);
            setParsingIds((prev) => {
              const next = new Set(prev);
              next.delete(materialId);
              return next;
            });
            addToast(statusValue === 'completed' ? '解析完成' : '解析失败', statusValue === 'completed' ? 'success' : 'error');
            loadMaterials();
          }
        } catch {
          clearInterval(timer);
          pollingRef.current.delete(materialId);
          setParsingIds((prev) => {
            const next = new Set(prev);
            next.delete(materialId);
            return next;
          });
        }
      }, 2000);

      pollingRef.current.set(materialId, timer);
    } catch {
      addToast('触发解析失败', 'error');
    }
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      setDeleting(true);
      await api.materials.delete(courseId, deleteId);
      addToast('删除成功', 'success');
      setDeleteId(null);
      loadMaterials();
    } catch {
      addToast('删除失败', 'error');
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em' }}>资料库</h1>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)', marginTop: '6px' }}>管理课程教学资料与解析</p>
        </div>
        <Button onClick={() => setUploadOpen(true)} icon={<Upload size={16} />}>
          上传资料
        </Button>
      </div>

      {/* Materials Table */}
      <div className="glass-card" style={{ padding: '16px', overflow: 'hidden' }}>
        {loading ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '80px 0' }}>
            <Spinner size="lg" />
          </div>
        ) : materials.length === 0 ? (
          <div style={{ padding: '80px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px', textAlign: 'center' }}>
            <div style={{ width: 56, height: 56, borderRadius: '18px', background: 'var(--accent-subtle)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <FileText size={28} />
            </div>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>暂无资料</h3>
            <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>点击「上传资料」按钮添加课程资料</p>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>类型</th>
                  <th>版本</th>
                  <th>文件大小</th>
                  <th>解析状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {materials.map((m) => (
                  <tr key={m.id}>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <FileText size={16} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
                        <span style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '240px' }}>{m.logical_name}</span>
                      </div>
                    </td>
                    <td>
                      <Badge variant={MATERIAL_TYPE_VARIANTS[m.material_type] || 'default'}>
                        {MATERIAL_TYPE_LABELS[m.material_type] || m.material_type}
                      </Badge>
                    </td>
                    <td suppressHydrationWarning>{m.latest_version ? 'v' + m.latest_version.version_no : '-'}</td>
                    <td suppressHydrationWarning>{m.latest_version ? formatFileSize(m.latest_version.size_bytes) : '-'}</td>
                    <td>
                      <Badge variant={PARSE_STATUS_VARIANTS[m.parse_status.status] || 'default'}>
                        {PARSE_STATUS_LABELS[m.parse_status.status] || m.parse_status.status}
                      </Badge>
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: '8px' }}>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => handleParse(m)}
                          disabled={parsingIds.has(m.id)}
                          icon={parsingIds.has(m.id) ? <Loader2 size={14} /> : <RefreshCw size={14} />}
                        >
                          解析
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => setDeleteId(m.id)}
                          icon={<Trash2 size={14} />}
                        >
                          删除
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Upload Dialog */}
      <Modal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        title="上传资料"
        onConfirm={handleUpload}
        confirmLabel="确认上传"
        loading={uploading}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <Input
            label="文件名"
            placeholder="请输入文件名"
            value={uploadFilename}
            onChange={(e) => setUploadFilename(e.target.value)}
          />
          <Select
            label="资料类型"
            value={uploadType}
            onChange={(e) => setUploadType(e.target.value)}
            options={[
              { value: 'teaching_syllabus', label: '教学大纲' },
              { value: 'assessment_syllabus', label: '考核大纲' },
              { value: 'teaching_material', label: '教材' },
              { value: 'exercise', label: '习题' },
            ]}
          />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>选择文件</label>
            <input
              type="file"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) {
                  setUploadFile(f);
                  if (!uploadFilename) setUploadFilename(f.name);
                }
              }}
              className="input-field"
            />
            {uploadFile && (
              <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>已选择: {uploadFile.name}</p>
            )}
          </div>
        </div>
      </Modal>

      {/* Delete Confirm Dialog */}
      <Modal
        open={!!deleteId}
        onClose={() => setDeleteId(null)}
        title="确认删除"
        onConfirm={handleDelete}
        confirmLabel="删除"
        loading={deleting}
        danger
      >
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>确定要删除这份资料吗？此操作不可撤销。</p>
      </Modal>
    </div>
  );
}
