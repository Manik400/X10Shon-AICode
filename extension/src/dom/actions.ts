/**
 * Action execution against resolved elements. Each action: confirm present → visible → enabled →
 * perform → wait for a meaningful change → report observed value.
 */
import { computeRole, normalizeText } from './accessibility';
import { choiceQuestion, choiceSelected } from './choice';
import { locate } from './locator';
import { hidden, wait } from './timing';
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

const sleep = wait; // not throttled in background tabs

/** Set a value in a way React/Vue/Angular controlled inputs notice. */
function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc && desc.set) desc.set.call(el, value);
  else el.value = value;
}

function fire(el: Element, types: string[]): void {
  for (const t of types) {
    // composed: inputs inside web components (SmartRecruiters) are listened to by their host.
    const ev = t === 'input' ? new InputEvent('input', { bubbles: true, cancelable: true, composed: true, inputType: 'insertText' }) : new Event(t, { bubbles: true, cancelable: true, composed: true });
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
  if (action.args.silent === true || hidden()) return; // nobody can see the cursor in a background tab
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

const DECLINE_RE = /declin|prefer not|rather not|do(n['’]?t| not) (wish|want) to|not (to )?(answer|disclose|say|specify)|choose not/i;

/** The option text begins with the whole wanted value ("india" → "india +91", not "british indian…"). */
function startsWithWord(text: string, w: string): boolean {
  return text.startsWith(w) && (text.length === w.length || !/[\p{L}\p{N}]/u.test(text[w.length]));
}

/** Best option for a wanted value: exact, decline wording, or an option that BEGINS with the value.
 * Never an option that merely contains it somewhere. */
export function matchOption(options: Element[], wanted: string): Element | null {
  const w = normalizeText(wanted).toLowerCase();
  if (!w) return null;
  const text = (o: Element) => optionText(o).toLowerCase();
  const exact = options.find((o) => text(o) === w);
  if (exact) return exact;
  if (DECLINE_RE.test(w)) return options.find((o) => DECLINE_RE.test(text(o))) || null;
  return options.find((o) => startsWithWord(text(o), w)) || null;
}

/** What a custom dropdown shows as chosen (react-select single/multi value), or ''. */
function displayedValue(el: Element): string {
  let scope: Element | null = el;
  for (let depth = 0; depth < 6 && scope; depth++, scope = scope.parentElement) {
    const shown = scope.querySelectorAll('[class*="singleValue" i],[class*="single-value" i],[class*="multiValue__label" i],[class*="multi-value__label" i],[class*="multiValue" i] > div:first-child');
    if (shown.length) return Array.from(shown).map((s) => normalizeText(s.textContent)).filter(Boolean).join(', ');
  }
  return '';
}

/** An option's visible text. Web-component lists (SmartRecruiters) render each option as an
 * empty [role=option] box whose text is slotted in from its host element. */
export function optionText(o: Element): string {
  let t = normalizeText(o.textContent);
  let host: Element | null = o;
  for (let depth = 0; depth < 3 && !t && host; depth++) {
    host = (host.getRootNode() as ShadowRoot).host ?? null;
    if (host) t = normalizeText(host.textContent);
  }
  return t;
}

/** querySelectorAll that also descends into open shadow roots. */
export function deepQuery(scope: ParentNode, selector: string): Element[] {
  const out: Element[] = [];
  const seen = new Set<Element>();
  const add = (el: Element) => {
    if (!seen.has(el)) {
      seen.add(el);
      out.push(el);
    }
  };
  const walk = (root: ParentNode) => {
    for (const el of Array.from(root.querySelectorAll(selector))) add(el);
    for (const host of Array.from(root.querySelectorAll('*'))) if (host.shadowRoot) walk(host.shadowRoot);
    // Light-DOM children slotted into a shadow tree are not descendants of the <slot>: follow them.
    for (const slot of Array.from(root.querySelectorAll('slot')) as HTMLSlotElement[]) {
      for (const a of slot.assignedElements({ flatten: true })) {
        if (a.matches(selector)) add(a);
        walk(a);
        if (a.shadowRoot) walk(a.shadowRoot);
      }
    }
  };
  walk(scope);
  if ((scope as Element).shadowRoot) walk((scope as Element).shadowRoot as ShadowRoot);
  return out;
}

function listFor(el: Element): Element | null {
  const listId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (!listId) return null;
  const byId = (scope: ParentNode): Element | null => deepQuery(scope, '[id]').find((e) => e.id === listId) || null;
  // Nested web components (SmartRecruiters: input inside <spl-input> inside <spl-autocomplete>,
  // whose shadow tree holds the menu): look in each enclosing shadow tree before the whole page.
  let root: Node = el.getRootNode();
  for (let depth = 0; depth < 6 && root instanceof ShadowRoot; depth++) {
    const hit = byId(root);
    if (hit) return hit;
    root = root.host.getRootNode();
  }
  return el.ownerDocument.getElementById(listId) || byId(el.ownerDocument);
}

function visibleOptions(el: Element, list?: Element | null): Element[] {
  const scope: ParentNode = (list === undefined ? listFor(el) : list) || el.ownerDocument;
  return deepQuery(scope, '[role="option"],li[role="menuitem"],[role="listbox"] li').filter((o) => o.getClientRects().length > 0 && o.getAttribute('aria-disabled') !== 'true');
}

/** The search box some dropdowns show inside their open list (SmartRecruiters "Search by
 * country/region or code"): typing there filters the options. */
function listSearchInput(el: Element): HTMLInputElement | null {
  const scope: ParentNode = listFor(el) || el.ownerDocument;
  const inputs = deepQuery(scope, 'input:not([type]),input[type="text"],input[type="search"]') as HTMLInputElement[];
  return inputs.find((i) => i.getClientRects().length > 0 && (i.getAttribute('role') === 'combobox' || i.getAttribute('role') === 'searchbox' || /search|filter|type to/i.test(i.placeholder || i.getAttribute('aria-label') || ''))) || null;
}

async function pollOption(el: Element, wanted: string, ms: number): Promise<Element | null> {
  const deadline = Date.now() + ms;
  // Resolve the option list once: a whole-page search through hundreds of shadow roots on every
  // poll is what made selections time out on component-heavy forms.
  const list = listFor(el);
  do {
    const hit = matchOption(visibleOptions(el, list), wanted);
    if (hit) return hit;
    await sleep(120);
  } while (Date.now() < deadline);
  return null;
}

function pressOn(el: Element): void {
  // react-select and most custom dropdowns open on mousedown, not click. Web components (Lit:
  // SmartRecruiters) listen for real PointerEvents with a pointer type, and need the events to
  // cross their shadow boundary (composed).
  const pointer = { bubbles: true, cancelable: true, composed: true, button: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true };
  const mouse = { bubbles: true, cancelable: true, composed: true, button: 0 };
  const PE = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
  el.dispatchEvent(new PE('pointerdown', pointer));
  el.dispatchEvent(new MouseEvent('mousedown', mouse));
  el.dispatchEvent(new PE('pointerup', pointer));
  el.dispatchEvent(new MouseEvent('mouseup', mouse));
  el.dispatchEvent(new MouseEvent('click', mouse));
}

// Only options that really mean the employer's own site / LinkedIn / a job board. Never "Google".
const LOOSE_SYNONYMS: Array<[RegExp, RegExp]> = [
  [/website|career/i, /\bwebsite\b|careers? (site|page|website|portal)|company (site|page|website)/i],
  [/linkedin/i, /linkedin/i],
  [/job board/i, /job board|indeed|glassdoor|naukri/i],
];

function looseOption(el: Element, wanted: string): Element | null {
  const pairs = LOOSE_SYNONYMS.filter(([want]) => want.test(wanted));
  return visibleOptions(el).find((o) => pairs.some(([, syn]) => syn.test(normalizeText(o.textContent)))) || null;
}

let lastSelectTrace: string[] = [];

async function selectCustomCombobox(el: Element, wanted: string, timeoutMs: number, loose = false): Promise<string | null> {
  const t0 = performance.now();
  const trace: string[] = [];
  lastSelectTrace = trace;
  const mark = (what: string) => trace.push(`${Math.round(performance.now() - t0)}ms ${what}`);
  mark(`el=${el.tagName.toLowerCase()} role=${el.getAttribute('role') || ''} ctrls=${el.getAttribute('aria-controls') || ''} inShadow=${el.getRootNode() !== document} timeout=${timeoutMs}`);
  // Searchable dropdown: type the answer first so the list is short and exact (long lists render
  // only part of their options and recycle option elements while scrolling, so clicking into an
  // unfiltered list can pick a different entry), pick the matching option, then report what the
  // dropdown actually DISPLAYS so the agent verifies the real selection.
  let input = (el.matches('input') ? el : el.querySelector('input')) as HTMLInputElement | null;
  const control = (el.closest('[class*="control" i]') as HTMLElement | null) || (el as HTMLElement);
  (input || (el as HTMLElement)).focus();
  pressOn(control);
  if (input) keyEvents(input, 'ArrowDown');
  mark(`pressed; expanded=${el.getAttribute('aria-expanded') || ''} list=${listFor(el)?.id || 'none'}`);
  if (!input) {
    await sleep(250);
    input = listSearchInput(el);
    if (input) input.focus();
    mark(`search input=${input ? input.placeholder || input.id || 'yes' : 'none'} expanded=${el.getAttribute('aria-expanded') || ''}`);
  }
  const pick = async (hit: Element): Promise<string> => {
    const label = optionText(hit); // slotted options have empty textContent of their own
    pressOn(hit);
    await sleep(150);
    return displayedValue(el) || (input && normalizeText(input.value)) || label;
  };
  const filter = DECLINE_RE.test(wanted) || loose ? '' : wanted; // decline / referral wording varies: list all
  if (input && filter) {
    setNativeValue(input, filter);
    fire(input, ['input']);
  }
  // The agent may send no timeout (NaN budget = a single poll): always allow 3-6 s for options.
  const budget = Number.isFinite(timeoutMs) ? timeoutMs : 3000;
  mark(`typed filter=${JSON.stringify(input && filter ? filter : '')}; polling ${Math.min(Math.max(budget, 3000), 6000)}ms`);
  let hit = await pollOption(el, wanted, Math.min(Math.max(budget, 3000), 6000));
  mark(`poll done hit=${hit ? JSON.stringify(optionText(hit).slice(0, 30)) : 'none'} visible=${visibleOptions(el).map((o) => optionText(o).slice(0, 20)).slice(0, 6).join('|')}`);
  if (!hit && loose) hit = looseOption(el, wanted);
  if (hit) return pick(hit);
  if (input && filter && el.matches('input') && visibleOptions(el).length === 0 && normalizeText(input.value) === normalizeText(filter)) {
    // A type-ahead with no suggestion at all for this answer (a university missing from the
    // site's list): a person keeps what they typed. Strict pickers reject it at validation, which
    // the agent reports; suggestion lists that DID show other entries are never overridden.
    keyEvents(input, 'Escape');
    return normalizeText(input.value);
  }
  if (input) {
    setNativeValue(input, '');
    fire(input, ['input']);
    keyEvents(input, 'Escape');
  }
  return null;
}

/**
 * The hidden <input type=file> behind a styled upload box ("Select the attachment" + Browse):
 * the nearest one walking up from the box, then anywhere in the same shadow root, then the page.
 * Inputs that accept documents (pdf/doc) win over image-only ones.
 */
export function findFileInput(el: Element): HTMLInputElement | null {
  const pick = (scope: ParentNode): HTMLInputElement | null => {
    const all = Array.from(scope.querySelectorAll<HTMLInputElement>('input[type="file"]'));
    return all.find((i) => /pdf|doc|application|\*/i.test(i.accept || '*')) || all[0] || null;
  };
  let node: Element | null = el;
  for (let depth = 0; depth < 8 && node; depth++, node = node.parentElement) {
    const hit = pick(node);
    if (hit) return hit;
  }
  const root = el.getRootNode() as Document | ShadowRoot;
  return pick(root) || (root !== document ? pick(document) : null);
}

/** Whether the page now shows the attached file's name near the upload box (walking up through
 * parents and shadow hosts, since styled boxes clear the input after reading it). */
export function fileNameShown(input: Element, fileName: string): boolean {
  const needle = fileName.trim().toLowerCase();
  if (!needle) return false;
  const textOf = (el: Element): string => {
    const own = el.textContent || '';
    const shadow = el.shadowRoot ? el.shadowRoot.textContent || '' : '';
    return (own + ' ' + shadow).toLowerCase();
  };
  let node: Element | null = input;
  for (let depth = 0; depth < 8 && node; depth++) {
    if (textOf(node).includes(needle)) return true;
    const parent: Element | null = node.parentElement || ((node.getRootNode() as ShadowRoot).host ?? null);
    node = parent;
  }
  if (!input.isConnected) {
    // The box re-rendered itself after taking the file (SmartRecruiters' autofill box): look for
    // the name anywhere on the page, shadow roots included.
    const roots: ParentNode[] = [document];
    for (const host of deepQuery(document, '*')) if (host.shadowRoot) roots.push(host.shadowRoot);
    return roots.some((r) => (r.textContent || '').toLowerCase().includes(needle));
  }
  return false;
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
        const picked = await selectCustomCombobox(el as Element, wanted, action.timeout_ms, action.args.loose === true);
        if (!picked) return done(fail(action, 'ELEMENT_NOT_FOUND', `option "${wanted}" not found in custom widget`, { matched_count: matched, evidence: { options: visibleOptions(el as Element).map((o) => optionText(o)).slice(0, 30), trace: lastSelectTrace.slice(0, 12) } }));
        return done(result({ ok: true, action_type: 'select', matched_count: matched, value: picked }));
      }
      case 'read_options': {
        // Open a searchable dropdown just long enough to list its options; selects nothing.
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        const target = el as Element;
        if (target instanceof HTMLSelectElement) {
          const opts = Array.from(target.options).map((o) => normalizeText(o.textContent)).filter(Boolean);
          return done(result({ ok: true, action_type: 'read_options', matched_count: matched, value: opts.length, evidence: { options: opts.slice(0, 200) } }));
        }
        const input = (target.matches('input') ? target : target.querySelector('input')) as HTMLInputElement | null;
        const control = (target.closest('[class*="control" i]') as HTMLElement | null) || (target as HTMLElement);
        scrollIntoView(target);
        (input || (target as HTMLElement)).focus();
        pressOn(control);
        if (input) keyEvents(input, 'ArrowDown');
        let opts: string[] = [];
        const deadline = Date.now() + 1500;
        while (Date.now() < deadline) {
          opts = visibleOptions(target).map((o) => normalizeText(o.textContent)).filter(Boolean);
          if (opts.length) break;
          await sleep(80);
        }
        if (input) keyEvents(input, 'Escape');
        (input || (target as HTMLElement)).blur();
        // Dropdowns and country pickers close on an outside press; leave nothing open behind.
        if (target.ownerDocument.body) pressOn(target.ownerDocument.body);
        await sleep(60);
        return done(result({ ok: true, action_type: 'read_options', matched_count: matched, value: opts.length, evidence: { options: Array.from(new Set(opts)).slice(0, 200) } }));
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
        if (choiceQuestion(target)) {
          // Yes/No answer button: click it; confirm by a selected marker or a class change.
          if (!wanted) return done(fail(action, 'ELEMENT_NOT_FOUND', 'choice buttons cannot be unchecked'));
          if (choiceSelected(target)) return done(result({ ok: true, action_type: 'check', matched_count: matched, value: true }));
          const before = typeof target.className === 'string' ? target.className : '';
          target.click();
          await sleep(150);
          const after = typeof target.className === 'string' ? target.className : '';
          const selected = target.isConnected && (choiceSelected(target) || (after !== before && after.length > before.length));
          return done(result({ ok: true, action_type: 'check', matched_count: matched, value: selected, evidence: { class_before: before.slice(0, 120), class_after: after.slice(0, 120) } }));
        }
        return done(fail(action, 'ELEMENT_NOT_FOUND', 'target is not checkable'));
      }
      case 'upload': {
        const { el, err, matched } = resolve(action);
        if (err) return done(err);
        if (!payload || !payload.bytes_b64 || !payload.file_name) return done(fail(action, 'PERMISSION_DENIED', 'upload payload was not resolved by the agent'));
        await point(action, el as Element);
        let input: HTMLInputElement | null = el instanceof HTMLInputElement && el.type === 'file' ? el : null;
        if (!input) input = findFileInput(el as Element);
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
        // Many upload boxes (SmartRecruiters, Workday) read the file in their change handler and
        // then clear the input so the same file can be chosen again: the file name shown next to
        // the box (usually within a second) is then the proof that it was taken.
        let retained = false;
        let ok = false;
        let consumed = false; // the box re-rendered itself after taking the file (autofill boxes parse it)
        for (let i = 0; i < 25 && !ok; i++) {
          await sleep(i === 0 ? 150 : 100);
          retained = !!input.files && input.files.length === 1 && input.files[0].name === payload.file_name;
          consumed = !input.isConnected;
          ok = retained || fileNameShown(input, payload.file_name) || consumed;
        }
        const how = retained ? 'file attached' : consumed ? 'file attached (the box re-rendered after taking it)' : 'file attached (shown by the page)';
        const parent = input.parentElement || (input.getRootNode() as ShadowRoot).host;
        const evidence = { retained, consumed, files: input.files ? input.files.length : -1, connected: input.isConnected, input_id: input.id, parent: parent ? `${parent.tagName.toLowerCase()}.${String(parent.className).slice(0, 40)}` : '', nearby_text: (parent?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120), target: (el as Element).tagName.toLowerCase() };
        return done(result({ ok, action_type: 'upload', matched_count: matched, value: ok ? payload.file_name : null, message: ok ? how : 'file not attached', evidence }));
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
