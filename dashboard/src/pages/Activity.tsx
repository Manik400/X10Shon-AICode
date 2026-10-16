import { useState } from 'react';
import { download, post, qs } from '../api/client';
import { useGet } from '../hooks/useApi';
import type { AuditEvent, Diagnostics, Paged, Task } from '../types';
import { Confirm, ErrorBox, Pagination, Pill, StatCard, fmtDate } from '../components/ui';
import { summarize } from './Overview';

const LIMIT = 50;

export default function Activity() {
  const diag = useGet<Diagnostics>('/api/diagnostics', [], 5000);
  const tasks = useGet<Paged<Task> & { running: string[] }>('/api/tasks?limit=20', [], 5000);
  const [search, setSearch] = useState('');
  const [level, setLevel] = useState('');
  const [since, setSince] = useState('');
  const [offset, setOffset] = useState(0);
  const events = useGet<Paged<AuditEvent>>(`/api/activity${qs({ limit: LIMIT, offset, search, level, since: since ? new Date(since).toISOString() : '' })}`, [], 5000);
  const d = diag.data;

  return (
    <>
      <h1>Activity &amp; diagnostics</h1>
      <ErrorBox error={diag.error} />
      {d && (
        <>
          <div className="stats">
            <StatCard label="Agent RSS" value={`${d.resources.process_rss_mb} MB`} hint={`${d.resources.threads} threads`} />
            <StatCard label="Agent CPU" value={`${d.resources.process_cpu_percent}%`} hint={`system ${d.resources.system_cpu_percent}%`} />
            <StatCard label="System memory" value={`${d.resources.system_memory_percent}%`} hint={`${Math.round(d.resources.system_available_mb)} MB free`} />
            <StatCard label="Model calls (24h)" value={d.model_metrics.total_calls} hint={`${d.model_gateway.cache_hits} cache hits · ${d.model_gateway.failures} failures`} />
            <StatCard label="Extension" value={d.extension.connected ? 'connected' : 'offline'} tone={d.extension.connected ? 'ok' : 'err'} hint={d.extension.connected_at ?? ''} />
            <StatCard label="Scheduler" value={d.scheduler.enabled ? (d.scheduler.paused ? 'paused' : 'on') : 'off'} hint={`${d.scheduler.last_reason} · ${fmtDate(d.scheduler.last_tick)}`} />
          </div>
          <h2>Model latency (last 24h)</h2>
          {d.model_metrics.roles.length === 0 ? (
            <p className="muted small">No model calls recorded yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Provider</th>
                  <th>Model</th>
                  <th>Calls</th>
                  <th>Cached</th>
                  <th>Errors</th>
                  <th>p50</th>
                  <th>p95</th>
                </tr>
              </thead>
              <tbody>
                {d.model_metrics.roles.map((r) => (
                  <tr key={r.role}>
                    <td>{r.role}</td>
                    <td>{r.provider}</td>
                    <td>{r.model}</td>
                    <td>{r.calls}</td>
                    <td>{r.cached}</td>
                    <td>{r.errors}</td>
                    <td>{r.p50_ms ?? '—'} ms</td>
                    <td>{r.p95_ms ?? '—'} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h2>Current task</h2>
          {d.current_task ? <pre className="small">{JSON.stringify(d.current_task, null, 1)}</pre> : <p className="muted small">Idle.</p>}
        </>
      )}

      <h2>Recent tasks</h2>
      {tasks.data && (
        <table>
          <thead>
            <tr>
              <th>Created</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Actions</th>
              <th>Model calls</th>
              <th>Instruction / failure</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tasks.data.items.map((t) => (
              <tr key={t.id} className={t.status === 'failed' ? 'row-err' : t.status === 'waiting_user' ? 'row-warn' : ''}>
                <td className="nowrap">{fmtDate(t.created_at)}</td>
                <td>{t.kind}</td>
                <td>
                  <Pill tone={t.status === 'succeeded' ? 'ok' : t.status === 'failed' ? 'err' : t.status === 'running' ? 'info' : 'warn'}>{t.status}</Pill>
                </td>
                <td>{t.action_count}</td>
                <td>{t.model_calls}</td>
                <td className="small">
                  {t.instruction ?? ''} {t.failure_json ? <span className="err">{t.failure_json.category}: {t.failure_json.message}</span> : null}
                </td>
                <td>
                  {(t.status === 'running' || t.status === 'pending' || t.status === 'waiting_approval') && (
                    <button className="danger small" onClick={() => void post(`/api/tasks/${t.id}/cancel`).then(() => tasks.reload())}>
                      Cancel
                    </button>
                  )}
                  {t.status === 'waiting_user' && (
                    <button className="primary small" onClick={() => void post(`/api/tasks/${t.id}/resume`).then(() => tasks.reload())}>
                      Resume
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Audit log</h2>
      <div className="row toolbar">
        <input placeholder="Search events" value={search} onChange={(e) => (setOffset(0), setSearch(e.target.value))} />
        <select value={level} onChange={(e) => (setOffset(0), setLevel(e.target.value))}>
          <option value="">All levels</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
        <label className="small">
          Since <input type="date" value={since} onChange={(e) => (setOffset(0), setSince(e.target.value))} />
        </label>
        <button onClick={() => void download('/api/diagnostics/export', 'lfa-diagnostics.json')}>Export diagnostics (redacted)</button>
        <button onClick={() => void download('/api/data/export', 'lfa-export.json')}>Export all data</button>
        <Confirm label="Delete ALL local data (profile, jobs, applications, logs)? This cannot be undone." onConfirm={() => void post('/api/data/delete', { confirm: 'DELETE' }).then(() => window.location.reload())}>
          Delete all data
        </Confirm>
      </div>
      <ErrorBox error={events.error} />
      {events.data && (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Level</th>
              <th>Event</th>
              <th>Task</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {events.data.items.map((e) => (
              <tr key={e.id} className={e.level === 'error' ? 'row-err' : e.level === 'warning' ? 'row-warn' : ''}>
                <td className="nowrap">{fmtDate(e.ts)}</td>
                <td>{e.level}</td>
                <td>
                  <code>{e.event}</code>
                </td>
                <td className="small">{e.task_id?.slice(0, 8) ?? ''}</td>
                <td className="small muted">{summarize(e.data_json)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {events.data && <Pagination total={events.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </>
  );
}
