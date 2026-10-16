import { NavLink, Outlet } from 'react-router-dom';
import { post } from '../api/client';
import { useGet } from '../hooks/useApi';
import type { Approval } from '../types';
import { Pill } from './ui';

interface ControlStatus {
  paused: boolean;
  running: string[];
  current: { id: string; kind: string; message?: string; action_count?: number } | null;
  extension: { connected: boolean };
  pending_approvals: number;
  scheduler: { enabled: boolean; last_reason: string };
}

export default function Layout() {
  const status = useGet<ControlStatus>('/api/control/status', [], 3000);
  const approvals = useGet<{ items: Approval[] }>('/api/approvals?status=pending', [], 3000);
  const s = status.data;

  async function toggle() {
    await post(s?.paused ? '/api/control/resume' : '/api/control/pause');
    await status.reload();
  }
  async function decide(id: string, decision: 'approved' | 'rejected') {
    await post(`/api/approvals/${id}/decide`, { decision });
    await approvals.reload();
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">Local-First Agent</div>
        <nav>
          <NavLink to="/">Overview</NavLink>
          <NavLink to="/jobs">Job queue</NavLink>
          <NavLink to="/applications">Applications</NavLink>
          <NavLink to="/profile">Candidate profile</NavLink>
          <NavLink to="/settings">Automation settings</NavLink>
          <NavLink to="/activity">Activity &amp; diagnostics</NavLink>
        </nav>
        <div className="sidebar-status">
          <div className="row">
            <span>Extension</span>
            <Pill tone={s?.extension.connected ? 'ok' : 'err'}>{s ? (s.extension.connected ? 'connected' : 'offline') : '…'}</Pill>
          </div>
          <div className="row">
            <span>Scheduler</span>
            <Pill tone={s?.scheduler.enabled ? 'ok' : 'muted'}>{s ? (s.scheduler.enabled ? 'on' : 'off') : '…'}</Pill>
          </div>
          <div className="row">
            <span>Agent</span>
            <Pill tone={s?.paused ? 'warn' : s?.current ? 'info' : ''}>{s ? (s.paused ? 'paused' : s.current ? 'running' : 'idle') : '…'}</Pill>
          </div>
          {s?.current && (
            <div className="small muted">
              {s.current.kind} · step {s.current.action_count ?? 0} · {s.current.message ?? ''}
            </div>
          )}
          <button className={s?.paused ? 'primary' : 'danger'} onClick={() => void toggle()} disabled={!s}>
            {s?.paused ? 'Resume automation' : 'Pause all'}
          </button>
          {status.error && <div className="notice err small">{status.error}</div>}
        </div>
      </aside>
      <main className="content">
        {approvals.data && approvals.data.items.length > 0 && (
          <div className="approvals">
            {approvals.data.items.map((a) => (
              <div key={a.id} className="notice warn">
                <strong>Approval needed ({a.kind}):</strong> {String(a.payload_json.summary ?? '')}
                {a.payload_json.url ? <div className="small muted">{String(a.payload_json.url)}</div> : null}
                <div className="row">
                  <button className="primary" onClick={() => void decide(a.id, 'approved')}>
                    Approve
                  </button>
                  <button className="danger" onClick={() => void decide(a.id, 'rejected')}>
                    Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}
