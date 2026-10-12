"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()  # does NOT override real env vars

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


# ---------------------------------------------------------------------------
# Helpers — rounding & dates
# ---------------------------------------------------------------------------
def round_half_up(value, decimals=2):
    """ROUND_HALF_UP to *decimals* places (R8)."""
    if value is None:
        return None
    d = Decimal(str(value))
    q = Decimal(10) ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def truncate_ms(epoch_ms: int) -> int:
    """Truncate epoch-ms to whole seconds (R1)."""
    return (epoch_ms // 1000) * 1000


def epoch_to_dt(epoch_ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def dt_to_epoch(dt) -> Optional[int]:
    """Datetime → epoch milliseconds.  None → None."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, dict) and "$date" in dt:
        d = dt["$date"]
        if isinstance(d, (int, float)):
            return int(d)
        if isinstance(d, str):
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
    if hasattr(dt, "tzinfo"):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    return None


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def parse_date(s: str) -> date_type:
    """Parse YYYY-MM-DD; raise 422 on bad format."""
    try:
        return date_type.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(422, detail=[{"msg": f"Invalid date: {s}"}])


def last_day_of_month(year: int, month: int) -> date_type:
    if month == 12:
        return date_type(year + 1, 1, 1) - timedelta(days=1)
    return date_type(year, month + 1, 1) - timedelta(days=1)


def count_working_days(start: date_type, end: date_type) -> int:
    """Mon-Fri days between *start* and *end* inclusive."""
    if start > end:
        return 0
    total = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            total += 1
        cur += timedelta(days=1)
    return total


def validate_epoch(v, field_name="punched_at"):
    """Validate an epoch-ms value; raise 422 on bad input."""
    if isinstance(v, bool):
        raise HTTPException(422, detail=[{"msg": f"{field_name} must be an integer"}])
    if isinstance(v, float):
        raise HTTPException(422, detail=[{"msg": f"{field_name} must be an integer, not float"}])
    if not isinstance(v, int):
        raise HTTPException(422, detail=[{"msg": f"{field_name} must be an integer"}])
    if v < MIN_EPOCH_MS:
        raise HTTPException(
            422,
            detail=[{"msg": f"{field_name} looks like seconds, expected milliseconds"}],
        )
    if v > MAX_EPOCH_MS:
        raise HTTPException(422, detail=[{"msg": f"{field_name} out of range"}])
    return v


# ---------------------------------------------------------------------------
# Business rules R1-R5
# ---------------------------------------------------------------------------
def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1 — attendance date is IST calendar date of punch-in, with overnight-shift logic."""
    ist = to_ist(punch_in_utc)
    is_overnight = shift_end <= shift_start  # string compare on HH:MM
    if is_overnight:
        se_h, se_m = map(int, shift_end.split(":"))
        punch_secs = ist.hour * 3600 + ist.minute * 60 + ist.second
        end_secs = se_h * 3600 + se_m * 60
        if punch_secs < end_secs:
            return (ist - timedelta(days=1)).strftime("%Y-%m-%d")
    return ist.strftime("%Y-%m-%d")


def _shift_start_dt(att_date: str, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    d = date_type.fromisoformat(att_date)
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def _shift_end_dt(att_date: str, shift_start: str, shift_end: str) -> datetime:
    h, m = map(int, shift_end.split(":"))
    d = date_type.fromisoformat(att_date)
    dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    if shift_end <= shift_start:  # overnight
        dt += timedelta(days=1)
    return dt


def compute_late(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """R2 — late minutes.  Grace = 10 min.  floor(seconds since shift_start / 60)."""
    start_dt = _shift_start_dt(att_date, shift_start)
    diff = (to_ist(punch_in_utc) - start_dt).total_seconds()
    if diff > 600:  # strictly more than 10 minutes
        return int(diff // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4 — work hours, 2-decimal half-up."""
    secs = (punch_out - punch_in).total_seconds()
    return round_half_up(secs / 3600, 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """R3 — overtime minutes; counted only if >= 30."""
    end_dt = _shift_end_dt(att_date, shift_start, shift_end)
    diff = (to_ist(punch_out) - end_dt).total_seconds()
    mins = int(diff // 60)
    return mins if mins >= 30 else 0


def is_half_day(work_hours: float) -> bool:
    """R5 — half day when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _format_history(history):
    out = []
    for entry in (history or []):
        e = {
            "at": dt_to_epoch(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, ch in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                e["changes"][field] = {
                    "from": dt_to_epoch(ch.get("from")),
                    "to": dt_to_epoch(ch.get("to")),
                }
            else:
                e["changes"][field] = ch
        out.append(e)
    return out


def format_record(doc) -> dict:
    """attendance_logs document → API response."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch(doc.get("punch_in")),
        "punch_out": dt_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _format_history(doc.get("history", [])),
    }


def format_employee(doc) -> dict:
    """employees document → API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def _emp_code(cls, v):
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not (1 <= len(v) <= 100):
            raise ValueError("name length must be 1-100")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if len(v) > 120 or not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def _dept(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("department length must be 1-50")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _shift(cls, v):
        if not SHIFT_RE.match(v):
            raise ValueError("shift must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v):
        parse_date(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def _ts(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or isinstance(v, float) or not isinstance(v, int):
            raise ValueError("punched_at must be an integer")
        if v < MIN_EPOCH_MS:
            raise ValueError("punched_at looks like seconds")
        if v > MAX_EPOCH_MS:
            raise ValueError("punched_at out of range")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def _ts(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or isinstance(v, float) or not isinstance(v, int):
            raise ValueError("punched_at must be an integer")
        if v < MIN_EPOCH_MS:
            raise ValueError("punched_at looks like seconds")
        if v > MAX_EPOCH_MS:
            raise ValueError("punched_at out of range")
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def _ts(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or isinstance(v, float) or not isinstance(v, int):
            raise ValueError("timestamp must be an integer")
        if v < MIN_EPOCH_MS:
            raise ValueError("timestamp looks like seconds")
        if v > MAX_EPOCH_MS:
            raise ValueError("timestamp out of range")
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v):
        if not (5 <= len(v) <= 200):
            raise ValueError("reason length must be 5-200")
        return v

    @field_validator("regularized_by")
    @classmethod
    def _by(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by length must be 1-50")
        return v


# ---------------------------------------------------------------------------
# Query / pipeline builders (also used by /admin/explain)
# ---------------------------------------------------------------------------
def _build_attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
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
    return q


def _month_bounds(month_str: str):
    """Return (first_day, last_day) strings for a YYYY-MM month."""
    y, m = map(int, month_str.split("-"))
    first = date_type(y, m, 1)
    last = last_day_of_month(y, m)
    return first.isoformat(), last.isoformat()


def _build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$addFields": {
                "_date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "_is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "_half": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$addFields": {
                "_weekday": {"$in": [{"$dayOfWeek": "$_date_obj"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", "$_weekday"]},
                            {"$cond": ["$_half", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def _build_dept_summary_pipeline(month_start, month_end, department=None):
    emp_match = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        # lookup attendance for each employee
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}}},
                    {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # per-log flags
        {
            "$addFields": {
                "_has_log": {"$ne": [{"$ifNull": ["$logs", None]}, None]},
            }
        },
        {
            "$addFields": {
                "_is_present": {
                    "$and": [
                        "$_has_log",
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE_STATUSES)]},
                    ]
                },
                "_weekday": {
                    "$cond": [
                        "$_has_log",
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$logs.date",
                                            "format": "%Y-%m-%d",
                                        }
                                    }
                                },
                                [2, 3, 4, 5, 6],
                            ]
                        },
                        False,
                    ]
                },
                "_half": {
                    "$cond": [
                        "$_has_log",
                        {"$ifNull": ["$logs.half_day", False]},
                        False,
                    ]
                },
            }
        },
        # group by department
        {
            "$group": {
                "_id": "$department",
                "_emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", "$_weekday"]},
                            {"$cond": ["$_half", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "_wh_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_is_present",
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "_wh_cnt": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_is_present",
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": ["$_has_log", {"$ifNull": ["$logs.late_minutes", 0]}, 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$_emp_codes"},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$_wh_cnt", 0]},
                        {"$divide": ["$_wh_sum", "$_wh_cnt"]},
                        None,
                    ]
                },
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def _build_leaderboard_pipeline(month_start, month_end, limit=10, department=None):
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
                "as": "_emp",
            }
        },
        {"$unwind": "$_emp"},  # drops rows whose employee doesn't exist
    ]
    if department:
        pipeline.append({"$match": {"_emp.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$_emp.name",
                "department": "$_emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def _build_trend_pipeline(department, from_str, to_str, emp_codes):
    from_dt = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=UTC)
    to_dt = datetime.strptime(to_str, "%Y-%m-%d").replace(tzinfo=UTC)
    to_upper = to_dt + timedelta(days=1)  # exclusive upper bound for $densify

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$addFields": {
                "_date_obj": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                },
                "_is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "_half": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": "$_date_obj",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", "$_half"]},
                            0.5,
                            {"$cond": ["$_is_present", 1.0, 0]},
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_upper]},
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]},
            }
        },
        # headcount lookup per day
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_emps",
            }
        },
        {"$addFields": {"headcount": {"$size": "$_emps"}}},
        {"$project": {"_emps": 0}},
        # attendance_rate (only on working days with headcount > 0)
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # 7-day moving average over non-null rates
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]
    return pipeline


# ---------------------------------------------------------------------------
# Lifespan — create indexes at startup (idempotent)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return format_employee(doc)


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
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance — punch in / out / list / regularize
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = truncate_ms(body.punched_at)
        ts = epoch_to_dt(ts_ms)
    else:
        now = datetime.now(UTC)
        ts_ms = truncate_ms(dt_to_epoch(now))
        ts = epoch_to_dt(ts_ms)

    att_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    doc.pop("_id", None)
    return format_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        po_ms = truncate_ms(body.punched_at)
        po_dt = epoch_to_dt(po_ms)
    else:
        now = datetime.now(UTC)
        po_ms = truncate_ms(dt_to_epoch(now))
        po_dt = epoch_to_dt(po_ms)

    # Find the most recent record whose punch_in <= po_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": po_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # If there is a record whose punch_in is after po_dt, punch_out is before punch_in (422)
        future = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": po_dt}},
            sort=[("punch_in", ASCENDING)],
        )
        if future and (future["punch_in"] - po_dt).total_seconds() <= 86400:
            raise HTTPException(
                422, detail=[{"msg": "punched_at must be after punch_in"}]
            )
        raise HTTPException(404, detail="No punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="Already punched out")

    pi_dt = record["punch_in"]
    if pi_dt.tzinfo is None:
        pi_dt = pi_dt.replace(tzinfo=UTC)

    # Validation: punch_out must be after punch_in and within 24 hours (422)
    if po_dt <= pi_dt:
        raise HTTPException(
            422, detail=[{"msg": "punched_at must be after punch_in"}]
        )
    if (po_dt - pi_dt).total_seconds() > 86400:
        raise HTTPException(
            422, detail=[{"msg": "punched_at must be within 24 hours of punch_in"}]
        )

    wh = compute_work_hours(pi_dt, po_dt)
    ot = compute_overtime(
        po_dt, emp["shift_start"], emp["shift_end"], record["date"]
    )
    hd = is_half_day(wh)

    # Atomic update — only one concurrent punch-out succeeds
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": po_dt,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, detail="Already punched out")

    return format_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422, detail=[{"msg": "date_from must be before or equal to date_to"}]
        )
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=[{"msg": f"Invalid status: {status}"}])

    q = _build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code: str, date_str: str, body: RegularizeRequest):
    parse_date(date_str)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, detail="No attendance record for this date")

    # --- snapshot old values ---
    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    # --- determine new direct values ---
    new_status = body.status if body.status is not None else old["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                detail=[
                    {"msg": "Cannot supply punch times with ABSENT/LEAVE status"}
                ],
            )
        new_pi = None
        new_po = None
    else:
        # presence status needs punch_in
        if body.punch_in is not None:
            new_pi = epoch_to_dt(truncate_ms(body.punch_in))
        elif old["punch_in"] is not None:
            new_pi = old["punch_in"]
            if new_pi.tzinfo is None:
                new_pi = new_pi.replace(tzinfo=UTC)
        else:
            raise HTTPException(
                422,
                detail=[{"msg": "Presence status requires a punch_in"}],
            )

        # validate punch_in stays on this attendance date
        computed = compute_attendance_date(new_pi, emp["shift_start"], emp["shift_end"])
        if computed != date_str:
            raise HTTPException(
                422,
                detail=[{"msg": "punch_in does not fall on this attendance date"}],
            )

        if body.punch_out is not None:
            new_po = epoch_to_dt(truncate_ms(body.punch_out))
        elif old["punch_out"] is not None:
            new_po = old["punch_out"]
            if new_po.tzinfo is None:
                new_po = new_po.replace(tzinfo=UTC)
        else:
            new_po = None

        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(
                    422,
                    detail=[{"msg": "punch_out must be after punch_in"}],
                )
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(
                    422,
                    detail=[{"msg": "punch_out must be within 24h of punch_in"}],
                )

    # --- recompute derived fields ---
    if new_status in PRESENCE_STATUSES:
        new_late = compute_late(new_pi, emp["shift_start"], date_str)
        if new_po is not None:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(new_po, emp["shift_start"], emp["shift_end"], date_str)
            new_hd = is_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False
    else:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False

    new_vals = {
        "status": new_status,
        "punch_in": new_pi,
        "punch_out": new_po,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }

    # --- compute changes dict ---
    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        ov = old[field]
        nv = new_vals[field]
        if field in ("punch_in", "punch_out"):
            o_ms = dt_to_epoch(ov)
            n_ms = dt_to_epoch(nv)
            if o_ms != n_ms:
                changes[field] = {"from": ov, "to": nv}  # store as datetimes
        else:
            if ov != nv:
                changes[field] = {"from": ov, "to": nv}

    if not changes:
        raise HTTPException(422, detail=[{"msg": "No fields changed"}])

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # --- atomic update with OCC on history length ---
    cur_hist = record.get("history") or []
    hist_len = len(cur_hist)

    if hist_len == 0:
        occ_filter = {
            "_id": record["_id"],
            "$or": [
                {"history": {"$size": 0}},
                {"history": {"$exists": False}},
            ],
        }
    else:
        occ_filter = {"_id": record["_id"], "history": {"$size": hist_len}}

    result = db.attendance_logs.update_one(
        occ_filter,
        {"$set": new_vals, "$push": {"history": history_entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="Concurrent modification, please retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_record(updated)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(...),
):
    if not MONTH_RE.match(month):
        raise HTTPException(422, detail=[{"msg": "Invalid month format YYYY-MM"}])

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    month_start, month_end = _month_bounds(month)
    y, m = map(int, month.split("-"))
    joined = date_type.fromisoformat(emp["joined_on"])
    first = date_type.fromisoformat(month_start)
    last = date_type.fromisoformat(month_end)

    wd_start = max(first, joined)
    working_days = count_working_days(wd_start, last)

    pipeline = _build_employee_monthly_pipeline(emp_code, month_start, month_end)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        r = results[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = None,
):
    if not MONTH_RE.match(month):
        raise HTTPException(422, detail=[{"msg": "Invalid month format YYYY-MM"}])

    month_start, month_end = _month_bounds(month)
    pipeline = _build_dept_summary_pipeline(month_start, month_end, department)
    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not MONTH_RE.match(month):
        raise HTTPException(422, detail=[{"msg": "Invalid month format YYYY-MM"}])

    month_start, month_end = _month_bounds(month)
    pipeline = _build_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    from_d = parse_date(date_from)
    to_d = parse_date(date_to)

    if to_d < from_d:
        raise HTTPException(422, detail=[{"msg": "to must be >= from"}])
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(422, detail=[{"msg": "Range exceeds 92 days"}])

    # verify department exists
    dept_emps = list(
        db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
    )
    if not dept_emps:
        raise HTTPException(404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in dept_emps]
    pipeline = _build_trend_pipeline(department, date_from, date_to, emp_codes)
    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin — explain
# ---------------------------------------------------------------------------
def _serializable(obj):
    """Recursively make bson types JSON-serializable."""
    return json.loads(json_util.dumps(obj))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, alias="date_from"),
    date_to: Optional[str] = Query(default=None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid:
        raise HTTPException(422, detail=[{"msg": f"endpoint must be one of {sorted(valid)}"}])

    if endpoint == "attendance_list":
        q = _build_attendance_query(emp_code, date_from, date_to, status)
        skip_val = (page - 1) * page_size
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip_val,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _serializable(explain)}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail=[{"msg": "emp_code and month are required"}])
        ms, me = _month_bounds(month)
        pipeline = _build_employee_monthly_pipeline(emp_code, ms, me)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _serializable(explain)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month is required"}])
        ms, me = _month_bounds(month)
        pipeline = _build_dept_summary_pipeline(ms, me, department)
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": _serializable(explain)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month is required"}])
        ms, me = _month_bounds(month)
        pipeline = _build_leaderboard_pipeline(ms, me, limit, department)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _serializable(explain)}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422,
                detail=[{"msg": "department, from, and to are required"}],
            )
        dept_emps = list(
            db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
        )
        emp_codes = [e["emp_code"] for e in dept_emps]
        pipeline = _build_trend_pipeline(department, from_date, to_date, emp_codes)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _serializable(explain)}
