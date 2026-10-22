/**
 * Pauses that keep working in background tabs. Chrome throttles setTimeout in hidden tabs to once
 * per second, and after five minutes hidden to once per minute, which turned every short pause in
 * an action into a 20 s timeout as soon as the user looked at another tab. MessageChannel tasks are
 * not throttled, so hidden tabs wait on those instead.
 */
export function wait(ms: number): Promise<void> {
  if (typeof document === 'undefined' || !document.hidden || typeof MessageChannel === 'undefined') {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
  return new Promise((resolve) => {
    const end = Date.now() + ms;
    const ch = new MessageChannel();
    ch.port1.onmessage = () => {
      if (Date.now() >= end) {
        ch.port1.close();
        resolve();
      } else {
        ch.port2.postMessage(0);
      }
    };
    ch.port2.postMessage(0);
  });
}

/** True when the tab is not visible: decorative animation is skipped. */
export function hidden(): boolean {
  return typeof document !== 'undefined' && document.hidden;
}
