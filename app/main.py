"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from a local .env for convenience)
"""
import calendar
import os
from datetime import datetime, date, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Indexes - created idempotently at startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    # employees: unique on emp_code (enforces concurrent-create guarantee)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")

    # attendance_logs: unique on (emp_code, date) (enforces concurrent punch-in guarantee)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_date_unique",
    )
    # attendance_logs: (date desc, emp_code asc) for the list endpoint sort + range filter
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc",
    )
    # attendance_logs: emp_code + date + status prefix scan used by analytics
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="emp_date_status",
    )
    # employees: department + emp_code for listing by dept
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="dept_emp_asc",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_half_up(value: float, decimals: int) -> float:
    """Decimal half-up rounding (not Python's banker's rounding)."""
    d = Decimal(str(value)).quantize(
        Decimal("0." + "0" * decimals), rounding=ROUND_HALF_UP
    )
    return float(d)


def _to_ist(dt: datetime) -> datetime:
    """Convert a naive UTC datetime (as returned by PyMongo) to IST-aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _from_epoch_ms(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime, truncated to seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def _to_epoch_ms(dt) -> Optional[int]:
    """Convert datetime to epoch milliseconds, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Determine the attendance date (IST calendar date).
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is earlier than shift_end belongs to the shift that started the previous day.
    """
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    shift_start_minutes = h_s * 60 + m_s
    shift_end_minutes = h_e * 60 + m_e
    punch_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute

    # Overnight shift: shift_end <= shift_start (e.g. 22:00-06:00)
    if shift_end_minutes <= shift_start_minutes:
        # punch before shift_end belongs to previous day's shift
        if punch_minutes < shift_end_minutes:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def _compute_late_minutes(punch_in_ist: datetime, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes. Late only if strictly more than 10 minutes
    after shift_start. late_minutes = floor minutes since shift_start (not since end of grace).
    Example (shift 09:30): 09:40:00->0; 09:40:01->10; 10:15:59->45.
    """
    h, m = map(int, shift_start.split(":"))
    start = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_seconds = int((punch_in_ist - start).total_seconds())
    if elapsed_seconds <= 600:  # 10 minutes grace (inclusive of exactly 10:00)
        return 0
    return elapsed_seconds // 60  # floor minutes since shift_start


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return _round_half_up(seconds / 3600, 2)


def _compute_overtime_minutes(punch_out_ist: datetime, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = floor minutes between shift_end and punch_out, only if >= 30; else 0.
    For overnight shifts, shift_end falls on the next calendar day from attendance_date.
    """
    h, m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date)
    end_ist = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, tzinfo=IST)

    # If punch_out is on the next day or later, shift_end is also on the next day (overnight)
    if punch_out_ist.date() > att_date:
        end_ist = end_ist + timedelta(days=1)

    diff_seconds = int((punch_out_ist - end_ist).total_seconds())
    if diff_seconds < 30 * 60:
        return 0
    return diff_seconds // 60


def _compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _format_record(doc: dict) -> dict:
    """Convert a MongoDB attendance document to the API response format."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_epoch_ms(doc.get("punch_in")),
        "punch_out": _to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _format_history(doc.get("history", [])),
    }


def _format_history(history: list) -> list:
    """Convert history entries: BSON dates in changes -> epoch ms."""
    result = []
    for entry in history:
        changes = {}
        for field, change in entry.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                from_val = _to_epoch_ms(from_val) if from_val is not None else None
                to_val = _to_epoch_ms(to_val) if to_val is not None else None
            changes[field] = {"from": from_val, "to": to_val}
        result.append({
            "at": _to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return result


def _format_employee(doc: dict) -> dict:
    """Convert a MongoDB employee document to the API response format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _to_epoch_ms(doc["created_at"]),
    }


