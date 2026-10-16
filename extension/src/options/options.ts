import type { UiRequest } from '../lib/protocol';
import { getConfig, setConfig } from '../lib/storage';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

async function refresh(): Promise<void> {
  const cfg = await getConfig();
  ($('agentUrl') as HTMLInputElement).value = cfg.agentUrl;
  const p = $('paired');
  p.textContent = cfg.token ? `paired ${cfg.pairedAt ? new Date(cfg.pairedAt).toLocaleString() : ''}` : 'not paired';
  p.className = `pill ${cfg.token ? 'ok' : 'warn'}`;
  const perms = await chrome.permissions.getAll();
  const ul = $('origins');
  ul.innerHTML = '';
  for (const o of perms.origins || []) {
    if (/127\.0\.0\.1|localhost/.test(o)) continue;
    const li = document.createElement('li');
    li.textContent = o + ' ';
    const b = document.createElement('button');
    b.textContent = 'Revoke';
    b.onclick = async () => {
      await chrome.permissions.remove({ origins: [o] });
      await refresh();
    };
    li.append(b);
    ul.append(li);
  }
  if (!ul.children.length) ul.innerHTML = '<li class="muted">No sites allowed yet.</li>';
}

$('saveUrl').onclick = async () => {
  const url = ($('agentUrl') as HTMLInputElement).value.trim();
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/.test(url)) {
    $('msg').textContent = 'Only http://127.0.0.1:<port> or http://localhost:<port> are accepted.';
    return;
  }
  await setConfig({ agentUrl: url.replace(/\/$/, '') });
  $('msg').textContent = 'Saved.';
  await chrome.runtime.sendMessage({ kind: 'reconnect' } as UiRequest);
};
$('pair').onclick = async () => {
  const code = ($('code') as HTMLInputElement).value.trim().toUpperCase();
  if (code.length !== 8) {
    $('msg').textContent = 'Enter the 8-character code.';
    return;
  }
  $('msg').textContent = 'Pairing…';
  const r = (await chrome.runtime.sendMessage({ kind: 'pair', code } as UiRequest)) as { ok: boolean; error?: string; connected?: boolean };
  $('msg').textContent = r.ok ? (r.connected ? 'Paired and connected.' : 'Paired. Connecting…') : `Pairing failed: ${r.error || 'unknown error'}`;
  await refresh();
};
$('unpair').onclick = async () => {
  await chrome.runtime.sendMessage({ kind: 'unpair' } as UiRequest);
  $('msg').textContent = 'Unpaired. Revoke the pairing in the dashboard too.';
  await refresh();
};
// Pairing code passed in the URL (e.g. opened by a launcher): pair immediately.
const urlCode = new URLSearchParams(location.search).get('code');
if (urlCode && urlCode.length === 8) {
  ($('code') as HTMLInputElement).value = urlCode.toUpperCase();
  ($('pair') as HTMLButtonElement).click();
}
void refresh();
