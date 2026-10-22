/**
 * Choice buttons: questions rendered as a label plus a row of short buttons ("Yes" / "No")
 * instead of native radios (Ashby, some Lever/Workable forms). The snapshot reports each button
 * as a radio whose section is the question, so the agent plans them like a radio group.
 */
import { normalizeText } from './accessibility';

const MAX_OPTION_CHARS = 40;
// Only answer words: a row of "Open" / "Apply" / pagination buttons is never a question.
const ANSWER_RE = /^(yes|no|maybe|not sure|true|false|n\/a|prefer not to (say|answer|disclose)|decline( to (answer|self[- ]identify))?|i don'?t wish to answer)$/i;
const NOT_CHOICE_RE = /\b(submit|apply|next|back|continue|upload|attach|dismiss|cancel|close|save|remove|delete|edit|add|search)\b/i;
const SELECTED_CLASS_RE = /(^|[\s_-])(active|selected|checked|chosen|is-on)([\s_-]|$)/i;

function buttonText(el: Element): string {
  return normalizeText((el as HTMLElement).innerText || el.textContent);
}

function isButton(el: Element): boolean {
  return el.tagName === 'BUTTON' || el.getAttribute('role') === 'button';
}

function isOption(el: Element): boolean {
  const t = buttonText(el);
  return isButton(el) && !!t && t.length <= MAX_OPTION_CHARS && !NOT_CHOICE_RE.test(t) && ANSWER_RE.test(t);
}

/** The option buttons of the group `el` belongs to (2-6 short sibling buttons), or null. */
function groupOf(el: Element): Element[] | null {
  if (!isOption(el)) return null;
  const parent = el.parentElement;
  if (!parent) return null;
  const buttons = Array.from(parent.children).filter(isButton);
  if (buttons.length < 2 || buttons.length > 6 || !buttons.every(isOption)) return null;
  const words = buttons.map((b) => buttonText(b).toLowerCase());
  if (!words.includes('yes') || !words.includes('no')) return null;
  return buttons;
}

/** Question text for a choice button, or '' when `el` is not part of a choice group. */
export function choiceQuestion(el: Element): string {
  const group = groupOf(el);
  if (!group) return '';
  const row = el.parentElement!;
  // Walk up a few levels looking for the field's label: a <label>/legend, or the nearest
  // preceding text-only sibling of the button row.
  let scope: Element | null = row;
  for (let depth = 0; depth < 4 && scope; depth++) {
    const container: Element | null = scope.parentElement;
    if (!container) break;
    const label = container.querySelector(':scope > label, :scope > legend, :scope > [class*="label" i]');
    if (label && !label.contains(row)) {
      const t = normalizeText(label.textContent);
      if (t) return t.replace(/[\s:*✱]+$/g, '').slice(0, 200);
    }
    let prev = scope.previousElementSibling;
    while (prev) {
      if (!prev.querySelector('button,input,select,textarea')) {
        const t = normalizeText((prev as HTMLElement).innerText || prev.textContent);
        if (t && t.length <= 300) return t.replace(/[\s:*✱]+$/g, '');
      }
      prev = prev.previousElementSibling;
    }
    scope = container;
  }
  return '';
}

export function choiceSelected(el: Element): boolean {
  if (el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true') return true;
  if (el.getAttribute('data-state') === 'on' || el.getAttribute('data-selected') === 'true') return true;
  return SELECTED_CLASS_RE.test(typeof el.className === 'string' ? el.className : '');
}

/** True when the question's field container marks it required (asterisk or a required input). */
export function choiceRequired(el: Element, question: string): boolean {
  if (/[*✱]\s*$/.test(question)) return true;
  let c: Element | null = el.parentElement;
  for (let depth = 0; depth < 4 && c; depth++, c = c.parentElement) {
    if (c.querySelector('input[required],[aria-required="true"]')) return true;
    const label = c.querySelector(':scope > label');
    if (label && /[*✱]/.test(label.textContent || '')) return true;
  }
  return false;
}
