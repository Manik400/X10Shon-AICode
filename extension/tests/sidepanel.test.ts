// @vitest-environment jsdom
import html from '../src/static/sidepanel.html?raw';
import { describe, expect, it, vi } from 'vitest';

/** The side panel must start (render, wire every button) without throwing. */
describe('side panel', () => {
  it('starts, renders the connection state and wires the guide buttons', async () => {
    document.documentElement.innerHTML = html.replace(/<script[^>]*><\/script>/g, '');
    const state = {
      config: { agentUrl: 'http://127.0.0.1:8765', paired: true, pairedAt: null },
      status: { connected: true, lastError: null, currentTask: null, pausedAll: false, pendingApprovals: 0, waitingUser: null },
      tab: { id: 7, url: 'https://jobs.example.test/1', title: 'Job', permitted: true, origin: 'https://jobs.example.test' },
    };
    const listeners: unknown[] = [];
    (globalThis as unknown as { chrome: unknown }).chrome = {
      runtime: { sendMessage: vi.fn(async () => state), onMessage: { addListener: (f: unknown) => listeners.push(f) }, getManifest: () => ({ version: 'test' }) },
      tabs: { onActivated: { addListener: () => undefined }, onUpdated: { addListener: () => undefined } },
      storage: { local: { get: vi.fn(async () => ({ agentUrl: 'http://127.0.0.1:8765', token: 't' })), set: vi.fn() }, session: { get: vi.fn(async () => ({})), set: vi.fn() } },
    };
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ items: [], scheduler: { approval_policy: 'never' } }), { status: 200, headers: { 'content-type': 'application/json' } })) as unknown as typeof fetch;
    const errors: unknown[] = [];
    window.addEventListener('error', (e) => errors.push(e.error ?? e.message));
    await import('../src/sidepanel/sidepanel');
    await new Promise((r) => setTimeout(r, 50));
    expect(errors).toEqual([]);
    expect(document.getElementById('conn')?.textContent).toBe('connected');
    (document.getElementById('infoToggle') as HTMLButtonElement).click();
    const chip = document.querySelector<HTMLButtonElement>('#infoPanel .chip')!;
    chip.click();
    expect((document.getElementById('instruction') as HTMLTextAreaElement).value).toBe(chip.dataset.prompt);
  });
});
