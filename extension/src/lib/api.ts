/** REST client used by extension pages (side panel, popup, options). */
import { getConfig } from './storage';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}, opts: { auth?: boolean } = {}): Promise<T> {
  const cfg = await getConfig();
  const headers: Record<string, string> = { 'X-LFA-Client': 'extension', ...(init.headers as Record<string, string> | undefined) };
  if (init.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  if (opts.auth !== false && cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  let res: Response;
  try {
    res = await fetch(cfg.agentUrl.replace(/\/$/, '') + path, { ...init, headers });
  } catch (e) {
    throw new ApiError(0, `Local agent unreachable at ${cfg.agentUrl}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* no body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function claimPairing(code: string): Promise<{ token: string; pairing_id: string }> {
  return api('/api/pairing/claim', { method: 'POST', body: JSON.stringify({ code, extension_id: chrome.runtime.id }) }, { auth: false });
}

/** Code-less pairing: accepted by the agent only when this extension was loaded unpacked from the
 * project's own extension/dist folder on the same machine (origin id derived from that path). */
export async function autoPair(): Promise<{ token: string; pairing_id: string }> {
  return claimPairing('AUTOPAIR');
}
