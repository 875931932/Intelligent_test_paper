import { useState, useEffect, useRef, Fragment, type ReactNode } from 'react';
import { Check, ClipboardList, FileText, PlayCircle } from 'lucide-react';
import { api } from '@/api/client';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { useNameMaps } from '@/hooks/useNameMaps';
import type { ContractSnapshot } from '@/api/domains/examProjects';
import type { ExamProject, ExamRules, PlanItem, TaskRun } from '@/types/api';
import { type StageKey, isInFlight, isTerminal } from './stage/stageShared';
import { renderBlueprint } from './stage/StageBlueprint';
import { renderContract } from './stage/StageContract';
import { renderGenerate } from './stage/StageGeneration';

// ─── Stage pipeline ───
// 流水线只负责「出题」：蓝图 → 合同 → 生成。审核/编辑/定稿/导出属于同一页面
// 的「试卷」页签（PaperPanel），不占流水线阶段。

const STAGE_ORDER: StageKey[] = ['blueprint', 'contract', 'generate'];

const STAGE_META: Record<StageKey, { label: string; icon: ReactNode; color: string }> = {
  blueprint: { label: '蓝图', icon: <ClipboardList size={16} />, color: '#0071e3' },
  contract:  { label: '合同', icon: <FileText size={16} />, color: '#5856d6' },
  generate:  { label: '生成', icon: <PlayCircle size={16} />, color: '#34c759' },
};

const STATUS_TO_STAGE: Record<string, StageKey> = {
  draft: 'blueprint',
  blueprint: 'blueprint',
  contract: 'contract',
  generating: 'generate',
  // 后端项目状态仍保留 review/exported（试卷已生成）；归一到生成阶段，
  // 后续查看/审核在「试卷」页签进行，流水线到此为止。
  review: 'generate',
  exported: 'generate',
};

function stageFromStatus(status: string): StageKey {
  return STATUS_TO_STAGE[status] ?? 'blueprint';
}

