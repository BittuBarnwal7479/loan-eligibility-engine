// Client-side pre-checks. The backend re-validates everything; these only give fast feedback.

export const REQUIRED_COLUMNS = [
  "user_id",
  "email",
  "monthly_income",
  "credit_score",
  "employment_status",
  "age",
] as const;

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export const COLUMN_HELP: Record<(typeof REQUIRED_COLUMNS)[number], string> = {
  user_id: "Unique id, letters/digits/_ . : - (max 64)",
  email: "Valid email (stored, never emailed - notifications go to the test inbox)",
  monthly_income: "Number >= 0, e.g. 55000",
  credit_score: "Integer 300-900",
  employment_status: "salaried, self_employed, unemployed, student or retired",
  age: "Integer 18-100",
};

export function normalizeHeader(name: string): string {
  return name.trim().replace(/^﻿/, "").toLowerCase().replace(/[\s-]+/g, "_");
}

export function checkFileBasics(name: string, size: number, maxBytes = MAX_UPLOAD_BYTES): string | null {
  if (!name.toLowerCase().endsWith(".csv")) return "Only .csv files are accepted.";
  if (size === 0) return "The file is empty.";
  if (size > maxBytes) return `The file is larger than ${Math.round(maxBytes / 1024 / 1024)} MB.`;
  return null;
}

export function checkHeaderLine(firstLine: string): string | null {
  const columns = firstLine.split(",").map((c) => normalizeHeader(c.replace(/^"|"$/g, "")));
  const missing = REQUIRED_COLUMNS.filter((c) => !columns.includes(c));
  return missing.length ? `Missing column(s): ${missing.join(", ")}` : null;
}

export function templateCsv(): string {
  return `${REQUIRED_COLUMNS.join(",")}\nU0001,someone@example.com,55000,742,salaried,29\n`;
}
