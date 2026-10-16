/**
 * Minimal MV3 service worker: keeps the agent connection, routes agent requests to tabs,
 * enforces per-site permission, and reports tab events. No polling loops; alarms drive reconnects.
 */
import type { ActionResult, PageSnapshot } from '../dom/types';
import { AgentConnection } from '../lib/connection';
import type { AgentRequest, ContentRequest, ExtensionReply, TabSummary, UiRequest } from '../lib/protocol';
import { api, claimPairing } from '../lib/api';
import { getConfig, getStatus, setConfig, setStatus } from '../lib/storage';

const RECONNECT_ALARM = 'lfa-reconnect';

// ------------------------------------------------------------------ permissions
function originPattern(url: string): string | null {
  try {
    const u = new URL(url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return `${u.protocol}//${u.hostname}/*`;
  } catch {
    return null;
  }
}

async function isPermitted(url: string): Promise<boolean> {
  const pattern = originPattern(url);
  if (!pattern) return false; // chrome://, file://, about: pages are never automated
  if (/^https?:\/\/(127\.0\.0\.1|localhost)\//.test(pattern)) return true;
  // All http(s) sites are granted by the manifest; the check stays for builds that trim it down.
  return chrome.permissions.contains({ origins: [pattern] });
}

// ------------------------------------------------------------------ tabs
async function tabSummary(tab: chrome.tabs.Tab): Promise<TabSummary> {
  const url = tab.url || '';
  return { tab_id: tab.id ?? -1, url, title: tab.title || '', active: !!tab.active, permitted: await isPermitted(url) };
}

async function waitForTabLoad(tabId: number, timeoutMs: number): Promise<void> {
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === 'complete') return;
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('navigation timeout'));
    }, timeoutMs);
    const listener = (id: number, info: chrome.tabs.TabChangeInfo) => {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`${what} timed out after ${ms} ms`)), ms);
    p.then((v) => (clearTimeout(t), resolve(v)), (e) => (clearTimeout(t), reject(e)));
  });
}

async function ensureContentScript(tabId: number): Promise<void> {
  try {
    const pong = await withTimeout(chrome.tabs.sendMessage(tabId, { kind: 'ping' } as ContentRequest), 1500, 'ping');
    if (pong && (pong as { ok?: boolean }).ok) return;
  } catch {
    /* not injected yet (or no answer): inject the top frame; same-origin frames are walked from there */
  }
  await withTimeout(chrome.scripting.executeScript({ target: { tabId }, files: ['content-script.js'] }), 8000, 'content script injection');
}

async function withTab<T>(tabId: number, fn: (tab: chrome.tabs.Tab) => Promise<T>): Promise<T> {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.url || !(await isPermitted(tab.url))) {
    throw new Error(`PERMISSION_DENIED: site not allowed (${originPattern(tab.url || '') || 'unknown'}). Use "Allow this site" in the side panel.`);
  }
  await ensureContentScript(tabId);
  return fn(tab);
}

async function sendToContent<T>(tabId: number, req: ContentRequest, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('content script timeout')), timeoutMs);
    chrome.tabs.sendMessage(tabId, req, { frameId: 0 }, (resp) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message || 'message failed'));
      else resolve(resp as T);
    });
  });
}

