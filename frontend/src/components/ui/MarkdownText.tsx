import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * 模型返回内容的 Markdown 渲染（GFM：表格 / 围栏代码 / 列表 / 删除线）。
 * 助手气泡正文、流式打字机、思考区三处共用；样式见 global.css 的 `.md-body`。
 * react-markdown 默认不渲染原始 HTML 且过滤 javascript: 等危险链接（模型输出不可信）；
 * 流式期间未闭合的 ** / ``` 先按字面文本显示，闭合后自然切换，无需特殊处理。
 */
export function MarkdownText({ text }: { text: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
