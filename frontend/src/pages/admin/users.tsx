import { useCallback, useEffect, useState } from 'react';
import { Plus, Pencil, KeyRound, Ban, CheckCircle2, Search } from 'lucide-react';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import type { AdminOrganization, AdminRole, AdminUser } from '@/api/domains/admin';
import { Button, Card, Input, Modal, Select, BadgeSuccess, BadgeWarning, BadgeInfo, BadgePurple } from '@/components/ui';

const ROLE_LABEL: Record<AdminRole, string> = {
  admin: '平台管理员',
  org_admin: '单位管理员',
  teacher: '教师',
};

interface FormState {
  username: string;
  password: string;
  name: string;
  role: AdminRole;
  organization_id: string;
  title: string;
  employee_no: string;
}

const EMPTY_FORM: FormState = {
  username: '',
  password: '',
  name: '',
  role: 'teacher',
  organization_id: '',
  title: '',
  employee_no: '',
};

/** 账号管理：列表筛选 + 建号/编辑/停启用/重置密码（停用取代删除）。 */
export default function AdminUsersPage() {
  const role = useAuthStore((s) => s.user?.role);
  const isPlatform = role === 'admin';
  const addToast = useToastStore((s) => s.addToast);

  const [orgs, setOrgs] = useState<AdminOrganization[]>([]);
  const [items, setItems] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  // 筛选条件
  const [q, setQ] = useState('');
  const [orgFilter, setOrgFilter] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const pageSize = 20;

  // 建号/编辑弹窗
  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  // 重置密码弹窗
  const [pwdTarget, setPwdTarget] = useState<AdminUser | null>(null);
  const [newPwd, setNewPwd] = useState('');
  const [pwdSaving, setPwdSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.admin.userList({
        q: q || undefined,
        organization_id: orgFilter || undefined,
        role: roleFilter || undefined,
        status: statusFilter || undefined,
        page,
        page_size: pageSize,
      });
      setItems(data.items);
      setTotal(data.total);
    } catch {
      addToast('账号列表加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [q, orgFilter, roleFilter, statusFilter, page, addToast]);

  useEffect(() => {
    void load();
  }, [load]);

  // 组织下拉（筛选 + 归属选择共用）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const tree = await api.admin.orgTree();
        if (!cancelled) setOrgs(flatten(tree));
      } catch {
        // 组织清单不可用不阻塞账号列表
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const openCreate = () => {
    setEditing(null);
    setForm({ ...EMPTY_FORM, organization_id: orgFilter });
    setEditOpen(true);
  };

  const openEdit = (user: AdminUser) => {
    setEditing(user);
    setForm({
      username: user.username,
      password: '',
      name: user.name,
      role: user.role,
      organization_id: user.organization_id ?? '',
      title: user.title ?? '',
      employee_no: user.employee_no ?? '',
    });
    setEditOpen(true);
  };

  const save = async () => {
    if (!editing) {
      // 建号校验
      if (!/^[A-Za-z0-9_.@-]{3,120}$/.test(form.username)) {
        addToast('用户名需 3-120 位字母/数字/_.@-', 'error');
        return;
      }
      if (form.password.length < 6) {
        addToast('密码至少 6 位', 'error');
        return;
      }
    }
    if (!form.name.trim()) {
      addToast('姓名不能为空', 'error');
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        await api.admin.updateUser(editing.id, {
          name: form.name.trim(),
          role: form.role,
          organization_id: form.organization_id || undefined,
          title: form.title.trim() || undefined,
          employee_no: form.employee_no.trim() || undefined,
        });
        addToast('账号已更新', 'success');
      } else {
        await api.admin.createUser({
          username: form.username.trim(),
          password: form.password,
          name: form.name.trim(),
          role: form.role,
          organization_id: form.organization_id || undefined,
          title: form.title.trim() || undefined,
          employee_no: form.employee_no.trim() || undefined,
        });
        addToast('账号已创建', 'success');
      }
      setEditOpen(false);
      await load();
    } catch (e) {
      addToast((e as Error).message || '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const toggleStatus = async (user: AdminUser) => {
    try {
      await api.admin.setUserStatus(user.id, user.status === 'active' ? 'disabled' : 'active');
      addToast(user.status === 'active' ? '账号已停用' : '账号已启用', 'success');
      await load();
    } catch (e) {
      addToast((e as Error).message || '操作失败', 'error');
    }
  };

  const savePassword = async () => {
    if (newPwd.length < 6) {
      addToast('密码至少 6 位', 'error');
      return;
    }
    if (!pwdTarget) return;
    setPwdSaving(true);
    try {
      await api.admin.resetPassword(pwdTarget.id, newPwd);
      addToast('密码已重置', 'success');
      setPwdTarget(null);
      setNewPwd('');
    } catch (e) {
      addToast((e as Error).message || '重置失败', 'error');
    } finally {
      setPwdSaving(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600, color: 'var(--text)', margin: 0 }}>
          账号管理
        </h1>
        <Button icon={<Plus size={16} />} onClick={openCreate}>
          新建账号
        </Button>
      </div>

      <Card>
        {/* 筛选栏 */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'minmax(200px, 1.5fr) repeat(3, minmax(140px, 1fr))',
            gap: 10,
            marginBottom: 16,
          }}
        >
          <Input
            placeholder="搜索用户名 / 姓名 / 工号"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(1);
            }}
          />
          <Select
            value={orgFilter}
            onChange={(e) => {
              setOrgFilter(e.target.value);
              setPage(1);
            }}
            options={[
              { value: '', label: '全部单位' },
              ...orgs.map((o) => ({ value: o.id, label: o.name })),
            ]}
          />
          <Select
            value={roleFilter}
            onChange={(e) => {
              setRoleFilter(e.target.value);
              setPage(1);
            }}
            options={[
              { value: '', label: '全部角色' },
              ...(isPlatform ? [{ value: 'admin', label: ROLE_LABEL.admin }] : []),
              { value: 'org_admin', label: ROLE_LABEL.org_admin },
              { value: 'teacher', label: ROLE_LABEL.teacher },
            ]}
          />
          <Select
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
            options={[
              { value: '', label: '全部状态' },
              { value: 'active', label: '启用' },
              { value: 'disabled', label: '停用' },
            ]}
          />
        </div>

        {/* 表格 */}
        {loading ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}>
            <div className="spinner spinner-lg" />
          </div>
        ) : items.length === 0 ? (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 8,
              padding: '40px 0',
              color: 'var(--text-secondary)',
            }}
          >
            <Search size={28} />
            <span style={{ fontSize: 14 }}>没有匹配的账号</span>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--line)' }}>
                  {['用户名', '姓名', '角色', '单位', '状态', '最近登录', '操作'].map((h) => (
                    <th
                      key={h}
                      style={{
                        textAlign: 'left',
                        padding: '8px 10px',
                        fontSize: 12,
                        fontWeight: 600,
                        color: 'var(--text-secondary)',
                        background: 'var(--surface-sunken)',
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((u) => (
                  <tr key={u.id} style={{ borderBottom: '1px solid var(--line-soft)' }}>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }}>{u.username}</td>
                    <td style={{ padding: '10px' }}>
                      {u.name}
                      {u.title && (
                        <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}> · {u.title}</span>
                      )}
                    </td>
                    <td style={{ padding: '10px' }}>
                      {u.role === 'admin' ? (
                        <BadgePurple>{ROLE_LABEL.admin}</BadgePurple>
                      ) : u.role === 'org_admin' ? (
                        <BadgeInfo>{ROLE_LABEL.org_admin}</BadgeInfo>
                      ) : (
                        <span style={{ color: 'var(--text-secondary)' }}>{ROLE_LABEL.teacher}</span>
                      )}
                    </td>
                    <td style={{ padding: '10px', color: 'var(--text-secondary)' }}>
                      {u.organization_name ?? '—'}
                    </td>
                    <td style={{ padding: '10px' }}>
                      {u.status === 'active' ? <BadgeSuccess>启用</BadgeSuccess> : <BadgeWarning>停用</BadgeWarning>}
                    </td>
                    <td style={{ padding: '10px', color: 'var(--text-tertiary)', fontSize: 13 }}>
                      {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : '从未登录'}
                    </td>
                    <td style={{ padding: '10px' }}>
                      <div style={{ display: 'flex', gap: 4 }}>
                        <Button size="sm" variant="ghost" icon={<Pencil size={14} />} onClick={() => openEdit(u)} />
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={<KeyRound size={14} />}
                          onClick={() => {
                            setPwdTarget(u);
                            setNewPwd('');
                          }}
                        />
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={u.status === 'active' ? <Ban size={14} /> : <CheckCircle2 size={14} />}
                          onClick={() => toggleStatus(u)}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 分页 */}
        {totalPages > 1 && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'flex-end',
              gap: 8,
              marginTop: 14,
              fontSize: 13,
              color: 'var(--text-secondary)',
            }}
          >
            <span>
              第 {page} / {totalPages} 页（共 {total} 条）
            </span>
            <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              上一页
            </Button>
            <Button size="sm" variant="secondary" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
              下一页
            </Button>
          </div>
        )}
      </Card>

      {/* 建号 / 编辑 */}
      <Modal
        open={editOpen}
        onClose={() => setEditOpen(false)}
        title={editing ? '编辑账号' : '新建账号'}
        onConfirm={save}
        confirmLabel="保存"
        loading={saving}
        maxWidth="520px"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {!editing && (
            <Input
              label="用户名"
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              placeholder="3-120 位字母/数字/_.@-"
            />
          )}
          <Input
            label="姓名"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          {!editing && (
            <Input
              label="初始密码"
              type="password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              placeholder="至少 6 位"
            />
          )}
          <Select
            label="角色"
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value as AdminRole })}
            options={[
              { value: 'teacher', label: ROLE_LABEL.teacher },
              { value: 'org_admin', label: ROLE_LABEL.org_admin },
              ...(isPlatform ? [{ value: 'admin', label: ROLE_LABEL.admin }] : []),
            ]}
          />
          <Select
            label="单位"
            value={form.organization_id}
            onChange={(e) => setForm({ ...form, organization_id: e.target.value })}
            options={[
              { value: '', label: '不归属单位（仅平台管理员可设）' },
              ...orgs.map((o) => ({ value: o.id, label: o.name })),
            ]}
          />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <Input
              label="职称（可选）"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="如：副教授"
            />
            <Input
              label="工号（可选）"
              value={form.employee_no}
              onChange={(e) => setForm({ ...form, employee_no: e.target.value })}
              placeholder="如：T-042"
            />
          </div>
        </div>
      </Modal>

      {/* 重置密码 */}
      <Modal
        open={pwdTarget !== null}
        onClose={() => setPwdTarget(null)}
        title={`重置密码：${pwdTarget?.name ?? ''}`}
        onConfirm={savePassword}
        confirmLabel="重置"
        loading={pwdSaving}
        maxWidth="420px"
      >
        <Input
          label="新密码"
          type="password"
          value={newPwd}
          onChange={(e) => setNewPwd(e.target.value)}
          placeholder="至少 6 位"
        />
      </Modal>
    </div>
  );
}

/** 组织树 → 平铺列表（保留学校/学院顺序，树本身已按类型排序） */
function flatten(tree: AdminOrganization[]): AdminOrganization[] {
  return tree.flatMap((school) => [school, ...(school.children ?? [])]);
}
