# Design decisions

1. **Indexes.** A unique `employees.emp_code` index enforces employee identity. Department/join-date and join-date indexes support department headcount; department plus employee code supports filtered lists. `attendance_logs` uses a unique `(emp_code, date)` index for one record per day and race-safe punch-in, date/employee and status/date indexes for list filters and ordering, and employee/punch-time for punch-out. I rejected a status-only index because the compound status/date index also supports date ordering.

2. **Punch-in race.** Concurrent requests both attempt an insert. MongoDB's unique `(emp_code, date)` index accepts one and rejects the other with a duplicate-key error; the API maps that error to 409. A preliminary read is not the guarantee.

3. **Ties.** MongoDB ranks on total late minutes. Equal totals share a rank and the next rank is skipped. The limit applies to rank, so all employees tied at an included rank are returned; employee code orders tied rows.

4. **Headcount.** The department summary starts with employees who joined by month end, then looks up that month's records while preserving employees with no logs.

5. **100x scale.** I would measure query plans and timings on production-like data, then consider pre-aggregated summaries. They reduce repeated work but need a refresh strategy when corrections change attendance.