def _month_range(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM month."""
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    first_day_str = f"{year:04d}-{mon:02d}-01"
    last_day_str = f"{year:04d}-{mon:02d}-{last_day:02d}"
    return first_day_str, last_day_str


def _working_days_in_range(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days in [start_str, end_str]."""
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # 0=Mon, 4=Fri
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EPOCH_MIN = 100_000_000_000    # ~1973 - guard against seconds-looking values
EPOCH_MAX = 4_102_444_800_000  # 2100
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None and (v < EPOCH_MIN or v > EPOCH_MAX):
            raise ValueError(f"punched_at must be epoch milliseconds ({EPOCH_MIN}..{EPOCH_MAX})")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None and (v < EPOCH_MIN or v > EPOCH_MAX):
            raise ValueError(f"punched_at must be epoch milliseconds ({EPOCH_MIN}..{EPOCH_MAX})")
        return v


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None and (v < EPOCH_MIN or v > EPOCH_MAX):
            raise ValueError("punch_in/punch_out must be epoch milliseconds")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY, ABSENT, LEAVE")
        return v

    @model_validator(mode="after")
    def absent_leave_no_punches(self):
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "punch_in and punch_out must not be supplied when status is ABSENT or LEAVE"
                )
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB not reachable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    )
    return {
        "items": [_format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Determine the punch-in datetime (UTC, truncated to seconds) per R1
    if body.punched_at is not None:
        ts_utc = _from_epoch_ms(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    ts_ist = _to_ist(ts_utc)
    attendance_date = _attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    late_minutes = _compute_late_minutes(ts_ist, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")

    return _format_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = _from_epoch_ms(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent open record whose punch_in <= punched_at (works across midnight)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_utc},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "No open punch-in found for this employee at or before the given time")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 validations
    if ts_utc <= punch_in_dt:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if (ts_utc - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    ts_ist = _to_ist(ts_utc)
    work_hours = _compute_work_hours(punch_in_dt, ts_utc)
    overtime_minutes = _compute_overtime_minutes(ts_ist, emp["shift_end"], record["date"])
    half_day = _compute_half_day(work_hours)

    # Atomic update: condition on punch_out being null prevents double punch-out (concurrency)
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,  # guard against concurrent punch-out
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, "Record was already punched out (concurrent request won)")

    return _format_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_format_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "No attendance record found for this employee and date")

    # Build new state
    new_status = body.status if body.status is not None else record["status"]
    new_punch_in_dt: Optional[datetime] = None
    new_punch_out_dt: Optional[datetime] = None

    if new_status in ("ABSENT", "LEAVE"):
        # Clear both punch times
        new_punch_in_dt = None
        new_punch_out_dt = None
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in_dt = _from_epoch_ms(body.punch_in)
        else:
            new_punch_in_dt = record.get("punch_in")
            if new_punch_in_dt is not None and new_punch_in_dt.tzinfo is None:
                new_punch_in_dt = new_punch_in_dt.replace(tzinfo=timezone.utc)

        if body.punch_out is not None:
            new_punch_out_dt = _from_epoch_ms(body.punch_out)
        else:
            new_punch_out_dt = record.get("punch_out")
            if new_punch_out_dt is not None and new_punch_out_dt.tzinfo is None:
                new_punch_out_dt = new_punch_out_dt.replace(tzinfo=timezone.utc)

        if new_punch_in_dt is None:
            raise HTTPException(422, "A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        punch_in_ist = _to_ist(new_punch_in_dt)
        computed_date = _attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date (R1)")

        if new_punch_out_dt is not None:
            if new_punch_out_dt <= new_punch_in_dt:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out_dt - new_punch_in_dt).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_punch_in_dt is not None:
        new_late_minutes = _compute_late_minutes(_to_ist(new_punch_in_dt), emp["shift_start"])
    else:
        new_late_minutes = 0

    if new_punch_in_dt is not None and new_punch_out_dt is not None:
        new_work_hours = _compute_work_hours(new_punch_in_dt, new_punch_out_dt)
        new_overtime_minutes = _compute_overtime_minutes(
            _to_ist(new_punch_out_dt), emp["shift_end"], date
        )
        new_half_day = _compute_half_day(new_work_hours)
    else:
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False

    # Build changes dict (only fields that actually changed)
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes = {}

    def _maybe_add(field, old_val, new_val):
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    _maybe_add("status", record["status"], new_status)
    _maybe_add("punch_in", old_punch_in, new_punch_in_dt)
    _maybe_add("punch_out", old_punch_out, new_punch_out_dt)
    _maybe_add("work_hours", record.get("work_hours"), new_work_hours)
    _maybe_add("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    _maybe_add("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    _maybe_add("half_day", record.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(422, "Request makes no changes to the record")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {f: {"from": v["from"], "to": v["to"]} for f, v in changes.items()},
    }

    # Optimistic concurrency: history must still be the same length we read
    existing_history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{existing_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    return _format_record(result)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    first_day, last_day = _month_range(month)

    # R7: working days start from max(first_day, joined_on)
    effective_start = max(first_day, emp["joined_on"])
    if effective_start > last_day:
        return {
            "emp_code": emp_code, "month": month,
            "working_days": 0, "present_days": 0.0, "leave_days": 0,
            "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
            "attendance_pct": None,
        }

    working_days = _working_days_in_range(effective_start, last_day)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$let": {
                        "vars": {
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                        },
                        "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
                    }
                },
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "present_contribution": {
                    "$cond": {
                        "if": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        "then": {
                            "$cond": {
                                "if": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                "then": 0.5,
                                "else": 1.0,
                            }
                        },
                        "else": 0.0,
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": {
                            "if": {
                                "$and": [
                                    "$is_working_day",
                                    "$is_present",
                                    {"$gte": ["$date", effective_start]},
                                ]
                            },
                            "then": "$present_contribution",
                            "else": 0.0,
                        }
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    r = results[0] if results else {}

    present_days_raw = r.get("present_days", 0.0)
    present_days = _round_half_up(present_days_raw, 2)
    attendance_pct = (
        _round_half_up(present_days_raw / working_days * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = _month_range(month)

    # R9: headcount = employees with joined_on <= last_day
    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    emp_pipeline = [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
    ]
    dept_employees = {d["_id"]: d for d in db.employees.aggregate(emp_pipeline)}

    if not dept_employees:
        return {"month": month, "items": []}

    all_emp_codes = []
    for dept_data in dept_employees.values():
        all_emp_codes.extend(dept_data["emp_codes"])

    attendance_pipeline = [
        {
            "$match": {
                "emp_code": {"$in": all_emp_codes},
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
        {
            "$addFields": {
                "department": "$emp_info.department",
                "is_working_day": {
                    "$let": {
                        "vars": {
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                        },
                        "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
                    }
                },
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "present_contribution": {
                    "$cond": {
                        "if": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        "then": {
                            "$cond": {
                                "if": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                "then": 0.5,
                                "else": 1.0,
                            }
                        },
                        "else": 0.0,
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "present_days": {
                    "$sum": {
                        "$cond": {
                            "if": {"$and": ["$is_working_day", "$is_present"]},
                            "then": "$present_contribution",
                            "else": 0.0,
                        }
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": {
                            "if": {
                                "$and": [
                                    "$is_present",
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "then": "$work_hours",
                            "else": 0.0,
                        }
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": {
                            "if": {
                                "$and": [
                                    "$is_present",
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "then": 1,
                            "else": 0,
                        }
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]

    att_by_dept = {r["_id"]: r for r in db.attendance_logs.aggregate(attendance_pipeline)}

    items = []
    for dept_name in sorted(dept_employees.keys()):
        hc = dept_employees[dept_name]["headcount"]
        att = att_by_dept.get(dept_name, {})
        wh_sum = att.get("work_hours_sum", 0.0)
        wh_cnt = att.get("work_hours_count", 0)
        avg_wh = _round_half_up(wh_sum / wh_cnt, 2) if wh_cnt > 0 else None
        items.append({
            "department": dept_name,
            "headcount": hc,
            "present_days": _round_half_up(att.get("present_days", 0.0), 2),
            "avg_work_hours": avg_wh,
            "late_count": att.get("late_count", 0),
            "total_late_minutes": att.get("total_late_minutes", 0),
            "leave_count": att.get("leave_count", 0),
            "on_duty_count": att.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = _month_range(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$match": {"emp_info": {"$ne": []}}},
        {"$unwind": "$emp_info"},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline += [
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp_info.name",
                "department": "$emp_info.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        # Standard competition ranking via $rank (MongoDB 5.0+)
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1, "emp_code": 1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        # Return every employee whose rank <= limit (handles ties at cutoff)
        {"$match": {"rank": {"$lte": limit}}},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": [
            {
                "rank": r["rank"],
                "emp_code": r["emp_code"],
                "name": r["name"],
                "department": r["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            }
            for r in results
        ],
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    try:
        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")

    if to_d < from_d:
        raise HTTPException(422, "to must not be before from")
    if (to_d - from_d).days > 91:
        raise HTTPException(422, "Date range must not exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    from_str = from_date
    to_str = to_date

    # Aggregate attendance records for the department and date range
    pipeline = [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$match": {"emp_info": {"$ne": []}}},
        {"$unwind": "$emp_info"},
        {"$match": {"emp_info.department": department}},
        {
            "$addFields": {
                "present_contribution": {
                    "$cond": {
                        "if": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        "then": {
                            "$cond": {
                                "if": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                "then": 0.5,
                                "else": 1.0,
                            }
                        },
                        "else": 0.0,
                    }
                },
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_contribution"},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            }
        },
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        # $densify fills in every missing calendar day in the range
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(from_d, datetime.min.time(), tzinfo=timezone.utc),
                        datetime.combine(to_d + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
                    ],
                },
            }
        },
        {
            "$addFields": {
                "date_str": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj", "timezone": "UTC"}
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date_str": 1}},
    ]

    raw_rows = list(db.attendance_logs.aggregate(pipeline))
    rows_by_date = {r.get("date_str") or r.get("_id", ""): r for r in raw_rows}

    # Fetch employees in department who joined on or before to_date
    dept_emps = list(
        db.employees.find(
            {"department": department, "joined_on": {"$lte": to_str}},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        )
    )

    items = []
    non_null_rates: list = []  # running list of non-null rates for moving_avg_7d

    current = from_d
    while current <= to_d:
        ds = current.isoformat()
        is_working_day = current.weekday() < 5

        # R9: headcount = employees whose joined_on <= current day
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= ds)

        row = rows_by_date.get(ds, {})
        present_count_raw = row.get("present_count", 0.0)
        late_count = row.get("late_count", 0)
        present_count = _round_half_up(present_count_raw, 2)

        # attendance_rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            attendance_rate = _round_half_up(present_count_raw / headcount, 4)
        else:
            attendance_rate = None

        if attendance_rate is not None:
            non_null_rates.append(attendance_rate)

        # moving_avg_7d: mean of non-null rates within window of up to 7 rows (current + 6 preceding)
        window = non_null_rates[-7:]
        moving_avg_7d = _round_half_up(sum(window) / len(window), 4) if window else None

        items.append({
            "date": ds,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: Explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID)}")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        exp = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        first_day, last_day = _month_range(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        first_day, last_day = _month_range(month)
        emp_match: dict = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}]
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        first_day, last_day = _month_range(month)
        pipeline = [{"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(422, "from and to are required for department_trend")
        pipeline = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
