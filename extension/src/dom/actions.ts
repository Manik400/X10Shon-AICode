/**
 * Action execution against resolved elements. Each action: confirm present → visible → enabled →
 * perform → wait for a meaningful change → report observed value.
 */
import { computeRole, normalizeText } from './accessibility';
import { locate } from './locator';
import { buildSnapshot } from './snapshot';
import type { ActionPayload, ActionResult, BrowserAction } from './types';
import { clickPulse, hideBanner, moveCursorTo, showBanner } from './overlay';

function describe(action: BrowserAction, el: Element | null): string {
  const name = el ? normalizeText((el as HTMLElement).innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '').slice(0, 40) : '';
  switch (action.type) {
    case 'click':
      return `Clicking "${name || 'element'}"`;
    case 'type':
      return `Filling "${name || 'field'}"`;
    case 'select':
      return `Choosing "${String(action.args.value ?? '')}"`;
    case 'check':
      return `Selecting "${name || 'option'}"`;
    case 'upload':
      return 'Attaching resume';
    default:
      return 'Working…';
  }
}

function result(partial: Partial<ActionResult> & { ok: boolean; action_type: string }): ActionResult {
  return { message: '', duration_ms: 0, evidence: {}, ...partial };
}

function fail(action: BrowserAction, category: string, message: string, extra: Partial<ActionResult> = {}): ActionResult {
  return result({ ok: false, action_type: action.type, error_category: category, message, url: location.href, ...extra });
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Set a value in a way React/Vue/Angular controlled inputs notice. */
function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc && desc.set) desc.set.call(el, value);
  else el.value = value;
}

function fire(el: Element, types: string[]): void {
  for (const t of types) {
    const ev = t === 'input' ? new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText' }) : new Event(t, { bubbles: true, cancelable: true });
    el.dispatchEvent(ev);
  }
}

function keyEvents(el: Element, key: string): void {
  const init: KeyboardEventInit = { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, bubbles: true, cancelable: true };
  el.dispatchEvent(new KeyboardEvent('keydown', init));
  el.dispatchEvent(new KeyboardEvent('keypress', init));
  el.dispatchEvent(new KeyboardEvent('keyup', init));
}

async function waitForChange(before: () => string, timeoutMs: number): Promise<boolean> {
  const initial = before();
  const deadline = Date.now() + Math.min(timeoutMs, 4000);
  while (Date.now() < deadline) {
    await sleep(60);
    if (before() !== initial) return true;
  }
  return false;
}

function pageFingerprint(): string {
  return `${location.href}|${document.body?.innerText.length ?? 0}|${document.querySelectorAll('input,select,textarea,button,[role="dialog"]').length}`;
}

function scrollIntoView(el: Element): void {
  try {
    (el as HTMLElement).scrollIntoView({ block: 'center', inline: 'nearest' });
  } catch {
    /* ignore */
  }
}

async function point(action: BrowserAction, el: Element): Promise<void> {
  if (action.args.silent === true) return;
  await moveCursorTo(el, describe(action, el));
  if (action.type === 'click' || action.type === 'check' || action.type === 'select') await clickPulse();
}

function resolve(action: BrowserAction): { el: Element | null; err: ActionResult | null; matched: number } {
  if (!action.target) return { el: null, err: fail(action, 'ELEMENT_NOT_FOUND', 'no target provided'), matched: 0 };
  const loc = locate(document, action.target);
  if (loc.ambiguous) return { el: null, err: fail(action, 'AMBIGUOUS_TARGET', loc.reason, { matched_count: loc.matched }), matched: loc.matched };
  if (!loc.element) return { el: null, err: fail(action, 'ELEMENT_NOT_FOUND', loc.reason, { matched_count: loc.matched }), matched: loc.matched };
  const el = loc.element;
  if ((el as HTMLInputElement).disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('fieldset[disabled]')) {
    return { el: null, err: fail(action, 'ELEMENT_NOT_FOUND', 'element is disabled', { matched_count: loc.matched }), matched: loc.matched };
  }
  return { el, err: null, matched: loc.matched };
}

function selectOption(el: HTMLSelectElement, wanted: string): string | null {
  const w = normalizeText(wanted).toLowerCase();
  const opts = Array.from(el.options);
  let hit = opts.find((o) => normalizeText(o.textContent).toLowerCase() === w || o.value.toLowerCase() === w);
  if (!hit) hit = opts.find((o) => normalizeText(o.textContent).toLowerCase().startsWith(w));
  if (!hit) hit = opts.find((o) => normalizeText(o.textContent).toLowerCase().includes(w));
  if (!hit || hit.disabled) return null;
  el.value = hit.value;
  for (const o of opts) o.selected = o === hit;
  fire(el, ['input', 'change']);
  return normalizeText(hit.textContent) || hit.value;
}

