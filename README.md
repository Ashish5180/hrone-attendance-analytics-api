# Employee Attendance & Analytics API

A high-performance, race-safe REST API built with **Python**, **FastAPI**, and **MongoDB** for employee attendance tracking, regularization audit trails, and real-time aggregations.

Built for the HROne Software Engineer Trainee technical evaluation.

---

## Features

- **Full OpenAPI 3.0 Contract Compliance**: Implements all 12 endpoints specified in `openapi.yaml`.
- **Strict Business Rules (R1 – R10)**:
  - **R1 (Attendance Date & Overnight Shifts)**: Automatic resolution of IST calendar date and overnight shifts (`shift_end <= shift_start`).
  - **R2 (Late Minutes)**: Strict 10-minute grace period with floor-minute calculations from `shift_start`.
  - **R3 (Overtime)**: 30-minute threshold check with support for overnight shifts.
  - **R4 (Work Hours)**: Precision `ROUND_HALF_UP` 2-decimal work duration.
  - **R5 (Half Day)**: Automatic half-day marking (`work_hours < 4.50`, counts as 0.5 present day).
  - **R6 (Presence Statuses)**: Proper accounting of `PRESENT`, `WFH`, `ON_DUTY` vs `ABSENT`, `LEAVE`.
  - **R7 (Working Days)**: Mon–Fri calendar with mid-month joiner adjustment.
  - **R8 (Rounding)**: Half-up rounding (`ROUND_HALF_UP`) for numbers (2 decimals) and rates (4 decimals).
  - **R9 (Headcount)**: All employees joined on or before period cutoff counted (including zero-log employees).
  - **R10 (Pagination)**: 1-indexed pagination (`page >= 1`, `1 <= page_size <= 100`) with accurate filtered totals.
- **Concurrency & Race Safety**:
  - Punch-in: Guarded by database-level compound unique index `{"emp_code": 1, "date": 1}`.
  - Punch-out: Atomic `find_one_and_update` on unclosed punch-in record.
  - Regularization: Optimistic concurrency control (OCC) on audit history length to avoid lost updates.
- **In-Database Aggregations (No Python loops)**:
  - Monthly employee summary (`$group`, `$cond`).
  - Department summary (`$lookup`, `$unwind`, cross-collection aggregation).
  - Late leaderboard (`$setWindowFields` standard competition `$rank`, ties preservation).
  - Department trend (`$densify` daily series, `$lookup` per-day headcount, 7-day moving average).
- **Index Optimization & Explain Endpoint**:
  - `GET /admin/explain/{endpoint}` reports execution stats.
  - Fully verified to utilize index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`) at scale.

---

## Deliverables

- `app/main.py`: Complete application in a single file, starting via `uvicorn app.main:app --port 8000`.
- `REVIEW.md`: Comprehensive review of 12 starter defects with line numbers, failure cases, and fixes.
- `DECISIONS.md`: Concise rationale covering indexes, race conditions, leaderboard ties, headcount, and 100x scale strategy.
- `requirements.txt`: Minimal production dependencies.

---

## Local Setup

### 1. Requirements
- Python 3.10+ (or Python 3.14)
- MongoDB 6.0+ (Grader uses 7.0; local MongoDB community service or Atlas M0)

### 2. Installation
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file or export environment variables:
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 4. Seed Sample Data
```bash
python sample_seed.py
```

### 5. Start the Server
```bash
uvicorn app.main:app --port 8000
```
API Documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## Verification & Testing

To test all endpoints against the running instance:
```bash
.venv/bin/python -m pytest   # or run the automated test suite
```
All indexes are automatically and idempotently provisioned during FastAPI startup via the lifespan event handler.
