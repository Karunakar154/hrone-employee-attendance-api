# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` and a unique compound index on `attendance_logs (emp_code, date)`; these enforce identity and one record per employee/day even under concurrent requests. Supporting indexes cover employee department/code and department/join date filters, attendance date/code sorting, employee/date history queries, date/status filters, and employee/punch-in lookup for punch-out. I considered a standalone `late_minutes` index but did not add it: the leaderboard first filters a month by `date` and then sums lateness, so that index would not replace the date-range access path.

2. **Punch-in race.** Both requests may check the employee and calculate the same attendance date. The unique compound index is the final authority: one insert succeeds with 201; the competing insert raises `DuplicateKeyError`, which the API converts to 409. A pre-check alone is not enough.

3. **Ties.** `$rank` assigns equal late-minute totals the same rank. I filter on `rank <= limit` only after ranking, so ties at the cutoff are all returned; rows are then ordered by total late minutes descending and `emp_code` ascending.

4. **Headcount.** The department summary starts from eligible employee documents (`joined_on <= month end`) and looks up attendance logs. Employees with no logs still contribute to headcount; future joiners are excluded.

5. **100x scale.** I would load-test explain plans and likely pre-aggregate daily attendance metrics, while retaining MongoDB-side filtering and pagination. I would also revisit indexes using real query plans to balance read speed against index storage and write cost.

