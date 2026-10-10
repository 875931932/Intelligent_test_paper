import { useCallback, useEffect, useState } from 'react';
import { Plus, Pencil, Trash2, Zap, RefreshCw, Link2 } from 'lucide-react';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import type {
  LlmBinding,
  LlmModel,
  LlmPurpose,
  LlmProvider,
} from '@/api/domains/admin';
import {
  Button,
  Card,
  Input,
  Modal,
  Select,
  BadgeSuccess,
  BadgeWarning,
  BadgeInfo,
} from '@/components/ui';

const PURPOSE_LABEL: Record<LlmPurpose, string> = {
  default: '默认兜底（助手/改题/评审）',
  extract: '资料抽取',
  classify: '分类与推荐',
  consolidate: '知识归并',
  generation: '命题生成',
};

const EMPTY_PROVIDER = { name: '', base_url: '', api_key: '', api_key_env: '', remark: '' };

const EMPTY_MODEL = {
  provider_id: '',
  model_id: '',
  display_name: '',
  profile_text: '',
  price_input: '',
  price_output: '',
};

/** 模型管理：用途一键切换 + 型号/供应商维护 + 连通性测试（仅平台管理员）。 */
export default function AdminModelsPage() {
  const addToast = useToastStore((s) => s.addToast);

  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [models, setModels] = useState<LlmModel[]>([]);
  const [bindings, setBindings] = useState<LlmBinding[]>([]);
  const [loading, setLoading] = useState(true);

  // 切换待选项（每行独立 select）
  const [pending, setPending] = useState<Record<string, string>>({});

  // 供应商弹窗
  const [provOpen, setProvOpen] = useState(false);
  const [provTarget, setProvTarget] = useState<LlmProvider | null>(null);
  const [provForm, setProvForm] = useState(EMPTY_PROVIDER);
  const [savingProv, setSavingProv] = useState(false);

  // 型号弹窗
  const [modelOpen, setModelOpen] = useState(false);
  const [modelTarget, setModelTarget] = useState<LlmModel | null>(null);
  const [modelForm, setModelForm] = useState(EMPTY_MODEL);
  const [savingModel, setSavingModel] = useState(false);

  // 删除确认
  const [deleteTarget, setDeleteTarget] = useState<LlmModel | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, m, b] = await Promise.all([
        api.admin.llmProviders(),
        api.admin.llmModels(),
        api.admin.llmBindings(),
      ]);
      setProviders(p);
      setModels(m);
      setBindings(b);
    } catch {
      addToast('模型配置加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [addToast]);

  useEffect(() => {
    void load();
  }, [load]);

  // ── 供应商 ──────────────────────────────────────────────────────────────
  const openProvCreate = () => {
    setProvTarget(null);
    setProvForm(EMPTY_PROVIDER);
    setProvOpen(true);
  };

  const openProvEdit = (p: LlmProvider) => {
    setProvTarget(p);
    setProvForm({
      name: p.name,
      base_url: p.base_url,
      api_key: '', // 明文不回传：留空 = 不改密钥
      api_key_env: p.api_key_env ?? '',
      remark: p.remark ?? '',
    });
    setProvOpen(true);
  };

  const saveProv = async () => {
    if (!provForm.name.trim() || !provForm.base_url.trim()) {
      addToast('名称与 Base URL 必填', 'error');
      return;
    }
    setSavingProv(true);
    try {
      if (provTarget) {
        const payload: Record<string, string> = {
          name: provForm.name.trim(),
          base_url: provForm.base_url.trim(),
          api_key_env: provForm.api_key_env.trim(),
          remark: provForm.remark.trim(),
        };
        if (provForm.api_key.trim()) payload.api_key = provForm.api_key.trim();
        await api.admin.updateLlmProvider(provTarget.id, payload);
        addToast('供应商已更新', 'success');
      } else {
        await api.admin.createLlmProvider({
          name: provForm.name.trim(),
          base_url: provForm.base_url.trim(),
          ...(provForm.api_key.trim() ? { api_key: provForm.api_key.trim() } : {}),
          ...(provForm.api_key_env.trim() ? { api_key_env: provForm.api_key_env.trim() } : {}),
          ...(provForm.remark.trim() ? { remark: provForm.remark.trim() } : {}),
        });
        addToast('供应商已创建', 'success');
      }
      setProvOpen(false);
      await load();
    } catch (e) {
      addToast((e as Error).message || '保存失败', 'error');
    } finally {
      setSavingProv(false);
    }
  };

  const toggleProv = async (p: LlmProvider) => {
    try {
      await api.admin.updateLlmProvider(p.id, { status: p.status === 'active' ? 'disabled' : 'active' });
      addToast(p.status === 'active' ? '供应商已停用（其型号一并失效）' : '供应商已启用', 'success');
      await load();
    } catch (e) {
      addToast((e as Error).message || '操作失败', 'error');
    }
  };

  const probeProvider = async (p: LlmProvider) => {
    addToast(`正在测试 ${p.name} 连通性…`, 'info');
    try {
      const r = await api.admin.testLlm({ provider_id: p.id });
      if (r.ok) addToast(`${p.name} 连通正常（${r.latency_ms}ms）`, 'success');
      else addToast(`${p.name} 测试失败：${r.error ?? '未知错误'}`, 'error');
    } catch (e) {
      addToast((e as Error).message || '测试失败', 'error');
    }
  };

  // ── 型号 ────────────────────────────────────────────────────────────────
  const openModelCreate = () => {
    setModelTarget(null);
    setModelForm({ ...EMPTY_MODEL, provider_id: providers[0]?.id ?? '' });
    setModelOpen(true);
  };

  const openModelEdit = (m: LlmModel) => {
    setModelTarget(m);
    setModelForm({
      provider_id: m.provider_id,
      model_id: m.model_id,
      display_name: m.display_name,
      profile_text: Object.keys(m.profile ?? {}).length ? JSON.stringify(m.profile, null, 2) : '',
      price_input: m.price_input_per_mtk != null ? String(m.price_input_per_mtk) : '',
      price_output: m.price_output_per_mtk != null ? String(m.price_output_per_mtk) : '',
    });
    setModelOpen(true);
  };

  const saveModel = async () => {
    if (!modelTarget && (!modelForm.model_id.trim() || !modelForm.provider_id)) {
      addToast('型号标识与供应商必填', 'error');
      return;
    }
    if (!modelForm.display_name.trim()) {
      addToast('显示名称必填', 'error');
      return;
    }
    let profile: Record<string, unknown> = {};
    if (modelForm.profile_text.trim()) {
      try {
        profile = JSON.parse(modelForm.profile_text) as Record<string, unknown>;
      } catch {
        addToast('档案 JSON 解析失败，请检查格式', 'error');
        return;
      }
    }
    setSavingModel(true);
    try {
      const payload = {
        display_name: modelForm.display_name.trim(),
        profile,
        ...(modelForm.price_input.trim()
          ? { price_input_per_mtk: Number(modelForm.price_input) }
          : {}),
        ...(modelForm.price_output.trim()
          ? { price_output_per_mtk: Number(modelForm.price_output) }
          : {}),
      };
      if (modelTarget) {
        await api.admin.updateLlmModel(modelTarget.id, payload);
        addToast('型号已更新', 'success');
      } else {
        await api.admin.createLlmModel({
          ...payload,
          provider_id: modelForm.provider_id,
          model_id: modelForm.model_id.trim(),
        });
        addToast('型号已创建', 'success');
      }
      setModelOpen(false);
      await load();
    } catch (e) {
      addToast((e as Error).message || '保存失败', 'error');
    } finally {
      setSavingModel(false);
    }
  };

  const toggleModel = async (m: LlmModel) => {
    try {
      await api.admin.updateLlmModel(m.id, { status: m.status === 'active' ? 'disabled' : 'active' });
      addToast(m.status === 'active' ? '型号已停用' : '型号已启用', 'success');
      await load();
    } catch (e) {
      addToast((e as Error).message || '操作失败', 'error');
    }
  };

  const deleteModel = async () => {
    if (!deleteTarget) return;
    try {
      await api.admin.deleteLlmModel(deleteTarget.id);
      addToast('型号已删除', 'success');
      setDeleteTarget(null);
      await load();
    } catch (e) {
      addToast((e as Error).message || '删除失败', 'error');
      setDeleteTarget(null);
    }
  };

  // ── 用途切换 ────────────────────────────────────────────────────────────
  const switchBinding = async (purpose: LlmPurpose) => {
    const modelId = pending[purpose];
    if (!modelId) {
      addToast('请先选择目标型号', 'info');
      return;
    }
    try {
      await api.admin.setLlmBinding(purpose, modelId);
      addToast(`「${PURPOSE_LABEL[purpose]}」切换成功，最长 30 秒全进程生效`, 'success');
      await load();
    } catch (e) {
      addToast((e as Error).message || '切换失败', 'error');
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
        <div className="spinner spinner-lg" />
      </div>
    );
  }

  const activeModels = models.filter((m) => m.status === 'active');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, color: 'var(--text)', margin: 0 }}>
            模型管理
          </h1>
          <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', margin: '4px 0 0' }}>
            DB 配置优先，.env 仅作兜底；切换后最长 30 秒在 API 与 Worker 进程生效。
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" icon={<RefreshCw size={16} />} onClick={() => void load()}>
            刷新
          </Button>
          <Button variant="secondary" icon={<Plus size={16} />} onClick={openProvCreate}>
            新建供应商
          </Button>
          <Button icon={<Plus size={16} />} onClick={openModelCreate}>
            新建型号
          </Button>
        </div>
      </div>

      {/* 用途绑定 */}
      <Card title="用途绑定" subtitle="每个用途实际调用的型号；未细分的用途回落 default">
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--line)' }}>
                {['用途', '生效型号', '来源', '供应商', '切换', ''].map((h) => (
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
              {bindings.map((b) => (
                <tr key={b.purpose} style={{ borderBottom: '1px solid var(--line-soft)' }}>
                  <td style={{ padding: '10px', fontWeight: 500 }}>{PURPOSE_LABEL[b.purpose]}</td>
                  <td style={{ padding: '10px' }}>
                    {b.source === 'none' || !b.model ? (
                      <span style={{ color: 'var(--text-tertiary)' }}>未配置</span>
                    ) : (
                      <>
                        <span style={{ fontFamily: 'monospace' }}>{b.model.model_id}</span>
                        {b.via === 'default' && (
                          <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>（回落 default）</span>
                        )}
                      </>
                    )}
                  </td>
                  <td style={{ padding: '10px' }}>
                    {b.source === 'db' ? (
                      <BadgeSuccess>DB</BadgeSuccess>
                    ) : b.source === 'env' ? (
                      <BadgeInfo>.env</BadgeInfo>
                    ) : (
                      <BadgeWarning>无</BadgeWarning>
                    )}
                  </td>
                  <td style={{ padding: '10px', color: 'var(--text-secondary)' }}>
                    {b.provider?.name ?? '—'}
                  </td>
                  <td style={{ padding: '10px' }}>
                    <div style={{ display: 'flex', gap: 6, minWidth: 220 }}>
                      <Select
                        value={pending[b.purpose] ?? ''}
                        onChange={(e) => setPending({ ...pending, [b.purpose]: e.target.value })}
                        options={[
                          { value: '', label: '选择型号…' },
                          ...activeModels.map((m) => ({
                            value: m.id,
                            label: `${m.display_name}（${m.model_id}）`,
                          })),
                        ]}
                      />
                    </div>
                  </td>
                  <td style={{ padding: '10px' }}>
                    <Button
                      size="sm"
                      variant="secondary"
                      icon={<Link2 size={14} />}
                      disabled={!pending[b.purpose]}
                      onClick={() => void switchBinding(b.purpose)}
                    >
                      切换
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 型号 */}
      <Card title="型号" subtitle="调优档案（profile）决定思考档/结构化约束等供应商参数">
        {models.length === 0 ? (
          <p style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
            暂无型号，点击右上角「新建型号」，或先运行 <code>uv run python -m app.db.init_db</code> 从 .env 种入默认配置。
          </p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--line)' }}>
                  {['显示名', '型号标识', '供应商', '档案', '状态', '操作'].map((h) => (
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
                {models.map((m) => (
                  <tr key={m.id} style={{ borderBottom: '1px solid var(--line-soft)' }}>
                    <td style={{ padding: '10px', fontWeight: 500 }}>{m.display_name}</td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }}>{m.model_id}</td>
                    <td style={{ padding: '10px', color: 'var(--text-secondary)' }}>
                      {m.provider_name ?? '—'}
                    </td>
                    <td style={{ padding: '10px', fontSize: 12, color: 'var(--text-tertiary)' }}>
                      {Object.keys(m.profile ?? {}).length
                        ? `${Object.keys(m.profile).length} 项参数`
                        : '代码/端点回退'}
                    </td>
                    <td style={{ padding: '10px' }}>
                      {m.status === 'active' ? <BadgeSuccess>启用</BadgeSuccess> : <BadgeWarning>停用</BadgeWarning>}
                    </td>
                    <td style={{ padding: '10px' }}>
                      <div style={{ display: 'flex', gap: 4 }}>
                        <Button size="sm" variant="ghost" icon={<Pencil size={14} />} onClick={() => openModelEdit(m)} />
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => toggleModel(m)}
                        >
                          {m.status === 'active' ? '停用' : '启用'}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={<Trash2 size={14} />}
                          onClick={() => setDeleteTarget(m)}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 供应商 */}
      <Card title="供应商" subtitle="api_key 明文不出后端，此处只显示掩码">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {providers.map((p) => (
            <div
              key={p.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: '10px 12px',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--line-soft)',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>{p.name}</span>
                  {p.status === 'active' ? <BadgeSuccess>启用</BadgeSuccess> : <BadgeWarning>停用</BadgeWarning>}
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', wordBreak: 'break-all' }}>
                  {p.base_url}
                  {p.api_key_masked && (
                    <span style={{ fontFamily: 'monospace' }}> · {p.api_key_masked}</span>
                  )}
                  {p.api_key_env && <span> · env:{p.api_key_env}</span>}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <Button size="sm" variant="secondary" icon={<Zap size={14} />} onClick={() => void probeProvider(p)}>
                  测试
                </Button>
                <Button size="sm" variant="ghost" icon={<Pencil size={14} />} onClick={() => openProvEdit(p)} />
                <Button size="sm" variant="ghost" onClick={() => void toggleProv(p)}>
                  {p.status === 'active' ? '停用' : '启用'}
                </Button>
              </div>
            </div>
          ))}
          {providers.length === 0 && (
            <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>
              暂无供应商。
            </p>
          )}
        </div>
      </Card>

      {/* 供应商弹窗 */}
      <Modal
        open={provOpen}
        onClose={() => setProvOpen(false)}
        title={provTarget ? '编辑供应商' : '新建供应商'}
        onConfirm={saveProv}
        confirmLabel="保存"
        loading={savingProv}
        maxWidth="520px"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Input label="名称" value={provForm.name} onChange={(e) => setProvForm({ ...provForm, name: e.target.value })} />
          <Input
            label="Base URL（不含 /chat/completions）"
            value={provForm.base_url}
            onChange={(e) => setProvForm({ ...provForm, base_url: e.target.value })}
            placeholder="https://api.stepfun.com/v1"
          />
          <Input
            label={provTarget ? '新 api_key（留空 = 不修改）' : 'api_key'}
            type="password"
            value={provForm.api_key}
            onChange={(e) => setProvForm({ ...provForm, api_key: e.target.value })}
            autoComplete="new-password"
          />
          <Input
            label="api_key_env（可选，环境变量名，优先于明文）"
            value={provForm.api_key_env}
            onChange={(e) => setProvForm({ ...provForm, api_key_env: e.target.value })}
            placeholder="如 LLM_API_KEY"
          />
          <Input
            label="备注（可选）"
            value={provForm.remark}
            onChange={(e) => setProvForm({ ...provForm, remark: e.target.value })}
          />
        </div>
      </Modal>

      {/* 型号弹窗 */}
      <Modal
        open={modelOpen}
        onClose={() => setModelOpen(false)}
        title={modelTarget ? '编辑型号' : '新建型号'}
        onConfirm={saveModel}
        confirmLabel="保存"
        loading={savingModel}
        maxWidth="560px"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {!modelTarget && (
            <>
              <Select
                label="供应商"
                value={modelForm.provider_id}
                onChange={(e) => setModelForm({ ...modelForm, provider_id: e.target.value })}
                options={[
                  { value: '', label: '请选择供应商' },
                  ...providers.map((p) => ({ value: p.id, label: p.name })),
                ]}
              />
              <Input
                label="型号标识（上报供应商的名字）"
                value={modelForm.model_id}
                onChange={(e) => setModelForm({ ...modelForm, model_id: e.target.value })}
                placeholder="如 step-5-preview"
              />
            </>
          )}
          <Input
            label="显示名称"
            value={modelForm.display_name}
            onChange={(e) => setModelForm({ ...modelForm, display_name: e.target.value })}
          />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <Input
              label="输入价（元/百万 tokens，可选）"
              value={modelForm.price_input}
              onChange={(e) => setModelForm({ ...modelForm, price_input: e.target.value })}
              placeholder="如 2"
            />
            <Input
              label="输出价（元/百万 tokens，可选）"
              value={modelForm.price_output}
              onChange={(e) => setModelForm({ ...modelForm, price_output: e.target.value })}
              placeholder="如 8"
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>
              调优档案 profile（JSON，可选；留空 = 代码档案/端点回退）
            </label>
            <textarea
              className="input-field"
              rows={5}
              style={{ fontFamily: 'monospace', fontSize: 12, resize: 'vertical' }}
              value={modelForm.profile_text}
              onChange={(e) => setModelForm({ ...modelForm, profile_text: e.target.value })}
              placeholder={'{\n  "thinking_style": "reasoning_effort",\n  "supported_efforts": ["low", "high"],\n  "default_effort": "low",\n  "supports_tool_choice": false,\n  "supports_json_schema": true\n}'}
            />
          </div>
        </div>
      </Modal>

      {/* 删除型号确认 */}
      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="删除型号"
        confirmLabel="确认删除"
        danger
        onConfirm={deleteModel}
        maxWidth="420px"
      >
        <p style={{ margin: 0, fontSize: 14 }}>
          确定删除型号「<strong>{deleteTarget?.display_name}</strong>」吗？被用途绑定引用的型号无法删除（只能改绑或停用）。
        </p>
      </Modal>
    </div>
  );
}
