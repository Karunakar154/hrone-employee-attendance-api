"""HROne Employee Attendance & Analytics API.

Run from the repository root with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (local .env supported).
"""
import os
import re
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # existing environment variables take precedence over .env
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000, tz_aware=False)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- startup/indexes ----------------------------
def create_indexes() -> None:
    """Idempotent indexes for uniqueness, filters, joins, and stable sorting."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_emp_date")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="attendance_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_emp_punchin")


@app.on_event("startup")
def startup() -> None:
    # Keep the process alive so /health can report 503 while MongoDB is down.
    try:
        create_indexes()
    except PyMongoError:
        pass


# --------------------------------- helpers ----------------------------------
def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def epoch_to_dt(value: int) -> datetime:
    """Convert validated epoch milliseconds to naive UTC BSON datetime, truncated to seconds."""
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0, tzinfo=None)


def dt_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def as_utc_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def as_ist(value: datetime) -> datetime:
    return as_utc_aware(value).astimezone(IST)


def parse_date(value: str) -> Date:
    try:
        parsed = Date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return parsed


def parse_month(month: str) -> tuple[Date, Date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    start = Date(year, mon, 1)
    end = Date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    return start, end


def half_up(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def validate_epoch(value: object) -> int:
    # bool is an int subclass, so explicitly reject it. StrictInt model fields reject coercions.
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an epoch-millisecond integer in the supported range")
    return value


def shift_parts(text: str) -> tuple[int, int]:
    if not isinstance(text, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise HTTPException(422, "shift times must use HH:MM")
    h, m = map(int, text.split(":"))
    return h, m


def attendance_date(punch_in: datetime, emp: dict) -> Date:
    local = as_ist(punch_in)
    start = shift_parts(emp["shift_start"])
    end = shift_parts(emp["shift_end"])
    overnight = end <= start
    if overnight and (local.hour, local.minute) < end:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_dt(att_date: Date, emp: dict) -> datetime:
    h, m = shift_parts(emp["shift_start"])
    return datetime.combine(att_date, time(h, m), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def shift_end_dt(att_date: Date, emp: dict) -> datetime:
    sh = shift_parts(emp["shift_start"])
    eh = shift_parts(emp["shift_end"])
    end_day = att_date + timedelta(days=1) if eh <= sh else att_date
    return datetime.combine(end_day, time(*eh), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def compute_late_minutes(punch_in: Optional[datetime], emp: dict) -> int:
    if punch_in is None:
        return 0
    d = attendance_date(punch_in, emp)
    elapsed = (punch_in - shift_start_dt(d, emp)).total_seconds()
    return int(elapsed // 60) if elapsed > 10 * 60 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = (punch_out - punch_in).total_seconds()
    return half_up(seconds / 3600, 2)


def compute_overtime(punch_out: Optional[datetime], att_date: Date, emp: dict) -> int:
    if punch_out is None:
        return 0
    minutes = int((punch_out - shift_end_dt(att_date, emp)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def computed_fields(emp: dict, att_date: Date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if attendance_date(punch_in, emp) != att_date:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": compute_late_minutes(punch_in, emp),
        "work_hours": hours,
        "overtime_minutes": compute_overtime(punch_out, att_date, emp) if punch_out else 0,
        "half_day": bool(hours is not None and hours < 4.50),
    }


def serialize_record(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        result[key] = dt_to_epoch(result.get(key))
    result.setdefault("work_hours", None)
    result["late_minutes"] = int(result.get("late_minutes", 0) or 0)
    result["overtime_minutes"] = int(result.get("overtime_minutes", 0) or 0)
    result["half_day"] = bool(result.get("half_day", False))
    result["history"] = result.get("history", []) or []
    for entry in result["history"]:
        entry["at"] = dt_to_epoch(entry.get("at"))
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = dt_to_epoch(change.get("from"))
                change["to"] = dt_to_epoch(change.get("to"))
    return result


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["created_at"] = dt_to_epoch(result.get("created_at"))
    return result


def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def present_expr(status_field: str = "$status") -> dict:
    return {"$in": [status_field, list(PRESENCE)]}


def date_range_query(month_start: Date, month_end: Date) -> dict:
    return {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}


def weekdays_expression(start_expr: dict, end_expr: dict) -> dict:
    """Mongo expression counting Mon-Fri in [start_expr, end_expr]."""
    return {"$size": {"$filter": {
        "input": {"$map": {
            "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_expr, "endDate": end_expr, "unit": "day"}}, 1]}]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$offset"}},
        }},
        "as": "day",
        "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$$day"}, 6]}]},
    }}}


def month_attendance_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = parse_month(month)
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"date": date_range_query(start, end), "$expr": {"$eq": ["$emp_code", "$$code"]}}},
                {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}, "_present": present_expr(), "_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": ["$_present", {"$in": [{"$dayOfWeek": "$_day"}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": ["$_late", 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ], "as": "stats",
        }},
        {"$set": {"_month_start": {"$dateFromString": {"dateString": start.isoformat()}}, "_month_end": {"$dateFromString": {"dateString": end.isoformat()}}}},
        {"$set": {"_joined": {"$dateFromString": {"dateString": "$joined_on"}}}},
        {"$set": {"_work_start": {"$cond": [{"$gt": ["$_joined", "$_month_start"]}, "$_joined", "$_month_start"]}}},
        {"$set": {"working_days": {"$cond": [{"$gt": ["$_work_start", "$_month_end"]}, 0, weekdays_expression("$_work_start", "$_month_end")]}}},
        {"$set": {"_stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            "present_days": {"$ifNull": ["$_stats.present_days", 0]}, "leave_days": {"$ifNull": ["$_stats.leave_days", 0]},
            "late_count": {"$ifNull": ["$_stats.late_count", 0]}, "total_late_minutes": {"$ifNull": ["$_stats.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$_stats.total_overtime_minutes", 0]},
            "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, {"$multiply": [{"$divide": [{"$ifNull": ["$_stats.present_days", 0]}, "$working_days"]}, 100]}, None]},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    start, end = parse_month(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": date_range_query(start, end), "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}, "_present": present_expr()}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": ["$_present", {"$in": [{"$dayOfWeek": "$_day"}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "work_hours_sum": {"$sum": {"$cond": [{"$and": ["$_present", {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": [{"$and": ["$_present", {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
        ], "as": "stats"}},
        {"$set": {"_stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$_stats.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$_stats.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$_stats.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$_stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$_stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$_stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$_stats.on_duty_count", 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    start, end = parse_month(month)
    pipe = [
        {"$match": {"date": date_range_query(start, end)}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipe.append({"$match": {"employee.department": department}})
    pipe += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_pipeline(department: str, from_date: Date, to_date: Date) -> list[dict]:
    n_days = (to_date - from_date).days + 1
    start_dt = datetime.combine(from_date, time.min, tzinfo=UTC).replace(tzinfo=None)
    return [
        {"$documents": [{}]},
        {"$set": {"_dates": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": {"$dateAdd": {"startDate": {"$literal": start_dt}, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$_dates"},
        {"$set": {"_date": "$_dates", "date": {"$dateToString": {"date": "$_dates", "format": "%Y-%m-%d", "timezone": "UTC"}}, "_dow": {"$dayOfWeek": "$_dates"}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$project": {"_id": 0, "emp_code": 1}},
        ], "as": "_employees"}},
        {"$set": {"headcount": {"$size": "$_employees"}, "_codes": {"$map": {"input": "$_employees", "as": "e", "in": "$$e.emp_code"}}}},
        {"$lookup": {"from": "attendance_logs", "localField": "date", "foreignField": "date", "let": {"codes": "$_codes"}, "pipeline": [
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$project": {"_id": 0, "status": 1, "late_minutes": 1, "half_day": 1}},
        ], "as": "_logs"}},
        {"$set": {
            "is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
            "present_count": {"$sum": {"$map": {"input": {"$filter": {"input": "$_logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE)]}}}, "as": "l", "in": {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]}}}},
            "late_count": {"$size": {"$filter": {"input": "$_logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
        }},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_date": 1}, "output": {"_avg7": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}}, 10000]}, None]},
                   "moving_avg_7d": {"$cond": [{"$ne": ["$_avg7", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_avg7", 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


def explain_aggregate(collection_name: str, pipeline: list[dict], database_level: bool = False) -> dict:
    command = {"aggregate": 1 if database_level else collection_name, "pipeline": pipeline, "cursor": {}}
    return db.command("explain", command, verbosity="executionStats")


# ----------------------------------- models ---------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, v: str) -> str:
        try:
            if Date.fromisoformat(v).isoformat() != v:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None
    punch_out: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def validate_explicit_nulls(cls, values):
        # The contract permits epoch milliseconds for supplied punch fields; null is not a valid supplied instant.
        if isinstance(values, dict):
            for key in ("punch_in", "punch_out"):
                if key in values and values[key] is None:
                    raise ValueError(f"{key} must be an epoch-millisecond integer when supplied")
        return values


# --------------------------------- endpoints --------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = epoch_to_dt(validate_epoch(body.punched_at)) if body.punched_at is not None else now_utc_naive()
    att_day = attendance_date(ts, emp)
    d = att_day.isoformat()
    derived = computed_fields(emp, att_day, ts, None)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": derived["late_minutes"], "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    require_employee(body.emp_code)
    ts = epoch_to_dt(validate_epoch(body.punched_at)) if body.punched_at is not None else now_utc_naive()
    # The most recent punch-in not later than the requested punch-out; covers overnight shifts.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    if ts <= record["punch_in"] or ts > record["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    emp = require_employee(body.emp_code)
    att_day = parse_date(record["date"])
    hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, att_day, emp)
    half_day = bool(hours is not None and hours < 4.50)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record is already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date(date)
    emp = require_employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    if body.status in ("ABSENT", "LEAVE") and ("punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    status = body.status if body.status is not None else old.get("status", "PRESENT")
    if status in ("ABSENT", "LEAVE"):
        pin = pout = None
    else:
        pin = epoch_to_dt(body.punch_in) if "punch_in" in body.model_fields_set and body.punch_in is not None else old.get("punch_in")
        pout = epoch_to_dt(body.punch_out) if "punch_out" in body.model_fields_set and body.punch_out is not None else old.get("punch_out")
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(pin, emp) != att_date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if pout is not None and (pout <= pin or pout > pin + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = computed_fields(emp, att_date, pin, pout)
    new_values = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in tracked:
        before = old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None)
        after = new_values[key]
        if key == "half_day":
            before = bool(before)
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": now_utc_naive(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: matching the old history makes concurrent corrections conflict, not overwrite.
    guard = {"_id": old["_id"]}
    if "history" in old:
        guard["history"] = old["history"]
    else:
        guard["history"] = {"$exists": False}
    try:
        result = db.attendance_logs.update_one(guard, {"$set": new_values, "$push": {"history": history_entry}})
    except PyMongoError:
        raise HTTPException(409, "concurrent correction conflict")
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry the correction")
    return serialize_record(db.attendance_logs.find_one({"_id": old["_id"]}))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    require_employee(emp_code)
    result = list(db.employees.aggregate(month_attendance_pipeline(emp_code, month)))
    if not result:
        raise HTTPException(404, "employee not found")
    row = result[0]
    row["attendance_pct"] = half_up(row.get("attendance_pct"), 4)
    row["present_days"] = half_up(row.get("present_days", 0), 2)
    return row


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    start, end = parse_month(month)
    # Omit departments whose headcount is zero for the requested month.
    rows = [r for r in rows if r["headcount"] > 0]
    for row in rows:
        row["present_days"] = half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = half_up(row.get("avg_work_hours"), 2)
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[str, Query(alias="from")],
    to_date: Annotated[str, Query(alias="to")],
):
    start, end = parse_date(from_date), parse_date(to_date)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    rows = list(db.aggregate(trend_pipeline(department, start, end)))
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query: dict = {}
        if emp_code: query["emp_code"] = emp_code
        if status: query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from
            if date_to: query["date"]["$lte"] = date_to
        return db.command("explain", {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        require_employee(emp_code)
        return explain_aggregate("employees", month_attendance_pipeline(emp_code, month))
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        return explain_aggregate("employees", department_summary_pipeline(month, department))
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        return explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department))
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_date), parse_date(to_date)
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(404, "department not found")
        return explain_aggregate("", trend_pipeline(department, start, end), database_level=True)
    raise HTTPException(422, "unsupported endpoint")
