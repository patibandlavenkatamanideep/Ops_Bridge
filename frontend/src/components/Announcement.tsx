import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

/**
 * A status line that takes focus when it says something. After an action whose button goes
 * away or is disabled (Approve, Create proposal, Take this case, a run that finishes), focus
 * would otherwise drop to the page and a keyboard or screen-reader user would lose their
 * place; this says what happened and keeps them where it happened.
 */
export function useAnnouncement(): [ReactNode, (message: string) => void] {
  const [said, setSaid] = useState<{ message: string; n: number } | null>(null);
  const ref = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (said) ref.current?.focus();
  }, [said]);
  const node = said ? (
    <p ref={ref} role="status" tabIndex={-1} className="announcement small">
      {said.message}
    </p>
  ) : null;
  const announce = useCallback(
    (message: string) => setSaid((prev) => ({ message, n: (prev?.n ?? 0) + 1 })),
    [],
  );
  return [node, announce];
}
