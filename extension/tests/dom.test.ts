/**
 * dom-agent unit tests under jsdom: snapshot, locator and actions on inline fixtures.
 * (Layout-dependent visibility is covered by the Playwright tests in agent/tests.)
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { buildSnapshot, radioGroupLabel } from '../src/dom/snapshot';
import { locate } from '../src/dom/locator';
import { executeAction } from '../src/dom/actions';
import { computeAccessibleName, computeRole, findLabelText } from '../src/dom/accessibility';
import { collectSignals } from '../src/dom/signals';
import type { BrowserAction } from '../src/dom/types';

// jsdom has no layout: give every element a non-empty box so isVisible() passes.
beforeEach(() => {
  document.body.innerHTML = '';
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    const hidden = this.closest('[hidden],[style*="display: none"],[style*="display:none"]');
    return (hidden ? [] : [{ width: 10, height: 10 }]) as unknown as DOMRectList;
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
});

function action(partial: Partial<BrowserAction> & { type: BrowserAction['type'] }): BrowserAction {
  return { args: {}, timeout_ms: 1000, risk: 'low', verification: 'none', ...partial };
}

describe('accessibility', () => {
  it('computes roles and names from labels, aria and placeholders', () => {
    document.body.innerHTML = `
      <label for="a">First name *</label><input id="a">
      <label>Email <input id="b" type="email"></label>
      <input id="c" aria-label="Phone number">
      <input id="d" placeholder="City">
      <div id="lbl">Country</div><select id="e" aria-labelledby="lbl"><option>DE</option></select>
      <button id="f"><img alt="Search"></button>
      <a id="g" href="/x">Apply now</a>`;
    const byId = (id: string) => document.getElementById(id)!;
    expect(computeRole(byId('a'))).toBe('textbox');
    expect(computeAccessibleName(byId('a'), 'textbox')).toBe('First name *');
    expect(findLabelText(byId('b'))).toBe('Email');
    expect(computeAccessibleName(byId('c'), 'textbox')).toBe('Phone number');
    expect(computeAccessibleName(byId('d'), 'textbox')).toBe('City');
    expect(computeRole(byId('e'))).toBe('combobox');
    expect(computeAccessibleName(byId('e'), 'combobox')).toBe('Country');
    expect(computeAccessibleName(byId('f'), 'button')).toBe('Search');
    expect(computeRole(byId('g'))).toBe('link');
    expect(computeAccessibleName(byId('g'), 'link')).toBe('Apply now');
  });

  it('derives radio group questions without fieldsets', () => {
    document.body.innerHTML = `<div><div class="q">Will you require sponsorship?<span>*</span></div>
      <label><input type="radio" name="s" value="y"> Yes</label><label><input type="radio" name="s" value="n"> No</label></div>`;
    const radio = document.querySelector('input') as HTMLInputElement;
    expect(radioGroupLabel(radio)).toBe('Will you require sponsorship?');
  });
});

describe('snapshot', () => {
  it('lists interactive nodes with sections, forms and signals', () => {
    document.body.innerHTML = `
      <form id="f"><fieldset><legend>Contact</legend><label for="n">Name</label><input id="n" required></fieldset>
      <select id="s"><option>One</option><option>Two</option></select>
      <input type="password" id="p"><button type="submit">Submit application</button></form>
      <div role="alert">Something went wrong</div>`;
    const snap = buildSnapshot(document, { maxNodes: 50 });
    const name = snap.nodes.find((n) => n.name === 'Name')!;
    expect(name.role).toBe('textbox');
    expect(name.required).toBe(true);
    expect(name.section).toBe('Contact');
    expect(snap.nodes.find((n) => n.role === 'combobox')!.options).toEqual(['One', 'Two']);
    expect(snap.forms[0].submit_nodes.length).toBe(1);
    expect(snap.forms[0].fields.length).toBe(3);
    expect(snap.signals.has_password_field).toBe(true);
    expect(snap.signals.error_messages).toContain('Something went wrong');
    expect(snap.nodes.find((n) => n.id === 'p')!.value).toBeUndefined(); // password values never leave the page
  });

  it('truncates at maxNodes and reports it', () => {
    document.body.innerHTML = Array.from({ length: 30 }, (_, i) => `<button>b${i}</button>`).join('');
    const snap = buildSnapshot(document, { maxNodes: 10 });
    expect(snap.nodes.length).toBe(10);
    expect(snap.truncated).toBe(true);
  });

  it('extracts JobPosting JSON-LD', () => {
    document.body.innerHTML = `<script type="application/ld+json">{"@type":"JobPosting","title":"Dev"}</script>`;
    const snap = buildSnapshot(document);
    expect(snap.jsonld[0].title).toBe('Dev');
    expect(snap.signals.has_jsonld_jobposting).toBe(true);
  });
});

describe('signals', () => {
  it('detects captcha, otp and recovery pages', () => {
    document.body.innerHTML = `<div class="g-recaptcha"></div>`;
    expect(collectSignals(document, '').has_captcha).toBe(true);
    document.body.innerHTML = `<input autocomplete="one-time-code" maxlength="6">`;
    expect(collectSignals(document, 'Enter the code').has_otp_field).toBe(true);
    document.body.innerHTML = `<h1>Reset your password</h1><input type="email" name="email"><button>Send link</button>`;
    document.title = 'Reset your password';
    expect(collectSignals(document, 'Reset your password').password_recovery).toBe(true);
  });

  it('finds confirmation text and reference numbers', () => {
    document.body.innerHTML = `<h2>Thank you for applying!</h2><p>Reference number: ABC-12345</p>`;
    const s = collectSignals(document, 'Thank you for applying! Reference number: ABC-12345');
    expect(s.success_messages.length).toBeGreaterThan(0);
    expect(s.reference_numbers).toContain('ABC-12345');
  });
});

describe('locator', () => {
  it('ranks exact label matches above partial ones and reports ambiguity', () => {
    document.body.innerHTML = `<label>Name<input id="a"></label><label>Last name<input id="b"></label>`;
    const r = locate(document, { strategy: 'label', value: 'Name' });
    expect((r.element as HTMLElement).id).toBe('a');
    document.body.innerHTML = `<section><h2>A</h2><label>Name<input id="x"></label></section><section><h2>B</h2><label>Name<input id="y"></label></section>`;
    const amb = locate(document, { strategy: 'label', value: 'Name' });
    expect(amb.ambiguous).toBe(true);
    expect(amb.matched).toBe(2);
    const scoped = locate(document, { strategy: 'label', value: 'Name', section: 'B' });
    expect((scoped.element as HTMLElement).id).toBe('y');
  });

  it('supports role+name, testid, id, name, text and css', () => {
    document.body.innerHTML = `<button data-testid="go">Continue</button><input id="i1" name="nm"><a href="#">Read more</a>`;
    expect(locate(document, { strategy: 'role', value: 'button', name: 'continue' }).element).not.toBeNull();
    expect(locate(document, { strategy: 'testid', value: 'go' }).element).not.toBeNull();
    expect(locate(document, { strategy: 'id', value: 'i1' }).element).not.toBeNull();
    expect(locate(document, { strategy: 'name', value: 'nm' }).element).not.toBeNull();
    expect(locate(document, { strategy: 'text', value: 'Read more' }).element).not.toBeNull();
    expect(locate(document, { strategy: 'css', value: 'a[href]' }).element).not.toBeNull();
    expect(locate(document, { strategy: 'css', value: ':::bad' }).reason).toBe('invalid css selector');
  });

  it('resolves snapshot node indices and detects stale nodes', () => {
    document.body.innerHTML = `<button id="b">Go</button>`;
    const snap = buildSnapshot(document);
    const idx = snap.nodes.find((n) => n.id === 'b')!.i;
    expect(locate(document, { strategy: 'node', value: String(idx) }).element).not.toBeNull();
    document.body.innerHTML = '';
    expect(locate(document, { strategy: 'node', value: String(idx) }).reason).toMatch(/stale/);
  });
});

describe('actions', () => {
  it('types, selects, checks and verifies values', async () => {
    document.body.innerHTML = `<label>Email<input id="e"></label><label>Country<select id="c"><option value="">-</option><option>Germany</option></select></label><label><input type="checkbox" id="k"> Agree</label>`;
    const typed = await executeAction(action({ type: 'type', target: { strategy: 'label', value: 'Email' }, args: { text: 'a@b.c' } }), null);
    expect(typed.ok).toBe(true);
    expect(typed.value).toBe('a@b.c');
    const sel = await executeAction(action({ type: 'select', target: { strategy: 'id', value: 'c' }, args: { value: 'germany' } }), null);
    expect(sel.value).toBe('Germany');
    const bad = await executeAction(action({ type: 'select', target: { strategy: 'id', value: 'c' }, args: { value: 'Mars' } }), null);
    expect(bad.ok).toBe(false);
    expect(bad.error_category).toBe('ELEMENT_NOT_FOUND');
    const chk = await executeAction(action({ type: 'check', target: { strategy: 'id', value: 'k' }, args: { checked: true } }), null);
    expect(chk.value).toBe(true);
  });

  it('dispatches input events so frameworks see the change', async () => {
    document.body.innerHTML = `<input id="i">`;
    const seen: string[] = [];
    document.getElementById('i')!.addEventListener('input', () => seen.push('input'));
    document.getElementById('i')!.addEventListener('change', () => seen.push('change'));
    await executeAction(action({ type: 'type', target: { strategy: 'id', value: 'i' }, args: { text: 'x' } }), null);
    expect(seen).toContain('input');
    expect(seen).toContain('change');
  });

  it('refuses uploads without an agent-resolved payload', async () => {
    document.body.innerHTML = `<input type="file" id="f">`;
    const r = await executeAction(action({ type: 'upload', target: { strategy: 'id', value: 'f' }, args: { resume_id: 'r' } }), null);
    expect(r.ok).toBe(false);
    expect(r.error_category).toBe('PERMISSION_DENIED');
  });

  it('reports ambiguous and missing targets instead of guessing', async () => {
    document.body.innerHTML = `<section><h2>A</h2><label>Name<input></label></section><section><h2>B</h2><label>Name<input></label></section>`;
    const amb = await executeAction(action({ type: 'type', target: { strategy: 'label', value: 'Name' }, args: { text: 'x' } }), null);
    expect(amb.error_category).toBe('AMBIGUOUS_TARGET');
    const missing = await executeAction(action({ type: 'click', target: { strategy: 'id', value: 'nope' } }), null);
    expect(missing.error_category).toBe('ELEMENT_NOT_FOUND');
  });

  it('never executes navigation in-page', async () => {
    const r = await executeAction(action({ type: 'navigate', args: { url: 'https://x' } }), null);
    expect(r.ok).toBe(false);
    expect(r.error_category).toBe('PERMISSION_DENIED');
  });
});
