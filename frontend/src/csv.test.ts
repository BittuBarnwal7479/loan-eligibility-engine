import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getLatestBatch,
  importCompletedAt,
  isImportDone,
  isTerminal,
  MATCHING_TRIGGER_GRACE_MS,
  matchingNotTriggered,
  pollOutcome,
  type BatchStatus,
} from "./api";
import { checkFileBasics, checkHeaderLine, normalizeHeader, templateCsv } from "./csv";

describe("csv pre-checks", () => {
  it("accepts a valid header in any case/spacing and with a BOM", () => {
    expect(checkHeaderLine("﻿User ID,Email,monthly-income,CREDIT_SCORE,employment_status,age")).toBeNull();
  });

  it("reports missing columns", () => {
    expect(checkHeaderLine("user_id,email")).toBe("Missing column(s): monthly_income, credit_score, employment_status, age");
  });

  it("checks extension and size", () => {
    expect(checkFileBasics("users.xlsx", 10)).toMatch(/csv/);
    expect(checkFileBasics("users.csv", 0)).toMatch(/empty/);
    expect(checkFileBasics("users.csv", 11 * 1024 * 1024)).toMatch(/larger/);
    expect(checkFileBasics("USERS.CSV", 100)).toBeNull();
  });

  it("template header passes its own check", () => {
    expect(checkHeaderLine(templateCsv().split("\n")[0])).toBeNull();
    expect(normalizeHeader(" Monthly Income ")).toBe("monthly_income");
  });
});

describe("polling stop condition", () => {
  const base = { status: "COMPLETED", matching_status: "RUNNING", notification_status: "NOT_STARTED" } as BatchStatus;
  it("keeps polling while work is in progress", () => expect(isTerminal(base)).toBe(false));
  it("stops on failure or completion", () => {
    expect(isTerminal({ ...base, status: "FAILED" })).toBe(true);
    expect(isTerminal({ ...base, matching_status: "TRIGGER_FAILED" })).toBe(true);
    expect(isTerminal({ ...base, notification_status: "COMPLETED" })).toBe(true);
  });
});

describe("matching not triggered (webhook disabled / dry-run)", () => {
  const done = { status: "COMPLETED", matching_status: "NOT_STARTED", notification_status: "NOT_STARTED" } as BatchStatus;
  const t0 = 1_000_000;

  it("stops after the grace period when import is done and matching never started", () => {
    expect(isTerminal(done)).toBe(false);
    expect(matchingNotTriggered(done, t0, t0 + MATCHING_TRIGGER_GRACE_MS)).toBe(true);
    expect(matchingNotTriggered({ ...done, status: "COMPLETED_WITH_ERRORS" }, t0, t0 + MATCHING_TRIGGER_GRACE_MS)).toBe(true);
  });

  it("keeps polling within the grace period so the backend can trigger matching", () => {
    expect(matchingNotTriggered(done, t0, t0)).toBe(false);
    expect(matchingNotTriggered(done, t0, t0 + MATCHING_TRIGGER_GRACE_MS - 1)).toBe(false);
  });

  it("grace period outlasts the backend's worst-case webhook attempts (~37 s)", () => {
    expect(MATCHING_TRIGGER_GRACE_MS).toBeGreaterThan(37_000);
  });

  it("keeps polling while the import is still running", () => {
    for (const status of ["PENDING", "PROCESSING"] as const) {
      expect(isImportDone({ ...done, status })).toBe(false);
      expect(matchingNotTriggered({ ...done, status }, null, t0 + 10 * MATCHING_TRIGGER_GRACE_MS)).toBe(false);
    }
  });

  it("does not stop in normal mode once matching has started", () => {
    const late = t0 + 10 * MATCHING_TRIGGER_GRACE_MS;
    for (const matching_status of ["TRIGGERED", "RUNNING", "COMPLETED"]) {
      const b = { ...done, matching_status };
      expect(matchingNotTriggered(b, t0, late)).toBe(false);
      expect(isTerminal(b)).toBe(false);
    }
    expect(isTerminal({ ...done, matching_status: "COMPLETED", notification_status: "COMPLETED" })).toBe(true);
  });
});

describe("poll outcome (UI leaves tracking state whenever polling stops)", () => {
  const limit = 10 * 60 * 1000;
  const t0 = 1_000_000;
  const done = { status: "COMPLETED", matching_status: "NOT_STARTED", notification_status: "NOT_STARTED" } as BatchStatus;

  it("dry-run: import done, matching NOT_STARTED past the grace period stops polling", () => {
    expect(pollOutcome(done, t0, t0, t0 + MATCHING_TRIGGER_GRACE_MS, limit)).toBe("matching_not_triggered");
  });

  it("continues during the grace period and while matching runs", () => {
    expect(pollOutcome(done, t0, t0, t0 + 1000, limit)).toBe("continue");
    expect(pollOutcome({ ...done, matching_status: "RUNNING" }, t0, t0, t0 + MATCHING_TRIGGER_GRACE_MS, limit)).toBe("continue");
  });

  it("reports finished and timed-out as stop outcomes", () => {
    expect(pollOutcome({ ...done, matching_status: "COMPLETED", notification_status: "COMPLETED" }, t0, t0, t0, limit)).toBe("finished");
    expect(pollOutcome({ ...done, status: "PROCESSING" }, null, t0, t0 + limit + 1, limit)).toBe("timed_out");
  });
});

describe("restoring the latest batch after a page reload", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fetches only the newest batch and returns it", async () => {
    const latest = { upload_batch_id: "b-1", status: "COMPLETED", total_rows: 10000 };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ uploads: [latest] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await getLatestBatch()).toEqual(latest);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/uploads\?limit=1$/));
  });

  it("returns null when there are no uploads yet", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ uploads: [] }), { status: 200 })));
    expect(await getLatestBatch()).toBeNull();
  });

  it("uses the server completion time so an old dry-run batch stops at once", () => {
    const completed = "2026-10-12T19:22:55.826Z";
    const doneAt = importCompletedAt({ processing_completed_at: completed });
    expect(doneAt).toBe(Date.parse(completed));
    const b = { status: "COMPLETED", matching_status: "NOT_STARTED", notification_status: "NOT_STARTED" } as BatchStatus;
    const reloadedAt = Date.parse(completed) + 60 * 60 * 1000;
    expect(pollOutcome(b, doneAt, reloadedAt, reloadedAt, 10 * 60 * 1000)).toBe("matching_not_triggered");
  });

  it("has no completion time while the import is still running", () => {
    expect(importCompletedAt({ processing_completed_at: null })).toBeNull();
    expect(importCompletedAt({ processing_completed_at: "not-a-date" })).toBeNull();
  });
});
