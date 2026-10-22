import { useState } from 'react';
import { ApiError, api, download, post, put, qs } from '../api/client';
import { useGet } from '../hooks/useApi';
import { ALL_STATES, type ClearQueueResult, type Job, type Paged, type QueueSummary } from '../types';
import { Empty, ErrorBox, Pagination, SortHeader, StatePill, fmtDate, fmtScore } from '../components/ui';

interface Filters {
  titles: string[];
  keywords: string[];
  excluded_keywords: string[];
  min_experience_years: number | null;
  max_experience_years: number | null;
  locations: string[];
  remote_types: string[];
  salary_min: number | null;
  salary_max: number | null;
  employment_types: string[];
  excluded_companies: string[];
  requires_sponsorship: boolean | null;
  required_skills: string[];
  max_posting_age_days: number | null;
  min_match_score: number;
}

const LIMIT = 25;

export default function Jobs() {
  const [search, setSearch] = useState('');
  const [state, setState] = useState('');
  const [since, setSince] = useState('');
  const [sort, setSort] = useState('created_at');
  const [order, setOrder] = useState('desc');
  const [offset, setOffset] = useState(0);
  const [url, setUrl] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [busy, setBusy] = useState(false);

  const path = `/api/jobs${qs({ limit: LIMIT, offset, search, state, since: since ? new Date(since).toISOString() : '', sort, order })}`;
  const jobs = useGet<Paged<Job>>(path, [], 5000);
  const filters = useGet<Filters>('/api/jobs/filters');
  const queueInfo = useGet<QueueSummary>('/api/applications/queue', [], 5000);

  function onSort(f: string) {
    if (sort === f) setOrder(order === 'asc' ? 'desc' : 'asc');
    else {
      setSort(f);
      setOrder('desc');
    }
  }

  async function importUrl() {
    if (!url.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ job: Job; duplicate: boolean }>('/api/jobs/import', { url: url.trim(), source_kind: 'url' });
      setMsg(r.duplicate ? `Already imported: ${r.job.company ?? ''} ${r.job.title ?? ''}` : `Imported ${r.job.company ?? ''} ${r.job.title ?? '(title pending)'} · score ${fmtScore(r.job.match_score)}`);
      setUrl('');
      await jobs.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function importCsv(file: File) {
    setBusy(true);
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await api<{ imported: number; duplicates: number; errors: string[] }>('/api/jobs/import-csv', { method: 'POST', body: fd });
      setMsg(`CSV: ${r.imported} imported, ${r.duplicates} duplicates${r.errors.length ? `, ${r.errors.length} errors: ${r.errors.slice(0, 3).join('; ')}` : ''}`);
      await jobs.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function queue(job: Job) {
    try {
      await post(`/api/jobs/${job.id}/queue`);
      await jobs.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function emptyQueue() {
    const q = queueInfo.data;
    const waiting = q ? q.queued + q.retry_scheduled + q.pending_tasks : 0;
    const runs = q ? q.running_queue_tasks : 0;
    const what = [waiting ? `${waiting} waiting application(s) will be cancelled` : '', runs ? `${runs} running list pass(es) will be stopped` : ''].filter(Boolean).join(' and ');
    if (!window.confirm(`Empty the queue? ${what}. Applications already sent are not touched, and the jobs stay in this list.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<ClearQueueResult>('/api/applications/queue/clear', { stop_running: true });
      setMsg(`Queue emptied: ${r.applications_cancelled} application(s) cancelled, ${r.tasks_cancelled} waiting task(s) cancelled, ${r.runs_stopped} list pass(es) stopped.`);
      await queueInfo.reload();
      await jobs.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveFilters(f: Filters) {
    const r = await put<{ rescreened: number }>('/api/jobs/filters', f);
    setMsg(`Filters saved; ${r.rescreened} jobs re-screened.`);
    await filters.reload();
    await jobs.reload();
  }

  return (
    <>
      <h1>Job queue</h1>
      <div className="card">
        <div className="row">
          <input placeholder="Paste a job or application URL (Greenhouse, Lever, company careers page…)" value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && void importUrl()} />
          <button className="primary" onClick={() => void importUrl()} disabled={busy}>
            Import URL
          </button>
          <label className="file-btn">
            Import CSV
            <input type="file" accept=".csv,text/csv" hidden onChange={(e) => e.target.files?.[0] && void importCsv(e.target.files[0])} />
          </label>
          <button onClick={() => void download('/api/jobs/export.csv', 'jobs.csv')}>Export CSV</button>
          <button onClick={() => setShowFilters(!showFilters)}>{showFilters ? 'Hide filters' : 'Match filters'}</button>
          <button className="danger" onClick={() => void emptyQueue()} disabled={busy || !queueInfo.data || queueInfo.data.total === 0} title="Cancel every queued application and stop running list passes. Nothing already sent is touched.">
            Empty queue{queueInfo.data ? ` (${queueInfo.data.total})` : ''}
          </button>
        </div>
        {queueInfo.data && queueInfo.data.total > 0 && (
          <p className="small muted">
            In the queue: {queueInfo.data.queued} queued · {queueInfo.data.retry_scheduled} waiting for a retry · {queueInfo.data.pending_tasks} task(s) waiting their turn · {queueInfo.data.running_queue_tasks} running
          </p>
        )}
        {msg && <div className="notice">{msg}</div>}
      </div>
      {showFilters && filters.data && <FilterEditor initial={filters.data} onSave={saveFilters} />}
      <div className="row toolbar">
        <input placeholder="Search company, title, location" value={search} onChange={(e) => (setOffset(0), setSearch(e.target.value))} />
        <select value={state} onChange={(e) => (setOffset(0), setState(e.target.value))}>
          <option value="">All states</option>
          {ALL_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <label className="small">
          Since <input type="date" value={since} onChange={(e) => (setOffset(0), setSince(e.target.value))} />
        </label>
      </div>
      <ErrorBox error={jobs.error} />
      {jobs.data && jobs.data.items.length === 0 && <Empty>No jobs match. Import one above or add it from the extension side panel.</Empty>}
      {jobs.data && jobs.data.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <SortHeader label="Company" field="company" sort={sort} order={order} onSort={onSort} />
              <SortHeader label="Role" field="title" sort={sort} order={order} onSort={onSort} />
              <th>Location</th>
              <th>Source</th>
              <SortHeader label="Match" field="match_score" sort={sort} order={order} onSort={onSort} />
              <SortHeader label="Posted" field="posted_at" sort={sort} order={order} onSort={onSort} />
              <th>State</th>
              <th>Last attempt</th>
              <th>Next action</th>
            </tr>
          </thead>
          <tbody>
            {jobs.data.items.map((j) => (
              <JobRow key={j.id} job={j} onQueue={queue} />
            ))}
          </tbody>
        </table>
      )}
      {jobs.data && <Pagination total={jobs.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </>
  );
}

export function nextAction(job: Job): string {
  const st = job.application_state;
  if (job.duplicate_of) return 'Duplicate of an existing job';
  switch (st) {
    case 'ELIGIBLE':
      return 'Queue for application';
    case 'SKIPPED':
      return job.filter_result_json?.reasons?.[0] ? `Skipped: ${job.filter_result_json.reasons[0]}` : 'Skipped by filters';
    case 'QUEUED':
      return job.next_attempt_at ? `Scheduled for ${fmtDate(job.next_attempt_at)}` : 'Waiting for scheduler';
    case 'RETRY_SCHEDULED':
      return `Retry at ${fmtDate(job.next_attempt_at)}${job.failure_json ? ` (${job.failure_json.category})` : ''}`;
    case 'AUTH_REQUIRED':
      return 'Log in on the site, then resume';
    case 'NEEDS_USER_INPUT':
      return 'Answer pending questions';
    case 'READY_FOR_REVIEW':
      return 'Review and approve submission';
    case 'APPLICATION_FAILED':
      return job.failure_json ? `${job.failure_json.category}: ${job.failure_json.recommended_action}` : 'Failed';
    case 'APPLIED_CONFIRMED':
      return 'Done';
    case 'IN_PROGRESS':
      return 'Agent is working';
    default:
      return st ? st.toLowerCase() : 'Screen';
  }
}

function JobRow({ job, onQueue }: { job: Job; onQueue: (j: Job) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr onClick={() => setOpen(!open)} className="clickable">
        <td>{job.company ?? '—'}</td>
        <td>
          <a href={job.canonical_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
            {job.title ?? job.canonical_url}
          </a>
        </td>
        <td>
          {job.location ?? '—'} {job.remote_type && job.remote_type !== 'unknown' ? <span className="pill muted">{job.remote_type}</span> : null}
        </td>
        <td>{job.source_kind}</td>
        <td title={JSON.stringify(job.match_explanation_json?.evidence ?? {})}>{fmtScore(job.match_score)}</td>
        <td className="nowrap">{fmtDate(job.posted_at)}</td>
        <td>
          <StatePill state={job.duplicate_of ? 'DUPLICATE' : job.application_state} />
        </td>
        <td className="nowrap">{fmtDate(job.last_attempt_at)}</td>
        <td>
          {job.application_state === 'ELIGIBLE' || job.application_state === 'SKIPPED' ? (
            <button
              className="primary small"
              onClick={(e) => {
                e.stopPropagation();
                onQueue(job);
              }}
            >
              Queue
            </button>
          ) : null}{' '}
          <span className="small">{nextAction(job)}</span>
        </td>
      </tr>
      {open && (
        <tr className="detail">
          <td colSpan={9}>
            <MatchDetail job={job} />
          </td>
        </tr>
      )}
    </>
  );
}

function MatchDetail({ job }: { job: Job }) {
  const ex = job.match_explanation_json as { evidence?: Record<string, unknown>; model?: Record<string, unknown>; disclaimer?: string } | null;
  const fr = job.filter_result_json;
  return (
    <div className="detail-grid">
      <div>
        <h4>Match evidence</h4>
        {ex?.evidence ? (
          <ul className="small">
            {Object.entries(ex.evidence).map(([k, v]) => (
              <li key={k}>
                <b>{k}</b>: {Array.isArray(v) ? v.join(', ') || '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small">Not scored yet.</p>
        )}
        {ex?.model ? <p className="small">Model: {String((ex.model as { rationale?: string }).rationale ?? '')}</p> : null}
        <p className="small muted">{ex?.disclaimer}</p>
      </div>
      <div>
        <h4>Filters</h4>
        {fr ? (
          <ul className="small">
            <li>{fr.passed ? 'Passed all mandatory filters' : 'Rejected by filters'}</li>
            {fr.reasons.map((r) => (
              <li key={r} className="err">
                {r}
              </li>
            ))}
            {fr.notes.map((n) => (
              <li key={n} className="muted">
                {n}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small">Not screened yet.</p>
        )}
        {job.failure_json ? (
          <p className="small err">
            Last failure: {job.failure_json.category} — {job.failure_json.message}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function listField(v: string[]): string {
  return v.join(', ');
}
function parseList(s: string): string[] {
  return s
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean);
}
function numOrNull(s: string): number | null {
  return s.trim() === '' ? null : Number(s);
}

function FilterEditor({ initial, onSave }: { initial: Filters; onSave: (f: Filters) => Promise<void> }) {
  const [f, setF] = useState<Filters>(initial);
  const up = (patch: Partial<Filters>) => setF({ ...f, ...patch });
  return (
    <div className="card form-grid">
      <label>
        Titles / aliases <input value={listField(f.titles)} onChange={(e) => up({ titles: parseList(e.target.value) })} placeholder="Software Engineer, Backend Developer" />
      </label>
      <label>
        Keywords (any) <input value={listField(f.keywords)} onChange={(e) => up({ keywords: parseList(e.target.value) })} />
      </label>
      <label>
        Excluded keywords <input value={listField(f.excluded_keywords)} onChange={(e) => up({ excluded_keywords: parseList(e.target.value) })} />
      </label>
      <label>
        Required skills (all) <input value={listField(f.required_skills)} onChange={(e) => up({ required_skills: parseList(e.target.value) })} />
      </label>
      <label>
        Locations <input value={listField(f.locations)} onChange={(e) => up({ locations: parseList(e.target.value) })} />
      </label>
      <label>
        Work arrangement <input value={listField(f.remote_types)} onChange={(e) => up({ remote_types: parseList(e.target.value) })} placeholder="remote, hybrid, onsite" />
      </label>
      <label>
        Employment types <input value={listField(f.employment_types)} onChange={(e) => up({ employment_types: parseList(e.target.value) })} />
      </label>
      <label>
        Excluded companies <input value={listField(f.excluded_companies)} onChange={(e) => up({ excluded_companies: parseList(e.target.value) })} />
      </label>
      <label>
        Min experience (yrs) <input type="number" value={f.min_experience_years ?? ''} onChange={(e) => up({ min_experience_years: numOrNull(e.target.value) })} />
      </label>
      <label>
        Max experience required (yrs) <input type="number" value={f.max_experience_years ?? ''} onChange={(e) => up({ max_experience_years: numOrNull(e.target.value) })} />
      </label>
      <label>
        Salary min <input type="number" value={f.salary_min ?? ''} onChange={(e) => up({ salary_min: numOrNull(e.target.value) })} />
      </label>
      <label>
        Salary max <input type="number" value={f.salary_max ?? ''} onChange={(e) => up({ salary_max: numOrNull(e.target.value) })} />
      </label>
      <label>
        Max posting age (days) <input type="number" value={f.max_posting_age_days ?? ''} onChange={(e) => up({ max_posting_age_days: numOrNull(e.target.value) })} />
      </label>
      <label>
        Minimum match score (0–1) <input type="number" step="0.05" min="0" max="1" value={f.min_match_score} onChange={(e) => up({ min_match_score: Number(e.target.value) })} />
      </label>
      <label>
        I need visa sponsorship{' '}
        <select value={f.requires_sponsorship === null ? '' : String(f.requires_sponsorship)} onChange={(e) => up({ requires_sponsorship: e.target.value === '' ? null : e.target.value === 'true' })}>
          <option value="">not set</option>
          <option value="true">yes (reject 'no sponsorship' jobs)</option>
          <option value="false">no</option>
        </select>
      </label>
      <div className="row">
        <button className="primary" onClick={() => void onSave(f)}>
          Save filters &amp; re-screen
        </button>
      </div>
    </div>
  );
}
