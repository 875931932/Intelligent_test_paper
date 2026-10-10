import { useCallback, useEffect, useState } from 'react';
import { Building2, GraduationCap, Plus, Pencil, Ban, CheckCircle2 } from 'lucide-react';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import type { AdminOrganization, OrgKind } from '@/api/domains/admin';
import { Button, Card, Input, Modal, Select, BadgeSuccess, BadgeWarning } from '@/components/ui';

/** 单位管理：学校→学院两级组织树。平台管理员可增改/停启用，单位管理员只读。 */
export default function AdminOrganizationsPage() {
  const role = useAuthStore((s) => s.user?.role);
  const isPlatform = role === 'admin';
  const addToast = useToastStore((s) => s.addToast);

  const [tree, setTree] = useState<AdminOrganization[]>([]);
  const [loading, setLoading] = useState(true);
  // 新建/编辑弹窗：target=null 为新建，否则编辑该组织
  const [editOpen, setEditOpen] = useState(false);
  const [target, setTarget] = useState<AdminOrganization | null>(null);
  const [form, setForm] = useState<{ name: string; kind: OrgKind; parent_id: string; code: string }>({
    name: '',
    kind: 'school',
    parent_id: '',
    code: '',
  });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.admin.orgTree();
      setTree(data);
    } catch {
      addToast('组织列表加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [addToast]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = (kind: OrgKind, parentId?: string) => {
    setTarget(null);
    setForm({ name: '', kind, parent_id: parentId ?? '', code: '' });
    setEditOpen(true);
  };

  const openEdit = (org: AdminOrganization) => {
    setTarget(org);
    setForm({
      name: org.name,
      kind: org.kind,
      parent_id: org.parent_id ?? '',
      code: org.code ?? '',
    });
    setEditOpen(true);
  };

  const save = async () => {
    if (form.name.trim().length < 2) {
      addToast('名称至少 2 个字符', 'error');
      return;
    }
    setSaving(true);
    try {
      if (target) {
        await api.admin.updateOrg(target.id, {
          name: form.name.trim(),
          code: form.code.trim() || undefined,
        });
        addToast('组织已更新', 'success');
      } else {
        await api.admin.createOrg({
          name: form.name.trim(),
          kind: form.kind,
          parent_id: form.kind === 'college' ? form.parent_id : undefined,
          code: form.code.trim() || undefined,
        });
        addToast('组织已创建', 'success');
      }
      setEditOpen(false);
      await load();
    } catch (e) {
      addToast((e as Error).message || '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const toggleStatus = async (org: AdminOrganization) => {
    try {
      await api.admin.updateOrg(org.id, { status: org.status === 'active' ? 'disabled' : 'active' });
      addToast(org.status === 'active' ? '已停用' : '已启用', 'success');
      await load();
    } catch (e) {
      addToast((e as Error).message || '操作失败', 'error');
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
        <div className="spinner spinner-lg" />
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600, color: 'var(--text)', margin: 0 }}>
          单位管理
        </h1>
        {isPlatform && (
          <Button icon={<Plus size={16} />} onClick={() => openCreate('school')}>
            新建学校
          </Button>
        )}
      </div>

      {tree.length === 0 ? (
        <Card title="暂无组织">
          <p style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
            {isPlatform ? '点击右上角"新建学校"创建第一所单位。' : '本单位尚未归属任何组织，请联系平台管理员。'}
          </p>
        </Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-md)' }}>
          {tree.map((school) => (
            <Card key={school.id}>
              <OrgRow
                org={school}
                depth={0}
                isPlatform={isPlatform}
                onEdit={openEdit}
                onToggle={toggleStatus}
                onCreateChild={(parentId) => openCreate('college', parentId)}
              />
              {(school.children ?? []).map((college) => (
                <OrgRow
                  key={college.id}
                  org={college}
                  depth={1}
                  isPlatform={isPlatform}
                  onEdit={openEdit}
                  onToggle={toggleStatus}
                />
              ))}
            </Card>
          ))}
        </div>
      )}

      <Modal
        open={editOpen}
        onClose={() => setEditOpen(false)}
        title={target ? '编辑组织' : form.kind === 'school' ? '新建学校' : '新建学院'}
        onConfirm={save}
        confirmLabel="保存"
        loading={saving}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Input
            label="名称"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder={form.kind === 'school' ? '如：示例大学' : '如：计算机学院'}
          />
          {!target && form.kind === 'college' && (
            <Select
              label="所属学校"
              value={form.parent_id}
              onChange={(e) => setForm({ ...form, parent_id: e.target.value })}
              options={[
                { value: '', label: '请选择学校' },
                ...tree.map((s) => ({ value: s.id, label: s.name })),
              ]}
            />
          )}
          <Input
            label="代码（可选，全局唯一）"
            value={form.code}
            onChange={(e) => setForm({ ...form, code: e.target.value })}
            placeholder="如：S-001"
          />
        </div>
      </Modal>
    </div>
  );
}

function OrgRow({
  org,
  depth,
  isPlatform,
  onEdit,
  onToggle,
  onCreateChild,
}: {
  org: AdminOrganization;
  depth: number;
  isPlatform: boolean;
  onEdit: (org: AdminOrganization) => void;
  onToggle: (org: AdminOrganization) => void;
  onCreateChild?: (parentId: string) => void;
}) {
  const isSchool = org.kind === 'school';
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: depth === 0 ? '4px 0 10px' : '8px 0 8px 28px',
        borderBottom: depth === 0 ? 'none' : '1px solid var(--line-soft)',
      }}
    >
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 30,
          height: 30,
          borderRadius: 8,
          background: isSchool ? 'var(--accent-subtle)' : 'var(--fill-strong)',
          color: isSchool ? 'var(--accent)' : 'var(--text-secondary)',
          flexShrink: 0,
        }}
      >
        {isSchool ? <Building2 size={16} /> : <GraduationCap size={16} />}
      </span>
      <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text)' }}>{org.name}</span>
      {org.code && (
        <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{org.code}</span>
      )}
      {org.status === 'active' ? (
        <BadgeSuccess>启用</BadgeSuccess>
      ) : (
        <BadgeWarning>停用</BadgeWarning>
      )}
      <div style={{ flex: 1 }} />
      {isPlatform && (
        <div style={{ display: 'flex', gap: 6 }}>
          {isSchool && onCreateChild && (
            <Button size="sm" variant="ghost" onClick={() => onCreateChild(org.id)}>
              加学院
            </Button>
          )}
          <Button size="sm" variant="ghost" icon={<Pencil size={14} />} onClick={() => onEdit(org)} />
          <Button
            size="sm"
            variant="ghost"
            icon={org.status === 'active' ? <Ban size={14} /> : <CheckCircle2 size={14} />}
            onClick={() => onToggle(org)}
          />
        </div>
      )}
    </div>
  );
}
