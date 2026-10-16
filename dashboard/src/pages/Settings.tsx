import { useEffect, useState } from 'react';
import { ApiError, del, post, put } from '../api/client';
import { useGet } from '../hooks/useApi';
import type { SchedulerConfig, SettingsResponse } from '../types';
import { ErrorBox, Pill } from '../components/ui';

const ROLES = ['intent', 'planner', 'extractor', 'matcher', 'field_mapper', 'classifier'];
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

interface ModelHealth {
  roles: Record<string, { provider: string; model: string }>;
  providers: Record<string, { ok: boolean; base_url?: string; models?: string[]; error?: string }>;
}
interface Pairing {
  id: string;
  label: string;
  extension_id: string | null;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
}

export default function Settings() {
  const settings = useGet<SettingsResponse>('/api/settings');
  const health = useGet<ModelHealth>('/api/settings/models');
  const pairings = useGet<{ items: Pairing[]; pending_code: boolean }>('/api/pairing', [], 5000);
  const [cfg, setCfg] = useState<SchedulerConfig | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [code, setCode] = useState<{ code: string; expires_in: number } | null>(null);
  const [evalResult, setEvalResult] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    if (settings.data) setCfg({ ...settings.data.scheduler, model_roles: Object.keys(settings.data.scheduler.model_roles).length ? settings.data.scheduler.model_roles : settings.data.model_roles });
  }, [settings.data]);

  async function save() {
    if (!cfg) return;
    try {
      await put('/api/settings', cfg);
      setMsg('Settings saved.');
      await settings.reload();
      await health.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  }
  async function issueCode() {
    setCode(await post<{ code: string; expires_in: number }>('/api/pairing/code'));
  }
  async function evaluate() {
    setEvalResult({ running: true });
    try {
      setEvalResult(await post<Record<string, unknown>>('/api/settings/models/evaluate', {}));
    } catch (e) {
      setEvalResult({ error: e instanceof ApiError ? e.message : String(e) });
    }
  }

  if (settings.error) return <ErrorBox error={settings.error} />;
  if (!cfg || !settings.data) return <p className="muted">Loading…</p>;
  const up = (patch: Partial<SchedulerConfig>) => setCfg({ ...cfg, ...patch });

  return (
    <>
      <h1>Automation settings</h1>

      <h2>Extension pairing</h2>
      <div className="card">
        <p className="small muted">Generate a code, open the extension's options page and enter it within 5 minutes. The token is stored hashed; revoke it here at any time.</p>
        <div className="row">
          <button className="primary" onClick={() => void issueCode()}>
            Generate pairing code
          </button>
          {code && (
            <span>
              Code: <code className="big">{code.code}</code> <span className="small muted">(expires in {Math.round(code.expires_in / 60)} min)</span>
            </span>
          )}
        </div>
        <table>
          <thead>
            <tr>
              <th>Pairing</th>
              <th>Extension id</th>
              <th>Created</th>
              <th>Last seen</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {(pairings.data?.items ?? []).map((p) => (
              <tr key={p.id}>
                <td>
                  {p.label} {p.revoked_at ? <Pill tone="muted">revoked</Pill> : <Pill tone="ok">active</Pill>}
                </td>
                <td className="small">{p.extension_id ?? '—'}</td>
                <td className="small">{p.created_at}</td>
                <td className="small">{p.last_seen_at ?? '—'}</td>
                <td>{!p.revoked_at && <button className="danger small" onClick={() => void del(`/api/pairing/${p.id}`).then(() => pairings.reload())}>Revoke</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Scheduler &amp; limits</h2>
      <div className="card form-grid">
        <label className="checkbox">
          <input type="checkbox" checked={cfg.enabled} onChange={(e) => up({ enabled: e.target.checked })} /> Scheduler enabled (picks up queued applications automatically)
        </label>
        <label>
          Daily application preparation limit <input type="number" value={cfg.daily_application_limit} onChange={(e) => up({ daily_application_limit: Number(e.target.value) })} />
        </label>
        <label>
          Max concurrent tasks <input type="number" min={1} value={cfg.max_concurrent_tasks} onChange={(e) => up({ max_concurrent_tasks: Number(e.target.value) })} />
        </label>
        <label>
          Per-domain concurrency <input type="number" min={1} value={cfg.per_domain_concurrency} onChange={(e) => up({ per_domain_concurrency: Number(e.target.value) })} />
        </label>
        <label>
          Per-domain delay (s) <input type="number" value={cfg.per_domain_delay_seconds} onChange={(e) => up({ per_domain_delay_seconds: Number(e.target.value) })} />
        </label>
        <label>
          Active hours (start) <input type="number" min={0} max={23} value={cfg.active_hours_start} onChange={(e) => up({ active_hours_start: Number(e.target.value) })} />
        </label>
        <label>
          Active hours (end) <input type="number" min={1} max={24} value={cfg.active_hours_end} onChange={(e) => up({ active_hours_end: Number(e.target.value) })} />
        </label>
        <div className="span2 row wrap">
          Active days:
          {DAYS.map((d, i) => (
            <label key={d} className="checkbox">
              <input type="checkbox" checked={cfg.active_days.includes(i)} onChange={(e) => up({ active_days: e.target.checked ? [...cfg.active_days, i].sort() : cfg.active_days.filter((x) => x !== i) })} /> {d}
            </label>
          ))}
        </div>
        <label>
          Max retries <input type="number" value={cfg.max_retries} onChange={(e) => up({ max_retries: Number(e.target.value) })} />
        </label>
        <label>
          Submission approval{' '}
          <select value={cfg.approval_policy} onChange={(e) => up({ approval_policy: e.target.value as SchedulerConfig['approval_policy'] })}>
            <option value="always">always ask me (default)</option>
            <option value="per_site">ask when the site adapter requires it</option>
            <option value="never">never ask (submit automatically)</option>
          </select>
        </label>
        <label className="span2">
          Allowed domains (comma separated; the agent never acts outside this list)
          <input value={cfg.allowed_domains.join(', ')} onChange={(e) => up({ allowed_domains: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} placeholder="boards.greenhouse.io, jobs.lever.co" />
        </label>
      </div>

      <h2>Resource budgets</h2>
      <div className="card form-grid">
        <label>
          Max RAM for the agent process (MB) <input type="number" value={cfg.max_ram_mb} onChange={(e) => up({ max_ram_mb: Number(e.target.value) })} />
        </label>
        <label>
          CPU threads for inference (0 = runtime default) <input type="number" value={cfg.cpu_threads} onChange={(e) => up({ cpu_threads: Number(e.target.value) })} />
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={cfg.gpu_enabled} onChange={(e) => up({ gpu_enabled: e.target.checked })} /> Use GPU when the runtime supports it
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={cfg.auto_pause_on_resource_pressure} onChange={(e) => up({ auto_pause_on_resource_pressure: e.target.checked })} /> Pause automatically under memory pressure
        </label>
        <div className="span2 small muted">
          Runtime bounds (set via LFA_* environment variables): max actions/task {String(settings.data.runtime.max_actions_per_task)}, action timeout {String(settings.data.runtime.action_timeout_seconds)}s, task deadline {String(settings.data.runtime.task_deadline_seconds)}s, snapshot nodes {String(settings.data.runtime.snapshot_max_nodes)}, model context {String(settings.data.runtime.model_context_chars)} chars, max model response {String(settings.data.runtime.model_max_response_bytes)} bytes, concurrent pages {String(settings.data.runtime.max_concurrent_pages)}, concurrent inference {String(settings.data.runtime.model_max_concurrency)}.
        </div>
      </div>

      <h2>Model selection</h2>
      <div className="card">
        <p className="small muted">
          Local inference only. Providers: <b>ollama</b> ({String(settings.data.runtime.ollama_base_url)}), <b>openai_compatible</b> (llama.cpp / LM Studio at {String(settings.data.runtime.openai_compatible_base_url)}), <b>heuristic</b> (deterministic rules, no model).
        </p>
        {health.data && (
          <div className="row wrap">
            {Object.entries(health.data.providers).map(([name, h]) => (
              <span key={name}>
                <Pill tone={h.ok ? 'ok' : 'err'}>{name}</Pill> <span className="small muted">{h.ok ? `${h.models?.length ?? 0} models` : h.error ?? 'unavailable'}</span>
              </span>
            ))}
          </div>
        )}
        <table>
          <thead>
            <tr>
              <th>Role</th>
              <th>Provider</th>
              <th>Model</th>
              <th>Temperature</th>
              <th>Max output tokens</th>
            </tr>
          </thead>
          <tbody>
            {ROLES.map((r) => {
              const rc = cfg.model_roles[r] ?? { provider: 'heuristic', model: '', temperature: 0, max_output_tokens: 1024 };
              const set = (patch: Partial<typeof rc>) => up({ model_roles: { ...cfg.model_roles, [r]: { ...rc, ...patch } } });
              const models = health.data?.providers[rc.provider]?.models ?? [];
              return (
                <tr key={r}>
                  <td>{r}</td>
                  <td>
                    <select value={rc.provider} onChange={(e) => set({ provider: e.target.value })}>
                      <option value="heuristic">heuristic</option>
                      <option value="ollama">ollama</option>
                      <option value="openai_compatible">openai_compatible</option>
                    </select>
                  </td>
                  <td>
                    <input list={`models-${r}`} value={rc.model} onChange={(e) => set({ model: e.target.value })} disabled={rc.provider === 'heuristic'} />
                    <datalist id={`models-${r}`}>
                      {models.map((m) => (
                        <option key={m} value={m} />
                      ))}
                    </datalist>
                  </td>
                  <td>
                    <input type="number" step="0.1" min="0" max="2" value={rc.temperature} onChange={(e) => set({ temperature: Number(e.target.value) })} />
                  </td>
                  <td>
                    <input type="number" value={rc.max_output_tokens} onChange={(e) => set({ max_output_tokens: Number(e.target.value) })} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="row">
          <button onClick={() => void evaluate()}>Run model evaluation</button>
          {evalResult && 'running' in evalResult && <span className="small muted">Running…</span>}
          {evalResult && 'average_score' in evalResult && (
            <span className="small">
              Average score {String(evalResult.average_score)} over {String(evalResult.cases)} cases · p50 {String(evalResult.p50_latency_ms)} ms · p95 {String(evalResult.p95_latency_ms)} ms
            </span>
          )}
          {evalResult && 'error' in evalResult && <span className="small err">{String(evalResult.error)}</span>}
        </div>
      </div>

      <div className="row">
        <button className="primary" onClick={() => void save()}>
          Save settings
        </button>
        {msg && <span className="small">{msg}</span>}
      </div>

      <h2>Site adapters</h2>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Adapter</th>
              <th>Operations</th>
              <th>Access policy</th>
              <th>Verification</th>
            </tr>
          </thead>
          <tbody>
            {settings.data.adapters.map((a) => (
              <tr key={a.name}>
                <td>{a.name}</td>
                <td>{a.supported_operations.join(', ')}</td>
                <td className="small">{a.access_policy}</td>
                <td className="small">{a.verification_method}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
