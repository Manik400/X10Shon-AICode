/**
 * Visible cursor, target highlight and status banner so the user can follow the agent on the page.
 * Everything lives in a closed shadow root on a fixed-position host, so page styles and the
 * snapshot (which skips the host) are unaffected.
 */

import { wait } from './timing';

const HOST_ID = 'lfa-agent-overlay-host';
const MOVE_MS = 350;

interface Overlay {
  host: HTMLElement;
  cursor: HTMLElement;
  ring: HTMLElement;
  banner: HTMLElement;
  bannerText: HTMLElement;
}

let overlay: Overlay | null = null;

function ensure(): Overlay | null {
  if (typeof document === 'undefined' || !document.body) return null;
  if (overlay && overlay.host.isConnected) return overlay;
  const host = document.createElement('div');
  host.id = HOST_ID;
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  const root = host.attachShadow({ mode: 'closed' });
  root.innerHTML = `
    <style>
      .cursor{position:fixed;left:0;top:0;width:28px;height:28px;transform:translate(-9999px,-9999px);transition:transform ${MOVE_MS}ms cubic-bezier(.2,.8,.2,1);filter:drop-shadow(0 2px 3px rgba(0,0,0,.45));}
      .cursor.click svg{transform:scale(.85)}
      .cursor svg{transition:transform 120ms}
      .ring{position:fixed;left:0;top:0;border:2px solid #0969da;border-radius:6px;box-shadow:0 0 0 3px rgba(9,105,218,.25);opacity:0;transition:all ${MOVE_MS}ms;}
      .banner{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:#0d1117;color:#e6edf3;font:13px system-ui,sans-serif;padding:8px 14px;border-radius:999px;box-shadow:0 4px 14px rgba(0,0,0,.35);display:none;align-items:center;gap:8px;max-width:70vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .dot{width:8px;height:8px;border-radius:50%;background:#3fb950;animation:pulse 1.2s infinite}
      @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
    </style>
    <div class="ring"></div>
    <div class="cursor"><svg viewBox="0 0 24 24" width="28" height="28"><path d="M4 2l16 9.5-7.2 1.5L9 20z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg></div>
    <div class="banner"><span class="dot"></span><span class="text">Local-First Agent is working…</span></div>`;
  document.documentElement.appendChild(host);
  overlay = {
    host,
    cursor: root.querySelector('.cursor') as HTMLElement,
    ring: root.querySelector('.ring') as HTMLElement,
    banner: root.querySelector('.banner') as HTMLElement,
    bannerText: root.querySelector('.text') as HTMLElement,
  };
  return overlay;
}

export function isOverlayHost(el: Element): boolean {
  return el.id === HOST_ID;
}

export function showBanner(text: string): void {
  const o = ensure();
  if (!o) return;
  o.bannerText.textContent = text;
  o.banner.style.display = 'flex';
}

export function hideBanner(): void {
  if (!overlay) return;
  overlay.banner.style.display = 'none';
  overlay.cursor.style.transform = 'translate(-9999px,-9999px)';
  overlay.ring.style.opacity = '0';
}

/** Glide the cursor to the element and outline it; resolves after the motion. */
export async function moveCursorTo(el: Element, label?: string): Promise<void> {
  const o = ensure();
  if (!o) return;
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return;
  const x = Math.min(Math.max(r.left + Math.min(r.width / 2, 40), 4), window.innerWidth - 8);
  const y = Math.min(Math.max(r.top + r.height / 2, 4), window.innerHeight - 8);
  o.ring.style.left = `${r.left - 3}px`;
  o.ring.style.top = `${r.top - 3}px`;
  o.ring.style.width = `${r.width + 2}px`;
  o.ring.style.height = `${r.height + 2}px`;
  o.ring.style.opacity = '1';
  o.cursor.style.transform = `translate(${x}px,${y}px)`;
  if (label) showBanner(label);
  await wait(MOVE_MS + 40);
}

export async function clickPulse(): Promise<void> {
  const o = ensure();
  if (!o) return;
  o.cursor.classList.add('click');
  await wait(120);
  o.cursor.classList.remove('click');
}
