export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8081";

export interface UploadTarget {
  method: "POST";
  url: string;
  fields: Record<string, string>;
  expires_at: number;
}

export interface CreateUploadResponse {
  upload_batch_id: string;
  status: string;
  storage_mode: "local" | "s3";
  max_upload_bytes: number;
  upload: UploadTarget;
}

export interface RowError {
  row: number;
  field: string | null;
  code: string;
  message: string;
}

export interface MatchingStats {
  status: string;
  users_considered: number;
  active_products: number;
  total_possible_pairs: number;
  sql_candidate_pairs: number;
  sql_eliminated_pairs: number;
  deterministic_rejections: number;
  eligible_matches: number;
  potential_matches: number;
  llm_invocations: number;
}

export interface BatchStatus {
  upload_batch_id: string;
  original_filename: string;
  status: "PENDING" | "PROCESSING" | "COMPLETED" | "COMPLETED_WITH_ERRORS" | "FAILED";
  total_rows: number;
  processed_rows: number;
  inserted_rows: number;
  updated_rows: number;
  failed_rows: number;
  row_errors: RowError[];
  error_message: string | null;
  matching_status: string;
  notification_status: string;
  matching: MatchingStats | null;
  notifications: Record<string, number>;
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export async function createUpload(file: File): Promise<CreateUploadResponse> {
  const response = await fetch(`${API_BASE_URL}/api/uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, size_bytes: file.size }),
  });
  return json<CreateUploadResponse>(response);
}

/** POST the file straight to the storage target (S3 presigned POST or the local stand-in) with progress. */
export function uploadFile(target: UploadTarget, file: File, onProgress: (fraction: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    Object.entries(target.fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file); // S3 requires the file to be the last field
    const xhr = new XMLHttpRequest();
    xhr.open("POST", target.url);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`Upload failed (HTTP ${xhr.status})${xhr.responseText ? ": " + xhr.responseText.slice(0, 200) : ""}`));
    xhr.onerror = () => reject(new Error("Network error during upload (check CORS and the API URL)."));
    xhr.send(form);
  });
}

export async function getBatch(batchId: string): Promise<BatchStatus> {
  return json<BatchStatus>(await fetch(`${API_BASE_URL}/api/uploads/${encodeURIComponent(batchId)}`));
}

export function isTerminal(b: BatchStatus): boolean {
  if (b.status === "FAILED") return true;
  if (["FAILED", "TRIGGER_FAILED"].includes(b.matching_status)) return true;
  return ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(b.notification_status);
}

export function isImportDone(b: BatchStatus): boolean {
  return ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(b.status);
}

// Longer than the backend's worst-case webhook attempts (3 x 10 s timeout + backoff), during which
// matching_status is still NOT_STARTED even though matching is configured.
export const MATCHING_TRIGGER_GRACE_MS = 60 * 1000;

// Import finished but matching never left NOT_STARTED (e.g. the matching webhook is disabled).
export function matchingNotTriggered(b: BatchStatus, importDoneAt: number | null, now: number): boolean {
  return (
    isImportDone(b) &&
    b.matching_status === "NOT_STARTED" &&
    importDoneAt !== null &&
    now - importDoneAt >= MATCHING_TRIGGER_GRACE_MS
  );
}

export type PollOutcome = "continue" | "finished" | "matching_not_triggered" | "timed_out";

// Every outcome except "continue" means polling has stopped and the UI must leave its tracking state.
export function pollOutcome(
  b: BatchStatus,
  importDoneAt: number | null,
  started: number,
  now: number,
  limitMs: number,
): PollOutcome {
  if (isTerminal(b)) return "finished";
  if (matchingNotTriggered(b, importDoneAt, now)) return "matching_not_triggered";
  if (now - started > limitMs) return "timed_out";
  return "continue";
}
