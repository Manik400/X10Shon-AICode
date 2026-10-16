/**
 * Compact page snapshot. Walks the DOM (including open shadow roots and same-origin iframes),
 * records interactive/semantic nodes with roles, names and section context, and keeps a
 * reference table so `strategy: "node"` targets resolve to the exact element.
 */
import { computeAccessibleName, computeRole, isDisabled, isVisible, normalizeText, sectionContext } from './accessibility';
import { collectSignals } from './signals';
import type { FormInfo, PageSnapshot, SnapshotNode, SnapshotOptions } from './types';

export interface SnapshotRegistry {
  generation: number;
  elements: Element[];
}

const registry: SnapshotRegistry = { generation: 0, elements: [] };

export function getRegistry(): SnapshotRegistry {
  return registry;
}

const SEMANTIC_ROLES = new Set(['heading', 'alert', 'status', 'dialog', 'legend']);
const INTERACTIVE_ROLES = new Set([
  'button',
  'link',
  'textbox',
  'searchbox',
  'combobox',
  'listbox',
  'checkbox',
  'radio',
  'switch',
  'slider',
  'spinbutton',
  'menuitem',
  'tab',
  'option',
  'file',
  'date',
]);

function* walk(root: Node, frame: number): Generator<{ el: Element; frame: number }> {
  const tw = (root.ownerDocument || (root as Document)).createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let cur = tw.currentNode as Element | null;
  if (root.nodeType === Node.ELEMENT_NODE) yield { el: root as Element, frame };
  while ((cur = tw.nextNode() as Element | null)) {
    const tag = cur.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript' || tag === 'template') continue;
    if (cur.id === 'lfa-agent-overlay-host') continue;
    yield { el: cur, frame };
    if (cur.shadowRoot) {
      for (const child of Array.from(cur.shadowRoot.children)) yield* walk(child, frame);
    }
    if (tag === 'iframe') {
      try {
        const doc = (cur as HTMLIFrameElement).contentDocument;
        if (doc && doc.body) yield* walk(doc.body, frame + 1);
      } catch {
        /* cross-origin: reported in signals */
      }
    }
  }
}

/**
 * Question text for a radio group: the legend/aria-label of the enclosing group, or, for groups
 * built from plain divs, the first text-bearing element of the smallest container that holds
 * every radio of the same name.
 */
