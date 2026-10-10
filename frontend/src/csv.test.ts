import { describe, expect, it } from "vitest";
import { isTerminal, type BatchStatus } from "./api";
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
