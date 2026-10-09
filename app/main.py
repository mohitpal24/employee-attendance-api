"""Employee Attendance & Analytics API for the HROne assignment."""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS, MAX_EPOCH_MS = 100_000_000_000, 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

# Shared parsing and business-rule helpers
def invalid(message: str) -> None:
    raise HTTPException(422, message)


def parse_date(value: str, name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        invalid(f"{name} must be a valid YYYY-MM-DD date")
    if parsed.isoformat() != value:
        invalid(f"{name} must be a valid YYYY-MM-DD date")
    return parsed


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        invalid("month must use YYYY-MM format")
    year, number = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def whole_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def parse_epoch(value: int | None) -> datetime:
    if value is None:
        return whole_second(datetime.now(UTC))
    if isinstance(value, bool) or not isinstance(value, int):
        invalid("timestamp must be integer epoch milliseconds")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        invalid("timestamp is outside the supported epoch-millisecond range")
    return datetime.fromtimestamp(value // 1000, UTC)


def to_epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def attendance_day(punch_in: datetime, employee: dict) -> str:
    local = punch_in.astimezone(IST)
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_instant(day: str, value: str, next_day: bool = False) -> datetime:
    base = date.fromisoformat(day) + (timedelta(days=1) if next_day else timedelta())
    hour, minute = map(int, value.split(":"))
    return datetime.combine(base, time(hour, minute), tzinfo=IST)


def late_minutes(punch_in: datetime, day: str, employee: dict) -> int:
    seconds = int((punch_in.astimezone(IST) - shift_instant(day, employee["shift_start"])).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> tuple[float, Decimal]:
    seconds = int((punch_out - punch_in).total_seconds())
    rounded = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded), rounded


def overtime_minutes(punch_out: datetime, day: str, employee: dict) -> int:
    overnight = employee["shift_end"] <= employee["shift_start"]
    end = shift_instant(day, employee["shift_end"], next_day=overnight)
    seconds = int((punch_out.astimezone(IST) - end).total_seconds())
    return seconds // 60 if seconds >= 1800 else 0


def half_up(value, places: int):
    scale = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value, scale]}, 0.5]}}, scale]}


def weekday(date_text):
    day = {"$dayOfWeek": {"$dateFromString": {"dateString": date_text}}}
    return {"$cond": [{"$eq": [date_text, None]}, False,
                      {"$and": [{"$ne": [day, 1]}, {"$ne": [day, 7]}]}]}


# Request models keep client input separate from stored derived values.
class EmployeeCreate(BaseModel):
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
    def check_joined_on(cls, value: str) -> str:
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def timestamp_must_be_omitted_or_integer(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

    @model_validator(mode="after")
    def timestamp_must_be_omitted_or_integer(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


_indexes_ready = False


def create_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employee_department_joined")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="employee_joined_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING), ("punch_out", ASCENDING)], name="attendance_open_punch_lookup")
    _indexes_ready = True


@app.on_event("startup")
def initialize_database() -> None:
    if not MONGO_URI:
        return
    try:
        client.admin.command("ping")
        create_indexes()
    except PyMongoError:
        return


def employee_json(doc: dict) -> dict:
    return {key: doc[key] for key in (
        "emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"
    )} | {"created_at": to_epoch(doc["created_at"])}


def attendance_json(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for name, values in entry.get("changes", {}).items():
            changes[name] = {key: to_epoch(value) if isinstance(value, datetime) else value
                             for key, value in values.items()}
        history.append({"at": to_epoch(entry.get("at")), "by": entry.get("by"),
                        "reason": entry.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_epoch(doc.get("punch_in")), "punch_out": to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": history,
    }


def snapshot_filter(query: dict, doc: dict, field: str) -> None:
    query[field] = doc[field] if field in doc else {"$exists": False}


# The analytics are expressed as MongoDB pipelines so filtering, joining, ranking,
# and date-window work happen beside the stored data.
def monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "late_minutes": 1, "overtime_minutes": 1}},
            ], "as": "logs",
        }},
        {"$set": {
            "_period_start": {"$cond": [{"$gt": ["$joined_on", first]}, "$joined_on", first]},
            "_period_end": last,
        }},
        {"$set": {
            "_start": {"$dateFromString": {"dateString": "$_period_start"}},
            "_end": {"$dateFromString": {"dateString": "$_period_end"}},
        }},
        {"$set": {
            "working_days": {"$cond": [
                {"$lte": ["$_start", "$_end"]},
                {"$let": {
                    "vars": {"count": {"$dateDiff": {"startDate": "$_start", "endDate": "$_end", "unit": "day"}}},
                    "in": {"$size": {"$filter": {
                        "input": {"$map": {
                            "input": {"$range": [0, {"$add": ["$$count", 1]}]}, "as": "n",
                            "in": {"$dateAdd": {"startDate": "$_start", "unit": "day", "amount": "$$n"}},
                        }}, "as": "day",
                        "cond": {"$and": [{"$ne": [{"$dayOfWeek": "$$day"}, 1]},
                                           {"$ne": [{"$dayOfWeek": "$$day"}, 7]}]},
                    }}}
                }},
                0,
            ]},
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [
                {"$and": [{"$in": ["$$log.status", list(PRESENCE)]}, weekday("$$log.date")]},
                {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]}, 0.0,
            ]}}}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "log",
                                                   "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log",
                "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log",
                "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log",
                "in": {"$ifNull": ["$$log.overtime_minutes", 0]}}}},
        }},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            "present_days": 1, "leave_days": 1, "late_count": 1,
            "total_late_minutes": 1, "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [{"$eq": ["$working_days", 0]}, None,
                half_up({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4)]},
        }},
    ]


