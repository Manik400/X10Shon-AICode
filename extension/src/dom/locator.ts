/**
 * Element location in priority order: role+name → label → testid → id → name → text → css.
 * Every strategy returns ranked candidates; ambiguity is reported, never guessed away.
 */
import { computeAccessibleName, computeRole, findLabelText, isDisabled, isVisible, normalizeText, sectionContext } from './accessibility';
import { fingerprintKey, getRegistry, snapshotCandidates, type NodeFingerprint } from './snapshot';

/**
 * Re-find a snapshot node whose element was replaced by a re-render (React/Vue forms often swap
 * inputs right after hydration): same id, else the nth element with the same tag, role,
 * accessible name, name attribute and type. Returns null unless exactly that field is found.
 */
function refind(doc: Document, fp: NodeFingerprint | undefined): Element | null {
  if (!fp) return null;
  if (fp.id) {
    const byId = doc.getElementById(fp.id);
    if (byId && byId.tagName.toLowerCase() === fp.tag && isVisible(byId)) return byId;
  }
  if (!fp.name && !fp.nameAttr) return null; // nothing distinctive to match on
  const key = fingerprintKey(fp);
  let n = 0;
  for (const el of snapshotCandidates(doc)) {
    if (el.tagName.toLowerCase() !== fp.tag) continue;
    if ((el.getAttribute('name') || '') !== fp.nameAttr || (el.getAttribute('type') || '').toLowerCase() !== fp.type) continue;
    const role = computeRole(el);
    if (!role || !isVisible(el)) continue;
    if (fingerprintKey({ tag: fp.tag, role, name: computeAccessibleName(el, role), nameAttr: fp.nameAttr, type: fp.type }) !== key) continue;
    if (n === fp.nth) return el;
    n += 1;
  }
  return null;
}
import type { Target } from './types';

export interface Candidate {
  el: Element;
  score: number;
  reason: string;
}

export interface LocateResult {
  element: Element | null;
  matched: number;
  ambiguous: boolean;
  reason: string;
  candidates: Candidate[];
}

const FIELD_SELECTOR = 'input:not([type="hidden"]),textarea,select,button,a[href],[role],[contenteditable="true"],summary';

function allElements(doc: Document): Element[] {
  const out: Element[] = [];
  const collect = (root: ParentNode) => {
    for (const el of Array.from(root.querySelectorAll(FIELD_SELECTOR))) {
      out.push(el);
      if (el.shadowRoot) collect(el.shadowRoot);
    }
    for (const host of Array.from(root.querySelectorAll('*')).filter((e) => e.shadowRoot)) collect(host.shadowRoot as ShadowRoot);
    for (const f of Array.from(root.querySelectorAll('iframe'))) {
      try {
        const d = (f as HTMLIFrameElement).contentDocument;
        if (d) collect(d);
      } catch {
        /* cross-origin */
      }
    }
  };
  collect(doc);
  return Array.from(new Set(out));
}

function matchText(hay: string, needle: string, exact: boolean): number {
  const h = normalizeText(hay).toLowerCase();
  const n = normalizeText(needle).toLowerCase();
  if (!h || !n) return 0;
  if (h === n) return 1;
  if (exact) return 0;
  // Strip trailing required markers/colons before comparing.
  const hs = h.replace(/[\s:*]+$/g, '').replace(/\s*\((required|optional)\)$/, '');
  if (hs === n) return 0.95;
  if (h.startsWith(n) || hs.startsWith(n)) return 0.8;
  if (h.includes(n)) return 0.6;
  // Token overlap for small differences in wording.
  const ht = new Set(hs.split(/[^a-z0-9]+/).filter(Boolean));
  const nt = n.split(/[^a-z0-9]+/).filter(Boolean);
  if (!nt.length) return 0;
  const hit = nt.filter((t) => ht.has(t)).length;
  return hit === nt.length ? 0.5 : 0;
}

function sectionBoost(el: Element, section: string | null | undefined): number {
  if (!section) return 0;
  const s = sectionContext(el);
  const m = matchText(s, section, false);
  return m > 0 ? 0.2 * m : -0.3;
}

