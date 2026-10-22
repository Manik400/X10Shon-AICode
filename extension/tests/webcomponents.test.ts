/**
 * Replicas of SmartRecruiters' Lit web components (upload box, country-code picker), built from the
 * live page's DOM, driven through the real action code: these widgets defeated the agent three times.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { executeAction } from '../src/dom/actions';
import { buildSnapshot } from '../src/dom/snapshot';
import type { BrowserAction } from '../src/dom/types';

// jsdom has no PointerEvent: a minimal one, so handlers that insist on a pointer type can be tested.
class FakePointerEvent extends MouseEvent {
  pointerType: string;
  pointerId: number;
  constructor(type: string, init: MouseEventInit & { pointerType?: string; pointerId?: number } = {}) {
    super(type, init);
    this.pointerType = init.pointerType || '';
    this.pointerId = init.pointerId ?? 0;
  }
}
(globalThis as unknown as { PointerEvent: unknown }).PointerEvent = FakePointerEvent;

// jsdom has no DataTransfer and its file inputs reject assignment: minimal stand-ins so the
// upload path can run; `files` becomes a plain settable property on the test's inputs.
class FakeDataTransfer {
  files: File[] = [];
  items = { add: (f: File) => void this.files.push(f) };
}
(globalThis as unknown as { DataTransfer: unknown }).DataTransfer = FakeDataTransfer;
function settableFiles(input: HTMLInputElement): void {
  let current: File[] = [];
  Object.defineProperty(input, 'files', { get: () => current, set: (v: File[]) => void (current = Array.from(v || [])), configurable: true });
  Object.defineProperty(input, 'value', { get: () => (current[0] ? 'C:\\fakepath\\' + current[0].name : ''), set: (v: string) => void (v === '' && (current = [])), configurable: true });
}

beforeEach(() => {
  document.body.innerHTML = '';
  // jsdom has no layout: elements are "visible" unless an ancestor (or host) is hidden.
  const hiddenUp = (el: Element): boolean => {
    let n: Element | null = el;
    while (n) {
      if (n.hasAttribute('hidden') || /display:\s*none/.test(n.getAttribute('style') || '')) return true;
      n = n.parentElement || ((n.getRootNode() as ShadowRoot).host ?? null);
    }
    return false;
  };
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    return (hiddenUp(this) ? [] : [{ width: 10, height: 10 }]) as unknown as DOMRectList;
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const w = hiddenUp(this) ? 0 : 10;
    return { width: w, height: w, top: 0, left: 0, right: w, bottom: w, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
  });
  (HTMLElement.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView = () => undefined;
});

function action(partial: Partial<BrowserAction> & { type: BrowserAction['type'] }): BrowserAction {
  return { args: {}, risk: 'low', verification: 'none', ...partial } as BrowserAction;
}

/** <spl-dropzone>: reads the file in its change handler, clears the input, shows the name. */
function makeDropzone(): HTMLElement {
  const host = document.createElement('spl-dropzone');
  const sr = host.attachShadow({ mode: 'open' });
  sr.innerHTML = `<div class="c-spl-dropzone-wrapper"><input type="file" id="spl-dropzone-file-input-1"><label for="spl-dropzone-file-input-1">Choose a file or drop it here</label><div class="shown"></div></div>`;
  const input = sr.querySelector('input')!;
  settableFiles(input);
  input.addEventListener('change', () => {
    const name = input.files?.[0]?.name || '';
    setTimeout(() => {
      sr.querySelector('.shown')!.textContent = name;
      input.value = ''; // the site lets the same file be chosen again
    }, 120);
  });
  document.body.appendChild(host);
  return host;
}

