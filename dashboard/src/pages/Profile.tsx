import { useEffect, useState } from 'react';
import { ApiError, api, del, post, put } from '../api/client';
import { useGet } from '../hooks/useApi';
import type { Answer, Profile as ProfileData, Resume } from '../types';
import { ErrorBox, Pill } from '../components/ui';

interface ProfileResponse {
  profile: ProfileData;
  completeness: { score: number; missing_required: string[]; missing_recommended: string[]; has_default_resume: boolean };
}

const CONTACT_FIELDS = ['first_name', 'last_name', 'email', 'phone', 'address_line1', 'address_line2', 'city', 'state', 'postal_code', 'country'];
const LINK_FIELDS = ['linkedin', 'github', 'portfolio', 'website', 'other'];
const LEGAL_FIELDS: [string, string][] = [
  ['work_authorization', 'Work authorization (e.g. "Yes, authorized to work in the US without sponsorship")'],
  ['requires_sponsorship', 'Requires visa sponsorship (Yes / No)'],
  ['willing_to_relocate', 'Willing to relocate'],
  ['disability_status', 'Disability status (may be "Prefer not to answer")'],
  ['veteran_status', 'Veteran status'],
  ['gender', 'Gender (optional EEO)'],
  ['ethnicity', 'Ethnicity (optional EEO)'],
  ['criminal_history', 'Criminal history declaration'],
  ['over_18', 'Over 18 (Yes / No)'],
];