// ═══════════════════════════════════════════════
//  玻璃步骤条
// ═══════════════════════════════════════════════
function StageStepper({ current, onSelect }: { current: StageKey; onSelect: (s: StageKey) => void }) {
  const currentIdx = STAGE_ORDER.indexOf(current);
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', marginTop: '18px' }}>
      {STAGE_ORDER.map((key, i) => {
        const done = i < currentIdx;
        const active = i === currentIdx;
        const meta = STAGE_META[key];
        const reachable = i <= currentIdx;
        return (
          <Fragment key={key}>
            {i > 0 && (
              <div style={{
                flex: 1,
                height: 2,
                alignSelf: 'center',
                marginTop: '-20px',
                background: reachable ? meta.color : 'rgba(0,0,0,0.08)',
                borderRadius: 2,
                transition: 'background 0.3s',
                opacity: reachable ? 0.6 : 1,
              }} />
            )}
            <button
              onClick={() => onSelect(key)}
              disabled={!reachable}
              style={{
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 7,
                background: 'none', border: 'none', padding: 0,
                minWidth: 58, cursor: reachable ? 'pointer' : 'not-allowed',
              }}
            >
              <span style={{
                width: 32, height: 32, borderRadius: '50%',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: active || done ? '#fff' : 'var(--text-tertiary)',
                background: active || done ? meta.color : 'rgba(0,0,0,0.05)',
                boxShadow: active ? '0 0 0 4px ' + meta.color + '30' : 'none',
                transition: 'all 0.25s var(--ease-out-expo)',
              }}>
                {done ? <Check size={16} /> : meta.icon}
              </span>
              <span style={{
                fontSize: '0.7rem', fontWeight: active ? 600 : 400,
                color: active ? meta.color : done ? 'var(--text-secondary)' : 'var(--text-tertiary)',
                whiteSpace: 'nowrap',
              }}>
                {meta.label}
              </span>
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}

// ═══════════════════════════════════════════════
//  出卷流水线面板
// ═══════════════════════════════════════════════
export default function PipelinePanel({
  sp, courseId, onOpenPaper, onBlueprintCreated, onProjectChanged, stageRequest,
}: {
  sp: ExamProject;
  courseId: string;
  /** 生成完成：父级切到「试卷」页签继续查看/审核 */
  onOpenPaper: () => void;
  /** 蓝图创建成功：父级用返回的版本号刷新项目，保证后续阶段立即可用 */
  onBlueprintCreated: (blueprintVersionId: string) => void;
  /** 阶段推进改变了项目状态（如确认合同→generating），父级刷新页头徽章 */
  onProjectChanged: () => void;
  /** 外部请求切换到某个阶段（如试卷页签点「重新生成」切到生成阶段） */
  stageRequest?: { stage: StageKey; nonce: number } | null;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const { maps, reload: reloadNameMaps } = useNameMaps(courseId);

  const [currentStage, setCurrentStage] = useState<StageKey>(() => stageFromStatus(sp.status));
  const [planItems, setPlanItems] = useState<PlanItem[]>([]);
  const [contractSnapshot, setContractSnapshot] = useState<ContractSnapshot | null>(null);
  const [taskRun, setTaskRun] = useState<TaskRun | null>(null);
  const [bpCreating, setBpCreating] = useState(false);
  const [contractConfirming, setContractConfirming] = useState(false);
  const [contractAllocating, setContractAllocating] = useState(false);
  // 正在看 AI 解释的槽位题位号（null = 关闭）。renderContract 是普通函数调用
  // 不能自带 hook，状态统一放在面板组件这一层。
  const [explainItem, setExplainItem] = useState<number | null>(null);
  // 「分配方案」默认随机一版：每次新建/刷新项目时不再固定回到第 1 版，
  // 否则每套卷子都从同一套搭配起步。历史种子由 hydrate 覆盖回填。
  const [contractVariant, setContractVariant] = useState(
    () => 1 + Math.floor(Math.random() * 6),
  );
  const [generating, setGenerating] = useState(false);
  // 考核大纲的题型比例：用来核对已有蓝图是不是按考纲比例生成的
  const [examRules, setExamRules] = useState<ExamRules | null>(null);

  useEffect(() => {
    if (!courseId) return;
    api.framework.getCurrent(courseId)
      .then((fw) => setExamRules(fw?.exam_rules ?? null))
      .catch(() => setExamRules(null));
  }, [courseId]);

  const loadPlanItems = async (proj: ExamProject) => {
    try {
      const items = await api.examProjects.getPlanItems(courseId, proj.id);
      setPlanItems(items);
    } catch {
      addToast('加载计划项失败', 'error');
    }
  };

  // 以服务端为权威数据源恢复流水线状态。
  // 合同快照持久化在 generation_runs.contract_snapshot，任务进度持久化在
  // task_runs 并由项目摘要归并出 active_task_run_id。二者都与"任务是否仍在
  // 进行中"无关，因此退出项目再进入（或刷新页面）后都不会消失 —— 前端不再
  // 把组件内存态当作事实源。
  //
  // 注意：create_blueprint 只写 status 与 active_blueprint_version_id，
  // **不会**清空 active_generation_run_id。所以重建蓝图后这个指针仍指向旧蓝图
  // 生成的 run，hydrate 会把旧蓝图的合同快照当成现状展示。因此
  // handleCreateBlueprint 成功后主动清掉 contractSnapshot/taskRun，让合同阶段
  // 回到「待分配」而不是展示过期快照（刷新页面则仍会看到旧快照，属后端缺口）。
  const hydrateProjectState = async (proj: ExamProject) => {
    // 1) 合同快照：只有确认过合同（active_generation_run_id 有值）才可能读得到。
    // 没确认过去探 contracts/current 只会拿到 404——既刷一屏控制台错误，又多一次往返。
    if (!proj.active_generation_run_id) {
      setContractSnapshot(null);
    } else {
      try {
        const cur = await api.examProjects.getCurrentContract(courseId, proj.id, token ?? undefined);
        const snap = cur?.contract_snapshot;
        setContractSnapshot(snap && Array.isArray(snap.slots) && snap.slots.length > 0 ? snap : null);
        // 分配方案回填：快照里记下了当初用的种子（前端第 N 版 = 种子 N-1），
        // 退出再进入后下拉必须跟着回来，否则再次确认会静默换成另一套方案。
        // 越界值（旧数据/手工改库）不采用，保留随机默认。
        const seed = snap?.allocation_seed;
        if (typeof seed === 'number' && seed >= 0 && seed <= 5) {
          setContractVariant(seed + 1);
        }
      } catch {
        setContractSnapshot(null);
      }
    }

    // 2) 生成任务进度：无条件按 active_task_run_id 恢复
    if (!proj.active_task_run_id) {
      setTaskRun(null);
      setGenerating(false);
      return;
    }
    try {
      const tr = await api.examProjects.getTaskRun(courseId, proj.active_task_run_id, token ?? undefined);
      if (tr.status === 'succeeded') {
        // 任务已完成：留在生成阶段展示成功面板，由「查看试卷」按钮切换到试卷页签
        setGenerating(false);
        setTaskRun(tr);
        setCurrentStage('generate');
      } else {
        // 失败/取消态恢复错误面板与「重新生成」入口；进行中则继续轮询
        setGenerating(isInFlight(tr.status));
        setTaskRun(tr);
      }
    } catch {
      setTaskRun(null);
      setGenerating(false);
    }
  };

  // 切换项目时重置并恢复状态
  useEffect(() => {
    setCurrentStage(stageFromStatus(sp.status));
    setContractSnapshot(null);
    setTaskRun(null);
    setPlanItems([]);
    // 上一项目的方案号不能带到新项目：hydrate 没有历史种子时会保留随机值，
    // 落在别的项目上就是"看着第 4 版、实际按第 1 版分配"。
    setContractVariant(1 + Math.floor(Math.random() * 6));
    void reloadNameMaps();
    if (sp.active_blueprint_version_id) {
      void loadPlanItems(sp);
    }
    void hydrateProjectState(sp);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sp.id]);

  // stageRequest 按 nonce 消费：合同阶段/生成阶段的推进都靠 setCurrentStage，
  // 残留的旧请求会在下次挂载（如退出项目再进入）时把用户拽到错误的阶段，
  // 记账后只消费一次。
  const handledNonceRef = useRef(0);
  useEffect(() => {
    if (stageRequest && stageRequest.nonce !== handledNonceRef.current) {
      handledNonceRef.current = stageRequest.nonce;
      setCurrentStage(stageRequest.stage);
    }
  }, [stageRequest]);

  // 轮询生成任务。依赖只取 id + 终态判定：若把整个 taskRun 放进依赖，
  // 每次响应都会 clear/re-create interval，2.5s 周期被反复重置成 2.5s+RTT。
  const taskRunId = taskRun?.id;
  const taskRunDone = !taskRun || isTerminal(taskRun.status);
  useEffect(() => {
    if (!taskRunId || taskRunDone) return;
    const id = setInterval(async () => {
      try {
        const tr = await api.examProjects.getTaskRun(courseId, taskRunId, token ?? undefined);
        setTaskRun(tr);
        if (isTerminal(tr.status)) {
          clearInterval(id);
          setGenerating(false);
          if (tr.status === 'succeeded') {
            addToast('试题生成完成', 'success');
            // 项目摘要是打开项目时取的快照，此刻 paper_version_id 仍为 null，
            // 父级刷新项目后再切到「试卷」页签，否则那边读不到新试卷。
            onOpenPaper();
          } else if (tr.status === 'failed') {
            addToast('生成失败: ' + (tr.error_message || '未知错误'), 'error');
          } else {
            addToast('任务已取消', 'info');
          }
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, 2500);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskRunId, taskRunDone, courseId, token, addToast]);

  // 重新生成的幂等键 = sha256(project_id:active_generation_run_id:gen)，
  // 而 active_generation_run_id 只在确认合同时更新。已生成过的项目再点
  // 「重新生成」会命中同一把 key，后端返回**同一条已 succeeded 的旧任务**：
  // 界面显示"生成完成"，却没有新任务、没有新试卷版本。因此凡当前 run 已有
  // 任务（taskRun 非空，含 hydrate 恢复的终态任务），先重新确认合同铸造新的
  // generation_run，换一把 key 才是真重跑。
  //
  // 副作用（已确认）：后端分配带 _HISTORY_RUN_LIMIT=10 的考点避重，
  // 重新确认后的合同可能与当前这份微调不同；allocation_seed 沿用当前方案号，
  // 保证同一方案号语义不变。
  const startGeneration = async () => {
    if (generating) return;
    if (taskRun && isInFlight(taskRun.status)) {
      addToast('任务正在进行中，请等待完成', 'info');
      return;
    }
    if (!sp.active_blueprint_version_id) {
      addToast('尚未创建蓝图，无法生成', 'error');
      return;
    }
    try {
      setGenerating(true);
      if (taskRun) {
        await api.examProjects.confirmContract(courseId, sp.id, {
          blueprint_version_id: sp.active_blueprint_version_id,
          slot_revisions: [],
          allocation_seed: contractVariant - 1,
        });
        // 新 run 的快照可能因考点避重而变化，回填合同页避免展示旧快照
        try {
          const cur = await api.examProjects.getCurrentContract(courseId, sp.id, token ?? undefined);
          const snap = cur?.contract_snapshot;
          setContractSnapshot(snap && Array.isArray(snap.slots) && snap.slots.length > 0 ? snap : null);
        } catch {
          /* 快照读取失败不阻断生成 */
        }
        setTaskRun(null);
        onProjectChanged();
      }
      const res = await api.examProjects.startGeneration(courseId, sp.id);
      const tr = await api.examProjects.getTaskRun(courseId, res.task_run_id, token ?? undefined);
      setTaskRun(tr);
      addToast('任务已启动', 'success');
    } catch (e) {
      addToast('生成失败: ' + (e as Error).message, 'error');
    } finally {
      setGenerating(false);
    }
  };

  const handleCreateBlueprint = async () => {
    try {
      setBpCreating(true);
      // 同时取知识目录与当前框架：后者带考核大纲抽取出的考试规则（题型比例/章节权重）
      const [data, fw] = await Promise.all([
        api.knowledge.getPublished(courseId),
        api.framework.getCurrent(courseId).catch(() => null),
      ]);
      if (data?.published === false || !data?.units || data.units.length === 0) {
        addToast('请先发布知识目录', 'error');
        setBpCreating(false);
        return;
      }

      const units = (data.units || []).map((u) => ({
        unit_id: u.unit_id,
        exam_point_id: u.exam_point_id || u.exam_point_code || '',
        anchor_key: u.anchor_key || '',
        card_ids: u.card_ids || [],
      }));

      // 章节权重：优先用考核大纲声明的命题权重（框架 payload 里的 exam_rules），
      // 那才是考纲的硬约束；考纲没声明时才回退到考点权重累加（模型自报）。
      // 未声明的锚点补 0，保证章权重覆盖全部考核单元——蓝图引擎要求一个都不能少。
      const unitAnchors = new Set(units.map((u) => u.anchor_key).filter(Boolean));
      const declared: Record<string, number> = {};
      (fw?.exam_rules?.chapter_weights || []).forEach((c) => {
        if (c.anchor_key) declared[c.anchor_key] = Number(c.weight) || 0;
      });
      let chapter_weights: Record<string, number> = {};
      const declaredKeys = Object.keys(declared);
      if (declaredKeys.length > 0 && declaredKeys.some((k) => declared[k] > 0)) {
        unitAnchors.forEach((a) => {
          chapter_weights[a] = declared[a] ?? 0;
        });
      }
      if (Object.keys(chapter_weights).length === 0) {
        (data.exam_points || []).forEach((p) => {
          const key = p.anchor_key || p.id;
          // 同一章（anchor_key）下可能有多个考点，权重需累加，而不是后者覆盖前者，
          // 否则 chapter_weights 合计远小于 100，蓝图引擎的章节权重校验会失败。
          if (key && p.weight_value != null) {
            chapter_weights[key] = (chapter_weights[key] ?? 0) + p.weight_value;
          }
        });
      }

      const bp = await api.examProjects.createBlueprint(courseId, sp.id, {
        framework_version_id: data.framework_version_id,
        catalog_version_id: data.catalog_version_id,
        type_rules: {},
        chapter_weights,
        units,
      });
      addToast('蓝图已生成', 'success');
      // 刚取过知识目录，直接复用刷新名称映射，保证考点/知识卡列显示中文名
      void reloadNameMaps(data);
      // 旧合同是按旧蓝图题位分配的，蓝图一重建即失效；后端不清
      // active_generation_run_id，这里不丢掉的话合同阶段会展示过期快照。
      setContractSnapshot(null);
      setTaskRun(null);
      // 通知父级刷新项目（拿回 active_blueprint_version_id），界面立即展示蓝图
      // 并开放「进入合同阶段」，不依赖 list 接口的返回（可能因时序未包含新版本）。
      onBlueprintCreated(bp.blueprint_version_id);
      setCurrentStage('blueprint');
      if (bp.plan && bp.plan.length > 0) {
        setPlanItems(bp.plan);
      }
    } catch (e) {
      addToast('蓝图创建失败: ' + (e as Error).message, 'error');
    } finally {
      setBpCreating(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageStepper current={currentStage} onSelect={setCurrentStage} />
      {currentStage === 'blueprint' && renderBlueprint({
        sp, setStep: setCurrentStage, bpCreating, handleCreateBlueprint, loadPlanItems, planItems, maps, examRules,
      })}
      {currentStage === 'contract' && renderContract({
        sp, courseId, setStep: setCurrentStage, contractVariant, setContractVariant,
        contractSnapshot, setContractSnapshot, contractConfirming, setContractConfirming,
        contractAllocating, setContractAllocating, setTaskRun, addToast, maps, planItems, onProjectChanged,
        explainItem, setExplainItem,
      })}
      {currentStage === 'generate' && renderGenerate({
        sp, setStep: setCurrentStage, taskRun, generating, startGeneration,
        onOpenPaper,
      })}
    </div>
  );
}