async function selectCustomCombobox(el: Element, wanted: string, timeoutMs: number): Promise<string | null> {
  // ARIA combobox / custom dropdown: open, find an option by text, click it.
  (el as HTMLElement).focus();
  (el as HTMLElement).click();
  const w = normalizeText(wanted).toLowerCase();
  const deadline = Date.now() + Math.min(timeoutMs, 3000);
  while (Date.now() < deadline) {
    const options = Array.from(document.querySelectorAll('[role="option"],li[role="menuitem"],[role="listbox"] li'));
    const hit = options.find((o) => normalizeText(o.textContent).toLowerCase() === w) || options.find((o) => normalizeText(o.textContent).toLowerCase().includes(w));
    if (hit) {
      (hit as HTMLElement).click();
      await sleep(80);
      return normalizeText(hit.textContent);
    }
    await sleep(60);
  }
  // Try typing to filter (react-select style) then Enter.
  const input = (el.matches('input') ? el : el.querySelector('input')) as HTMLInputElement | null;
  if (input) {
    setNativeValue(input, wanted);
    fire(input, ['input']);
    await sleep(150);
    const options = Array.from(document.querySelectorAll('[role="option"]'));
    const hit = options.find((o) => normalizeText(o.textContent).toLowerCase().includes(w));
    if (hit) {
      (hit as HTMLElement).click();
      await sleep(80);
      return normalizeText(hit.textContent);
    }
    keyEvents(input, 'Enter');
    await sleep(80);
    return normalizeText(input.value) || null;
  }
  return null;
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export async function executeAction(action: BrowserAction, payload: ActionPayload | null): Promise<ActionResult> {
  const t0 = performance.now();
  const done = (r: ActionResult): ActionResult => ({ ...r, duration_ms: Math.round(performance.now() - t0), url: r.url ?? location.href, title: document.title });
  try {
    switch (action.type) {
      case 'wait': {
        await sleep(Number(action.args.ms ?? 500));
        return done(result({ ok: true, action_type: 'wait' }));
      }
      case 'snapshot':
        return done(result({ ok: true, action_type: action.type }));
      case 'done':
      case 'ask_user':
        hideBanner();
        return done(result({ ok: true, action_type: action.type }));
      case 'banner': {
        const text = String(action.args.text ?? '');
        if (text) showBanner(text);
        else hideBanner();
        return done(result({ ok: true, action_type: 'banner' }));
      }
      case 'scroll': {
        const dir = String(action.args.direction || 'down');
        const amount = Number(action.args.amount ?? 600);
        if (action.target) {
          const { el, err } = resolve(action);
          if (err) return done(err);
          scrollIntoView(el as Element);
        } else {
          window.scrollBy({ top: dir === 'up' ? -amount : amount, behavior: 'instant' as ScrollBehavior });
        }
        await sleep(100);
        return done(result({ ok: true, action_type: 'scroll', value: window.scrollY }));
      }
      case 'extract': {
        const snap = buildSnapshot(document, { maxNodes: 50, maxTextChars: Number(action.args.max_chars ?? 20000) });
        return done(result({ ok: true, action_type: 'extract', value: { text: snap.text_excerpt, jsonld: snap.jsonld, title: snap.title } }));
      }
      case 'click': {
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        const target = el as HTMLElement;
        scrollIntoView(target);
        await point(action, target);
        const fp = pageFingerprint();
        target.focus();
        target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
        target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
        target.click();
        const changed = await waitForChange(pageFingerprint, action.timeout_ms);
        const role = computeRole(target);
        const ev: Record<string, unknown> = { changed, role, name: target.textContent?.trim().slice(0, 80) };
        if (role === 'checkbox' || role === 'radio') ev.checked = (target as HTMLInputElement).checked;
        return done(result({ ok: true, action_type: 'click', matched_count: matched, value: ev.checked ?? changed, evidence: ev, message: changed ? 'page changed after click' : `clicked; page fingerprint unchanged (${fp === pageFingerprint() ? 'same' : 'different'})` }));
      }
      case 'type': {
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        const text = String(action.args.text ?? '');
        const target = el as HTMLElement;
        scrollIntoView(target);
        await point(action, target);
        target.focus();
        if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
          if (target.readOnly) return done(fail(action, 'ELEMENT_NOT_FOUND', 'field is read-only'));
          if (action.args.clear !== false) {
            setNativeValue(target, '');
            fire(target, ['input']);
          }
          setNativeValue(target, text);
          fire(target, ['input', 'change']);
          target.dispatchEvent(new KeyboardEvent('keyup', { key: text.slice(-1), bubbles: true }));
          if (action.args.blur !== false) {
            target.blur();
            fire(target, ['blur']);
          }
          await sleep(30);
          return done(result({ ok: true, action_type: 'type', matched_count: matched, value: target.value }));
        }
        if (target.isContentEditable) {
          target.focus();
          document.execCommand('selectAll', false);
          document.execCommand('insertText', false, text);
          fire(target, ['input', 'change']);
          return done(result({ ok: true, action_type: 'type', matched_count: matched, value: normalizeText(target.innerText) }));
        }
        return done(fail(action, 'ELEMENT_NOT_FOUND', 'target is not a text field'));
      }
      case 'select': {
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        const wanted = String(action.args.value ?? '');
        scrollIntoView(el as Element);
        await point(action, el as Element);
        if (el instanceof HTMLSelectElement) {
          const picked = selectOption(el, wanted);
          if (picked === null) return done(fail(action, 'ELEMENT_NOT_FOUND', `option "${wanted}" not found`, { matched_count: matched, evidence: { options: Array.from(el.options).map((o) => o.textContent?.trim()).slice(0, 50) } }));
          await sleep(30);
          return done(result({ ok: true, action_type: 'select', matched_count: matched, value: picked }));
        }
        const picked = await selectCustomCombobox(el as Element, wanted, action.timeout_ms);
        if (!picked) return done(fail(action, 'ELEMENT_NOT_FOUND', `option "${wanted}" not found in custom widget`, { matched_count: matched }));
        return done(result({ ok: true, action_type: 'select', matched_count: matched, value: picked }));
      }
      case 'check': {
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        const wanted = action.args.checked !== false;
        const target = el as HTMLElement;
        scrollIntoView(target);
        await point(action, target);
        if (target instanceof HTMLInputElement && (target.type === 'checkbox' || target.type === 'radio')) {
          if (target.checked !== wanted) {
            if (target.type === 'radio' && !wanted) return done(fail(action, 'ELEMENT_NOT_FOUND', 'radio buttons cannot be unchecked'));
            target.click();
            await sleep(30);
            if (target.checked !== wanted) {
              target.checked = wanted;
              fire(target, ['input', 'change']);
            }
          }
          return done(result({ ok: true, action_type: 'check', matched_count: matched, value: target.checked }));
        }
        const ariaRole = computeRole(target);
        if (ariaRole === 'checkbox' || ariaRole === 'radio' || ariaRole === 'switch') {
          const cur = target.getAttribute('aria-checked') === 'true';
          if (cur !== wanted) target.click();
          await sleep(50);
          return done(result({ ok: true, action_type: 'check', matched_count: matched, value: target.getAttribute('aria-checked') === 'true' }));
        }
        return done(fail(action, 'ELEMENT_NOT_FOUND', 'target is not checkable'));
      }
      case 'upload': {
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        if (!payload || !payload.bytes_b64 || !payload.file_name) return done(fail(action, 'PERMISSION_DENIED', 'upload payload was not resolved by the agent'));
        await point(action, el as Element);
        let input: HTMLInputElement | null = el instanceof HTMLInputElement && el.type === 'file' ? el : null;
        if (!input) {
          // Styled upload buttons: find the associated hidden file input.
          const container = (el as HTMLElement).closest('label,div,section,form') || document;
          input = (container.querySelector('input[type="file"]') as HTMLInputElement | null) || (document.querySelector('input[type="file"]') as HTMLInputElement | null);
        }
        if (!input) return done(fail(action, 'ELEMENT_NOT_FOUND', 'no file input associated with target', { matched_count: matched }));
        const bytes = b64ToBytes(payload.bytes_b64);
        const file = new File([bytes], payload.file_name, { type: payload.mime_type || 'application/octet-stream' });
        const dt = new DataTransfer();
        dt.items.add(file);
        try {
          input.files = dt.files;
        } catch {
          return done(fail(action, 'SITE_UNSUPPORTED', 'browser refused programmatic file assignment'));
        }
        fire(input, ['input', 'change']);
        await sleep(100);
        const ok = !!input.files && input.files.length === 1 && input.files[0].name === payload.file_name;
        return done(result({ ok, action_type: 'upload', matched_count: matched, value: ok ? payload.file_name : null, message: ok ? 'file attached' : 'file not attached' }));
      }
      case 'press_key': {
        const key = String(action.args.key);
        let target: Element | null = document.activeElement;
        if (action.target) {
          const { el, err } = resolve(action);
          if (err) return done(err);
          target = el;
          (el as HTMLElement).focus();
        }
        const fp = pageFingerprint();
        keyEvents(target || document.body, key);
        if (key === 'Enter' && target instanceof HTMLInputElement && target.form) {
          target.form.requestSubmit?.();
        }
        const changed = await waitForChange(pageFingerprint, action.timeout_ms);
        return done(result({ ok: true, action_type: 'press_key', value: changed, message: changed ? 'page changed' : `no change (${fp === pageFingerprint()})` }));
      }
      case 'navigate':
        return done(fail(action, 'PERMISSION_DENIED', 'navigation is performed by the service worker / driver, not in-page'));
      default:
        return done(fail(action, 'UNKNOWN', `unsupported action ${(action as BrowserAction).type}`));
    }
  } catch (e) {
    return done(fail(action, 'UNKNOWN', (e as Error).message?.slice(0, 300) || 'action threw'));
  }
}
