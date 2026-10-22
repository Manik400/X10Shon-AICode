/**
 * Service-worker routing tests with a fake `chrome` API: permission enforcement, tab routing,
 * navigation handling and the pairing/UI message surface.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

type Listener = (...args: unknown[]) => unknown;

function makeChrome() {
  const onMessage: Listener[] = [];
  const tabsUpdated: Listener[] = [];
  const granted = new Set<string>(['http://127.0.0.1/*']);
  const tabs = new Map<number, { id: number; url: string; title: string; status: string; active: boolean }>([[1, { id: 1, url: 'https://jobs.example.com/apply', title: 'Apply', status: 'complete', active: true }]]);
  const contentResponses: Record<string, unknown> = {};
  const injected: number[] = [];
  const actionClicked: Listener[] = [];
  const chrome = {
    action: { onClicked: { addListener: (l: Listener) => actionClicked.push(l) } },
    runtime: {
      id: 'abcdefghijklmnopabcdefghijklmnop',
      onMessage: { addListener: (l: Listener) => onMessage.push(l) },
      onInstalled: { addListener: () => undefined },
      onStartup: { addListener: () => undefined },
      getManifest: () => ({ version: '0.1.0' }),
      sendMessage: vi.fn(async () => undefined),
      lastError: undefined as undefined | { message: string },
    },
    alarms: { create: vi.fn(async () => undefined), get: vi.fn(async () => undefined), clear: vi.fn(async () => undefined), onAlarm: { addListener: () => undefined } },
    permissions: {
      contains: vi.fn(async ({ origins }: { origins: string[] }) => origins.every((o) => granted.has(o))),
      request: vi.fn(async ({ origins }: { origins: string[] }) => {
        origins.forEach((o) => granted.add(o));
        return true;
      }),
      getAll: vi.fn(async () => ({ origins: Array.from(granted) })),
      remove: vi.fn(async () => true),
    },
    tabs: {
      query: vi.fn(async () => Array.from(tabs.values())),
      get: vi.fn(async (id: number) => tabs.get(id)!),
      update: vi.fn(async (id: number, { url }: { url: string }) => {
        const t = tabs.get(id)!;
        t.url = url;
        t.status = 'complete';
        return t;
      }),
      create: vi.fn(async ({ url }: { url: string }) => {
        const t = { id: 2, url, title: '', status: 'complete', active: false };
        tabs.set(2, t);
        return t;
      }),
      remove: vi.fn(async () => undefined),
      sendMessage: vi.fn((_tabId: number, msg: { kind: string }, _opts: unknown, cb: (r: unknown) => void) => {
        cb(contentResponses[msg.kind] ?? { ok: true });
      }),
      onActivated: { addListener: () => undefined },
      onCreated: { addListener: () => undefined },
      onUpdated: { addListener: (l: Listener) => tabsUpdated.push(l), removeListener: () => undefined },
    },
    windows: { update: vi.fn(async () => undefined) },
    scripting: { executeScript: vi.fn(async ({ target }: { target: { tabId: number } }) => void injected.push(target.tabId)) },
    sidePanel: { setPanelBehavior: vi.fn(async () => undefined), open: vi.fn(async () => undefined) },
    storage: {
      local: { get: vi.fn(async () => ({ config: { agentUrl: 'http://127.0.0.1:8765', token: null, pairedAt: null } })), set: vi.fn(async () => undefined) },
      session: { get: vi.fn(async () => ({})), set: vi.fn(async () => undefined) },
    },
  };
  return { chrome, onMessage, granted, tabs, contentResponses, injected, actionClicked };
}

// Capture the agent-request handler by intercepting the AgentConnection constructor.
const captured: { handler?: (msg: unknown, send: (r: unknown) => void) => Promise<void> | void } = {};
vi.mock('../src/lib/connection', () => ({
  AgentConnection: class {
    isOpen = false;
    constructor(handler: (msg: unknown, send: (r: unknown) => void) => Promise<void>) {
      captured.handler = handler;
    }
    async connect() {
      return undefined;
    }
    disconnect() {
      return undefined;
    }
    send() {
      return undefined;
    }
    nextRetryMs() {
      return 1000;
    }
  },
}));

let fake: ReturnType<typeof makeChrome>;

beforeEach(async () => {
  vi.resetModules();
  fake = makeChrome();
  (globalThis as unknown as { chrome: unknown }).chrome = fake.chrome;
  await import('../src/background/service-worker');
});

async function ask(msg: unknown): Promise<unknown> {
  const replies: unknown[] = [];
  await captured.handler!(msg, (r) => replies.push(r));
  return replies[0];
}

describe('service worker agent routing', () => {
  it('refuses actions on sites the user has not allowed', async () => {
    const reply = (await ask({ type: 'snapshot', id: 'r1', tab_id: 1 })) as { ok: boolean; error: string };
    expect(reply.ok).toBe(false);
    expect(reply.error).toMatch(/PERMISSION_DENIED/);
    expect(fake.injected).toEqual([]);
  });

  it('injects the content script and forwards snapshots on allowed sites', async () => {
    fake.granted.add('https://jobs.example.com/*');
    fake.contentResponses.ping = { ok: false };
    fake.contentResponses.snapshot = { url: 'https://jobs.example.com/apply', nodes: [] };
    const reply = (await ask({ type: 'snapshot', id: 'r2', tab_id: 1 })) as { ok: boolean; snapshot: { url: string } };
    expect(reply.ok).toBe(true);
    expect(reply.snapshot.url).toBe('https://jobs.example.com/apply');
    expect(fake.injected).toEqual([1]);
  });

  it('forwards actions and returns the content result', async () => {
    fake.granted.add('https://jobs.example.com/*');
    fake.contentResponses.action = { ok: true, action_type: 'type', value: 'Ada', duration_ms: 2, evidence: {} };
    const reply = (await ask({ type: 'action', id: 'r3', tab_id: 1, action: { type: 'type', target: { strategy: 'id', value: 'x' }, args: { text: 'Ada' }, timeout_ms: 1000, risk: 'low', verification: 'none' } })) as { result: { value: string } };
    expect(reply.result.value).toBe('Ada');
  });

  it('blocks navigation to non-allowed origins and performs allowed ones', async () => {
    const denied = (await ask({ type: 'navigate', id: 'r4', tab_id: 1, url: 'https://evil.example.net/', timeout_ms: 1000 })) as { result: { ok: boolean; error_category: string } };
    expect(denied.result.ok).toBe(false);
    expect(denied.result.error_category).toBe('PERMISSION_DENIED');
    fake.granted.add('https://jobs.example.com/*');
    const ok = (await ask({ type: 'navigate', id: 'r5', tab_id: 1, url: 'https://jobs.example.com/next', timeout_ms: 1000 })) as { result: { ok: boolean; url: string } };
    expect(ok.result.ok).toBe(true);
    expect(ok.result.url).toBe('https://jobs.example.com/next');
  });

  it('takes over the tab on icon click: side panel, per-site grant, agent allow-list', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ allowed_domains: ['jobs.example.com'] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await fake.actionClicked[0](fake.tabs.get(1));
    await new Promise((r) => setTimeout(r, 20)); // permission request and allow-domain call happen after awaits
    expect(fake.chrome.sidePanel.open).toHaveBeenCalledWith({ tabId: 1 });
    expect(fake.chrome.permissions.request).toHaveBeenCalledWith({ origins: ['https://jobs.example.com/*'] });
    expect(fake.granted.has('https://jobs.example.com/*')).toBe(true);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/control\/allow-domain$/);
    expect(JSON.parse(init.body as string).url).toBe('https://jobs.example.com/apply');
    fetchSpy.mockRestore();
  });

  it('lists tabs with their permission state', async () => {
    const reply = (await ask({ type: 'list_tabs', id: 'r6' })) as { tabs: { tab_id: number; permitted: boolean }[] };
    expect(reply.tabs[0].tab_id).toBe(1);
    expect(reply.tabs[0].permitted).toBe(false);
  });
});
