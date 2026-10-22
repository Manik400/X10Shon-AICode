/**
 * Protocol types shared by the extension and the local agent. These mirror the pydantic models in
 * `agent/app/core/actions.py` and `agent/app/browser/snapshot.py`.
 */

export type ActionType =
  | 'navigate'
  | 'click'
  | 'type'
  | 'select'
  | 'check'
  | 'upload'
  | 'press_key'
  | 'scroll'
  | 'wait'
  | 'extract'
  | 'snapshot'
  | 'done'
  | 'ask_user'
  | 'banner'
  | 'read_options';

export type LocatorStrategy = 'role' | 'label' | 'testid' | 'id' | 'name' | 'text' | 'css' | 'node';

export type VerificationStrategy =
  | 'none'
  | 'url_change'
  | 'element_visible'
  | 'element_hidden'
  | 'text_present'
  | 'value_set'
  | 'navigation_complete';

export interface Target {
  strategy: LocatorStrategy;
  value: string;
  name?: string | null;
  section?: string | null;
  nth?: number;
  exact?: boolean;
}

export interface BrowserAction {
  type: ActionType;
  target?: Target | null;
  args: Record<string, unknown>;
  timeout_ms: number;
  expected_outcome?: string | null;
  risk: 'low' | 'medium' | 'high';
  verification: VerificationStrategy;
  verification_arg?: string | null;
}

export interface ActionResult {
  ok: boolean;
  action_type: string;
  error_category?: string | null;
  message: string;
  url?: string | null;
  title?: string | null;
  matched_count?: number | null;
  value?: unknown;
  duration_ms: number;
  evidence: Record<string, unknown>;
}

export interface SnapshotNode {
  i: number;
  role: string;
  name: string;
  tag: string;
  type?: string | null;
  value?: string | null;
  placeholder?: string | null;
  label?: string | null;
  checked?: boolean | null;
  disabled: boolean;
  required: boolean;
  options?: string[] | null;
  section?: string | null;
  text?: string | null;
  href?: string | null;
  testid?: string | null;
  id?: string | null;
  name_attr?: string | null;
  visible: boolean;
  form?: number | null;
  frame: number;
  in_dialog: boolean;
  autocomplete?: string | null;
  accept?: string | null;
  maxlength?: number | null;
  pattern?: string | null;
}

export interface FormInfo {
  index: number;
  fields: number[];
  submit_nodes: number[];
  name?: string | null;
  action?: string | null;
}

export interface PageSignals {
  has_password_field: boolean;
  has_email_or_username_field: boolean;
  has_captcha: boolean;
  captcha_solved?: boolean;
  has_otp_field: boolean;
  login_cta_present: boolean;
  logout_cta_present: boolean;
  signup_cta_present: boolean;
  password_recovery: boolean;
  account_locked: boolean;
  error_messages: string[];
  success_messages: string[];
  reference_numbers: string[];
  unsupported_reasons: string[];
  iframe_srcs: string[];
  has_jsonld_jobposting: boolean;
}

export interface PageSnapshot {
  url: string;
  title: string;
  nodes: SnapshotNode[];
  forms: FormInfo[];
  text_excerpt: string;
  signals: PageSignals;
  truncated: boolean;
  generation: number;
  jsonld: Record<string, unknown>[];
  frame_count: number;
  content_hash: string;
}

export interface SnapshotOptions {
  maxNodes?: number;
  maxTextChars?: number;
}

/** Agent-resolved payload for actions whose data the model must never choose (file uploads). */
export interface ActionPayload {
  file_name?: string;
  mime_type?: string;
  bytes_b64?: string;
}
