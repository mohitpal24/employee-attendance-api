# Starter code review

| # | Where | What is wrong | How to notice | How it is fixed |
|---|---|---|---|---|
| 1 | `health` | Always reports ready without checking MongoDB and never returns 503. | Stop MongoDB and call `/health`; it still says `ok`. | Ping MongoDB and map connection failures to 503. |
| 2 | `compute_late_minutes` | Compares against a naive same-day time and applies the grace test after flooring to minutes. A punch at 09:40:01 for a 09:30 shift should count 10 late minutes but is returned as zero. | Check the exact 10-minute boundary and an overnight shift. | Compare timezone-aware instants on the attendance date; apply the strict 600-second test before flooring. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even, not the required half-up rule. | Use a duration whose hour value lands on a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It counts any positive overtime, omits the 30-minute minimum, and always places shift end on the attendance date. | Test 29:59 and 30:00 overtime and a 22:00–06:00 shift. | Use the overnight end date and apply the 30-minute threshold after whole-minute flooring. |
| 5 | `create_employee` | The read-then-insert duplicate check races; `created_at` is naive and returned as a datetime rather than epoch milliseconds. | Send parallel creates with one `emp_code`; inspect the response timestamp. | Enforce uniqueness with a database index, handle duplicate-key errors, store UTC, and serialize epoch milliseconds. |
| 6 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores department filtering. Page values are unbounded, and there is no guaranteed `emp_code` sort. | Request page 1 with two employees and filter by a department. | Use `(page - 1) * page_size`, count the filtered query, validate limits, and sort by `emp_code`. |
| 7 | `punch_in` employee lookup | A missing employee is dereferenced and causes a 500. | Punch in with an unknown code. | Return 404 before reading shift fields. |
| 8 | `punch_in` timestamp/date | `fromtimestamp` interprets local time and treats the value as seconds after dividing, so date and stored time can vary by host timezone; it misses overnight attendance-date rules and second truncation. | Send a known epoch-millisecond value near midnight from a non-IST host. | Parse UTC epoch milliseconds, truncate to whole seconds, convert to IST, and apply the overnight date rule. |
| 9 | `punch_in` validation | Status is unrestricted, timestamps accept bad shapes, and equal-day duplicates rely on a racy pre-check. | Send `ABSENT`, a seconds-like timestamp, and simultaneous duplicate requests. | Validate the request model and use the unique `(emp_code, date)` index as the race-safe guarantee. |
| 10 | `punch_in` response | It adds an `id` that is not in the contract and returns BSON datetimes rather than epoch milliseconds. | Compare its JSON to the `AttendanceRecord` schema. | Return only contract fields and convert all API instants, including nested history times. |
| 11 | `list_attendance` | Reads and sorts the entire collection in Python, which does not scale to 100,000 records. It also lacks the required stable employee-code tie-break. | Load a large collection and inspect memory/query time; create same-date records for multiple employees. | Apply filter, sort, skip, and limit in MongoDB with supporting indexes. |
| 12 | `list_attendance` totals and output | It has no date validation or page bounds and returns an `id` plus raw BSON dates. | Use invalid/reversed dates, an oversized page, or inspect JSON for a seeded record. | Validate inclusive filters and pagination, count the filtered query, omit internal IDs, and normalize legacy fields. |
| 13 | Module setup | No indexes are created, so identity, attendance uniqueness, and query speed depend on accidental database state. | Start against the grader's index-free database and inspect index plans. | Create the required indexes idempotently at startup. |
| 14 | Missing contract endpoints | Punch-out, regularization, analytics, and explain are not implemented. | Call each documented route. | Implement every route marked for the candidate, including pipelines and atomic update behavior. |

| 15 | `EmployeeIn` model | It accepts malformed employee codes, emails, shift times, equal shifts, and invalid join dates. | Try a bad email, `25:99`, equal shifts, or an invalid date. | Add contract-aligned field and cross-field validators. |

## Inspected and not treated as defects

- `emp_code` is the correct business identifier; MongoDB `_id` should remain internal.
- Punch-in initializes `history` as an empty list and the derived fields to their open-record defaults, as required.
- Department matching is intended to be exact and case-sensitive; the query does not need case folding.


