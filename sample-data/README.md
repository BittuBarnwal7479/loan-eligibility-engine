# Sample data (synthetic)

All files here are **generated test data**. User IDs, incomes, scores and ages are random;
emails use the reserved `example.com` domain. They are not real people.

| File | Purpose |
|---|---|
| `users_sample.csv` | 60 valid users, mixed employment types (happy path) |
| `users_with_errors.csv` | 5 valid rows + 7 invalid rows (bad email, non-numeric income, score out of range, unknown employment status, under-age, duplicate user_id, missing column) -> `COMPLETED_WITH_ERRORS` |
| `invalid_header.csv` | Wrong header -> batch `FAILED`, nothing imported |

The assignment's original sample CSV is not included. Even if you add it, the system
never emails CSV addresses: every message goes to `NOTIFICATION_TEST_RECIPIENT`.
