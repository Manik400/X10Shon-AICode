/**
 * Page-level signals: authentication, challenges, outcomes. Deterministic and conservative.
 */
import type { PageSignals } from './types';
import { normalizeText } from './accessibility';

const CAPTCHA_SELECTORS = [
  'iframe[src*="recaptcha"]',
  'iframe[src*="hcaptcha"]',
  'iframe[src*="turnstile"]',
  'iframe[src*="arkoselabs"]',
  'iframe[src*="funcaptcha"]',
  '.g-recaptcha',
  '.h-captcha',
  '.cf-turnstile',
  '[data-sitekey]',
  '#captcha',
  '[id*="captcha" i]',
  '[class*="captcha" i]',
];

const OTP_RE = /(one[- ]?time|verification|security|auth(entication)?|2fa|mfa)\s*(code|pin|passcode)|\botp\b|enter the code/i;
const LOGIN_CTA_RE = /^(log ?in|sign ?in|login with|sign in with|continue with (google|microsoft|linkedin|apple|github))$/i;
const LOGOUT_CTA_RE = /^(log ?out|sign ?out)$/i;
const SIGNUP_CTA_RE = /^(sign ?up|create (an |your )?account|register|join now)$/i;
const RECOVERY_RE = /(forgot (your )?(password|username)|reset (your )?password|password reset|recover (your )?account)/i;
const LOCKED_RE = /(account (is |has been )?(locked|suspended|disabled)|too many (failed )?attempts|unusual activity|temporarily blocked)/i;
const SUCCESS_RE =
  /(thank you for (applying|your application|submitting)|application (has been )?(received|submitted|sent|complete)|we('ve| have) received your application|successfully (submitted|applied)|your application was submitted)/i;
const REFERENCE_RE = /(?:reference|confirmation|application|request)\s*(?:number|id|#|no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})/gi;

const ERROR_SELECTORS = [
  '[role="alert"]',
  '[aria-live="assertive"]',
  '.error:not(input):not(select):not(textarea)',
  '.field-error',
  '.form-error',
  '.invalid-feedback',
  '.help-block.error',
  '[class*="error-message" i]',
  '[class*="errorMessage" i]',
  '[id*="error" i]:not(input):not(script)',
  '.alert-danger',
  '.alert-error',
];
const SUCCESS_SELECTORS = ['[role="status"]', '.alert-success', '.success', '[class*="confirmation" i]', '[id*="confirmation" i]', '[class*="thank" i]'];

/** Layout-based visibility: zero client rects means hidden (display:none on self or an ancestor). */
function isShown(el: Element): boolean {
  if (el.getClientRects().length > 0) return true;
  const he = el as HTMLElement;
  return !!he.offsetParent || getComputedStyle(he).position === 'fixed';
}

function visibleText(el: Element): string {
  if (!isShown(el)) return '';
  const he = el as HTMLElement;
  return normalizeText(he.innerText || he.textContent);
}

export function collectSignals(doc: Document, bodyText: string): PageSignals {
  const signals: PageSignals = {
    has_password_field: false,
    has_email_or_username_field: false,
    has_captcha: false,
    has_otp_field: false,
    login_cta_present: false,
    logout_cta_present: false,
    signup_cta_present: false,
    password_recovery: false,
    account_locked: false,
    error_messages: [],
    success_messages: [],
    reference_numbers: [],
    unsupported_reasons: [],
    iframe_srcs: [],
    has_jsonld_jobposting: false,
  };

  const inputs = Array.from(doc.querySelectorAll('input'));
  for (const inp of inputs) {
    const type = (inp.type || 'text').toLowerCase();
    const descriptor = `${inp.name} ${inp.id} ${inp.autocomplete} ${inp.placeholder} ${inp.getAttribute('aria-label') || ''}`.toLowerCase();
    if (!isShown(inp)) continue;
    if (type === 'password') signals.has_password_field = true;
    if (type === 'email' || /email|username|user_name|login|userid/.test(descriptor)) signals.has_email_or_username_field = true;
    if (inp.autocomplete === 'one-time-code' || (/otp|one.?time|verification.?code|mfa|2fa|passcode/.test(descriptor) && (inp.maxLength > 0 && inp.maxLength <= 8 || type === 'tel' || type === 'number'))) {
      signals.has_otp_field = true;
    }
  }
  // OTP phrasing in a short form with a short code input.
  if (!signals.has_otp_field && OTP_RE.test(bodyText) && inputs.filter((i) => isShown(i)).length <= 3 && !signals.has_password_field) {
    const short = inputs.find((i) => i.maxLength > 0 && i.maxLength <= 8 && isShown(i));
    if (short) signals.has_otp_field = true;
  }

  for (const sel of CAPTCHA_SELECTORS) {
    try {
      const el = doc.querySelector(sel);
      if (el && (isShown(el) || el.tagName === 'IFRAME')) {
        signals.has_captcha = true;
        break;
      }
    } catch {
      /* invalid selector in exotic documents */
    }
  }

  const ctas = Array.from(doc.querySelectorAll('a,button,input[type="submit"],input[type="button"],[role="button"]'));
  for (const c of ctas) {
    const t = normalizeText(c instanceof HTMLInputElement ? c.value : (c as HTMLElement).innerText || c.textContent).toLowerCase();
    if (!t || t.length > 40) continue;
    if (LOGIN_CTA_RE.test(t)) signals.login_cta_present = true;
    if (LOGOUT_CTA_RE.test(t)) signals.logout_cta_present = true;
    if (SIGNUP_CTA_RE.test(t)) signals.signup_cta_present = true;
  }

  if (RECOVERY_RE.test(doc.title) || (RECOVERY_RE.test(bodyText.slice(0, 2000)) && signals.has_email_or_username_field && !signals.has_password_field)) {
    signals.password_recovery = true;
  }
  if (LOCKED_RE.test(bodyText.slice(0, 4000))) signals.account_locked = true;

  const seen = new Set<string>();
  for (const sel of ERROR_SELECTORS) {
    let els: Element[] = [];
    try {
      els = Array.from(doc.querySelectorAll(sel));
    } catch {
      continue;
    }
    for (const el of els) {
      const t = visibleText(el);
      if (t && t.length <= 300 && !seen.has(t)) {
        seen.add(t);
        signals.error_messages.push(t);
        if (signals.error_messages.length >= 10) break;
      }
    }
  }
  // Native constraint validation messages on invalid fields.
  for (const f of Array.from(doc.querySelectorAll<HTMLInputElement>('input,select,textarea'))) {
    if (f.getAttribute('aria-invalid') === 'true') {
      const msg = normalizeText(f.validationMessage) || `invalid: ${f.name || f.id}`;
      if (!seen.has(msg)) {
        seen.add(msg);
        signals.error_messages.push(msg);
      }
    }
  }

  for (const sel of SUCCESS_SELECTORS) {
    let els: Element[] = [];
    try {
      els = Array.from(doc.querySelectorAll(sel));
    } catch {
      continue;
    }
    for (const el of els) {
      const t = visibleText(el);
      if (t && t.length <= 400 && SUCCESS_RE.test(t) && !signals.success_messages.includes(t)) signals.success_messages.push(t);
    }
  }
  const headings = Array.from(doc.querySelectorAll('h1,h2,h3,[role="heading"]'));
  for (const h of headings) {
    const t = visibleText(h);
    if (t && SUCCESS_RE.test(t) && !signals.success_messages.includes(t)) signals.success_messages.push(t);
  }
  if (!signals.success_messages.length && SUCCESS_RE.test(bodyText.slice(0, 3000))) {
    const m = bodyText.match(SUCCESS_RE);
    if (m) signals.success_messages.push(m[0]);
  }

  let m: RegExpExecArray | null;
  REFERENCE_RE.lastIndex = 0;
  while ((m = REFERENCE_RE.exec(bodyText.slice(0, 20000))) && signals.reference_numbers.length < 5) {
    if (!signals.reference_numbers.includes(m[1])) signals.reference_numbers.push(m[1]);
  }

  // Unsupported structures (reported, never worked around).
  const frames = Array.from(doc.querySelectorAll('iframe'));
  for (const f of frames) {
    try {
      // Accessing contentDocument throws or returns null for cross-origin frames.
      if (!f.contentDocument) {
        const src = f.getAttribute('src') || '';
        if (src && !/recaptcha|hcaptcha|turnstile|youtube|vimeo|googletagmanager|doubleclick|analytics/i.test(src)) {
          signals.unsupported_reasons.push(`cross-origin iframe: ${src.slice(0, 80)}`);
          // Full, absolute address so the agent can open an embedded application form directly.
          try {
            signals.iframe_srcs.push(new URL(src, doc.baseURI).href.slice(0, 600));
          } catch {
            /* malformed src */
          }
        }
      }
    } catch {
      signals.unsupported_reasons.push('cross-origin iframe');
    }
  }
  if (doc.querySelector('canvas') && !doc.querySelector('input,select,textarea,button')) signals.unsupported_reasons.push('canvas-only interface');

  const ld = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
  for (const s of ld) {
    if (/"@type"\s*:\s*"JobPosting"/.test(s.textContent || '')) {
      signals.has_jsonld_jobposting = true;
      break;
    }
  }
  return signals;
}