def department_summary_pipeline(month: str, department: str | None = None) -> list[dict]:
    first, last = month_bounds(month)
    employee_filter = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_filter["department"] = department
    return [
        {"$match": employee_filter},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "work_hours": 1, "late_minutes": 1}},
            ], "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "headcount": {"$first": 1},
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs", None]}, weekday("$logs.date")
            ]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]}, 0.0]}},
            "hours_sum": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}
            ]}, "$logs.work_hours", 0.0]}},
            "hours_count": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}
            ]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department", "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"}, "hours_sum": {"$sum": "$hours_sum"},
            "hours_count": {"$sum": "$hours_count"}, "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$eq": ["$hours_count", 0]}, None,
                half_up({"$divide": ["$hours_sum", "$hours_count"]}, 2)]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: str | None = None) -> list[dict]:
    first, last = month_bounds(month)
    employee_pipe = [{"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}]
    if department is not None:
        employee_pipe.append({"$match": {"department": department}})
    employee_pipe.append({"$project": {"_id": 0, "name": 1, "department": 1}})
    return [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "let": {"code": "$_id"}, "pipeline": employee_pipe, "as": "employee"}},
        {"$unwind": "$employee"},
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                       "total_late_minutes": 1, "late_count": 1}},
    ]


def trend_pipeline(department: str, first: str, day_count: int) -> list[dict]:
    start = {"$dateFromString": {"dateString": {"$literal": first}, "timezone": "UTC"}}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"_department_codes": {"$map": {"input": "$employees", "as": "e", "in": "$$e.emp_code"}}}},
        {"$set": {"_days": {"$map": {
            "input": {"$range": [0, day_count]}, "as": "n",
            "in": {"$dateToString": {"date": {"$dateAdd": {
                "startDate": start, "unit": "day", "amount": "$$n", "timezone": "UTC"
            }}, "format": "%Y-%m-%d", "timezone": "UTC"}},
        }}}},
        {"$unwind": "$_days"},
        {"$set": {"_active": {"$map": {"input": {"$filter": {
            "input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$_days"]}
        }}, "as": "e", "in": "$$e.emp_code"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$_days", "codes": "$_department_codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None,
                "present": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0]}},
                "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }},
        ], "as": "_daily"}},
        {"$set": {"_headcount": {"$size": "$_active"},
            "_present": {"$ifNull": [{"$arrayElemAt": ["$_daily.present", 0]}, 0.0]},
            "_late": {"$ifNull": [{"$arrayElemAt": ["$_daily.late", 0]}, 0]},
            "_working": weekday("$_days")}},
        {"$set": {"_rate": {"$cond": [{"$and": ["$_working", {"$gt": ["$_headcount", 0]}]},
            half_up({"$divide": ["$_present", "$_headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"_days": 1}, "output": {
            "_moving": {"$avg": "$_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "department": {"$literal": department}, "date": "$_days",
            "is_working_day": "$_working", "headcount": "$_headcount", "present_count": "$_present",
            "late_count": "$_late", "attendance_rate": "$_rate",
            "moving_avg_7d": {"$cond": [{"$eq": ["$_moving", None]}, None, half_up("$_moving", 4)]}}},
        {"$sort": {"date": 1}},
    ]


# Basic health, employee management, and attendance operations
@app.get("/health")
def health():
    if not MONGO_URI:
        raise HTTPException(503, "MONGO_URI is not configured")
    try:
        client.admin.command("ping")
        create_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    document = body.model_dump()
    document["created_at"] = whole_second(datetime.now(UTC))
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    items = [employee_json(doc) for doc in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = parse_epoch(body.punched_at)
    day = attendance_day(instant, employee)
    document = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": instant, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(instant, day, employee), "overtime_minutes": 0,
        "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return attendance_json(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = parse_epoch(body.punched_at)
    latest = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": instant}},
        sort=[("punch_in", DESCENDING)],
    )
    if latest is None:
        raise HTTPException(404, "no punch-in found")
    if latest.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")

    punch_in_time = whole_second(latest["punch_in"])
    instant = whole_second(instant)
    elapsed = (instant - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours, rounded_hours = work_hours(punch_in_time, instant)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": latest["_id"], "punch_out": None},
        {"$set": {"punch_out": instant, "work_hours": hours,
                   "overtime_minutes": overtime_minutes(instant, latest["date"], employee),
                   "half_day": rounded_hours < Decimal("4.50")}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return attendance_json(updated)


def attendance_filter(emp_code: str | None, date_from: str | None,
                      date_to: str | None, status: str | None) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            parse_date(date_from, "date_from")
            bounds["$gte"] = date_from
        if date_to is not None:
            parse_date(date_to, "date_to")
            bounds["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            invalid("date_from must not be after date_to")
        query["date"] = bounds
    if status is not None:
        if status not in STATUSES:
            invalid("invalid status")
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort([
        ("date", DESCENDING), ("emp_code", ASCENDING)
    ])
    items = [attendance_json(doc) for doc in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if employee is None or current is None:
        raise HTTPException(404, "employee or attendance record not found")
    supplied = body.model_fields_set
    if "status" in supplied and body.status is None:
        invalid("status cannot be null")
    status = body.status if "status" in supplied else current["status"]
    absent = status in ("ABSENT", "LEAVE")
    if absent and ("punch_in" in supplied or "punch_out" in supplied):
        invalid("ABSENT and LEAVE corrections cannot include punch times")
    if absent:
        punch_in_time = punch_out_time = None
    else:
        if ("punch_in" in supplied and body.punch_in is None) or (
            "punch_out" in supplied and body.punch_out is None
        ):
            invalid("punch times must be epoch milliseconds or omitted")
        punch_in_time = parse_epoch(body.punch_in) if "punch_in" in supplied else current.get("punch_in")
        punch_out_time = parse_epoch(body.punch_out) if "punch_out" in supplied else current.get("punch_out")
        if punch_in_time is None:
            invalid("presence status requires punch_in")
        punch_in_time = whole_second(punch_in_time)
        if attendance_day(punch_in_time, employee) != date:
            invalid("punch_in must belong to the record attendance date")
        if punch_out_time is not None:
            punch_out_time = whole_second(punch_out_time)
            span = (punch_out_time - punch_in_time).total_seconds()
            if span <= 0 or span > 24 * 3600:
                invalid("punch_out must be after punch_in and within 24 hours")

    if absent:
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = late_minutes(punch_in_time, date, employee)
        if punch_out_time is None:
            derived = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        else:
            hours, rounded = work_hours(punch_in_time, punch_out_time)
            derived = {"work_hours": hours, "late_minutes": late,
                       "overtime_minutes": overtime_minutes(punch_out_time, date, employee),
                       "half_day": rounded < Decimal("4.50")}

    values = {"status": status, "punch_in": punch_in_time, "punch_out": punch_out_time, **derived}
    changes = {}
    for field, new_value in values.items():
        old_value = current.get(field)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        invalid("regularization does not change the record")
    entry = {"at": whole_second(datetime.now(UTC)), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    condition = {"_id": current["_id"]}
    for field in ("status", "punch_in", "punch_out", "history"):
        snapshot_filter(condition, current, field)
    updated = db.attendance_logs.find_one_and_update(
        condition, {"$set": values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed during regularization; retry")
    return attendance_json(updated)


# Aggregation-backed reporting endpoints
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month_bounds(month)
    results = list(db.employees.aggregate(monthly_pipeline(emp_code, month)))
    if not results:
        raise HTTPException(404, "employee not found")
    return results[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    month_bounds(month)
    pipeline = department_summary_pipeline(month, department)
    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
):
    month_bounds(month)
    pipeline = leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[str, Query(alias="from")],
    to_date: Annotated[str, Query(alias="to")],
):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        invalid("to must be on or after from")
    count = (end - start).days + 1
    if count > 92:
        invalid("trend range cannot exceed 92 days")
    rows = list(db.employees.aggregate(trend_pipeline(department, from_date, count)))
    if not rows:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": rows}


def explain_aggregate(collection_name: str, pipeline: list[dict]) -> dict:
    return db.command("explain", {
        "aggregate": collection_name, "pipeline": pipeline, "cursor": {},
    }, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    from_date: Annotated[str | None, Query(alias="from")] = None,
    to_date: Annotated[str | None, Query(alias="to")] = None,
):
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs", "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size, "limit": page_size,
        }
        result = db.command("explain", command, verbosity="executionStats")
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            invalid("employee_monthly requires emp_code and month")
        month_bounds(month)
        result = explain_aggregate("employees", monthly_pipeline(emp_code, month))
        collection_name = "employees"
    elif endpoint == "department_summary":
        if month is None:
            invalid("department_summary requires month")
        month_bounds(month)
        result = explain_aggregate("employees", department_summary_pipeline(month, department))
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            invalid("late_leaderboard requires month")
        month_bounds(month)
        result = explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department))
        collection_name = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            invalid("department_trend requires department, from, and to")
        start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
        if end < start:
            invalid("to must be on or after from")
        count = (end - start).days + 1
        if count > 92:
            invalid("trend range cannot exceed 92 days")
        result = explain_aggregate("employees", trend_pipeline(department, from_date, count))
        collection_name = "employees"
    explain_json = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection_name, "explain": explain_json}











