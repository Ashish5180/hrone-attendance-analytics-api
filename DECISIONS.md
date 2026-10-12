# DECISIONS.md

1. **Indexes.**
   - `employees: {"emp_code": 1}` (unique): guarantees emp_code uniqueness and speeds up employee lookups.
   - `employees: {"joined_on": 1}` and `{"department": 1, "joined_on": 1}`: accelerate department headcount filtering and trend queries without collection scans (`COLLSCAN`).
   - `attendance_logs: {"emp_code": 1, "date": 1}` (unique): enforces the single record per employee per day constraint (R1) and prevents punch-in race conditions.
   - `attendance_logs: {"date": -1, "emp_code": 1}`: supports fast index-ordered pagination for attendance listings.
   - `attendance_logs: {"emp_code": 1, "punch_in": -1}`: optimizes punch-out matching for the latest punch-in.
   - `attendance_logs: {"date": 1, "late_minutes": 1}`: speeds up late leaderboard pipeline filtering.
   - *Considered and rejected*: An index on `status` alone was rejected due to low cardinality (only 5 distinct values); compound indexes starting with `date` provide far superior selectivity.

2. **Punch-in race.**
   Two identical punch-in requests validate the employee, compute the attendance date (R1), and attempt `db.attendance_logs.insert_one(doc)`. WiredTiger's document-level concurrency commits the first write. The second immediately violates the unique compound index `{"emp_code": 1, "date": 1}`, raising `DuplicateKeyError`. Our code catches this exception and returns `409 Conflict`, guaranteeing exactly one `201 Created` and preventing duplicate records.

3. **Ties.**
   The aggregation pipeline uses `$setWindowFields` with standard competition ranking (`$rank: {}`), producing ranks like 1, 2, 2, 4. Filtering `{"rank": {"$lte": limit}}` applies *after* ranking, so if two employees are tied at the cutoff of `limit`, both are included (e.g., returning 3 records when `limit=2`). Tied rows are deterministically ordered by `emp_code` ascending.

4. **Headcount.**
   The department summary pipeline originates on `employees` (filtering `joined_on <= month_end`), then `$lookup`s matching attendance logs for that month, followed by `$unwind: {preserveNullAndEmptyArrays: true}`. Headcount is calculated as `{"$size": "$_emp_codes"}` from `{"$addToSet": "$emp_code"}`. Employees who joined before month-end but have zero logs are retained through the outer join and counted in headcount.

5. **One thing you would change for 100x data.**
   At 100x scale (~10 million logs), calculating multi-stage aggregations (`$lookup`, `$densify`, `$setWindowFields`) on-the-fly would overwhelm database memory and CPU. I would implement incremental pre-aggregation using MongoDB Change Streams or a background rollup worker to maintain precomputed daily department summaries and monthly employee statistics, turning expensive analytic scans into instant O(1) reads.
