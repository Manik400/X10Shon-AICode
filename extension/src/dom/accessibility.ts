/**
 * Accessible role and name computation. A pragmatic subset of the ARIA accname algorithm that
 * covers the controls found in application forms.
 */

const INPUT_ROLES: Record<string, string> = {
  text: 'textbox',
  email: 'textbox',
  tel: 'textbox',
  url: 'textbox',
  password: 'textbox',
  number: 'spinbutton',
  search: 'searchbox',
  checkbox: 'checkbox',
  radio: 'radio',
  file: 'file',
  date: 'date',
  'datetime-local': 'date',
  month: 'date',
  range: 'slider',
  submit: 'button',
  button: 'button',
  reset: 'button',
  image: 'button',
  hidden: '',
};

export function computeRole(el: Element): string {
  const explicit = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
  if (explicit) return explicit.toLowerCase();
  const tag = el.tagName.toLowerCase();
  switch (tag) {
    case 'a':
      return el.hasAttribute('href') ? 'link' : '';
    case 'button':
      return 'button';
    case 'input': {
      const t = ((el as HTMLInputElement).type || 'text').toLowerCase();
      if (t in INPUT_ROLES) return INPUT_ROLES[t];
      if (el.hasAttribute('list')) return 'combobox';
      return 'textbox';
    }
    case 'textarea':
      return 'textbox';
    case 'select':
      return (el as HTMLSelectElement).multiple ? 'listbox' : 'combobox';
    case 'option':
      return 'option';
    case 'h1':
    case 'h2':
    case 'h3':
    case 'h4':
    case 'h5':
    case 'h6':
      return 'heading';
    case 'label':
      return 'label';
    case 'fieldset':
      return 'group';
    case 'legend':
      return 'legend';
    case 'dialog':
      return 'dialog';
    case 'form':
      return 'form';
    case 'nav':
      return 'navigation';
    case 'main':
      return 'main';
    case 'img':
      return 'img';
    case 'summary':
      return 'button';
    default:
      if ((el as HTMLElement).isContentEditable) return 'textbox';
      return '';
  }
}

export function normalizeText(s: string | null | undefined): string {
  return (s || '').replace(/\s+/g, ' ').trim();
}

function textOf(el: Element | null): string {
  if (!el) return '';
  const clone = el.cloneNode(true) as Element;
  clone.querySelectorAll('script,style,noscript,select,textarea').forEach((n) => n.remove());
  return normalizeText(clone.textContent);
}

export function findLabelText(el: Element): string {
  const root = el.getRootNode() as Document | ShadowRoot;
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    const parts = labelledBy
      .split(/\s+/)
      .map((id) => textOf(root.getElementById ? root.getElementById(id) : null))
      .filter(Boolean);
    if (parts.length) return parts.join(' ');
  }
  const ariaLabel = normalizeText(el.getAttribute('aria-label'));
  if (ariaLabel) return ariaLabel;
  const id = el.getAttribute('id');
  if (id && 'querySelectorAll' in root) {
    const esc = (window as unknown as { CSS?: { escape?: (s: string) => string } }).CSS?.escape?.(id) ?? id.replace(/"/g, '\\"');
    const labels = (root as Document).querySelectorAll(`label[for="${esc}"]`);
    const parts = Array.from(labels)
      .map((l) => textOf(l))
      .filter(Boolean);
    if (parts.length) return parts.join(' ');
  }
  const wrapping = el.closest('label');
  if (wrapping) {
    const clone = wrapping.cloneNode(true) as HTMLElement;
    clone.querySelectorAll('input,select,textarea,button').forEach((n) => n.remove());
    const t = normalizeText(clone.textContent);
    if (t) return t;
  }
  // aria-describedby is weaker but still useful for custom widgets.
  const describedBy = el.getAttribute('aria-describedby');
  if (describedBy && (root as Document).getElementById) {
    const t = textOf((root as Document).getElementById(describedBy.split(/\s+/)[0]));
    if (t && t.length < 120) return t;
  }
  return '';
}

/** Heuristic: the nearest preceding heading/label-ish text within a small container. */
export function nearbyPrecedingText(el: Element): string {
  let node: Element | null = el;
  for (let depth = 0; depth < 4 && node; depth++) {
    let sib = node.previousElementSibling;
    let hops = 0;
    while (sib && hops < 3) {
      const tag = sib.tagName.toLowerCase();
      if (['label', 'span', 'div', 'p', 'legend', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'dt'].includes(tag)) {
        const t = textOf(sib);
        if (t && t.length <= 120 && !sib.querySelector('input,select,textarea,button')) return t;
      }
      sib = sib.previousElementSibling;
      hops++;
    }
    node = node.parentElement;
  }
  return '';
}