export function radioGroupLabel(el: HTMLInputElement): string {
  const grp = el.closest('fieldset,[role="radiogroup"],[role="group"]');
  if (grp) {
    const legend = grp.querySelector('legend');
    const lt = normalizeText(legend?.textContent) || normalizeText(grp.getAttribute('aria-label'));
    if (lt) return lt;
  }
  const root = el.getRootNode() as Document | ShadowRoot;
  const esc = (window as unknown as { CSS?: { escape?: (s: string) => string } }).CSS?.escape?.(el.name) ?? el.name.replace(/["\\]/g, '\\$&');
  const siblings = Array.from((root as Document).querySelectorAll(`input[type="radio"][name="${esc}"]`));
  let container: Element | null = el.parentElement;
  while (container && container !== (root as Document).body) {
    const inside = siblings.filter((s) => container!.contains(s)).length;
    if (inside === siblings.length) break;
    container = container.parentElement;
  }
  if (!container) return '';
  for (const child of Array.from(container.children)) {
    if (child.querySelector('input,select,textarea') || child.matches('input,select,textarea,label')) continue;
    const t = normalizeText((child as HTMLElement).innerText || child.textContent);
    if (t && t.length <= 200) return t.replace(/[\s:*✱]+$/g, '');
  }
  return '';
}

function hashString(s: string): string {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16);
}

function bodyText(doc: Document, maxChars: number): string {
  const body = doc.body;
  if (!body) return '';
  const dialog = doc.querySelector('[role="dialog"]:not([aria-hidden="true"]),dialog[open]') as HTMLElement | null;
  const main = dialog || (doc.querySelector('main,[role="main"]') as HTMLElement | null) || body;
  const text = normalizeText(main.innerText || main.textContent);
  return text.length > maxChars ? text.slice(0, maxChars) : text;
}

export function buildSnapshot(doc: Document, opts: SnapshotOptions = {}): PageSnapshot {
  const maxNodes = opts.maxNodes ?? 600;
  const maxText = opts.maxTextChars ?? 6000;
  registry.generation += 1;
  registry.elements = [];

  const nodes: SnapshotNode[] = [];
  const forms: FormInfo[] = [];
  const formIndex = new Map<Element, number>();
  let truncated = false;
  let frames = 1;
  let maxFrame = 0;
  const activeDialog = doc.querySelector('[role="dialog"]:not([aria-hidden="true"]),dialog[open]');

  for (const { el, frame } of walk(doc.body || doc.documentElement, 0)) {
    if (frame > maxFrame) {
      maxFrame = frame;
      frames = maxFrame + 1;
    }
    const role = computeRole(el);
    if (!role) continue;
    const interactive = INTERACTIVE_ROLES.has(role);
    if (!interactive && !SEMANTIC_ROLES.has(role) && role !== 'form') continue;
    if (role === 'form') {
      const f = el as HTMLFormElement;
      formIndex.set(el, forms.length);
      forms.push({ index: forms.length, fields: [], submit_nodes: [], name: f.getAttribute('name') || f.id || null, action: f.getAttribute('action') });
      continue;
    }
    if (role === 'option' && el.closest('select')) continue; // select options are listed on the select
    const visible = isVisible(el);
    if (!visible && interactive) continue;
    if (nodes.length >= maxNodes) {
      truncated = true;
      break;
    }
    const i = registry.elements.length;
    registry.elements.push(el);
    const name = computeAccessibleName(el, role);
    const node: SnapshotNode = {
      i,
      role,
      name: name.slice(0, 300),
      tag: el.tagName.toLowerCase(),
      disabled: isDisabled(el),
      required: (el as HTMLInputElement).required || el.getAttribute('aria-required') === 'true',
      visible,
      frame,
      in_dialog: !!activeDialog && activeDialog.contains(el),
    };
    const testid = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || el.getAttribute('data-automation-id');
    if (testid) node.testid = testid.slice(0, 120);
    if (el.id) node.id = el.id.slice(0, 120);
    const nameAttr = el.getAttribute('name');
    if (nameAttr) node.name_attr = nameAttr.slice(0, 120);
    if (interactive) {
      const section = sectionContext(el);
      if (section) node.section = section;
    }
    if (el instanceof HTMLInputElement) {
      node.type = (el.type || 'text').toLowerCase();
      if (node.type !== 'password' && node.type !== 'file' && node.type !== 'submit' && node.type !== 'button') node.value = el.value.slice(0, 200);
      if (node.type === 'checkbox' || node.type === 'radio') node.checked = el.checked;
      if (el.placeholder) node.placeholder = el.placeholder.slice(0, 120);
      if (el.autocomplete) node.autocomplete = el.autocomplete;
      if (el.accept) node.accept = el.accept;
      if (el.maxLength > 0) node.maxlength = el.maxLength;
      if (el.pattern) node.pattern = el.pattern.slice(0, 120);
      if (node.type === 'radio' && el.name) {
        const groupLabel = radioGroupLabel(el);
        if (groupLabel) node.section = groupLabel.slice(0, 80);
      }
      if (el.form) {
        const fi = formIndex.get(el.form);
        if (fi !== undefined) {
          node.form = fi;
          if (node.type === 'submit' || node.type === 'image') forms[fi].submit_nodes.push(i);
          else if (node.type !== 'button' && node.type !== 'reset') forms[fi].fields.push(i);
        }
      }
    } else if (el instanceof HTMLTextAreaElement) {
      node.type = 'textarea';
      node.value = el.value.slice(0, 300);
      if (el.placeholder) node.placeholder = el.placeholder.slice(0, 120);
      if (el.maxLength > 0) node.maxlength = el.maxLength;
      if (el.form) {
        const fi = formIndex.get(el.form);
        if (fi !== undefined) {
          node.form = fi;
          forms[fi].fields.push(i);
        }
      }
    } else if (el instanceof HTMLSelectElement) {
      node.type = el.multiple ? 'select-multiple' : 'select';
      node.options = Array.from(el.options)
        .filter((o) => !o.disabled || o.selected)
        .map((o) => normalizeText(o.textContent) || o.value)
        .slice(0, 100);
      const sel = el.selectedOptions[0];
      node.value = sel ? normalizeText(sel.textContent) || sel.value : '';
      if (el.form) {
        const fi = formIndex.get(el.form);
        if (fi !== undefined) {
          node.form = fi;
          forms[fi].fields.push(i);
        }
      }
    } else if (el instanceof HTMLButtonElement) {
      node.type = (el.type || 'submit').toLowerCase();
      if (el.form) {
        const fi = formIndex.get(el.form);
        if (fi !== undefined) {
          node.form = fi;
          if (node.type === 'submit') forms[fi].submit_nodes.push(i);
        }
      }
    } else if (el instanceof HTMLAnchorElement) {
      node.href = (el.getAttribute('href') || '').slice(0, 300);
    } else if (role === 'combobox' || role === 'listbox' || role === 'textbox') {
      // Custom widgets (aria): expose state.
      const expanded = el.getAttribute('aria-expanded');
      if (expanded) node.value = `expanded=${expanded}`;
      if ((el as HTMLElement).isContentEditable) node.value = normalizeText((el as HTMLElement).innerText).slice(0, 200);
    } else if (role === 'checkbox' || role === 'radio' || role === 'switch') {
      node.checked = el.getAttribute('aria-checked') === 'true';
    }
    if (role === 'heading' || role === 'alert' || role === 'status' || role === 'legend') {
      node.text = normalizeText((el as HTMLElement).innerText || el.textContent).slice(0, 300);
    }
    nodes.push(node);
  }

  const text = bodyText(doc, maxText);
  const signals = collectSignals(doc, text);
  const jsonld: Record<string, unknown>[] = [];
  for (const s of Array.from(doc.querySelectorAll('script[type="application/ld+json"]')).slice(0, 5)) {
    try {
      const parsed = JSON.parse(s.textContent || '');
      const items = Array.isArray(parsed) ? parsed : parsed['@graph'] ? parsed['@graph'] : [parsed];
      for (const it of items) if (it && typeof it === 'object' && /JobPosting/.test(String(it['@type']))) jsonld.push(it);
    } catch {
      /* ignore malformed JSON-LD */
    }
  }

  const fingerprint = nodes.map((n) => `${n.role}|${n.name}|${n.value ?? ''}|${n.checked ?? ''}`).join('\n') + '\n' + text.slice(0, 2000);
  return {
    url: doc.location?.href || '',
    title: doc.title || '',
    nodes,
    forms,
    text_excerpt: text,
    signals,
    truncated,
    generation: registry.generation,
    jsonld,
    frame_count: frames,
    content_hash: hashString(fingerprint),
  };
}
