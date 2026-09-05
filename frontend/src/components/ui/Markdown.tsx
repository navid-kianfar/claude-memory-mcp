import { Fragment, useMemo } from "react";
import type { MdBlock, MdInline, MdList } from "../../lib/markdown";
import { parseMarkdown } from "../../lib/markdown";
import { cn } from "../../lib/utils";

export interface MarkdownProps {
  /** Raw markdown — the same text the edit textarea holds. */
  source: string | null | undefined;
  className?: string;
}

/**
 * Renders markdown as React elements.
 *
 * Every piece of text lands in a React child, so React escapes it: there is no
 * `dangerouslySetInnerHTML` here and no HTML string anywhere in the pipeline,
 * which is why untrusted description or comment text cannot become markup.
 *
 * Sizing is relative (`em`, not `rem`) so one component serves both the
 * description block and the smaller comment bodies: whatever font-size the
 * caller sets, the headings, code and rules scale with it.
 */
export function Markdown({ source, className }: MarkdownProps) {
  const blocks = useMemo(() => parseMarkdown(source), [source]);
  if (blocks.length === 0) return null;
  return (
    <div className={cn("break-words", className)}>
      <Blocks blocks={blocks} />
    </div>
  );
}

function Blocks({ blocks }: { blocks: MdBlock[] }) {
  return (
    <>
      {blocks.map((block, index) => (
        <Block key={index} block={block} first={index === 0} />
      ))}
    </>
  );
}

function Block({ block, first }: { block: MdBlock; first: boolean }) {
  switch (block.kind) {
    case "heading": {
      const size =
        block.level === 1
          ? "text-[1.15em]"
          : block.level === 2
            ? "text-[1.06em]"
            : "text-[1em]";
      const Tag = (["h1", "h2", "h3"] as const)[block.level - 1];
      return (
        <Tag
          className={cn(
            "mb-1.5 font-semibold leading-snug tracking-tight",
            size,
            first ? "mt-0" : "mt-4"
          )}
        >
          <Spans spans={block.spans} />
        </Tag>
      );
    }

    case "paragraph":
      return (
        <p className={cn("mb-2 last:mb-0", first && "mt-0")}>
          <Spans spans={block.spans} />
        </p>
      );

    case "list":
      return <List list={block.list} className={cn("mb-2 last:mb-0", first && "mt-0")} />;

    case "quote":
      return (
        <blockquote
          className={cn(
            "mb-2 border-l-2 border-primary/40 pl-3 text-muted-foreground last:mb-0",
            first && "mt-0"
          )}
        >
          <Blocks blocks={block.blocks} />
        </blockquote>
      );

    case "codeblock":
      return (
        <pre
          className={cn(
            "mb-2 overflow-x-auto rounded-md border border-border bg-foreground/[0.04] px-3 py-2 last:mb-0",
            first && "mt-0"
          )}
        >
          <code className="font-mono text-[0.85em] leading-relaxed">{block.value}</code>
        </pre>
      );

    case "rule":
      return <hr className="my-3 border-t border-border" />;
  }
}

function List({
  list,
  className,
  nested,
}: {
  list: MdList;
  className?: string;
  nested?: boolean;
}) {
  const Tag = list.ordered ? "ol" : "ul";
  return (
    <Tag
      start={list.ordered && list.start !== 1 ? list.start : undefined}
      className={cn(
        "space-y-1 pl-5 marker:text-muted-foreground/80",
        list.ordered
          ? nested
            ? "list-[lower-alpha]"
            : "list-decimal"
          : nested
            ? "list-[circle]"
            : "list-disc",
        nested && "mt-1",
        className
      )}
    >
      {list.items.map((item, index) => (
        <li key={index} className="pl-0.5">
          <Spans spans={item.spans} />
          {item.nested && <List list={item.nested} nested />}
        </li>
      ))}
    </Tag>
  );
}

function Spans({ spans }: { spans: MdInline[] }) {
  return (
    <>
      {spans.map((span, index) => {
        switch (span.kind) {
          case "text":
            return <Fragment key={index}>{span.value}</Fragment>;
          case "break":
            return <br key={index} />;
          case "code":
            return (
              <code
                key={index}
                className="rounded border border-border/70 bg-foreground/[0.06] px-1 py-px font-mono text-[0.88em]"
              >
                {span.value}
              </code>
            );
          case "strong":
            return (
              <strong key={index} className="font-semibold">
                <Spans spans={span.children} />
              </strong>
            );
          case "em":
            return (
              <em key={index} className="italic">
                <Spans spans={span.children} />
              </em>
            );
          case "strike":
            return (
              <s key={index} className="text-muted-foreground">
                <Spans spans={span.children} />
              </s>
            );
        }
      })}
    </>
  );
}