/** <spl-select> country-code picker: button[role=combobox] + slotted <spl-select-option> items. */
function makeCountryPicker(labels: string[]): HTMLElement {
  const host = document.createElement('spl-select') as HTMLElement & { value: string };
  host.value = 'IN';
  const sr = host.attachShadow({ mode: 'open' });
  sr.innerHTML = `<spl-dropdown><button class="c-spl-select-field" role="combobox" aria-haspopup="listbox" aria-label="Country code" id="spl-form-element_12" aria-expanded="false" aria-controls="spl-form-element_12-menu"></button><div id="spl-form-element_12-menu" role="listbox" hidden><spl-dropdown-search></spl-dropdown-search><slot></slot></div></spl-dropdown>`;
  const button = sr.querySelector('button')!;
  const menu = sr.querySelector('#spl-form-element_12-menu')!;
  const search = sr.querySelector('spl-dropdown-search')!.attachShadow({ mode: 'open' });
  search.innerHTML = `<input type="text" role="combobox" placeholder="Search by country/region or code" aria-controls="spl-form-element_12-menu">`;
  const searchInput = search.querySelector('input')!;
  // Opens only on a real PointerEvent with a pointer type (Lit's handler), not a bare click().
  button.addEventListener('pointerdown', (e) => {
    if (!(e as { pointerType?: string }).pointerType) return;
    const open = menu.hasAttribute('hidden');
    if (open) menu.removeAttribute('hidden');
    else menu.setAttribute('hidden', '');
    button.setAttribute('aria-expanded', String(open));
  });
  for (const label of labels) {
    const opt = document.createElement('spl-select-option');
    opt.setAttribute('label', label);
    opt.setAttribute('value', label === 'India' ? 'IN' : label.slice(0, 2).toUpperCase());
    opt.textContent = label;
    const osr = opt.attachShadow({ mode: 'open' });
    const item = document.createElement('spl-dropdown-item');
    const isr = item.attachShadow({ mode: 'open' });
    isr.innerHTML = `<div role="option"><slot></slot></div>`;
    item.appendChild(document.createElement('slot'));
    osr.appendChild(item);
    isr.querySelector('[role="option"]')!.addEventListener('click', () => {
      host.value = opt.getAttribute('value')!;
      menu.setAttribute('hidden', '');
      button.setAttribute('aria-expanded', 'false');
    });
    host.appendChild(opt); // light DOM, slotted into the menu
  }
  searchInput.addEventListener('input', () => {
    const q = searchInput.value.toLowerCase();
    for (const opt of Array.from(host.querySelectorAll('spl-select-option'))) {
      if ((opt.getAttribute('label') || '').toLowerCase().includes(q)) opt.removeAttribute('hidden');
      else opt.setAttribute('hidden', '');
    }
  });
  document.body.appendChild(host);
  return host;
}

describe('SmartRecruiters-style web components', () => {
  it('attaches a resume to a dropzone that clears its input after reading the file', async () => {
    makeDropzone();
    const snap = buildSnapshot(document, { maxNodes: 50 });
    const node = snap.nodes.find((n) => n.type === 'file')!;
    expect(node).toBeDefined();
    const r = await executeAction(action({ type: 'upload', target: { strategy: 'node', value: String(node.i) }, args: { resume_id: 'r' } }), { bytes_b64: btoa('%PDF-1.4'), file_name: 'Manik_Goyal_Resume.pdf', mime_type: 'application/pdf' });
    expect({ ok: r.ok, value: r.value, message: r.message, category: r.error_category ?? null }).toEqual({ ok: true, value: 'Manik_Goyal_Resume.pdf', message: expect.any(String), category: null });
  });

  it('picks India in a slotted, shadow-DOM country-code list that opens on pointerdown', async () => {
    const host = makeCountryPicker(['Afghanistan', 'British Indian Ocean Territory', 'India', 'Indonesia']);
    const snap = buildSnapshot(document, { maxNodes: 80 });
    const node = snap.nodes.find((n) => n.name === 'Country code' && n.role === 'combobox')!;
    expect(node).toBeDefined();
    const r = await executeAction(action({ type: 'select', target: { strategy: 'node', value: String(node.i) }, args: { value: 'India' } }), null); // no timeout_ms: the agent omits it
    expect(r).toMatchObject({ ok: true });
    expect((host as HTMLElement & { value: string }).value).toBe('IN');
    expect(String(r.value)).toMatch(/india/i);
  });
});
