/** Typed wrappers over chrome.storage. The pairing token lives in `local` (survives restarts). */

export interface AgentConfig {
  agentUrl: string; // http://127.0.0.1:8765
  token: string | null;
  pairedAt: string | null;
}

export interface LiveStatus {
  connected: boolean;
  agentReachable: boolean;
  lastError: string | null;
  currentTask: Record<string, unknown> | null;
  pausedAll: boolean;
  pendingApprovals: number;
  waitingUser: Record<string, unknown> | null;
  updatedAt: string;
}

const DEFAULT_CONFIG: AgentConfig = { agentUrl: 'http://127.0.0.1:8765', token: null, pairedAt: null };

export async function getConfig(): Promise<AgentConfig> {
  const r = await chrome.storage.local.get('config');
  return { ...DEFAULT_CONFIG, ...((r.config as Partial<AgentConfig>) || {}) };
}

export async function setConfig(patch: Partial<AgentConfig>): Promise<AgentConfig> {
  const cur = await getConfig();
  const next = { ...cur, ...patch };
  await chrome.storage.local.set({ config: next });
  return next;
}

export const DEFAULT_STATUS: LiveStatus = {
  connected: false,
  agentReachable: false,
  lastError: null,
  currentTask: null,
  pausedAll: false,
  pendingApprovals: 0,
  waitingUser: null,
  updatedAt: new Date(0).toISOString(),
};

export async function getStatus(): Promise<LiveStatus> {
  const r = await chrome.storage.session.get('status');
  return { ...DEFAULT_STATUS, ...((r.status as Partial<LiveStatus>) || {}) };
}

export async function setStatus(patch: Partial<LiveStatus>): Promise<LiveStatus> {
  const cur = await getStatus();
  const next = { ...cur, ...patch, updatedAt: new Date().toISOString() };
  await chrome.storage.session.set({ status: next });
  try {
    await chrome.runtime.sendMessage({ kind: 'status_changed', status: next });
  } catch {
    /* no listeners open */
  }
  return next;
}

export function wsUrl(agentUrl: string): string {
  return agentUrl.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws/extension';
}
