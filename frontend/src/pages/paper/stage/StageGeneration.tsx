import { ArrowLeft, RefreshCw, PlayCircle } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import type { ExamProject, TaskRun } from '@/types/api';
import { GenerationProgressPanel } from './GenerationProgressPanel';
import { StageHeading } from './StageHeading';
import { isInFlight, type StageKey } from './stageShared';

export function renderGenerate({
  sp, setStep, taskRun, generating, startGeneration, onOpenPaper,
}: {
  sp: ExamProject; setStep: (s: StageKey) => void;
  taskRun: TaskRun | null;
  generating: boolean;
  /** 首次启动、失败重试与「重新生成」共用；已处理幂等键问题，见 PipelinePanel */
  startGeneration: () => Promise<void>;
  onOpenPaper: () => void;
}) {
  // 任务在途时禁止再次发起：并发两次会各写一版试卷，且进度面板来回跳。
  const inFlight = !!taskRun && isInFlight(taskRun.status);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <StageHeading
        title="AI 生成试题"
        right={
          <Button
            variant="secondary" size="sm"
            onClick={() => { void startGeneration(); }}
            loading={generating}
            disabled={inFlight}
            icon={<RefreshCw size={14} />}
            title={inFlight ? '任务进行中，请等待完成' : '按当前合同重新生成，创建新版本试卷'}
          >
            重新生成
          </Button>
        }
      />
      {!taskRun ? (
        <div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '14px' }}>
            AI 将根据合同约定生成试题。生成过程大约需要 30-60 秒。
            {sp.item_count ? ' 重新生成会创建新版本的试卷，不影响已有版本。' : ''}
          </p>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <Button variant="secondary" onClick={() => setStep('contract')}><ArrowLeft size={16} /> 返回合同</Button>
            <Button
              onClick={() => { void startGeneration(); }}
              loading={generating}
              icon={<PlayCircle size={16} />}
            >
              {sp.item_count ? '重新生成' : '开始生成'}
            </Button>
          </div>
        </div>
      ) : (
        <GenerationProgressPanel
          taskRun={taskRun}
          onRetry={() => { void startGeneration(); }}
          onBack={() => setStep('contract')}
          onOpenPaper={onOpenPaper}
        />
      )}
    </div>
  );
}
