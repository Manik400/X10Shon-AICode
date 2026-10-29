import { api, ApiError } from '../lib/api';
import type { UiRequest } from '../lib/protocol';
import { getConfig } from '../lib/storage';

interface State {
  config: { agentUrl: string; paired: boolean; pairedAt: string | null };
  status: { connected: boolean; lastError: string | null; currentTask: Record<string, unknown> | null; pausedAll: boolean; pendingApprovals: number; waitingUser: Record<string, unknown> | null };
  tab: { id: number; url: string; title: string; permitted: boolean; origin: string | null } | null;
}

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

function pill(el: HTMLElement, text: string, cls: 'ok' | 'warn' | 'err' | '') {
  el.textContent = text;
  el.className = `pill ${cls}`;
}

async function send<T = unknown>(msg: UiRequest): Promise<T> {
  return chrome.runtime.sendMessage(msg) as Promise<T>;
}

let state: State | null = null;

interface TabTask {
  id: string;
  kind: string;
  status: string;
  tab_id: number | null;
  action_count: number;
  progress_json?: { message?: string } | null;
}
/** The task belonging to THIS panel's tab (each tab has its own context). */
let tabTask: TabTask | null = null;

async function loadTabTask(): Promise<void> {
  if (!state?.tab) {
    tabTask = null;
    return;
  }
  try {
    const r = await api<{ items: TabTask[] }>('/api/tasks?limit=25');
    const mine = r.items.filter((t) => t.tab_id === state!.tab!.id && ['pending', 'running', 'waiting_approval', 'waiting_user'].includes(t.status));
    tabTask = mine[0] ?? null;
  } catch {
    tabTask = null;
  }
}

async function refresh(): Promise<void> {
  // Each window's side panel follows the active tab of ITS window: two windows open at once
  // must not show (or act on) the same tab.
  let windowId: number | undefined;
  try {
    windowId = (await chrome.windows.getCurrent()).id;
  } catch {
    windowId = undefined;
  }
  state = await send<State>({ kind: 'get_state', windowId });
  if (state.config.paired && state.status.connected) await loadTabTask();
  render();
  if (state.config.paired && state.status.connected) {
    void loadApprovals();
    void loadPolicy();
  }
}

async function loadPolicy(): Promise<void> {
  try {
    const s = await api<{ scheduler: { approval_policy: string } }>('/api/settings');
    ($('autoSubmit') as HTMLInputElement).checked = s.scheduler.approval_policy === 'never';
  } catch {
    /* agent offline */
  }
}

async function startTask(instruction: string, target: HTMLElement): Promise<void> {
  if (!state?.tab) return;
  if (!state.tab.permitted) {
    target.textContent = 'Allow this site first (click the toolbar icon or "Allow this site").';
    return;
  }
  target.textContent = 'Starting…';
  try {
    const r = await api<{ id: string; kind: string }>('/api/tasks', { method: 'POST', body: JSON.stringify({ instruction, tab_id: state.tab.id, url: state.tab.url }) });
    target.textContent = `${r.kind.replace('_', ' ')} started…`;
    void followTask(r.id, target);
  } catch (e) {
    target.textContent = e instanceof ApiError ? e.message : 'Failed';
  }
}

interface TaskView {
  task: { status: string; kind: string; progress_json?: { message?: string } | null; result_json?: Record<string, unknown> | null; failure_json?: { message?: string } | null };
}

/** Show live progress and the final result of a task in the panel (polls only while it runs). */
async function followTask(id: string, target: HTMLElement): Promise<void> {
  for (let i = 0; i < 2400; i++) {
    let v: TaskView;
    try {
      v = await api<TaskView>(`/api/tasks/${id}`);
    } catch {
      await new Promise((r) => setTimeout(r, 3000));
      continue;
    }
    const t = v.task;
    if (t.status === 'running' || t.status === 'pending' || t.status === 'waiting_approval') {
      target.textContent = `${t.kind.replace('_', ' ')}: ${t.progress_json?.message ?? 'working…'}`;
      await new Promise((r) => setTimeout(r, 3000));
      continue;
    }
    const res = t.result_json ?? {};
    const lines: string[] = [];
    if (typeof res.answer === 'string') {
      // Assistant mode: the full answer, plus where it came from.
      lines.push(res.answer);
      lines.push(`\n— from this ${res.source === 'pdf' ? 'PDF' : 'page'}${res.truncated ? ' (long: only the first part was read)' : ''}`);
    } else if (typeof res.summary === 'string') lines.push(res.summary);
    if (Array.isArray(res.items)) {
      lines.push(`${res.confirmed ?? 0} applied · ${res.waiting_user ?? 0} need you · ${res.failed ?? 0} failed of ${res.jobs ?? res.items.length}`);
      for (const it of (res.items as { state?: string; url: string; note?: string }[]).slice(0, 30)) {
        lines.push(`• ${it.state ?? '?'} — ${it.url.replace(/^https?:\/\//, '').slice(0, 60)}${it.note ? ` (${it.note.slice(0, 80)})` : ''}`);
      }
    }
    if (Array.isArray(res.emails)) {
      lines.push(...(res.emails as string[]).map((e) => `✉ ${e}`));
      lines.push(...(res.linkedin_profiles as { name: string; url: string }[]).map((p) => `in ${p.name || ''} ${p.url}`));
      lines.push(...(res.people as { name: string; role: string }[]).map((p) => `👤 ${p.name} (${p.role})`));
    }
    if (t.failure_json?.message) lines.push(`${t.status}: ${t.failure_json.message}`);
    target.textContent = lines.join('\n') || t.status;
    target.style.whiteSpace = 'pre-wrap';
    return;
  }
}

