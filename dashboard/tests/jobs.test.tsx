import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Jobs from '../src/pages/Jobs';

afterEach(() => vi.restoreAllMocks());

const FILTERS = { titles: [], keywords: [], excluded_keywords: [], min_experience_years: null, max_experience_years: null, locations: [], remote_types: [], salary_min: null, salary_max: null, employment_types: [], excluded_companies: [], requires_sponsorship: null, required_skills: [], max_posting_age_days: null, min_match_score: 0 };

/** Serve each API path from a table; records every call. */
function mockApi(routes: Record<string, unknown>) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const body = routes[path];
    if (body === undefined) return new Response(JSON.stringify({ detail: `no route for ${path}` }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
}

describe('Job queue page', () => {
  it('shows what is in the queue and empties it on confirmation', async () => {
    const f = mockApi({
      '/api/jobs': { items: [], total: 0, limit: 25, offset: 0 },
      '/api/jobs/filters': FILTERS,
      '/api/applications/queue': { queued: 2, retry_scheduled: 1, pending_tasks: 1, running_queue_tasks: 1, total: 5 },
      '/api/applications/queue/clear': { applications_cancelled: 3, tasks_cancelled: 1, runs_stopped: 1, queued: 0, retry_scheduled: 0, pending_tasks: 0, running_queue_tasks: 0, total: 0 },
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(
      <MemoryRouter>
        <Jobs />
      </MemoryRouter>,
    );
    const button = await screen.findByRole('button', { name: /Empty queue \(5\)/ });
    expect(screen.getByText(/2 queued/)).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByText(/Queue emptied: 3 application\(s\) cancelled, 1 waiting task\(s\) cancelled, 1 list pass\(es\) stopped/)).toBeInTheDocument());
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toMatch(/4 waiting application\(s\) will be cancelled and 1 running list pass\(es\) will be stopped/);
    const clearCall = f.mock.calls.find(([input]) => String(input).includes('/api/applications/queue/clear'));
    expect(clearCall).toBeTruthy();
    expect((clearCall![1] as RequestInit).method).toBe('POST');
  });

  it('does nothing when the confirmation is declined and is disabled on an empty queue', async () => {
    const f = mockApi({
      '/api/jobs': { items: [], total: 0, limit: 25, offset: 0 },
      '/api/jobs/filters': FILTERS,
      '/api/applications/queue': { queued: 0, retry_scheduled: 0, pending_tasks: 0, running_queue_tasks: 0, total: 0 },
    });
    render(
      <MemoryRouter>
        <Jobs />
      </MemoryRouter>,
    );
    const button = (await screen.findByRole('button', { name: /Empty queue \(0\)/ })) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(f.mock.calls.some(([input]) => String(input).includes('/queue/clear'))).toBe(false);
  });
});