export default function Profile() {
  const prof = useGet<ProfileResponse>('/api/profile');
  const answers = useGet<{ items: Answer[] }>('/api/profile/answers');
  const resumes = useGet<{ items: Resume[] }>('/api/profile/resumes');
  const [p, setP] = useState<ProfileData | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (prof.data) setP(prof.data.profile);
  }, [prof.data]);

  async function save() {
    if (!p) return;
    try {
      await put('/api/profile', p);
      setMsg('Profile saved.');
      await prof.reload();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (prof.error) return <ErrorBox error={prof.error} />;
  if (!p || !prof.data) return <p className="muted">Loading…</p>;
  const c = prof.data.completeness;
  const upd = (patch: Partial<ProfileData>) => setP({ ...p, ...patch });

  return (
    <>
      <h1>Candidate profile</h1>
      <div className="card">
        <div className="row">
          <b>Completeness: {Math.round(c.score * 100)}%</b>
          {c.missing_required.length > 0 && <span className="small err">Missing required: {c.missing_required.join(', ')}</span>}
          {c.missing_recommended.length > 0 && <span className="small muted">Recommended: {c.missing_recommended.join(', ')}</span>}
          {!c.has_default_resume && <span className="small warn">No default resume</span>}
        </div>
        <p className="small muted">Everything you type here is a verified fact and may be filled automatically. Empty legal answers always pause the agent so you can answer on the site.</p>
      </div>

      <h2>Contact</h2>
      <div className="card form-grid">
        {CONTACT_FIELDS.map((k) => (
          <label key={k}>
            {k.replace(/_/g, ' ')}
            <input value={p.contact[k] ?? ''} onChange={(e) => upd({ contact: { ...p.contact, [k]: e.target.value } })} />
          </label>
        ))}
      </div>

      <h2>Summary, skills &amp; experience</h2>
      <div className="card form-grid">
        <label className="span2">
          Summary
          <textarea value={p.summary} onChange={(e) => upd({ summary: e.target.value })} />
        </label>
        <label className="span2">
          Skills (comma separated)
          <input value={p.skills.join(', ')} onChange={(e) => upd({ skills: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} />
        </label>
        <label>
          Total years of experience
          <input type="number" value={p.experience_years ?? ''} onChange={(e) => upd({ experience_years: e.target.value === '' ? null : Number(e.target.value) })} />
        </label>
        <label className="span2">
          Cover letter template
          <textarea value={p.cover_letter_template} onChange={(e) => upd({ cover_letter_template: e.target.value })} />
        </label>
      </div>

      <h2>Education</h2>
      <RepeatingList items={p.education} fields={['school', 'degree', 'field_of_study', 'start_date', 'end_date', 'gpa']} onChange={(education) => upd({ education })} />
      <h2>Employment history (most recent first)</h2>
      <RepeatingList items={p.employment as Record<string, string>[]} fields={['company', 'title', 'location', 'start_date', 'end_date', 'description']} onChange={(employment) => upd({ employment })} />
      <h2>Projects</h2>
      <RepeatingList items={p.projects} fields={['name', 'description', 'url']} onChange={(projects) => upd({ projects })} />

      <h2>Links</h2>
      <div className="card form-grid">
        {LINK_FIELDS.map((k) => (
          <label key={k}>
            {k}
            <input value={p.links[k] ?? ''} onChange={(e) => upd({ links: { ...p.links, [k]: e.target.value } })} />
          </label>
        ))}
      </div>

      <h2>Site accounts (automatic sign-in)</h2>
      <div className="card">
        <p className="small muted">
          When a site asks for an account before applying, the agent signs in with the matching entry here (the host or any of its subdomains). Where a site lets applicants create an account (Workday), the agent creates one with your email, a generated password and saves it below. Stored unencrypted in the local database, like the rest of the profile.
        </p>
        {(p.accounts ?? []).map((a, i) => (
          <div key={i} className="row wrap" style={{ gap: 8, marginBottom: 6 }}>
            <input placeholder="host, e.g. wellfound.com" value={a.host} onChange={(e) => upd({ accounts: (p.accounts ?? []).map((x, j) => (j === i ? { ...x, host: e.target.value } : x)) })} />
            <input placeholder="email / username" value={a.email} onChange={(e) => upd({ accounts: (p.accounts ?? []).map((x, j) => (j === i ? { ...x, email: e.target.value } : x)) })} />
            <input type="password" placeholder="password" value={a.password} onChange={(e) => upd({ accounts: (p.accounts ?? []).map((x, j) => (j === i ? { ...x, password: e.target.value } : x)) })} />
            {a.created_by_agent && <Pill>created by the agent</Pill>}
            <button className="danger small" onClick={() => upd({ accounts: (p.accounts ?? []).filter((_, j) => j !== i) })}>
              Remove
            </button>
          </div>
        ))}
        <button onClick={() => upd({ accounts: [...(p.accounts ?? []), { host: '', email: p.contact.email ?? '', password: '' }] })}>Add account</button>
      </div>

      <h2>Preferences</h2>
      <div className="card form-grid">
        <label>
          Target titles
          <input value={((p.preferences.titles as string[]) ?? []).join(', ')} onChange={(e) => upd({ preferences: { ...p.preferences, titles: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) } })} />
        </label>
        <label>
          Preferred locations
          <input value={((p.preferences.locations as string[]) ?? []).join(', ')} onChange={(e) => upd({ preferences: { ...p.preferences, locations: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) } })} />
        </label>
        <label>
          Work arrangement (remote, hybrid, onsite)
          <input value={((p.preferences.remote_types as string[]) ?? []).join(', ')} onChange={(e) => upd({ preferences: { ...p.preferences, remote_types: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) } })} />
        </label>
        <label>
          Experience level
          <input value={String(p.preferences.experience_level ?? '')} onChange={(e) => upd({ preferences: { ...p.preferences, experience_level: e.target.value } })} />
        </label>
        <label>
          Salary min
          <input type="number" value={(p.preferences.salary_min as number | null) ?? ''} onChange={(e) => upd({ preferences: { ...p.preferences, salary_min: e.target.value === '' ? null : Number(e.target.value) } })} />
        </label>
        <label>
          Salary currency
          <input value={String(p.preferences.salary_currency ?? '')} onChange={(e) => upd({ preferences: { ...p.preferences, salary_currency: e.target.value } })} />
        </label>
        <label>
          Notice period
          <input value={String(p.preferences.notice_period ?? '')} onChange={(e) => upd({ preferences: { ...p.preferences, notice_period: e.target.value } })} />
        </label>
        <label>
          Earliest start date
          <input value={String(p.preferences.earliest_start_date ?? '')} onChange={(e) => upd({ preferences: { ...p.preferences, earliest_start_date: e.target.value } })} />
        </label>
      </div>

      <h2>Legally significant answers</h2>
      <div className="card form-grid">
        {LEGAL_FIELDS.map(([k, label]) => (
          <label key={k}>
            {label}
            <input value={p.legal[k] ?? ''} onChange={(e) => upd({ legal: { ...p.legal, [k]: e.target.value } })} />
          </label>
        ))}
      </div>
      <div className="row">
        <button className="primary" onClick={() => void save()}>
          Save profile
        </button>
        {msg && <span className="small">{msg}</span>}
      </div>

      <h2>Resumes</h2>
      <Resumes data={resumes.data?.items ?? []} defaultId={p.default_resume_id} onChange={() => (void resumes.reload(), void prof.reload())} />

      <h2>Approved answers</h2>
      <Answers items={answers.data?.items ?? []} onChange={() => void answers.reload()} />
    </>
  );
}