function render(): void {
  if (!state) return;
  const { config, status, tab } = state;
  pill($('conn'), status.connected ? 'connected' : 'disconnected', status.connected ? 'ok' : 'err');
  pill($('paired'), config.paired ? 'paired' : 'not paired', config.paired ? 'ok' : 'warn');
  const err = $('connError');
  err.hidden = !status.lastError;
  err.textContent = status.lastError || '';

  if (tab) {
    $('tabTitle').textContent = tab.title || '(untitled)';
    $('tabUrl').textContent = tab.url;
    const allowable = !!tab.origin;
    pill($('sitePermitted'), tab.permitted ? 'allowed' : allowable ? 'not allowed' : 'n/a', tab.permitted ? 'ok' : allowable ? 'warn' : '');
    ($('allowSite') as HTMLButtonElement).disabled = tab.permitted || !allowable;
    ($('importJob') as HTMLButtonElement).disabled = !allowable || !status.connected;
    ($('applyHere') as HTMLButtonElement).disabled = !allowable || !status.connected;
    ($('scoreHere') as HTMLButtonElement).disabled = !allowable || !status.connected;
  }

  // Only this tab's task is shown: other tabs' work never mixes into this panel.
  const agentState = $('agentState');
  const t = tabTask;
  if (status.pausedAll) pill(agentState, 'paused', 'warn');
  else if (t?.status === 'waiting_user') pill(agentState, 'waiting for you', 'warn');
  else if (t) pill(agentState, 'running on this tab', 'ok');
  else pill(agentState, 'idle on this tab', '');

  $('taskBox').textContent = t ? `${t.kind.replace('_', ' ')} · ${t.status} · step ${t.action_count ?? 0} · ${t.progress_json?.message ?? ''}` : 'No task on this tab.';
  const w = $('waitingBox');
  if (t?.status === 'waiting_user') {
    w.hidden = false;
    w.innerHTML = '';
    const p = document.createElement('div');
    p.textContent = t.progress_json?.message || 'The agent needs you to act on this tab.';
    const b = document.createElement('button');
    b.textContent = 'I have done it – resume';
    b.className = 'primary';
    b.onclick = () => void resume(t.id);
    w.append(p, b);
  } else {
    w.hidden = true;
  }
  ($('cancelTask') as HTMLButtonElement).disabled = !t;
}

async function resume(taskId: string): Promise<void> {
  if (!taskId) return;
  try {
    await api(`/api/tasks/${taskId}/resume`, { method: 'POST' });
    $('runMsg').textContent = 'Resumed.';
  } catch (e) {
    $('runMsg').textContent = (e as Error).message;
  }
}

async function loadApprovals(): Promise<void> {
  const box = $('approvalsBox');
  try {
    const list = await api<{ items: { id: string; kind: string; payload_json: Record<string, unknown> }[] }>('/api/approvals?status=pending');
    box.innerHTML = '';
    for (const a of list.items) {
      const card = document.createElement('div');
      card.className = 'notice';
      const txt = document.createElement('div');
      txt.textContent = `${a.kind}: ${String(a.payload_json.summary || JSON.stringify(a.payload_json).slice(0, 200))}`;
      const row = document.createElement('div');
      row.className = 'row';
      const ok = document.createElement('button');
      ok.className = 'primary';
      ok.textContent = 'Approve';
      ok.onclick = async () => {
        await api(`/api/approvals/${a.id}/decide`, { method: 'POST', body: JSON.stringify({ decision: 'approved' }) });
        await loadApprovals();
      };
      const no = document.createElement('button');
      no.className = 'danger';
      no.textContent = 'Reject';
      no.onclick = async () => {
        await api(`/api/approvals/${a.id}/decide`, { method: 'POST', body: JSON.stringify({ decision: 'rejected' }) });
        await loadApprovals();
      };
      row.append(ok, no);
      card.append(txt, row);
      box.append(card);
    }
  } catch {
    box.innerHTML = '';
  }
}

