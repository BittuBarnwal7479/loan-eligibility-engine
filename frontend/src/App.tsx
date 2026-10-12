import { useEffect, useRef, useState } from "react";
import {
  API_BASE_URL,
  createUpload,
  getBatch,
  isImportDone,
  isTerminal,
  pollOutcome,
  uploadFile,
  type BatchStatus,
} from "./api";
import { COLUMN_HELP, REQUIRED_COLUMNS, checkFileBasics, checkHeaderLine, templateCsv } from "./csv";

type Phase = "idle" | "uploading" | "tracking" | "done" | "error";

const POLL_MS = 2000;
const POLL_LIMIT_MS = 10 * 60 * 1000;

function downloadTemplate() {
  const url = URL.createObjectURL(new Blob([templateCsv()], { type: "text/csv" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: "users_template.csv" });
  a.click();
  URL.revokeObjectURL(url);
}

function Badge({ label, value }: { label: string; value: string }) {
  const tone = /FAILED/.test(value) ? "bad" : /COMPLETED|TRIGGERED/.test(value) ? "good" : "neutral";
  return (
    <div className="badge-row">
      <span className="badge-label">{label}</span>
      <span className={`badge ${tone}`}>{value}</span>
    </div>
  );
}

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [storageMode, setStorageMode] = useState<string | null>(null);
  const [batch, setBatch] = useState<BatchStatus | null>(null);
  const [pollNote, setPollNote] = useState<string | null>(null);
  const [matchingSkipped, setMatchingSkipped] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);

  async function choose(f: File | null) {
    setFile(null);
    setProblem(null);
    if (!f) return;
    const basic = checkFileBasics(f.name, f.size);
    if (basic) return setProblem(basic);
    const firstLine = (await f.slice(0, 4096).text()).split(/\r?\n/)[0] ?? "";
    const header = checkHeaderLine(firstLine);
    if (header) return setProblem(header);
    setFile(f);
  }

  function poll(id: string, started: number, importDoneAt: number | null = null) {
    getBatch(id)
      .then((b) => {
        setBatch(b);
        setPollNote(null);
        const doneAt = isImportDone(b) ? (importDoneAt ?? Date.now()) : null;
        const outcome = pollOutcome(b, doneAt, started, Date.now(), POLL_LIMIT_MS);
        if (outcome === "continue") {
          timer.current = window.setTimeout(() => poll(id, started, doneAt), POLL_MS);
          return;
        }
        setPhase("done");
        if (outcome === "matching_not_triggered") {
          setMatchingSkipped(true);
          setPollNote("Import completed. Matching was not triggered in the current configuration.");
        } else if (outcome === "timed_out") {
          setPollNote("Stopped polling after 10 minutes. Processing may still continue; refresh to check again.");
        }
      })
      .catch((e: Error) => {
        setPollNote(`Status check failed: ${e.message}. Retrying...`);
        timer.current = window.setTimeout(() => poll(id, started, importDoneAt), POLL_MS * 2);
      });
  }

  async function start() {
    if (!file) return;
    setPhase("uploading");
    setProgress(0);
    setBatch(null);
    setProblem(null);
    setMatchingSkipped(false);
    try {
      const created = await createUpload(file);
      setBatchId(created.upload_batch_id);
      setStorageMode(created.storage_mode);
      await uploadFile(created.upload, file, setProgress);
      setProgress(1);
      setPhase("tracking");
      poll(created.upload_batch_id, Date.now());
    } catch (e) {
      setPhase("error");
      setProblem((e as Error).message);
    }
  }

  const m = batch?.matching;
  const busy = phase === "uploading";

  return (
    <main>
      <header>
        <h1>Loan Eligibility Engine</h1>
        <p className="muted">Upload a CSV of user profiles. Matching against published lender criteria and email notifications run in the background.</p>
      </header>

      <section className="card">
        <h2>1. Prepare your CSV</h2>
        <table className="columns">
          <thead><tr><th>Column</th><th>Rule</th></tr></thead>
          <tbody>
            {REQUIRED_COLUMNS.map((c) => (
              <tr key={c}><td><code>{c}</code></td><td>{COLUMN_HELP[c]}</td></tr>
            ))}
          </tbody>
        </table>
        <button className="secondary" onClick={downloadTemplate}>Download template</button>
      </section>

      <section className="card">
        <h2>2. Upload</h2>
        <label className="drop">
          <input type="file" accept=".csv,text/csv" disabled={busy} onChange={(e) => choose(e.target.files?.[0] ?? null)} />
          <span>{file ? `${file.name} (${(file.size / 1024).toFixed(1)} KB)` : "Choose a .csv file (max 10 MB)"}</span>
        </label>
        {problem && <p className="error" role="alert">{problem}</p>}
        <button disabled={!file || busy} onClick={start}>{busy ? "Uploading..." : "Upload and process"}</button>
        {(busy || phase === "tracking") && (
          <div className="progress" aria-label="upload progress"><div style={{ width: `${Math.round(progress * 100)}%` }} /></div>
        )}
        {batchId && (
          <p className="muted">
            Upload batch ID: <code>{batchId}</code>{storageMode && <> · storage: <code>{storageMode === "s3" ? "S3 (presigned POST)" : "local dev"}</code></>}
          </p>
        )}
      </section>

      {batch && (
        <section className="card">
          <h2>3. Processing status</h2>
          <Badge label="Import" value={batch.status} />
          <Badge label="Matching" value={batch.matching_status} />
          <Badge label="Notifications" value={batch.notification_status} />
          {!isTerminal(batch) && !matchingSkipped && <p className="muted">Matching and notifications continue asynchronously - you can close this page.</p>}
          {batch.error_message && <p className="error">{batch.error_message}</p>}

          <div className="stats">
            <div><b>{batch.total_rows}</b><span>rows read</span></div>
            <div><b>{batch.processed_rows}</b><span>imported</span></div>
            <div><b>{batch.failed_rows}</b><span>rejected</span></div>
            {m && <div><b>{m.eligible_matches + m.potential_matches}</b><span>matches</span></div>}
          </div>

          {m && (
            <details open>
              <summary>Matching pipeline</summary>
              <ul className="pipeline">
                <li>{m.total_possible_pairs.toLocaleString()} possible user-product pairs ({m.users_considered} users × {m.active_products} products)</li>
                <li>{m.sql_eliminated_pairs.toLocaleString()} eliminated by the SQL pre-filter; {m.sql_candidate_pairs.toLocaleString()} candidates evaluated</li>
                <li>{m.eligible_matches} meet all published criteria · {m.potential_matches} meet known criteria with some unknown</li>
                <li>{m.llm_invocations} LLM call(s)</li>
              </ul>
            </details>
          )}

          {Object.keys(batch.notifications).length > 0 && (
            <p className="muted">Emails: {Object.entries(batch.notifications).map(([k, v]) => `${v} ${k}`).join(", ")} (sent to the configured test inbox only)</p>
          )}

          {batch.row_errors.length > 0 && (
            <details open={batch.row_errors.length <= 20}>
              <summary>Rejected rows ({batch.failed_rows}{batch.row_errors.length < batch.failed_rows ? `, first ${batch.row_errors.length} shown` : ""})</summary>
              <table className="errors">
                <thead><tr><th>Line</th><th>Field</th><th>Problem</th></tr></thead>
                <tbody>
                  {batch.row_errors.map((e, i) => (
                    <tr key={i}><td>{e.row}</td><td>{e.field ?? "-"}</td><td>{e.message}</td></tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
          {pollNote && <p className="muted">{pollNote}</p>}
        </section>
      )}

      <footer className="muted">
        Matches are preliminary and based only on publicly published criteria; they are not loan offers or financial advice.
        API: <code>{API_BASE_URL}</code>
      </footer>
    </main>
  );
}