export function computeAccessibleName(el: Element, role: string): string {
  const tag = el.tagName.toLowerCase();
  const fieldLike = ['textbox', 'searchbox', 'combobox', 'listbox', 'checkbox', 'radio', 'switch', 'spinbutton', 'file', 'date', 'slider'];
  if (fieldLike.includes(role)) {
    const label = findLabelText(el);
    if (label) return label;
    const placeholder = normalizeText(el.getAttribute('placeholder'));
    if (placeholder) return placeholder;
    const title = normalizeText(el.getAttribute('title'));
    if (title) return title;
    const near = nearbyPrecedingText(el);
    if (near) return near;
    return normalizeText(el.getAttribute('name'));
  }
  if (role === 'button' || role === 'link' || role === 'menuitem' || role === 'tab' || role === 'option' || role === 'heading') {
    const aria = findLabelText(el);
    if (aria) return aria;
    if (tag === 'input') {
      const v = normalizeText((el as HTMLInputElement).value);
      if (v) return v;
      const alt = normalizeText(el.getAttribute('alt'));
      if (alt) return alt;
    }
    const t = textOf(el);
    if (t) return t.slice(0, 200);
    const title = normalizeText(el.getAttribute('title'));
    if (title) return title;
    const img = el.querySelector('img[alt]');
    if (img) return normalizeText(img.getAttribute('alt'));
    return '';
  }
  if (role === 'img') return normalizeText(el.getAttribute('alt'));
  const aria = findLabelText(el);
  if (aria) return aria;
  return textOf(el).slice(0, 200);
}

export function isVisible(el: Element): boolean {
  if (!(el instanceof HTMLElement) && !(el instanceof SVGElement)) return false;
  // File inputs are almost always hidden behind a styled label; they stay actionable as long as
  // they are in the document and not explicitly display:none via a disabled ancestor.
  if (el instanceof HTMLInputElement && el.type === 'file') return el.isConnected && getComputedStyle(el).display !== 'none' || !!el.labels?.length;
  const style = getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  if (parseFloat(style.opacity || '1') === 0) return false;
  if (el.getAttribute('aria-hidden') === 'true') return false;
  const rects = el.getClientRects();
  if (rects.length === 0) {
    // Inputs of type=file are frequently hidden behind styled labels; treat as visible if a label exists.
    if (el instanceof HTMLInputElement && el.type === 'file') return true;
    return false;
  }
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return el instanceof HTMLInputElement && (el.type === 'file' || el.type === 'radio' || el.type === 'checkbox');
  return true;
}

export function isDisabled(el: Element): boolean {
  if ((el as HTMLInputElement).disabled) return true;
  if (el.getAttribute('aria-disabled') === 'true') return true;
  const fs = el.closest('fieldset[disabled]');
  return !!fs;
}

export function sectionContext(el: Element): string {
  const dialog = el.closest('[role="dialog"],dialog');
  const fs = el.closest('fieldset');
  if (fs) {
    const legend = fs.querySelector('legend');
    const t = textOf(legend);
    if (t) return t.slice(0, 80);
  }
  const group = el.closest('[role="group"][aria-label],[role="group"][aria-labelledby],[role="radiogroup"]');
  if (group) {
    const t = findLabelText(group);
    if (t) return t.slice(0, 80);
  }
  const section = el.closest('section,article,[role="region"],form,.section,.form-section,.card,fieldset');
  if (section) {
    const h = section.querySelector('h1,h2,h3,h4,h5,h6,legend,[role="heading"]');
    const t = textOf(h);
    if (t) return t.slice(0, 80);
  }
  if (dialog) return 'dialog';
  // Walk backwards for the nearest heading in document order.
  let node: Element | null = el;
  while (node) {
    let sib = node.previousElementSibling;
    while (sib) {
      if (/^H[1-6]$/.test(sib.tagName)) return textOf(sib).slice(0, 80);
      const inner = sib.querySelector('h1,h2,h3,h4,h5,h6');
      if (inner && !sib.querySelector('input,select,textarea')) return textOf(inner).slice(0, 80);
      sib = sib.previousElementSibling;
    }
    node = node.parentElement;
    if (node && (node.tagName === 'BODY' || node.tagName === 'HTML')) break;
  }
  return '';
}
