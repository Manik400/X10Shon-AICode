/** Messages exchanged over the agent WebSocket and between extension contexts. */
import type { ActionPayload, ActionResult, BrowserAction, PageSnapshot } from '../dom/types';

// ---- agent -> extension (requests carry an id; replies echo it) -----------------------------
export type AgentRequest =
  | { type: 'ping' }
  | { type: 'list_tabs'; id: string }
  | { type: 'open_tab'; id: string; url: string; active?: boolean; opener_tab_id?: number; home_url?: string }
  | { type: 'protect_tab'; id: string; tab_id: number }
  | { type: 'capabilities'; id: string }
  | { type: 'activate_tab'; id: string; tab_id: number }
  | { type: 'reload_extension'; reason?: string }
  | { type: 'notify'; title: string; message: string; tab_id?: number | null; key?: string }
  | { type: 'close_tab'; id: string; tab_id: number }
  | { type: 'navigate'; id: string; tab_id: number; url: string; timeout_ms: number }
  | { type: 'snapshot'; id: string; tab_id: number; max_nodes?: number }
  | { type: 'action'; id: string; tab_id: number; action: BrowserAction; payload?: ActionPayload }
  | { type: 'status_update'; status: AgentStatus }
  | { type: 'welcome'; agent_version: string };

export interface AgentStatus {
  paused_all: boolean;
  current_task: Record<string, unknown> | null;
  pending_approvals: number;
  waiting_user: Record<string, unknown> | null;
}

export type ExtensionReply =
  | { type: 'pong' }
  | { type: 'hello'; extension_id: string; version: string; granted_origins: string[] }
  | { type: 'tabs'; id: string; tabs: TabSummary[] }
  | { type: 'tab_opened'; id: string; ok: boolean; tab_id?: number; error?: string }
  | { type: 'tab_closed'; id: string; ok: boolean }
  | { type: 'tab_activated'; id: string; ok: boolean; error?: string }
  | { type: 'capabilities_result'; id: string; features: string[]; version: string }
  | { type: 'navigate_result'; id: string; result: ActionResult }
  | { type: 'snapshot_result'; id: string; ok: boolean; snapshot?: PageSnapshot; error?: string }
  | { type: 'action_result'; id: string; result: ActionResult }
  | { type: 'tab_event'; tab_id: number; url: string; title: string; active: boolean; event: string; opener_tab_id?: number }
  | { type: 'error'; id?: string; error: string };

export interface TabSummary {
  tab_id: number;
  url: string;
  title: string;
  active: boolean;
  permitted: boolean;
}

// ---- in-extension messages (runtime.sendMessage) ------------------------------------------
export type ContentRequest = { kind: 'snapshot'; max_nodes?: number } | { kind: 'action'; action: BrowserAction; payload?: ActionPayload | null } | { kind: 'ping' };

export type UiRequest =
  | { kind: 'get_state' }
  | { kind: 'reconnect' }
  | { kind: 'disconnect' }
  | { kind: 'pair'; code: string }
  | { kind: 'unpair' }
  | { kind: 'request_site_permission'; origin: string }
  | { kind: 'open_side_panel'; tabId: number };
