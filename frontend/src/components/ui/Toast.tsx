import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { cn } from "../../lib/utils";

export type ToastVariant = "default" | "success" | "error" | "warning";

export interface ToastItem {
  id: number;
  title: string;
  description?: string;
  variant: ToastVariant;
}

export interface ToastOptions {
  title: string;
  description?: string;
  variant?: ToastVariant;
  duration?: number;
}

interface ToastContextValue {
  toast: (opts: ToastOptions) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const counter = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const toast = useCallback(
    (opts: ToastOptions) => {
      const id = ++counter.current;
      const item: ToastItem = {
        id,
        title: opts.title,
        description: opts.description,
        variant: opts.variant ?? "default",
      };
      setToasts((t) => [...t, item]);
      const duration = opts.duration ?? 4500;
      if (duration > 0) {
        window.setTimeout(() => dismiss(id), duration);
      }
    },
    [dismiss]
  );

  const value = useMemo(() => ({ toast, dismiss }), [toast, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

const ICONS: Record<ToastVariant, React.ReactNode> = {
  default: <Info className="size-4 text-primary" />,
  success: <CheckCircle2 className="size-4 text-emerald-500" />,
  error: <AlertTriangle className="size-4 text-destructive" />,
  warning: <AlertTriangle className="size-4 text-amber-500" />,
};

function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}) {
  return createPortal(
    <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-full max-w-sm flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto flex items-start gap-3 rounded-lg border bg-popover p-3.5 text-popover-foreground shadow-lg animate-slide-in",
            t.variant === "error"
              ? "border-destructive/40"
              : t.variant === "success"
                ? "border-emerald-500/40"
                : "border-border"
          )}
        >
          <div className="mt-0.5">{ICONS[t.variant]}</div>
          <div className="flex-1 space-y-0.5">
            <p className="text-sm font-medium leading-tight">{t.title}</p>
            {t.description && (
              <p className="text-xs text-muted-foreground">
                {t.description}
              </p>
            )}
          </div>
          <button
            onClick={() => onDismiss(t.id)}
            aria-label="Dismiss"
            className="rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
          >
            <X className="size-3.5" />
          </button>
        </div>
      ))}
    </div>,
    document.body
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return ctx;
}
