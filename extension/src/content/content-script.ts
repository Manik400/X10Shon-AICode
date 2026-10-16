/**
 * Content script: executes snapshot/action requests from the service worker using the shared
 * dom-agent. Runs in all frames; only the top frame answers, sub-frames are reached through the
 * top frame's DOM walk when same-origin. No timers, no observers, nothing runs between requests.
 */
import { domAgent } from '../dom/index';
import type { ContentRequest } from '../lib/protocol';

declare global {
  interface Window {
    __lfaContentScriptLoaded?: boolean;
  }
}

if (!window.__lfaContentScriptLoaded) {
  window.__lfaContentScriptLoaded = true;
  chrome.runtime.onMessage.addListener((msg: ContentRequest, _sender, sendResponse) => {
    if (window !== window.top) return undefined; // only the top frame responds
    switch (msg.kind) {
      case 'ping':
        sendResponse({ ok: true, version: domAgent.version });
        return undefined;
      case 'snapshot':
        try {
          sendResponse(domAgent.snapshot({ maxNodes: msg.max_nodes }));
        } catch (e) {
          sendResponse({ error: (e as Error).message });
        }
        return undefined;
      case 'action':
        domAgent
          .execute(msg.action, msg.payload ?? null)
          .then((r) => sendResponse(r))
          .catch((e: Error) => sendResponse({ ok: false, action_type: msg.action.type, error_category: 'UNKNOWN', message: e.message, duration_ms: 0, evidence: {} }));
        return true;
      default:
        return undefined;
    }
  });
}