// ------------------------------------------------------------------ agent request routing
async function handleAgentRequest(msg: AgentRequest, send: (r: ExtensionReply) => void): Promise<void> {
  switch (msg.type) {
    case 'welcome':
      return;
    case 'status_update': {
      await setStatus({ currentTask: msg.status.current_task, pausedAll: msg.status.paused_all, pendingApprovals: msg.status.pending_approvals, waitingUser: msg.status.waiting_user });
      return;
    }
    case 'list_tabs': {
      const tabs = await chrome.tabs.query({});
      send({ type: 'tabs', id: msg.id, tabs: await Promise.all(tabs.map(tabSummary)) });
      return;
    }
    case 'reload_extension': {
      // The agent saw a newer build in extension/dist: reload so Chrome picks up the new service
      // worker and manifest. The connection re-establishes itself after the reload.
      await setStatus({ lastError: `Updating to the new build (${msg.reason ?? 'newer version on disk'})…` });
      setTimeout(() => chrome.runtime.reload(), 200);
      return;
    }
    case 'activate_tab': {
      try {
        const t = await chrome.tabs.update(msg.tab_id, { active: true });
        if (t?.windowId !== undefined) await chrome.windows.update(t.windowId, { focused: true });
        send({ type: 'tab_activated', id: msg.id, ok: true });
      } catch (e) {
        send({ type: 'tab_activated', id: msg.id, ok: false, error: (e as Error).message });
      }
      return;
    }
    case 'open_tab': {
      if (!(await isPermitted(msg.url))) {
        send({ type: 'tab_opened', id: msg.id, ok: false, error: `PERMISSION_DENIED: ${originPattern(msg.url)} is not allowed` });
        return;
      }
      const tab = await chrome.tabs.create({ url: msg.url, active: msg.active !== false });
      try {
        await waitForTabLoad(tab.id!, 30_000);
      } catch {
        /* report anyway */
      }
      send({ type: 'tab_opened', id: msg.id, ok: true, tab_id: tab.id });
      return;
    }
    case 'close_tab': {
      try {
        await chrome.tabs.remove(msg.tab_id);
        send({ type: 'tab_closed', id: msg.id, ok: true });
      } catch {
        send({ type: 'tab_closed', id: msg.id, ok: false });
      }
      return;
    }
    case 'navigate': {
      const t0 = Date.now();
      if (!(await isPermitted(msg.url))) {
        send({ type: 'navigate_result', id: msg.id, result: { ok: false, action_type: 'navigate', error_category: 'PERMISSION_DENIED', message: `site not allowed: ${originPattern(msg.url)}`, duration_ms: 0, evidence: {} } });
        return;
      }
      try {
        await chrome.tabs.update(msg.tab_id, { url: msg.url });
        await waitForTabLoad(msg.tab_id, msg.timeout_ms);
        const tab = await chrome.tabs.get(msg.tab_id);
        send({ type: 'navigate_result', id: msg.id, result: { ok: true, action_type: 'navigate', message: 'loaded', url: tab.url, title: tab.title, duration_ms: Date.now() - t0, evidence: {} } });
      } catch (e) {
        send({ type: 'navigate_result', id: msg.id, result: { ok: false, action_type: 'navigate', error_category: 'NAVIGATION_FAILED', message: (e as Error).message, duration_ms: Date.now() - t0, evidence: {} } });
      }
      return;
    }
    case 'snapshot': {
      try {
        const snapshot = await withTab(msg.tab_id, () => sendToContent<PageSnapshot>(msg.tab_id, { kind: 'snapshot', max_nodes: msg.max_nodes }, 15_000));
        send({ type: 'snapshot_result', id: msg.id, ok: true, snapshot });
      } catch (e) {
        send({ type: 'snapshot_result', id: msg.id, ok: false, error: (e as Error).message });
      }
      return;
    }
    case 'action': {
      const t0 = Date.now();
      try {
        const result = await withTab(msg.tab_id, async () => {
          try {
            return await sendToContent<ActionResult>(msg.tab_id, { kind: 'action', action: msg.action, payload: msg.payload ?? null }, msg.action.timeout_ms + 2000);
          } catch (e) {
            // The page navigated away mid-action (e.g. submit click). Wait for the load and report.
            const m = (e as Error).message || '';
            if (/port closed|receiving end|context invalidated|timeout/i.test(m)) {
              try {
                await waitForTabLoad(msg.tab_id, msg.action.timeout_ms);
              } catch {
                /* fallthrough */
              }
              const tab = await chrome.tabs.get(msg.tab_id);
              return { ok: true, action_type: msg.action.type, message: 'navigation occurred during action', url: tab.url, title: tab.title, duration_ms: Date.now() - t0, evidence: { navigated: true } } as ActionResult;
            }
            throw e;
          }
        });
        send({ type: 'action_result', id: msg.id, result });
      } catch (e) {
        const m = (e as Error).message || 'action failed';
        send({ type: 'action_result', id: msg.id, result: { ok: false, action_type: msg.action.type, error_category: m.startsWith('PERMISSION_DENIED') ? 'PERMISSION_DENIED' : 'UNKNOWN', message: m, duration_ms: Date.now() - t0, evidence: {} } });
      }
      return;
    }
    default:
      return;
  }
}

const connection = new AgentConnection(handleAgentRequest);

