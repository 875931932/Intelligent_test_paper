import { api } from '@/api/client';
import { qlabel } from '@/lib/examDisplay';
import { runSuggestAndApplyAll } from '@/lib/blueprintSuggest';
import { assembleBlueprintRequestBody } from '@/pages/paper/blueprintAssembly';
import { useCourseStore } from '@/stores/course';
import type {
  AssistantMessage,
  CourseCreate,
  CourseUpdate,
  ExamRules,
} from '@/types/api';

/**
 * 提案卡执行契约的**唯一**实现：助手页「确认执行」按钮与自动执行队列共用，
 * 不开第二套写路径——每个分支都只是转发到既有业务 API（助手自身从不写业务数据）。
 *
 * relay=false 表示这一步只完成了一部分（例如蓝图建议只应用了一半）：调用方
 * 必须停止自动接力，把剩余部分交给教师到试卷页处理，不能带着半套结果往下走。
 */
export interface ProposalExecResult {
  receipt: string;
  relay: boolean;
}

/**
 * 接力集合：执行成功后由前端自动追问「继续」，让出卷主线的下一张卡自动弹出，
 * 成为教师的下一个动作。课程级一次性操作（新建/改课程、发起解析）与只读的整卷
 * 评审不在链上——追问也问不出下一步，只会白烧一次模型调用。
 */
export const RELAY_TOOLS: ReadonlySet<string> = new Set([
  'create_exam_project',
  'update_exam_rules',
  'create_blueprint',
  'confirm_blueprint',
  'enqueue_blueprint_suggest',
  'confirm_contract',
  'start_generation',
  'update_question_type_format',
]);

export interface ProposalExecContext {
  courseId: string;
  token?: string;
  /** 长步骤（蓝图建议任务）期间的进度文案：顶到卡片回执上让教师看得见 */
  onProgress?: (text: string) => void;
}

export async function executeProposalAction(
  m: AssistantMessage,
  { courseId, token, onProgress }: ProposalExecContext,
): Promise<ProposalExecResult> {
  const payload = m.action.payload ?? {};
  const body = payload.body ?? {};
  const done = (receipt: string, relay = true): ProposalExecResult => ({ receipt, relay });

  switch (m.action.tool) {
    case 'create_course': {
      // body 经后端白名单硬校验（name 必填），运行时形状由 build_proposal_payload 保证
      const created = await api.courses.create(body as unknown as CourseCreate, token);
      useCourseStore.getState().addCourse(created);
      return done(`已创建课程「${created.name}」`);
    }
    case 'update_course': {
      const updated = await api.courses.update(
        payload.course_id ?? courseId,
        body as CourseUpdate,
        token,
      );
      // course store 无单条更新动作，就地替换避免侧栏课程名显示陈旧值
      useCourseStore.setState((s) => ({
        courses: s.courses.map((c) => (c.id === updated.id ? updated : c)),
      }));
      return done(`已更新课程「${updated.name}」`);
    }
    case 'start_parse': {
      if (!payload.material_id) throw new Error('提案缺少资料 id');
      await api.materials.parse(courseId, payload.material_id, token);
      return done('已发起解析，进度见资料库');
    }
    case 'create_exam_project': {
      if (typeof body.name !== 'string' || !body.name.trim()) throw new Error('提案缺少项目名称');
      const created = await api.examProjects.create(courseId, { name: body.name.trim() }, token);
      return done(`已创建试卷项目「${created.name}」`);
    }
    case 'update_exam_rules': {
      // body 由后端按现值合并成完整规则（整份替换语义，未涉及字段不丢）
      await api.framework.updateExamRules(courseId, body as unknown as ExamRules, token);
      return done('考核规则已更新（蓝图创建时按新规则确定性生成）');
    }
    case 'create_blueprint': {
      if (!payload.project_id) throw new Error('提案缺少项目 id');
      // 与试卷页同一条组装路径（共享 blueprintAssembly），不开第二套写路径
      const assembly = await assembleBlueprintRequestBody(courseId);
      if (!assembly.ok) throw new Error('请先发布知识目录');
      const pool = Array.isArray(body.comprehensive_archetypes)
        ? body.comprehensive_archetypes
        : [];
      await api.examProjects.createBlueprint(
        courseId,
        payload.project_id,
        pool.length > 0
          ? { ...assembly.body, comprehensive_archetypes: pool }
          : assembly.body,
        token,
      );
      return done(
        `已为「${payload.project_name || payload.project_id}」创建草稿蓝图，确认题位后可确认蓝图`,
      );
    }
    case 'confirm_blueprint': {
      if (!payload.project_id) throw new Error('提案缺少项目 id');
      await api.examProjects.confirmBlueprint(courseId, payload.project_id, {}, token);
      return done('蓝图已确认冻结，可进入合同分配');
    }
    case 'enqueue_blueprint_suggest': {
      if (!payload.project_id) throw new Error('提案缺少项目 id');
      const instruction = typeof body.instruction === 'string' ? body.instruction : '';
      // 发起建议任务 → 等完成 → 自动应用全部待办建议（教师不再跳页点「全部应用」）
      const outcome = await runSuggestAndApplyAll(
        courseId, payload.project_id, instruction, token, onProgress,
      );
      if (outcome.error) throw new Error(outcome.error);
      if (outcome.total === 0) return done('AI 检查完成：蓝图无需调整');
      if (outcome.failed > 0) {
        // 有建议没落地：不接力到确认蓝图，交教师到试卷页处理剩余条目
        return done(
          `已应用 ${outcome.applied}/${outcome.total} 条调整建议，剩余请到试卷页处理`,
          false,
        );
      }
      return done(`已应用 ${outcome.applied} 条蓝图调整建议`);
    }
    case 'confirm_contract': {
      if (!payload.project_id) throw new Error('提案缺少项目 id');
      await api.examProjects.confirmContract(courseId, payload.project_id, {}, token);
      return done('合同已确认落库（分配由既有确定性算法执行）');
    }
    case 'start_generation': {
      if (!payload.project_id) throw new Error('提案缺少项目 id');
      await api.examProjects.startGeneration(courseId, payload.project_id, undefined, token);
      return done('已发起 AI 生成任务，进度见试卷页');
    }
    case 'enqueue_paper_review': {
      if (!payload.paper_version_id) throw new Error('提案缺少试卷版本 id');
      await api.paperVersions.aiReview(
        courseId,
        payload.paper_version_id,
        typeof body.instruction === 'string' ? body.instruction : '',
        token,
      );
      return done('已发起整卷 AI 评审任务，完成后在试卷页查看报告');
    }
    case 'enqueue_framework_review': {
      await api.framework.reviewFramework(
        courseId,
        typeof body.instruction === 'string' ? body.instruction : '',
        token,
      );
      return done('已发起框架 AI 评审任务，完成后在命题框架页查看报告');
    }
    case 'update_question_type_format': {
      if (typeof body.question_type !== 'string') throw new Error('提案缺少题型');
      await api.framework.setQuestionTypeFormat(
        courseId,
        {
          question_type: body.question_type,
          template: typeof body.template === 'string' ? body.template : '',
        },
        token,
      );
      const qtLabel = qlabel(body.question_type);
      return done(
        body.template
          ? `已更新「${qtLabel}」出题格式（之后生成生效）`
          : `已恢复「${qtLabel}」系统默认出题格式`,
      );
    }
    default:
      throw new Error(`不支持的提案操作：${m.action.tool ?? '未知'}`);
  }
}