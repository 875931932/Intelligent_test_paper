import { useMemo, type CSSProperties } from 'react';

// ─── 阅读态题干分块：与后端导出 _stem_html 同一套解析规则 ───
// GFM 管道表格 → 真表格，``` 围栏 → 等宽代码块，其余保持 pre-wrap 原文。
// 整体预览/下载走后端导出 HTML，这里保证站内阅读（试题详情）看到同一形态，
// 综合题的特性对比表不再是裸竖线纯文字。

type StemBlock =
  | { kind: 'text'; text: string }
  | { kind: 'code'; code: string }
  | { kind: 'table'; header: string[]; rows: string[][] };

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const DELIM_CELL = /^:?-{2,}:?$/;

function tableCells(row: string): string[] {
  return row.trim().slice(1, -1).split('|').map((c) => c.trim());
}

function isDelim(row: string | undefined): boolean {
  if (!row || !TABLE_ROW.test(row)) return false;
  const cells = tableCells(row);
  return cells.length > 0 && cells.every((c) => DELIM_CELL.test(c));
}

/** 列数以表头为准：少的补齐、多的截齐（GFM 语义，与后端一致）。 */
function pad(cells: string[], cols: number): string[] {
  return [...cells, ...Array(Math.max(cols - cells.length, 0)).fill('')].slice(0, cols);
}

/** 正文块：管道表格聚成 table 块，其余聚成 text 块（对应后端 _text_blocks_html）。 */
function pushTextOrTable(blocks: StemBlock[], chunk: string): void {
  if (!chunk.trim()) return;
  if (!chunk.includes('|')) {
    blocks.push({ kind: 'text', text: chunk.trim() });
    return;
  }
  const lines = chunk.split('\n');
  let buf: string[] = [];
  const flush = () => {
    const text = buf.join('\n').trim();
    if (text) blocks.push({ kind: 'text', text });
    buf = [];
  };
  let i = 0;
  while (i < lines.length) {
    if (TABLE_ROW.test(lines[i]) && isDelim(lines[i + 1])) {
      flush();
      const header = tableCells(lines[i]);
      i += 2; // 跳过表头与分隔行
      const rows: string[][] = [];
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(tableCells(lines[i]));
        i += 1;
      }
      blocks.push({ kind: 'table', header, rows });
      continue;
    }
    buf.push(lines[i]);
    i += 1;
  }
  flush();
}

function parseStemBlocks(stem: string): StemBlock[] {
  const blocks: StemBlock[] = [];
  (stem ?? '').split('```').forEach((chunk, i) => {
    if (i % 2 === 0) {
      pushTextOrTable(blocks, chunk);
      return;
    }
    // 围栏首行是语言标记（```python），不是代码本体；单行块整段即代码（与后端一致）
    const nl = chunk.indexOf('\n');
    const code = (nl >= 0 ? chunk.slice(nl + 1) : chunk).replace(/\s+$/, '');
    blocks.push({ kind: 'code', code });
  });
  return blocks;
}

const cellBase: CSSProperties = {
  border: '1px solid rgba(0, 0, 0, 0.18)',
  padding: '5px 9px',
  textAlign: 'center',
  lineHeight: 1.6,
};

export function StemBlocks({ text }: { text: string }) {
  const blocks = useMemo(() => parseStemBlocks(text), [text]);
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === 'code') {
          return (
            <pre
              key={i}
              style={{
                fontFamily: "Consolas, 'Courier New', monospace",
                fontSize: '0.82rem',
                lineHeight: 1.6,
                background: 'rgba(0, 0, 0, 0.04)',
                border: '1px solid rgba(0, 0, 0, 0.08)',
                borderRadius: 8,
                padding: '10px 12px',
                margin: '8px 0',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                overflowX: 'auto',
              }}
            >
              {b.code}
            </pre>
          );
        }
        if (b.kind === 'table') {
          const cols = Math.max(b.header.length, 1);
          return (
            <table key={i} style={{ borderCollapse: 'collapse', width: '100%', margin: '8px 0', fontSize: '0.85rem' }}>
              <thead>
                <tr>
                  {pad(b.header, cols).map((c, ci) => (
                    <th key={ci} style={{ ...cellBase, fontWeight: 600, background: 'rgba(0, 0, 0, 0.04)' }}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row, ri) => (
                  <tr key={ri}>
                    {pad(row, cols).map((c, ci) => (
                      <td key={ci} style={cellBase}>{c}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          );
        }
        return (
          <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{b.text}</div>
        );
      })}
    </>
  );
}