$('reconnect').onclick = async () => {
  await send({ kind: 'reconnect' });
  await refresh();
};
$('openDashboard').onclick = async () => {
  const cfg = await getConfig();
  await chrome.tabs.create({ url: cfg.agentUrl });
};
$('allowSite').onclick = async () => {
  if (!state?.tab?.origin) return;
  const r = await send<{ ok: boolean }>({ kind: 'request_site_permission', origin: state.tab.origin });
  if (r.ok) {
    try {
      await api('/api/control/allow-domain', { method: 'POST', body: JSON.stringify({ url: state.tab.url }) });
    } catch {
      /* reported through status */
    }
  }
  $('tabMsg').textContent = r.ok ? 'Site allowed for this extension and the agent.' : 'Permission not granted.';
  await refresh();
};
$('importJob').onclick = async () => {
  if (!state?.tab) return;
  $('tabMsg').textContent = 'Importing…';
  try {
    const r = await api<{ job: { id: string; title?: string; company?: string }; duplicate: boolean }>('/api/jobs/import', {
      method: 'POST',
      body: JSON.stringify({ url: state.tab.url, source_kind: 'manual', tab_id: state.tab.id }),
    });
    $('tabMsg').textContent = r.duplicate ? 'Already imported (duplicate).' : `Imported: ${r.job.company || ''} ${r.job.title || ''}`.trim();
  } catch (e) {
    $('tabMsg').textContent = e instanceof ApiError ? e.message : 'Import failed';
  }
};
$('applyHere').onclick = () => void startTask('Apply to the job on this page', $('applyMsg'));
$('scoreHere').onclick = () => void startTask('Import and score the job on this page', $('applyMsg'));
$('autoSubmit').onchange = async (e) => {
  const on = (e.target as HTMLInputElement).checked;
  try {
    await api('/api/control/approval-policy', { method: 'POST', body: JSON.stringify({ policy: on ? 'never' : 'always' }) });
    $('applyMsg').textContent = on ? 'Auto-submit on: applications are submitted without a confirmation prompt.' : 'Auto-submit off: you approve each submission.';
  } catch (err) {
    $('applyMsg').textContent = err instanceof ApiError ? err.message : 'Failed';
  }
};
$('pauseAll').onclick = async () => {
  await api('/api/control/pause', { method: 'POST' });
  await refresh();
};
$('resumeAll').onclick = async () => {
  await api('/api/control/resume', { method: 'POST' });
  await refresh();
};
$('cancelTask').onclick = async () => {
  if (!tabTask) return;
  await api(`/api/tasks/${tabTask.id}/cancel`, { method: 'POST' });
  await refresh();
};
// Keep this tab's task line current while the panel is visible (lightweight, only when shown).
setInterval(() => {
  if (document.visibilityState === 'visible' && state?.status.connected) void loadTabTask().then(render);
}, 4000);
$('runTask').onclick = async () => {
  const instruction = ($('instruction') as HTMLTextAreaElement).value.trim();
  if (!instruction || !state?.tab) return;
  await startTask(instruction, $('runMsg'));
};

// "How to use" guide: toggled by the ⓘ button; opens by itself the first time the panel is used.
function setInfo(open: boolean): void {
  $('infoPanel').hidden = !open;
  $('infoToggle').setAttribute('aria-expanded', String(open));
  try {
    localStorage.setItem('lfa.infoSeen', '1');
  } catch {
    /* storage unavailable: the guide simply opens again next time */
  }
}
$('infoToggle').onclick = () => setInfo($('infoPanel').hidden);
$('infoClose').onclick = () => setInfo(false);
for (const chip of Array.from(document.querySelectorAll<HTMLButtonElement>('#infoPanel .chip'))) {
  chip.onclick = () => {
    const box = $('instruction') as HTMLTextAreaElement;
    box.value = chip.dataset.prompt || chip.textContent || '';
    box.focus();
    if (typeof box.scrollIntoView === 'function') box.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
}
try {
  if (!localStorage.getItem('lfa.infoSeen')) setInfo(true);
} catch {
  /* ignore */
}

chrome.runtime.onMessage.addListener((msg: { kind?: string }) => {
  if (msg.kind === 'status_changed') void refresh();
});
chrome.tabs.onActivated.addListener(async ({ windowId }) => {
  // Only this window's tab switches concern this panel.
  try {
    if ((await chrome.windows.getCurrent()).id !== windowId) return;
  } catch {
    /* fall through: refresh anyway */
  }
  void refresh();
});
chrome.tabs.onUpdated.addListener(async (_id, info, tab) => {
  if (info.status !== 'complete') return;
  try {
    if (tab?.windowId !== undefined && (await chrome.windows.getCurrent()).id !== tab.windowId) return;
  } catch {
    /* fall through */
  }
  void refresh();
});
void refresh();
