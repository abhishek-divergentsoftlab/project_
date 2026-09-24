import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";

import { IconAlert, IconCheck, IconClose } from "@/components/icons";
import { Modal } from "@/components/ui/Modal";
import {
  FeedbackContext,
  type ConfirmOptions,
  type PromptOptions,
  type ToastTone,
} from "@/context/useFeedback";

type Toast = { id: number; message: string; tone: ToastTone };

type PendingConfirm = ConfirmOptions & { resolve: (ok: boolean) => void };
type PendingPrompt = PromptOptions & { resolve: (value: string | null) => void };

/**
 * App-wide feedback: short-lived toasts for "that worked", and a styled
 * confirmation dialog in place of window.confirm.
 */
export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [pendingPrompt, setPendingPrompt] = useState<PendingPrompt | null>(null);
  const [promptValue, setPromptValue] = useState("");
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const toast = useCallback(
    (message: string, tone: ToastTone = "success") => {
      const id = nextId.current++;
      setToasts((current) => [...current.slice(-2), { id, message, tone }]);
      window.setTimeout(() => dismiss(id), tone === "error" ? 6000 : 3500);
    },
    [dismiss],
  );

  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        setPending({ ...options, resolve });
      }),
    [],
  );

  const prompt = useCallback(
    (options: PromptOptions) =>
      new Promise<string | null>((resolve) => {
        setPromptValue(options.defaultValue ?? "");
        setPendingPrompt({ ...options, resolve });
      }),
    [],
  );

  function settle(ok: boolean) {
    pending?.resolve(ok);
    setPending(null);
  }

  function settlePrompt(value: string | null) {
    pendingPrompt?.resolve(value);
    setPendingPrompt(null);
  }

  const value = useMemo(() => ({ toast, confirm, prompt }), [toast, confirm, prompt]);

  return (
    <FeedbackContext.Provider value={value}>
      {children}

      <div className="toast-region" role="status" aria-live="polite">
        {toasts.map((item) => (
          <div key={item.id} className={`toast toast-${item.tone}`}>
            <span className="toast-icon" aria-hidden="true">
              {item.tone === "error" ? <IconAlert size={16} /> : <IconCheck size={16} />}
            </span>
            <span className="toast-message">{item.message}</span>
            <button
              type="button"
              className="toast-dismiss"
              onClick={() => dismiss(item.id)}
              aria-label="Dismiss"
            >
              <IconClose size={14} />
            </button>
          </div>
        ))}
      </div>

      <Modal
        open={pending !== null}
        onClose={() => settle(false)}
        title={pending?.title ?? ""}
        description={pending?.message}
        size="sm"
        footer={
          <>
            <button type="button" className="secondary" onClick={() => settle(false)}>
              {pending?.cancelLabel ?? "Cancel"}
            </button>
            <button
              type="button"
              className={pending?.tone === "danger" ? "danger" : "primary"}
              onClick={() => settle(true)}
              data-autofocus
            >
              {pending?.confirmLabel ?? "Confirm"}
            </button>
          </>
        }
      />

      <Modal
        open={pendingPrompt !== null}
        onClose={() => settlePrompt(null)}
        title={pendingPrompt?.title ?? ""}
        description={pendingPrompt?.message}
        size="sm"
      >
        <form
          className="form-stack"
          onSubmit={(event) => {
            event.preventDefault();
            settlePrompt(promptValue.trim());
          }}
        >
          <div className="field">
            <label htmlFor="feedback-prompt">{pendingPrompt?.label}</label>
            {pendingPrompt?.multiline ? (
              <textarea
                id="feedback-prompt"
                rows={3}
                value={promptValue}
                placeholder={pendingPrompt?.placeholder}
                onChange={(e) => setPromptValue(e.target.value)}
              />
            ) : (
              <input
                id="feedback-prompt"
                value={promptValue}
                placeholder={pendingPrompt?.placeholder}
                onChange={(e) => setPromptValue(e.target.value)}
              />
            )}
          </div>
          <div className="modal-actions">
            <button type="button" className="secondary" onClick={() => settlePrompt(null)}>
              {pendingPrompt?.cancelLabel ?? "Cancel"}
            </button>
            <button type="submit" className={pendingPrompt?.tone === "danger" ? "danger" : "primary"}>
              {pendingPrompt?.confirmLabel ?? "Confirm"}
            </button>
          </div>
        </form>
      </Modal>
    </FeedbackContext.Provider>
  );
}
