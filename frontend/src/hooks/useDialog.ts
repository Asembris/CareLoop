import { useEffect } from "react";
import type { RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Every open dialog panel, in DOM order. DOM order tracks stacking here: the lens is rendered
 *  after the drawer in `App` and sits above it, so the last one is the one on top. */
function openDialogs(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>("[data-cl-dialog]"));
}

/**
 * Dialog behaviour for the two drawers: initial focus, a focus trap, Escape to close, and focus
 * returned to the opener on close.
 *
 * One document-level listener per open dialog, each of which acts only when it is the *topmost*
 * open dialog — the lens can open over the drawer, and Escape must close the one on top rather
 * than both. Checked by DOM order at keypress time, not at mount, because which dialog is on top
 * changes while both listeners exist.
 *
 * The panel itself receives initial focus (it needs `tabIndex={-1}`), so a screen reader
 * announces the dialog's label before its first control rather than a bare "Close, button".
 */
export function useDialog(ref: RefObject<HTMLElement | null>, onClose: () => void): void {
  useEffect(() => {
    const node = ref.current;
    if (!node) return;

    node.dataset.clDialog = "true";
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    node.focus();

    function onKeyDown(event: KeyboardEvent) {
      const stack = openDialogs();
      if (stack[stack.length - 1] !== node) return;

      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !node) return;

      const items = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && (active === first || active === node)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (active !== null && !node.contains(active)) {
        // Focus escaped (e.g. the opener was re-focused by other code): pull it back in.
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      delete node.dataset.clDialog;
      opener?.focus();
    };
  }, [ref, onClose]);
}
