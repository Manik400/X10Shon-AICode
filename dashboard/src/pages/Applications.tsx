import { useState } from 'react';
import { ApiError, download, post, put, qs } from '../api/client';
import { useGet } from '../hooks/useApi';
import { ALL_STATES, type Application, type Paged, type PendingQuestion, type Task, type Transition } from '../types';
import { Empty, ErrorBox, Pagination, Pill, SortHeader, StatePill, fmtDate } from '../components/ui';

const LIMIT = 25;

export default function Applications() {
  const [state, setState] = useState('');
  const [verification, setVerification] = useState('');
  const [search, setSearch] = useState('');
  const [since, setSince] = useState('');
  const [sort, setSort] = useState('updated_at');
  const [order, setOrder] = useState('desc');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const path = `/api/applications${qs({ limit: LIMIT, offset, state, verification, search, since: since ? new Date(since).toISOString() : '', sort, order })}`;
  const apps = useGet<Paged<Application>>(path, [], 5000);

  function onSort(f: string) {
    if (sort === f) setOrder(order === 'asc' ? 'desc' : 'asc');
    else {
      setSort(f);
      setOrder('desc');
    }
  }

  return (
    <>
      <h1>Application tracker</h1>
      <div className="row toolbar">
        <input placeholder="Search company, role, reference" value={search} onChange={(e) => (setOffset(0), setSearch(e.target.value))} />
        <select value={state} onChange={(e) => (setOffset(0), setState(e.target.value))}>
          <option value="">All states</option>
          {ALL_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select value={verification} onChange={(e) => (setOffset(0), setVerification(e.target.value))}>
          <option value="">Any verification</option>
          <option value="confirmed">confirmed</option>
          <option value="uncertain">uncertain</option>
          <option value="rejected">rejected</option>
          <option value="not_attempted">not attempted</option>
        </select>
        <label className="small">
          Since <input type="date" value={since} onChange={(e) => (setOffset(0), setSince(e.target.value))} />
        </label>
        <button onClick={() => void download('/api/applications/export.csv', 'applications.csv')}>Export CSV</button>
      </div>
      <ErrorBox error={apps.error} />
      {apps.data && apps.data.items.length === 0 && <Empty>No applications yet. Queue an eligible job from the Job queue.</Empty>}
      {apps.data && apps.data.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <SortHeader label="Company / role" field="company" sort={sort} order={order} onSort={onSort} />
              <SortHeader label="State" field="state" sort={sort} order={order} onSort={onSort} />
              <th>Verification</th>
              <th>Reference</th>
              <th>Resume</th>
              <SortHeader label="Applied" field="applied_at" sort={sort} order={order} onSort={onSort} />
              <th>Attempts</th>
              <SortHeader label="Updated" field="updated_at" sort={sort} order={order} onSort={onSort} />
            </tr>
          </thead>
          <tbody>
            {apps.data.items.map((a) => (
              <tr key={a.id} className="clickable" onClick={() => setSelected(selected === a.id ? null : a.id)}>
                <td>
                  <b>{a.company ?? '—'}</b> {a.title ?? ''}
                  <div className="small muted">{a.application_url}</div>
                </td>
                <td>
                  <StatePill state={a.state} />
                </td>
                <td>
                  <Pill tone={a.verification_status === 'confirmed' ? 'ok' : a.verification_status === 'rejected' ? 'err' : a.verification_status === 'uncertain' ? 'warn' : 'muted'}>{a.verification_status.replace('_', ' ')}</Pill>
                </td>
                <td>{a.reference_number ?? '—'}</td>
                <td>{a.resume_label ?? '—'}</td>
                <td className="nowrap">{fmtDate(a.applied_at)}</td>
                <td>{a.attempt_count}</td>
                <td className="nowrap">{fmtDate(a.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {apps.data && <Pagination total={apps.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      {selected && <ApplicationDetail id={selected} onChange={() => void apps.reload()} onClose={() => setSelected(null)} />}
    </>
  );
}

interface Detail {
  application: Application;
  job: { title: string | null; company: string | null; canonical_url: string } | null;
  history: Transition[];
  tasks: Task[];
}

export function allowedActions(a: Application): { action: string; label: string; danger?: boolean }[] {
  const out: { action: string; label: string; danger?: boolean }[] = [];
  const s = a.state;
  if (s === 'ELIGIBLE' || s === 'SKIPPED') out.push({ action: 'queue', label: 'Queue' });
  if (s === 'APPLICATION_FAILED' && a.verification_status === 'uncertain') {
    out.push({ action: 'reconcile_confirmed', label: 'I verified it was submitted' });
    out.push({ action: 'reconcile_failed', label: 'It was not submitted' });
  } else if (s === 'APPLICATION_FAILED' || s === 'RETRY_SCHEDULED') {
    out.push({ action: 'retry', label: 'Retry now' });
  }
  if (s === 'AUTH_REQUIRED' || s === 'READY_FOR_REVIEW') out.push({ action: 'queue', label: 'Resume' });
  if (!['APPLIED_CONFIRMED', 'DUPLICATE', 'CANCELLED'].includes(s)) out.push({ action: 'cancel', label: 'Cancel', danger: true });
  return out;
}

function ApplicationDetail({ id, onChange, onClose }: { id: string; onChange: () => void; onClose: () => void }) {
  const d = useGet<Detail>(`/api/applications/${id}`, [id], 4000);
  const [notes, setNotes] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [refNo, setRefNo] = useState('');
  if (d.error) return <ErrorBox error={d.error} />;
  if (!d.data) return <p className="muted">Loading…</p>;
  const a = d.data.application;
  const questions = (a.pending_questions_json ?? []).filter((q) => q.label);

  async function act(action: string) {
    setMsg(null);
    try {
      await post(`/api/applications/${a.id}/transition`, { action, reference_number: refNo || null });
      await d.reload();
      onChange();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  }
  async function submitAnswers() {
    const payload = questions
      .filter((q) => (answers[q.label] ?? '').trim())
      .map((q) => ({ label: q.label, answer: answers[q.label].trim(), category: q.legally_significant ? 'legal' : 'custom', legally_significant: !!q.legally_significant, save_as_approved: true }));
    if (!payload.length) return;
    try {
      await post(`/api/applications/${a.id}/answers`, { answers: payload, requeue: true });
      setMsg(`${payload.length} answer(s) saved as approved answers; application re-queued.`);
      setAnswers({});
      await d.reload();
      onChange();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  }
  async function saveNotes() {
    await put(`/api/applications/${a.id}/notes`, { notes: notes ?? '' });
    setMsg('Notes saved.');
    await d.reload();
  }

  return (
    <div className="card detail-panel">
      <div className="row">
        <h2>
          {d.data.job?.company} — {d.data.job?.title}
        </h2>
        <button onClick={onClose}>Close</button>
      </div>
      <div className="row wrap">
        <StatePill state={a.state} />
        <span className="small">
          Verification: <b>{a.verification_status}</b>
        </span>
        {a.reference_number && <span className="small">Reference: {a.reference_number}</span>}
        <a className="small" href={a.application_url ?? '#'} target="_blank" rel="noreferrer">
          Open application page
        </a>
      </div>
      {a.failure_json && (
        <div className="notice err">
          <b>{a.failure_json.category}</b>: {a.failure_json.message}
          <div className="small">{a.failure_json.recommended_action}</div>
        </div>
      )}
      {a.verification_status === 'uncertain' && (
        <div className="notice warn">
          The outcome could not be verified. Check the site; then record what you found. Retrying blindly could submit twice.
          <div className="row">
            <input placeholder="Reference number (optional)" value={refNo} onChange={(e) => setRefNo(e.target.value)} />
          </div>
        </div>
      )}
      {questions.length > 0 && a.state !== 'APPLIED_CONFIRMED' && (
        <div className="notice warn">
          <b>The agent needs your input ({questions.length}):</b>
          <div className="form-grid">
            {questions.map((q) => (
              <QuestionInput key={q.label} q={q} value={answers[q.label] ?? ''} onChange={(v) => setAnswers({ ...answers, [q.label]: v })} />
            ))}
          </div>
          <button className="primary" onClick={() => void submitAnswers()}>
            Save answers &amp; re-queue
          </button>
        </div>
      )}
      <div className="row wrap">
        {allowedActions(a).map((x) => (
          <button key={x.action} className={x.danger ? 'danger' : 'primary'} onClick={() => void act(x.action)}>
            {x.label}
          </button>
        ))}
      </div>
      {msg && <div className="notice">{msg}</div>}
      <div className="detail-grid">
        <div>
          <h4>Status history</h4>
          <ul className="small">
            {d.data.history.map((h) => (
              <li key={h.id}>
                {fmtDate(h.created_at)} · {h.from_state ?? '∅'} → <b>{h.to_state}</b> ({h.actor}) {h.reason ? `— ${h.reason}` : ''}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h4>Evidence &amp; fill report</h4>
          {a.evidence_json ? <pre className="small">{JSON.stringify(a.evidence_json, null, 1)}</pre> : <p className="muted small">No submission evidence recorded.</p>}
          <FillSummary plan={a.fill_plan_json} />
          <h4>Tasks</h4>
          <ul className="small">
            {d.data.tasks.map((t) => (
              <li key={t.id}>
                {fmtDate(t.created_at)} · {t.kind} · <b>{t.status}</b> · {t.action_count} actions {t.failure_json ? `· ${t.failure_json.category}` : ''}
              </li>
            ))}
          </ul>
        </div>
      </div>
      <h4>Notes</h4>
      <textarea value={notes ?? a.notes ?? ''} onChange={(e) => setNotes(e.target.value)} />
      <button onClick={() => void saveNotes()}>Save notes</button>
    </div>
  );
}

function QuestionInput({ q, value, onChange }: { q: PendingQuestion; value: string; onChange: (v: string) => void }) {
  const label = `${q.label}${q.legally_significant ? ' (legally significant)' : ''}${q.reason ? ` — ${q.reason}` : ''}`;
  if (q.options && q.options.length) {
    return (
      <label>
        {label}
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">choose…</option>
          {q.options.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      </label>
    );
  }
  return (
    <label>
      {label}
      <input value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function FillSummary({ plan }: { plan: Record<string, unknown> | null }) {
  const steps = (plan?.steps as { step: number; filled: { label: string; provenance: string }[]; failed: { label: string; message: string }[]; skipped: { label: string; status: string }[] }[] | undefined) ?? [];
  if (!steps.length) return null;
  return (
    <div className="small">
      {steps.map((s) => (
        <div key={s.step}>
          <b>Step {s.step + 1}</b>: {s.filled.length} filled, {s.failed.length} failed, {s.skipped.length} skipped
          <ul>
            {s.filled.map((f) => (
              <li key={f.label}>
                ✓ {f.label} <span className="muted">({f.provenance})</span>
              </li>
            ))}
            {s.failed.map((f) => (
              <li key={f.label} className="err">
                ✗ {f.label}: {f.message}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
