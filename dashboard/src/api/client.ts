/**
 * Single API client. For a remote deployment only BASE_URL and the auth header need to change.
 */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

const BASE_URL = (import.meta.env?.VITE_API_BASE as string | undefined) ?? '';
const CLIENT_HEADER = 'X-LFA-Client';

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { [CLIENT_HEADER]: 'dashboard', ...(init.headers as Record<string, string> | undefined) };
  if (init.body && typeof init.body === 'string' && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  let res: Response;
  try {
    res = await fetch(BASE_URL + path, { ...init, headers });
  } catch {
    throw new ApiError(0, 'The local agent is not reachable. Start it with `python -m app.main` in agent/.');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* no JSON body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get('content-type') || '';
  return (ct.includes('application/json') ? await res.json() : await res.text()) as T;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: 'PUT', body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' });

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export function downloadUrl(path: string): string {
  return BASE_URL + path;
}

export async function download(path: string, filename: string): Promise<void> {
  const res = await fetch(BASE_URL + path, { headers: { [CLIENT_HEADER]: 'dashboard' } });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
