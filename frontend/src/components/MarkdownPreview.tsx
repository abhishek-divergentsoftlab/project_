import { Fragment, useMemo, type ReactNode } from "react";

interface MarkdownPreviewProps {
  content: string;
  className?: string;
}

/**
 * Safely parse inline markdown (bold, italic, inline code, links) into React elements.
 * Uses zero innerHTML, ensuring 100% XSS safety through React JSX text escaping.
 */
function renderInline(text: string): ReactNode {
  if (!text) return null;

  // Regex matches: bold (**text**), inline code (`code`), italic (*text*), link ([text](url))
  const regex = /(\*\*.*?\*\*|`.*?`|\*.*?\*|\[.*?\]\(.*?\))/g;
  const parts = text.split(regex);

  return parts.map((part, index) => {
    if (!part) return null;

    // Bold: **text**
    if (part.startsWith("**") && part.endsWith("**") && part.length >= 4) {
      return <strong key={index}>{renderInline(part.slice(2, -2))}</strong>;
    }

    // Inline Code: `code`
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      return (
        <code key={index} className="md-inline-code">
          {part.slice(1, -1)}
        </code>
      );
    }

    // Italic: *text*
    if (part.startsWith("*") && part.endsWith("*") && part.length >= 2) {
      return <em key={index}>{renderInline(part.slice(1, -1))}</em>;
    }

    // Link: [text](url)
    const linkMatch = part.match(/^\[(.*?)\]\((.*?)\)$/);
    if (linkMatch) {
      const linkText = linkMatch[1];
      const linkUrl = linkMatch[2];
      // Only permit safe http, https or relative links
      const isSafeUrl = /^https?:\/\//i.test(linkUrl) || linkUrl.startsWith("/") || linkUrl.startsWith("#");
      if (isSafeUrl) {
        return (
          <a key={index} href={linkUrl} target="_blank" rel="noopener noreferrer" className="md-link">
            {linkText}
          </a>
        );
      }
      return <span key={index}>{linkText}</span>;
    }

    return <Fragment key={index}>{part}</Fragment>;
  });
}

interface Block {
  type: "heading" | "code" | "table" | "bullet-list" | "number-list" | "blockquote" | "paragraph";
  level?: number;
  lang?: string;
  items?: string[];
  headers?: string[];
  rows?: string[][];
  text?: string;
}

/**
 * Parse markdown blocks: headers, tables, code blocks, lists, blockquotes, and paragraphs.
 */
function parseBlocks(markdown: string): Block[] {
  const lines = markdown.split(/\r?\n/);
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // 1. Empty lines
    if (!trimmed) {
      i++;
      continue;
    }

    // 2. Fenced Code Block: ```lang
    if (trimmed.startsWith("```")) {
      const lang = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length && lines[i].trim().startsWith("```")) {
        i++; // skip closing ```
      }
      blocks.push({
        type: "code",
        lang,
        text: codeLines.join("\n"),
      });
      continue;
    }

    // 3. Headings: #, ##, ###, ####
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      blocks.push({
        type: "heading",
        level: headingMatch[1].length,
        text: headingMatch[2],
      });
      i++;
      continue;
    }

    // 4. Blockquotes: > text
    if (trimmed.startsWith(">")) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      blocks.push({
        type: "blockquote",
        text: quoteLines.join(" "),
      });
      continue;
    }

    // 5. Tables: lines with |
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) {
        tableLines.push(lines[i].trim());
        i++;
      }

      if (tableLines.length >= 2) {
        const splitRow = (row: string) =>
          row
            .slice(1, -1)
            .split("|")
            .map((cell) => cell.trim());

        const headers = splitRow(tableLines[0]);
        // Check if line 1 is separator (e.g. |---|---|)
        const isSeparator = /^\|?(\s*:?-+:?\s*\|?)+$/.test(tableLines[1]);
        const dataRows = isSeparator ? tableLines.slice(2) : tableLines.slice(1);
        const rows = dataRows.map(splitRow);

        blocks.push({
          type: "table",
          headers,
          rows,
        });
        continue;
      }
    }

    // 6. Bullet lists: - item, * item, • item
    if (/^[-*•]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*•]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*•]\s+/, ""));
        i++;
      }
      blocks.push({
        type: "bullet-list",
        items,
      });
      continue;
    }

    // 7. Numbered lists: 1. item
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, ""));
        i++;
      }
      blocks.push({
        type: "number-list",
        items,
      });
      continue;
    }

    // 8. Normal Paragraph
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trim().startsWith("```") &&
      !lines[i].trim().startsWith("#") &&
      !lines[i].trim().startsWith(">") &&
      !(lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) &&
      !/^[-*•]\s+/.test(lines[i].trim()) &&
      !/^\d+\.\s+/.test(lines[i].trim())
    ) {
      paraLines.push(lines[i]);
      i++;
    }

    if (paraLines.length > 0) {
      blocks.push({
        type: "paragraph",
        text: paraLines.join("\n"),
      });
    }
  }

  return blocks;
}

export function MarkdownPreview({ content, className = "" }: MarkdownPreviewProps) {
  const blocks = useMemo(() => parseBlocks(content), [content]);

  if (!content.trim()) return null;

  return (
    <div className={`md-preview ${className}`}>
      {blocks.map((block, idx) => {
        switch (block.type) {
          case "heading": {
            const level = block.level || 3;
            if (level === 1) return <h3 key={idx} className="md-h1">{renderInline(block.text || "")}</h3>;
            if (level === 2) return <h4 key={idx} className="md-h2">{renderInline(block.text || "")}</h4>;
            return <h5 key={idx} className="md-h3">{renderInline(block.text || "")}</h5>;
          }

          case "code":
            return (
              <div key={idx} className="md-code-block-wrapper">
                {block.lang && <span className="md-code-lang">{block.lang}</span>}
                <pre className="md-code-block">
                  <code>{block.text}</code>
                </pre>
              </div>
            );

          case "table":
            return (
              <div key={idx} className="md-table-wrapper">
                <table className="md-table">
                  {block.headers && block.headers.length > 0 && (
                    <thead>
                      <tr>
                        {block.headers.map((h, hIdx) => (
                          <th key={hIdx}>{renderInline(h)}</th>
                        ))}
                      </tr>
                    </thead>
                  )}
                  {block.rows && block.rows.length > 0 && (
                    <tbody>
                      {block.rows.map((row, rIdx) => (
                        <tr key={rIdx}>
                          {row.map((cell, cIdx) => (
                            <td key={cIdx}>{renderInline(cell)}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  )}
                </table>
              </div>
            );

          case "bullet-list":
            return (
              <ul key={idx} className="md-bullet-list">
                {block.items?.map((item, itemIdx) => (
                  <li key={itemIdx}>{renderInline(item)}</li>
                ))}
              </ul>
            );

          case "number-list":
            return (
              <ol key={idx} className="md-number-list">
                {block.items?.map((item, itemIdx) => (
                  <li key={itemIdx}>{renderInline(item)}</li>
                ))}
              </ol>
            );

          case "blockquote":
            return (
              <blockquote key={idx} className="md-blockquote">
                {renderInline(block.text || "")}
              </blockquote>
            );

          case "paragraph":
          default:
            return (
              <p key={idx} className="md-paragraph">
                {block.text?.split("\n").map((subLine, subIdx) => (
                  <Fragment key={subIdx}>
                    {subIdx > 0 && <br />}
                    {renderInline(subLine)}
                  </Fragment>
                ))}
              </p>
            );
        }
      })}
    </div>
  );
}
