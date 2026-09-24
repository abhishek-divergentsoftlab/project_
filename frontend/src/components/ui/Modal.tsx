import { useEffect, useId, useRef, type ReactNode } from "react";

import { IconClose } from "@/components/icons";

type ModalProps = {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Rendered in a right-aligned action row under the body. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** Blocks Escape and backdrop clicks, e.g. while a request is in flight. */
  busy?: boolean;
  className?: string;
};

const WIDTH = { sm: "26rem", md: "32rem", lg: "44rem" } as const;

/**
 * The one dialog: Escape and backdrop close it, focus moves inside on open
 * and returns to the trigger on close, and the page behind stops scrolling.
 */
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  busy = false,
  className = "",
}: ModalProps) {
  const titleId = useId();
  const descId = useId();
  const cardRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const card = cardRef.current;
    const firstField = card?.querySelector<HTMLElement>(
      "input:not([type=hidden]):not([disabled]), select, textarea, [data-autofocus]",
    );
    (firstField ?? card)?.focus({ preventScroll: true });

    return () => previouslyFocused?.focus?.({ preventScroll: true });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) {
        event.stopPropagation();
        onCloseRef.current();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, busy]);

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={cardRef}
        className={`modal-card ${className}`.trim()}
        style={{ width: `min(100%, ${WIDTH[size]})` }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
      >
        <div className="modal-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && (
              <p id={descId} className="muted">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            className="close-btn"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
          >
            <IconClose size={18} />
          </button>
        </div>
        {children}
        {footer && <div className="modal-actions">{footer}</div>}
      </div>
    </div>
  );
}
