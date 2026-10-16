/**
 * WebSocket connection to the local agent.
 *
 * Reconnects forever: exponential backoff (1 s → 60 s cap, unlimited attempts) inside the worker
 * while it is alive, plus a chrome.alarms safety net that re-enters connect() after the service
 * worker was suspended. A rejected token (agent database reset, pairing revoked) clears the stored
 * token and immediately tries code-less re-pairing before the next attempt.
 */
import type { AgentRequest, ExtensionReply } from './protocol';
import { autoPair } from './api';
import { getConfig, setConfig, setStatus, wsUrl } from './storage';

export type RequestHandler = (msg: AgentRequest, send: (reply: ExtensionReply) => void) => Promise<void> | void;

const HEARTBEAT_MS = 20_000;
const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 60_000;

export class AgentConnection {
  private ws: WebSocket | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private connecting = false;
  private backoffMs = BACKOFF_MIN_MS;
  private stopped = false;
  attempts = 0;

  constructor(private readonly onRequest: RequestHandler) {}

  get isOpen(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  /** Connect now; on failure schedule the next attempt with backoff (never gives up). */
  async connect(): Promise<void> {
    if (this.isOpen || this.connecting) return;
    this.stopped = false;
    this.connecting = true;
    this.attempts += 1;
    try {
      let cfg = await getConfig();
      if (!cfg.token) {
        try {
          const res = await autoPair();
          cfg = await setConfig({ token: res.token, pairedAt: new Date().toISOString() });
        } catch (e) {
          await setStatus({ connected: false, agentReachable: false, lastError: `Not paired yet (${(e as Error).message}). Retrying automatically; or enter a pairing code in the options page.` });
          this.scheduleRetry();
          return;
        }
      }
      const url = `${wsUrl(cfg.agentUrl)}?token=${encodeURIComponent(cfg.token!)}`;
      const ws = new WebSocket(url);
      this.ws = ws;
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('connection timeout')), 8000);
        ws.onopen = () => {
          clearTimeout(timer);
          resolve();
        };
        ws.onerror = () => {
          clearTimeout(timer);
          reject(new Error('socket error'));
        };
        ws.onclose = (ev) => {
          clearTimeout(timer);
          reject(new Error(ev.code === 4001 ? 'token rejected' : `closed ${ev.code}`));
        };
      });
      ws.onmessage = (ev) => void this.handleMessage(ev);
      ws.onclose = (ev) => void this.handleClose(ev);
      ws.onerror = () => undefined;
      this.backoffMs = BACKOFF_MIN_MS;
      const granted = await chrome.permissions.getAll();
      this.send({ type: 'hello', extension_id: chrome.runtime.id, version: chrome.runtime.getManifest().version, granted_origins: granted.origins || [] });
      this.startHeartbeat();
      await setStatus({ connected: true, agentReachable: true, lastError: null });
    } catch (e) {
      const msg = (e as Error).message || 'connect failed';
      this.ws = null;
      if (msg === 'token rejected') {
        // The agent no longer knows our token: forget it so the next attempt re-pairs.
        await setConfig({ token: null, pairedAt: null });
        await setStatus({ connected: false, agentReachable: true, lastError: 'Pairing token rejected by the agent; re-pairing automatically.' });
      } else {
        await setStatus({ connected: false, agentReachable: false, lastError: `Cannot reach the local agent (${msg}). Retrying in ${Math.round(this.backoffMs / 1000)} s.` });
      }
      this.scheduleRetry();
    } finally {
      this.connecting = false;
    }
  }

  disconnect(): void {
    this.stopped = true;
    this.stopHeartbeat();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.ws) {
      try {
        this.ws.close(1000, 'client disconnect');
      } catch {
        /* ignore */
      }
    }
    this.ws = null;
  }

  send(reply: ExtensionReply): void {
    if (!this.isOpen || !this.ws) return;
    this.ws.send(JSON.stringify(reply));
  }

  nextRetryMs(): number {
    return this.backoffMs;
  }

  private scheduleRetry(): void {
    if (this.stopped) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    const delay = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => {
      if (this.isOpen) this.send({ type: 'pong' });
    }, HEARTBEAT_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  private async handleClose(ev: CloseEvent): Promise<void> {
    this.stopHeartbeat();
    this.ws = null;
    if (ev.code === 4001) {
      await setConfig({ token: null, pairedAt: null });
      await setStatus({ connected: false, lastError: 'Pairing token rejected; re-pairing automatically.', currentTask: null });
    } else {
      await setStatus({ connected: false, lastError: ev.code === 4000 ? 'Replaced by a newer connection.' : `Disconnected (${ev.code}); reconnecting.`, currentTask: null });
    }
    if (ev.code !== 4000 && !this.stopped) this.scheduleRetry();
  }

  private async handleMessage(ev: MessageEvent): Promise<void> {
    let msg: AgentRequest;
    try {
      msg = JSON.parse(String(ev.data)) as AgentRequest;
    } catch {
      return;
    }
    if (msg.type === 'ping') {
      this.send({ type: 'pong' });
      return;
    }
    try {
      await this.onRequest(msg, (reply) => this.send(reply));
    } catch (e) {
      const id = 'id' in msg ? (msg as { id: string }).id : undefined;
      this.send({ type: 'error', id, error: (e as Error).message?.slice(0, 300) || 'handler failed' });
    }
  }
}