// Safety net for service-worker suspension: the in-worker backoff loop dies with the worker, so an
// alarm re-enters connect() every minute for as long as the extension is installed.
async function ensureAlarm(): Promise<void> {
  const existing = await chrome.alarms.get(RECONNECT_ALARM);
  if (!existing) await chrome.alarms.create(RECONNECT_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
}

async function connectNow(): Promise<void> {
  await ensureAlarm();
  await connection.connect();
}

// ------------------------------------------------------------------ tab takeover
// Clicking the toolbar icon hands the current tab to the agent: the side panel opens on that
// tab, Chrome asks once to allow the site (per-site grant, never a blanket permission), and the
// agent's own allow-list learns the domain. Both Chrome calls run inside the click gesture.
async function takeOverTab(tab: chrome.tabs.Tab): Promise<void> {
  if (tab.id === undefined) return;
  const pattern = originPattern(tab.url || '');
  void chrome.sidePanel.open({ tabId: tab.id });
  if (!pattern) return;
  let granted = await isPermitted(tab.url || '');
  if (!granted) {
    try {
      granted = await chrome.permissions.request({ origins: [pattern] });
    } catch {
      granted = false;
    }
  }
  if (!granted) return;
  try {
    await api('/api/control/allow-domain', { method: 'POST', body: JSON.stringify({ url: tab.url }) });
    await setStatus({ lastError: null });
  } catch (e) {
    await setStatus({ lastError: `Site allowed in Chrome, but the agent could not be told: ${(e as Error).message}` });
  }
  connection.send({ type: 'tab_event', tab_id: tab.id, url: tab.url || '', title: tab.title || '', active: true, event: 'taken_over' });
}
chrome.action.onClicked.addListener((tab) => void takeOverTab(tab));

// ------------------------------------------------------------------ lifecycle
chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false });
  void connectNow();
});
chrome.runtime.onStartup.addListener(() => void connectNow());
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) void connectNow();
});

// Tab events → agent (only URL/title of permitted or active tabs; never page content).
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    connection.send({ type: 'tab_event', tab_id: tabId, url: tab.url || '', title: tab.title || '', active: true, event: 'activated' });
  } catch {
    /* tab gone */
  }
});
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete') connection.send({ type: 'tab_event', tab_id: tabId, url: tab.url || '', title: tab.title || '', active: !!tab.active, event: 'updated' });
});
// New tabs opened by page scripts (e.g. an "Open" button that calls window.open) are reported with
// their opener so the agent can follow a click into the tab it created.
chrome.tabs.onCreated.addListener((tab) => {
  if (tab.id === undefined) return;
  connection.send({ type: 'tab_event', tab_id: tab.id, url: tab.pendingUrl || tab.url || '', title: tab.title || '', active: !!tab.active, event: 'created', opener_tab_id: tab.openerTabId });
});

// ------------------------------------------------------------------ UI messages
chrome.runtime.onMessage.addListener((raw: UiRequest | { kind: string }, _sender, sendResponse) => {
  const msg = raw as UiRequest;
  (async () => {
    switch (msg.kind) {
      case 'get_state': {
        const [cfg, status, tab] = await Promise.all([getConfig(), getStatus(), chrome.tabs.query({ active: true, lastFocusedWindow: true })]);
        const active = tab[0];
        const url = active?.url || '';
        return { config: { agentUrl: cfg.agentUrl, paired: !!cfg.token, pairedAt: cfg.pairedAt }, status: { ...status, connected: connection.isOpen }, tab: active ? { id: active.id, url, title: active.title, permitted: await isPermitted(url), origin: originPattern(url) } : null };
      }
      case 'reconnect':
        connection.disconnect();
        await connectNow();
        return { ok: connection.isOpen };
      case 'disconnect':
        connection.disconnect();
        await setStatus({ connected: false, lastError: 'Disconnected by user (reconnects on the next alarm).' });
        return { ok: true };
      case 'pair': {
        const res = await claimPairing(msg.code);
        await setConfig({ token: res.token, pairedAt: new Date().toISOString() });
        connection.disconnect();
        await connectNow();
        return { ok: true, connected: connection.isOpen };
      }
      case 'unpair':
        connection.disconnect();
        await setConfig({ token: null, pairedAt: null });
        await setStatus({ connected: false, lastError: 'Unpaired.' });
        return { ok: true };
      case 'request_site_permission': {
        // Must be triggered from a user gesture in an extension page (side panel button).
        const granted = await chrome.permissions.request({ origins: [msg.origin] });
        return { ok: granted };
      }
      case 'open_side_panel':
        await chrome.sidePanel.open({ tabId: msg.tabId });
        return { ok: true };
      default:
        return undefined;
    }
  })()
    .then((r) => sendResponse(r))
    .catch((e: Error) => sendResponse({ ok: false, error: e.message }));
  return true; // async response
});

void connectNow();
