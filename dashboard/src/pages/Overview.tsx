import { Link } from 'react-router-dom';
import { useGet } from '../hooks/useApi';
import type { Overview as OverviewData } from '../types';
import { ErrorBox, StatCard, fmtDate, fmtRate } from '../components/ui';

export default function Overview() {
  const { data, error, loading } = useGet<OverviewData>('/api/overview', [], 5000);
  if (error) return <ErrorBox error={error} />;
  if (loading || !data) return <p className="muted">Loading…</p>;
  return (
    <>
      <h1>Overview</h1>
      <div className="stats">
        <StatCard label="Jobs discovered" value={data.jobs_discovered} hint={`${data.jobs_duplicates} duplicates`} />
        <StatCard label="Eligible" value={data.eligible} />
        <StatCard label="Queued" value={data.queued} hint={`${data.in_progress} in progress`} />
        <StatCard label="Applications confirmed" value={data.confirmed} tone="ok" />
        <StatCard label="Awaiting your review / input" value={data.awaiting_review} tone={data.awaiting_review ? 'warn' : undefined} />
        <StatCard label="Failed" value={data.failed} tone={data.failed ? 'err' : undefined} />
        <StatCard label="Authentication required" value={data.auth_required} tone={data.auth_required ? 'warn' : undefined} />
        <StatCard label="Skipped by filters" value={data.skipped} />
        <StatCard label="Success rate" value={fmtRate(data.rates.success_rate)} hint={`${data.rates.attempted} attempted`} />
        <StatCard label="Failure rate" value={fmtRate(data.rates.failure_rate)} hint="of attempted" />
      </div>
      <p className="small muted">Rates: {data.rates.denominator}.</p>
      {!data.extension_connected && (
        <div className="notice warn">
          The Chrome extension is not connected. Pair it under <Link to="/settings">Settings → Extension pairing</Link> to run application workflows in your own browser.
        </div>
      )}
      {data.pending_approvals > 0 && <div className="notice warn">{data.pending_approvals} approval(s) waiting at the top of this page.</div>}
      <h2>Recent activity</h2>
      {data.recent_activity.length === 0 ? (
        <p className="muted">No activity yet. Import a job from the Job queue page or from the extension side panel.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Event</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {data.recent_activity.map((e) => (
              <tr key={e.id} className={e.level === 'error' ? 'row-err' : e.level === 'warning' ? 'row-warn' : ''}>
                <td className="nowrap">{fmtDate(e.ts)}</td>
                <td>
                  <code>{e.event}</code>
                </td>
                <td className="small muted">{summarize(e.data_json)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

export function summarize(data: Record<string, unknown> | null | undefined): string {
  if (!data) return '';
  return Object.entries(data)
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && typeof v !== 'object')
    .slice(0, 5)
    .map(([k, v]) => `${k}=${String(v).slice(0, 60)}`)
    .join(' · ');
}
