import type { ReactNode } from 'react';
import type { ApplicationState } from '../types';

export function StatCard({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: 'ok' | 'warn' | 'err' }) {
  return (
    <div className={`stat ${tone ?? ''}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

const STATE_TONE: Record<ApplicationState, string> = {
  DISCOVERED: '',
  IMPORTED: '',
  SCREENING: '',
  ELIGIBLE: 'info',
  SKIPPED: 'muted',
  QUEUED: 'info',
  IN_PROGRESS: 'info',
  AUTH_REQUIRED: 'warn',
  NEEDS_USER_INPUT: 'warn',
  READY_FOR_REVIEW: 'warn',
  SUBMISSION_PENDING: 'info',
  APPLIED_CONFIRMED: 'ok',
  APPLICATION_FAILED: 'err',
  RETRY_SCHEDULED: 'warn',
  DUPLICATE: 'muted',
  CANCELLED: 'muted',
};

export function StatePill({ state }: { state: ApplicationState | string | null | undefined }) {
  if (!state) return <span className="pill muted">—</span>;
  return <span className={`pill ${STATE_TONE[state as ApplicationState] ?? ''}`}>{state.replace(/_/g, ' ').toLowerCase()}</span>;
}

export function Pill({ children, tone }: { children: ReactNode; tone?: string }) {
  return <span className={`pill ${tone ?? ''}`}>{children}</span>;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function fmtScore(score: number | null | undefined): string {
  return score === null || score === undefined ? '—' : `${Math.round(score * 100)}%`;
}

export function fmtRate(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? 'n/a' : `${Math.round(rate * 100)}%`;
}

export function Pagination({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (offset: number) => void }) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="pagination">
      <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        ‹ Prev
      </button>
      <span>
        Page {page} of {pages} · {total} total
      </span>
      <button disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Next ›
      </button>
    </div>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="notice err">{error}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function SortHeader({ label, field, sort, order, onSort }: { label: string; field: string; sort: string; order: string; onSort: (f: string) => void }) {
  const active = sort === field;
  return (
    <th className="sortable" onClick={() => onSort(field)}>
      {label} {active ? (order === 'asc' ? '▲' : '▼') : ''}
    </th>
  );
}

export function Confirm({ children, onConfirm, label }: { children: ReactNode; onConfirm: () => void; label: string }) {
  return (
    <button
      className="danger"
      onClick={() => {
        if (window.confirm(label)) onConfirm();
      }}
    >
      {children}
    </button>
  );
}
