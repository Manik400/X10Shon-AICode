/**
 * Entry point of the dom-agent bundle. Exposes a small, typed API on `window.__lfaDomAgent` that
 * the extension content script and the Playwright driver both call.
 */
import { executeAction } from './actions';
import { locate } from './locator';
import { buildSnapshot } from './snapshot';
import type { ActionPayload, ActionResult, BrowserAction, PageSnapshot, SnapshotOptions, Target } from './types';

export interface DomAgentApi {
  version: string;
  snapshot: (opts?: SnapshotOptions) => PageSnapshot;
  execute: (action: BrowserAction, payload?: ActionPayload | null) => Promise<ActionResult>;
  locate: (target: Target) => { found: boolean; matched: number; ambiguous: boolean; reason: string };
}

const api: DomAgentApi = {
  version: '0.1.0',
  snapshot: (opts) => buildSnapshot(document, opts),
  execute: (action, payload) => executeAction(action, payload ?? null),
  locate: (target) => {
    const r = locate(document, target);
    return { found: !!r.element, matched: r.matched, ambiguous: r.ambiguous, reason: r.reason };
  },
};

declare global {
  interface Window {
    __lfaDomAgent: DomAgentApi;
  }
}

if (typeof window !== 'undefined') {
  window.__lfaDomAgent = api;
}

export { api as domAgent };
export * from './types';
