import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, qs } from '../src/api/client';
import { fmtRate, fmtScore } from '../src/components/ui';
import { allowedActions } from '../src/pages/Applications';
import { nextAction } from '../src/pages/Jobs';
import Overview from '../src/pages/Overview';
import type { Application, Job } from '../src/types';
import { MemoryRouter } from 'react-router-dom';

afterEach(() => vi.restoreAllMocks());

function mockFetch(body: unknown, status = 200) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

describe('api client', () => {
  it('always sends the X-LFA-Client header and parses errors', async () => {
    const f = mockFetch({ ok: true });
    await api('/api/health');
    const headers = (f.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers['X-LFA-Client']).toBe('dashboard');
    mockFetch({ detail: 'nope' }, 409);
    await expect(api('/api/x')).rejects.toMatchObject({ status: 409, message: 'nope' } satisfies Partial<ApiError>);
  });
  it('builds query strings without empty values', () => {
    expect(qs({ a: 1, b: '', c: null, d: 'x y' })).toBe('?a=1&d=x+y');
  });
});

describe('formatting', () => {
  it('formats scores and rates with explicit n/a', () => {
    expect(fmtScore(0.456)).toBe('46%');
    expect(fmtScore(null)).toBe('—');
    expect(fmtRate(null)).toBe('n/a');
    expect(fmtRate(1)).toBe('100%');
  });
});

describe('state-driven UI rules', () => {
  const base: Application = {
    id: 'a',
    job_id: 'j',
    state: 'APPLICATION_FAILED',
    verification_status: 'uncertain',
    reference_number: null,
    application_url: null,
    applied_at: null,
    attempt_count: 1,
    next_attempt_at: null,
    last_attempt_at: null,
    notes: null,
    updated_at: '',
    created_at: '',
    failure_json: null,
    evidence_json: null,
    pending_questions_json: null,
    fill_plan_json: null,
  };
  it('offers reconciliation instead of retry for uncertain outcomes', () => {
    const actions = allowedActions(base).map((a) => a.action);
    expect(actions).toContain('reconcile_confirmed');
    expect(actions).not.toContain('retry');
    const failed = allowedActions({ ...base, verification_status: 'rejected' }).map((a) => a.action);
    expect(failed).toContain('retry');
  });
  it('never offers cancel for terminal states', () => {
    expect(allowedActions({ ...base, state: 'APPLIED_CONFIRMED', verification_status: 'confirmed' })).toEqual([]);
  });
  it('describes the next action for a job', () => {
    const job = { duplicate_of: null, application_state: 'RETRY_SCHEDULED', next_attempt_at: '2026-10-16T10:00:00Z', failure_json: { category: 'RATE_LIMITED' } } as unknown as Job;
    expect(nextAction(job)).toMatch(/Retry at .*RATE_LIMITED/);
    expect(nextAction({ ...job, duplicate_of: 'x' })).toMatch(/Duplicate/);
  });
});

describe('Overview page', () => {
  it('renders real counts and the rate denominator from the API', async () => {
    mockFetch({
      jobs_discovered: 12,
      jobs_duplicates: 2,
      eligible: 5,
      queued: 1,
      in_progress: 0,
      confirmed: 3,
      awaiting_review: 1,
      failed: 1,
      auth_required: 0,
      skipped: 4,
      by_state: {},
      rates: { success_rate: 0.75, failure_rate: 0.25, denominator: 'attempted outcomes', attempted: 4 },
      recent_activity: [{ id: 1, ts: '2026-10-16T00:00:00Z', level: 'info', event: 'job.imported', task_id: null, application_id: null, job_id: 'j', data_json: { url: 'https://x' } }],
      paused: false,
      extension_connected: false,
      pending_approvals: 0,
    });
    render(
      <MemoryRouter>
        <Overview />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText('12')).toBeInTheDocument());
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText(/attempted outcomes/)).toBeInTheDocument();
    expect(screen.getByText('job.imported')).toBeInTheDocument();
    expect(screen.getByText(/extension is not connected/)).toBeInTheDocument();
  });
});
