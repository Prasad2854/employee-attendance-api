# Employee Attendance & Analytics API

A FastAPI + MongoDB API implementing employee attendance management with punch-in/out, audit-trailed corrections, and MongoDB aggregation-backed analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (local Docker or MongoDB Atlas M0 free tier both work)

## Local Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Set MongoDB connection:
# Create a .env file with:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db
# (or use a real MongoDB URI from Atlas)

python sample_seed.py          # load sample documents
uvicorn app.main:app --port 8000 --reload
```

Interactive docs: http://localhost:8000/docs

## What is Implemented

All endpoints in `openapi.yaml` are implemented in `app/main.py`:

| Endpoint | Description |
|---|---|
| `GET /health` | Readiness probe – pings MongoDB, 503 if unreachable |
| `POST /employees` | Create employee (unique emp_code enforced by DB index) |
| `GET /employees` | List employees with pagination and department filter |
| `POST /attendance/punch-in` | Record punch-in (R1 date, R2 late minutes, concurrency-safe) |
| `POST /attendance/punch-out` | Record punch-out (R3 overtime, R4 work hours, R5 half day, concurrency-safe) |
| `GET /attendance` | List attendance records (server-side sort + pagination) |
| `PATCH /attendance/{emp_code}/{date}` | Regularize a record (full audit trail, R1-R5 recomputed, optimistic concurrency) |
| `GET /analytics/employees/{emp_code}/monthly` | Monthly summary per employee (R7 working days, R8 rounding) |
| `GET /analytics/departments/summary` | Department summary (R9 headcount includes zero-log employees) |
| `GET /analytics/leaderboard/late` | Late-comers leaderboard (standard competition ranking via `$rank`) |
| `GET /analytics/departments/{department}/trend` | Daily trend with `$densify` gap-filling and 7-day moving average |
| `GET /admin/explain/{endpoint}` | MongoDB `explain(executionStats)` for any analytics query |

## Business Rules Implemented

R1 (IST dates, overnight shifts), R2 (late minutes with 10-min grace, floor), R3 (overtime ≥ 30 min, floor), R4 (work hours half-up rounding), R5 (half day < 4.50 hours), R6 (presence statuses), R7 (Mon–Fri working days from joined_on), R8 (2-decimal half-up rounding), R9 (headcount includes joined employees with no logs), R10 (pagination starting at page 1, max page_size 100).

## Indexes Created at Startup

- `employees.emp_code` (unique) – enforces duplicate create → 409
- `attendance_logs.(emp_code, date)` (unique) – enforces duplicate punch-in → 409
- `attendance_logs.(date DESC, emp_code ASC)` – fast attendance list sort
- `attendance_logs.(emp_code, date, status)` – analytics aggregation prefix scan
- `employees.(department, emp_code)` – fast department filter + sort

## Notes

- All instants are stored as BSON datetimes (UTC) in MongoDB and serialised as epoch milliseconds in the API.
- Concurrent punch-in is handled by the database unique index + `DuplicateKeyError` catch; no check-then-insert.
- Concurrent punch-out uses `find_one_and_update` with `punch_out: null` guard.
- Concurrent regularize uses an optimistic history-length check (`history.N: {$exists: false}`).
- The `$densify` stage (MongoDB 6.0+) fills calendar gaps in the trend endpoint.
- The `$rank` window function (MongoDB 5.0+) produces standard competition ranking for the leaderboard.

## What Is Missing

Nothing intentionally omitted. All 11 endpoints are implemented.