function RepeatingList({ items, fields, onChange }: { items: Record<string, string>[]; fields: string[]; onChange: (items: Record<string, string>[]) => void }) {
  return (
    <div className="card">
      {items.map((it, i) => (
        <div key={i} className="form-grid bordered">
          {fields.map((f) => (
            <label key={f}>
              {f.replace(/_/g, ' ')}
              <input value={String(it[f] ?? '')} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, [f]: e.target.value } : x)))} />
            </label>
          ))}
          <button className="danger small" onClick={() => onChange(items.filter((_, j) => j !== i))}>
            Remove
          </button>
        </div>
      ))}
      <button onClick={() => onChange([...items, Object.fromEntries(fields.map((f) => [f, '']))])}>Add</button>
    </div>
  );
}

function Resumes({ data, defaultId, onChange }: { data: Resume[]; defaultId: string | null; onChange: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  async function upload(file: File) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      await api(`/api/profile/resumes?label=${encodeURIComponent(file.name)}`, { method: 'POST', body: fd });
      setMsg('Resume uploaded.');
      onChange();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  }
  return (
    <div className="card">
      <label className="file-btn">
        Upload resume (pdf, doc, docx, txt)
        <input type="file" hidden accept=".pdf,.doc,.docx,.txt" onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
      </label>
      {msg && <div className="small">{msg}</div>}
      <ul>
        {data.map((r) => (
          <li key={r.id} className="row">
            <span>
              {r.label} <span className="muted small">({r.file_name}, {Math.round(r.size_bytes / 1024)} KB)</span> {defaultId === r.id && <Pill tone="ok">default</Pill>}
            </span>
            <span>
              {defaultId !== r.id && (
                <button className="small" onClick={() => void put(`/api/profile/resumes/${r.id}`, { make_default: true }).then(onChange)}>
                  Make default
                </button>
              )}{' '}
              <button className="danger small" onClick={() => void del(`/api/profile/resumes/${r.id}`).then(onChange)}>
                Delete
              </button>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Answers({ items, onChange }: { items: Answer[]; onChange: () => void }) {
  const [q, setQ] = useState('');
  const [a, setA] = useState('');
  const [cat, setCat] = useState('custom');
  const [legal, setLegal] = useState(false);
  async function add() {
    if (!q.trim() || !a.trim()) return;
    await post('/api/profile/answers', { category: cat, question: q.trim(), answer: a.trim(), provenance: 'approved_answer', legally_significant: legal });
    setQ('');
    setA('');
    onChange();
  }
  return (
    <div className="card">
      <p className="small muted">Approved answers are reused verbatim when a form asks the same question. Mark legally significant answers so they are only used for exact matches.</p>
      <div className="form-grid">
        <label className="span2">
          Question (as it appears on forms)
          <input value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <label className="span2">
          Answer
          <input value={a} onChange={(e) => setA(e.target.value)} />
        </label>
        <label>
          Category
          <select value={cat} onChange={(e) => setCat(e.target.value)}>
            {['custom', 'legal', 'sponsorship', 'salary', 'notice_period', 'consent', 'source', 'relocation'].map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={legal} onChange={(e) => setLegal(e.target.checked)} /> Legally significant
        </label>
      </div>
      <button className="primary" onClick={() => void add()}>
        Add approved answer
      </button>
      <table>
        <thead>
          <tr>
            <th>Category</th>
            <th>Question</th>
            <th>Answer</th>
            <th>Provenance</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((x) => (
            <tr key={x.id}>
              <td>{x.category}</td>
              <td>{x.question}</td>
              <td>{x.answer}</td>
              <td>
                {x.provenance} {x.legally_significant ? <Pill tone="warn">legal</Pill> : null}
              </td>
              <td>
                <button className="danger small" onClick={() => void del(`/api/profile/answers/${x.id}`).then(onChange)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
