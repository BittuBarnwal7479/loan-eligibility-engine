import { describe, expect, it } from "vitest";
import { isImportDone, isTerminal, MATCHING_TRIGGER_GRACE_MS, matchingNotTriggered, type BatchStatus } from "./api";
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
