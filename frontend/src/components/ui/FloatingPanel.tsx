import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ChevronUp, Minus, X } from 'lucide-react';

interface Props {
  /** 头部标题（可含徽章等节点） */
  title: ReactNode;
  /** 最小化胶囊上的短文案——title 是 ReactNode 时取不到纯文本，故单列 */
  pillLabel: string;
  onClose: () => void;
  /** 锚定选择器：给了就绝对定位悬浮在该元素（如被点击的表格行）下方、
   *  随页面滚动跟随；缺省则固定右下角 */
  anchorSelector?: string;
  children: ReactNode;
}

/**
 * 浮层互斥注册表：同一时刻至多一个面板展开，避免互相遮挡。
 * 新面板挂载时把注册表里已有的面板全部最小化（状态/轮询不丢）。
 */
const mountedPanels = new Set<() => void>();

/** 锚定模式下面板与锚点的垂直间隙、视口右缘留白 */
const ANCHOR_GAP = 8;
const EDGE_GAP = 16;

/**
 * AI 对话浮层外壳：portal 到 body，不进入页面流（不挤压双栏/表格等布局）。
 * 头部提供最小化与关闭。
 *
 * - 缺省固定右下角（AI 改题/质量评审）；给了 anchorSelector 则锚定悬浮在
 *   被点击的行下方（合同解释），定位用 ref 直改样式，不走 state。
 * - 最小化只换外壳不卸载 children（display:none）——轮询任务照常推进、
 *   面板内已生成的提案/输入不丢；只有父级 onClose 才真正卸载。
 * - 胶囊统一挂顶栏下方右侧（右下角整块让给展开面板），多个胶囊纵向错开。
 */
export function FloatingPanel({ title, pillLabel, onClose, anchorSelector, children }: Props) {
  const [minimized, setMinimized] = useState(false);
  const minimize = useCallback(() => setMinimized(true), []);
  const sectionRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    // 挂载即互斥：先把注册表里已展开的旧面板全部收起
    mountedPanels.forEach((m) => m());
    mountedPanels.add(minimize);
    return () => {
      mountedPanels.delete(minimize);
    };
  }, [minimize]);

  // 锚定模式：面板顶边贴锚点（行）下方、右缘不越出视口；scroll 用捕获阶段，
  // 页面滚动与 table-wrapper 内部横向滚动都能触发重定位
  useEffect(() => {
    if (!anchorSelector) return;
    const place = () => {
      const el = sectionRef.current;
      const anchor = document.querySelector<HTMLElement>(anchorSelector);
      if (!el || !anchor) return;
      const r = anchor.getBoundingClientRect();
      const vw = document.documentElement.clientWidth;
      const width = el.offsetWidth || 560;
      const left = Math.max(
        window.scrollX + EDGE_GAP,
        Math.min(r.left + window.scrollX, window.scrollX + vw - width - EDGE_GAP),
      );
      el.style.top = `${Math.round(r.bottom + window.scrollY + ANCHOR_GAP)}px`;
      el.style.left = `${Math.round(left)}px`;
    };
    place();
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [anchorSelector]);

  // 从胶囊展开同样互斥：先收起其余面板，再展开自己
  const expand = () => {
    mountedPanels.forEach((m) => {
      if (m !== minimize) m();
    });
    setMinimized(false);
  };

  return createPortal(
    <>
      <section
        ref={sectionRef}
        className={[
          'floating-panel',
          minimized ? 'is-minimized' : '',
          anchorSelector ? 'is-anchored' : '',
        ].filter(Boolean).join(' ')}
        role="dialog"
        aria-label={pillLabel}
      >
        <header className="floating-panel-head">
          <span className="floating-panel-title">{title}</span>
          <button
            className="floating-icon-btn"
            onClick={() => setMinimized(true)}
            aria-label="最小化"
            title="最小化（任务继续在后台跑）"
          >
            <Minus size={15} />
          </button>
          <button className="floating-icon-btn" onClick={onClose} aria-label="关闭">
            <X size={15} />
          </button>
        </header>
        <div className="floating-panel-body">{children}</div>
      </section>
      {minimized && (
        <button className="floating-pill" onClick={expand}>
          <ChevronUp size={14} />
          {pillLabel}
        </button>
      )}
    </>,
    document.body,
  );
}