export function locate(doc: Document, target: Target): LocateResult {
  const exact = !!target.exact;
  const candidates: Candidate[] = [];
  const push = (el: Element, score: number, reason: string) => {
    if (score <= 0) return;
    if (!isVisible(el)) return;
    score += sectionBoost(el, target.section);
    if (isDisabled(el)) score -= 0.05;
    candidates.push({ el, score, reason });
  };

  switch (target.strategy) {
    case 'node': {
      const reg = getRegistry();
      const idx = parseInt(target.value, 10);
      let el: Element | null | undefined = reg.elements[idx];
      if (el && !el.isConnected) {
        el = refind(doc, reg.fingerprints[idx]);
        if (el) {
          reg.elements[idx] = el;
          return { element: el, matched: 1, ambiguous: false, reason: 'node index (re-found after re-render)', candidates: [{ el, score: 1, reason: 'node' }] };
        }
      }
      if (!el || !el.isConnected) return { element: null, matched: 0, ambiguous: false, reason: 'snapshot node is stale; take a new snapshot', candidates: [] };
      return { element: el, matched: 1, ambiguous: false, reason: 'node index', candidates: [{ el, score: 1, reason: 'node' }] };
    }
    case 'role': {
      const wantRole = target.value.toLowerCase();
      for (const el of allElements(doc)) {
        const role = computeRole(el);
        if (role !== wantRole && !(wantRole === 'textbox' && (role === 'searchbox' || role === 'spinbutton'))) continue;
        if (target.name) {
          const nm = computeAccessibleName(el, role);
          const m = matchText(nm, target.name, exact);
          push(el, m, `role=${role} name≈"${nm.slice(0, 40)}"`);
        } else {
          push(el, 0.5, `role=${role}`);
        }
      }
      break;
    }
    case 'label': {
      for (const el of allElements(doc)) {
        const role = computeRole(el);
        if (!role) continue;
        const label = findLabelText(el) || computeAccessibleName(el, role);
        const m = matchText(label, target.value, exact);
        push(el, m, `label≈"${label.slice(0, 40)}"`);
      }
      break;
    }
    case 'testid': {
      for (const el of allElements(doc)) {
        const v = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || el.getAttribute('data-automation-id');
        if (v && v === target.value) push(el, 1, 'testid');
      }
      break;
    }
    case 'id': {
      for (const el of allElements(doc)) if (el.id === target.value) push(el, 1, 'id');
      break;
    }
    case 'name': {
      for (const el of allElements(doc)) if (el.getAttribute('name') === target.value) push(el, 1, 'name');
      break;
    }
    case 'text': {
      for (const el of allElements(doc)) {
        const role = computeRole(el);
        if (!role) continue;
        const txt = computeAccessibleName(el, role);
        const m = matchText(txt, target.value, exact);
        push(el, m, `text≈"${txt.slice(0, 40)}"`);
      }
      break;
    }
    case 'css': {
      let els: Element[] = [];
      try {
        els = Array.from(doc.querySelectorAll(target.value));
      } catch {
        return { element: null, matched: 0, ambiguous: false, reason: 'invalid css selector', candidates: [] };
      }
      for (const el of els) push(el, 1, 'css');
      break;
    }
  }

  candidates.sort((a, b) => b.score - a.score);
  if (!candidates.length) return { element: null, matched: 0, ambiguous: false, reason: 'no match', candidates };
  const top = candidates[0].score;
  const tied = candidates.filter((c) => Math.abs(c.score - top) < 0.001);
  const nth = target.nth ?? 0;
  if (tied.length > 1 && nth === 0 && target.strategy !== 'css') {
    // Identical scores: if they are radios/options in the same group that is expected, otherwise ambiguous.
    const sameGroup = tied.every((c) => c.el instanceof HTMLInputElement && c.el.type === 'radio' && c.el.name === (tied[0].el as HTMLInputElement).name);
    if (!sameGroup) {
      // Prefer the one inside an open dialog, then the first in document order.
      const inDialog = tied.filter((c) => c.el.closest('[role="dialog"],dialog[open]'));
      if (inDialog.length === 1) return { element: inDialog[0].el, matched: tied.length, ambiguous: false, reason: 'dialog scope', candidates };
      return { element: null, matched: tied.length, ambiguous: true, reason: `${tied.length} equally good matches; add section context or nth`, candidates };
    }
  }
  const pick = tied[nth] || candidates[nth];
  if (!pick) return { element: null, matched: candidates.length, ambiguous: false, reason: `nth=${nth} out of range`, candidates };
  return { element: pick.el, matched: candidates.length, ambiguous: false, reason: pick.reason, candidates };
}
