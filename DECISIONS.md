# DECISIONS.md

## 1. Indexes

**Created:**
- `employees.emp_code` – unique index. Enforces that emp_codes are globally unique and lets the concurrent-create guarantee be handled by the database (DuplicateKeyError) rather than by a check-then-insert race.
- `attendance_logs.(emp_code, date)` – unique index. Same guarantee for punch-in: two simultaneous inserts for the same employee on the same date will let exactly one succeed and the other will get a DuplicateKeyError, which maps to 409.
- `attendance_logs.(date DESC, emp_code ASC)` – supports the `GET /attendance` list endpoint, which sorts by date descending then emp_code ascending. Without this, MongoDB would do a full collection scan.
- `attendance_logs.(emp_code, date, status)` – a compound index used by the analytics aggregations that match on `emp_code` and `date` range. The `status` suffix allows index-only scans in some plans.
- `employees.(department, emp_code)` – supports `GET /employees?department=...` filtering with sorting by emp_code.

**Considered and rejected:** A partial index on `attendance_logs` filtered to `punch_out: null` for finding open records during punch-out. It would speed up the `find` call in `punch_out`, but the `(emp_code, date)` index already narrows the search to one document per employee per day, which is sufficient.

## 2. Punch-in race

Two identical `POST /attendance/punch-in` requests arrive at the same instant for the same employee. Both read the employee document successfully. Both compute the same `attendance_date`. Both call `db.attendance_logs.insert_one(doc)` with the same `(emp_code, date)` pair. MongoDB's unique index on `(emp_code, date)` allows only one insert to succeed; the other is rejected with a `DuplicateKeyError`. The winning request returns 201; the losing request catches `DuplicateKeyError` and raises `HTTPException(409)`. No check-then-insert pattern is used, so there is no window for both to pass the check.

## 3. Ties in the leaderboard

When two employees are tied on `total_late_minutes` at the cutoff of `limit`, both are returned even if that produces more than `limit` rows. This is implemented using MongoDB's `$rank` window function with `$setWindowFields`, which assigns the same rank to tied employees and skips ranks (standard competition ranking: 1, 2, 2, 4). The pipeline then filters `rank <= limit`, so all employees sharing the rank at the cutoff boundary are included. The secondary sort key is `emp_code` ascending for a stable and deterministic row order among ties.

## 4. Headcount with zero-log employees

The department summary first aggregates the `employees` collection to get headcount per department (filtering `joined_on <= last_day_of_month`). This step is independent of the `attendance_logs` collection, so employees who have no logs at all are counted. The attendance aggregation runs separately and is then merged with the employee headcount dict in Python. Departments with non-zero headcount but zero logs simply have all attendance metrics set to 0 / null.

## 5. One thing I would change at 100x data

At 100x scale (roughly 10 million attendance records) the analytics `$lookup` joins from `attendance_logs` to `employees` become expensive because they run document-by-document. I would denormalise `department` into every `attendance_logs` document at write time (punch-in and regularise), add a compound index on `(department, date)`, and eliminate the lookup stage from every analytics pipeline. This trades a small amount of write overhead and storage for dramatically faster aggregations, since all analytics filter by department and date and can then hit a single index.
