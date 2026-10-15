"""Employee Attendance & Analytics API, contract v2.0.
Run from repository root: uvicorn app.main:app --port 8000
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MAX_EPOCH_MS = 4102444800000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ---- time / response helpers ------------------------------------------------
def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def as_utc_dt(ms: int) -> datetime:
    if type(ms) is not int or ms < 100000000000 or ms > MAX_EPOCH_MS:
        raise ValueError("must be an integer epoch-millisecond timestamp in the allowed range")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)

def to_ms(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)

def public_doc(doc):
    if not doc: return doc
    out = {k: v for k, v in doc.items() if k != "_id"}
    for k in ("created_at", "punch_in", "punch_out"):
        if k in out: out[k] = to_ms(out[k])
    out.setdefault("history", [])
    out.setdefault("half_day", False)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("work_hours", None)
    for entry in out.get("history", []):
        if "at" in entry: entry["at"] = to_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime): change[side] = to_ms(change[side])
    return out

def round_half_up(value, places=2):
    if value is None: return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def parse_date(s):
    try: return date.fromisoformat(s)
    except (ValueError, TypeError): raise HTTPException(422, "date must be YYYY-MM-DD")

def parse_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-")); last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)

def local_dt(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), IST)

def attendance_day(punch_in: datetime, emp: dict) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day

def late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    local = punch_in.astimezone(IST)
    start = local_dt(date.fromisoformat(attendance_date), shift_start)
    elapsed = int((local - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0

def shift_end_dt(attendance_date: str, shift_start: str, shift_end: str):
    d = date.fromisoformat(attendance_date)
    end_day = d + timedelta(days=1) if shift_end <= shift_start else d
    return local_dt(end_day, shift_end).astimezone(UTC)

def derived_fields(punch_in, punch_out, emp, attendance_date):
    late = late_minutes(punch_in, emp["shift_start"], attendance_date) if punch_in else 0
    if punch_in and punch_out:
        hours = round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)
        end = shift_end_dt(attendance_date, emp["shift_start"], emp["shift_end"])
        extra = int((punch_out - end).total_seconds() // 60)
        overtime = extra if extra >= 30 else 0
        half = hours < 4.50
    else:
        hours, overtime, half = None, 0, False
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": max(0, overtime), "half_day": half}

def employee_or_404(code):
    emp = employees.find_one({"emp_code": code})
    if not emp: raise HTTPException(404, "employee not found")
    return emp

def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("emp_code", ASCENDING), ("created_at", DESCENDING)], name="ix_employee_code_created")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_employee_date_desc")
    logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")
    logs.create_index([("punch_in", DESCENDING)], name="ix_attendance_punch_in")

@app.on_event("startup")
def startup():
    ensure_indexes()

# ---- validated request models ----------------------------------------------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date
    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v):
        if v is not None: as_utc_dt(v)
        return v

class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v):
        if v is not None: as_utc_dt(v)
        return v

class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_time(cls, v):
        if v is not None: as_utc_dt(v)
        return v

# ---- system / employees -----------------------------------------------------
@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status": "ok"}
    except PyMongoError: raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump(); doc["joined_on"] = doc["joined_on"].isoformat(); doc["created_at"] = now_utc()
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return public_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = [public_doc(d) for d in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# ---- attendance -------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employee_or_404(body.emp_code)
    instant = as_utc_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_day(instant, emp).isoformat()
    # Atomicity is guaranteed by the unique natural-key index, not this pre-check.
    derived = derived_fields(instant, None, emp, day)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": instant,
           "punch_out": None, **derived, "history": []}
    try: logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return public_doc(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employee_or_404(body.emp_code)
    instant = as_utc_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": instant}}, sort=[("punch_in", DESCENDING)])
    if not rec:
        raise HTTPException(404, "no punch-in record found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    if instant <= rec["punch_in"] or instant - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    d = rec["date"]
    deriv = derived_fields(rec["punch_in"], instant, emp, d)
    result = logs.update_one({"emp_code": body.emp_code, "date": d, "punch_out": None}, {"$set": {"punch_out": instant, **deriv}})
    if result.modified_count != 1: raise HTTPException(409, "record already punched out")
    rec.update({"punch_out": instant, **deriv})
    return public_doc(rec)

def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    return q

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = attendance_query(emp_code, date_from.isoformat() if date_from else None, date_to.isoformat() if date_to else None, status)
    total = logs.count_documents(q)
    items = [public_doc(d) for d in logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date = Path(...), body: RegularizeRequest = ...):
    emp = employee_or_404(emp_code); day = date.isoformat()
    rec = logs.find_one({"emp_code": emp_code, "date": day})
    if not rec: raise HTTPException(404, "attendance record not found")
    old = {k: rec.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    changes_in = body.model_dump(exclude_unset=True)
    new_status = changes_in.get("status", rec["status"])
    if new_status in ("ABSENT", "LEAVE"):
        if changes_in.get("punch_in") is not None or changes_in.get("punch_out") is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pin = pout = None
    else:
        pin = as_utc_dt(changes_in["punch_in"]) if changes_in.get("punch_in") is not None else rec.get("punch_in")
        pout = as_utc_dt(changes_in["punch_out"]) if changes_in.get("punch_out") is not None else rec.get("punch_out")
        if pin is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(pin, emp).isoformat() != day: raise HTTPException(422, "punch_in must belong to attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    deriv = derived_fields(pin, pout, emp, day)
    new = {"status": new_status, "punch_in": pin, "punch_out": pout, **deriv}
    actual = {}
    for field, value in new.items():
        previous = old.get(field)
        if isinstance(previous, datetime) and isinstance(value, datetime):
            equal = previous == value
        else: equal = previous == value
        if not equal:
            actual[field] = {"from": previous, "to": value}
    if not actual: raise HTTPException(422, "regularization does not change the record")
    history_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": actual}
    # Compare-and-swap protects history from lost concurrent updates.
    filt = {"emp_code": emp_code, "date": day}
    for k in old:
        filt[k] = rec.get(k) if k in rec else {"$exists": False}
    update = {"$set": new, "$push": {"history": history_entry}}
    result = logs.update_one(filt, update)
    if result.modified_count != 1: raise HTTPException(409, "record changed concurrently; retry regularization")
    rec.update(new); rec.setdefault("history", []).append(history_entry)
    return public_doc(rec)

# ---- aggregation pipeline helpers -----------------------------------------
def month_range(month):
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end

def presence_expr(): return {"$in": ["$status", list(PRESENCE)]}

def employee_monthly_pipeline(emp_code, month, joined_on):
    start_s, end_s, start, end = month_range(month)
    joined = date.fromisoformat(joined_on)
    effective_start = max(start, joined)
    working_days = sum(1 for offset in range(max(0, (end-effective_start).days+1))
                       if (effective_start + timedelta(days=offset)).weekday() < 5)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", max(start, joined).isoformat()]}, {"$lte": ["$date", end_s]}
            ]}}},
            {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}
        ], "as": "records"}},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": {"$literal": working_days},
            "present_days": {"$sum": {"$map": {"input": {"$filter": {"input": "$records", "as": "r", "cond": {"$and": [
                {"$in": ["$$r.status", list(PRESENCE)]}, {"$gte": ["$$r._weekday", 2]}, {"$lte": ["$$r._weekday", 6]}, {"$gte": ["$$r.date", max(start, joined).isoformat()]}
            ]}}}, "as": "r", "in": {"$cond": [{"$eq": ["$$r.half_day", True]}, 0.5, 1]}}}},
            "leave_days": {"$size": {"$filter": {"input": "$records", "as": "r", "cond": {"$eq": ["$$r.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$records", "as": "r", "cond": {"$gt": [{"$ifNull": ["$$r.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$records", "as": "r", "in": {"$ifNull": ["$$r.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$records", "as": "r", "in": {"$ifNull": ["$$r.overtime_minutes", 0]}}}}
        }},
        {"$set": {"attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 10000]}, 0.5]}}, 100]}, None]}}}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee_or_404(emp_code)
    emp_doc = employees.find_one({"emp_code": emp_code})
    rows = list(employees.aggregate(employee_monthly_pipeline(emp_code, month, emp_doc["joined_on"])))
    if rows: return rows[0]
    # Employee joined after period end: still known, but zero working days in that period.
    _, _, start, end = month_range(month)
    joined = date.fromisoformat(employees.find_one({"emp_code": emp_code})["joined_on"])
    return {"emp_code": emp_code, "month": month, "working_days": 0, "present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": None}

def department_summary_pipeline(month, department=None):
    start_s, end_s, _, _ = month_range(month)
    match = {"joined_on": {"$lte": end_s}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}
                ]}}},
                {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}
            ], "as": "records"
        }},
        {"$unwind": {"path": "$records", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$records.status", list(PRESENCE)]}, {"$gte": ["$records._weekday", 2]}, {"$lte": ["$records._weekday", 6]}]},
                {"$cond": [{"$eq": ["$records.half_day", True]}, 0.5, 1]}, 0
            ]}},
            "hours": {"$push": {"$cond": [
                {"$and": [{"$in": ["$records.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$records.work_hours", None]}, None]}]},
                "$records.work_hours", None
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$records.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$records.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$records.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$records.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"}, "present_days": 1,
            "avg_work_hours": {"$let": {
                "vars": {"valid": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}},
                "in": {"$cond": [{"$gt": [{"$size": "$$valid"}, 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$avg": "$$valid"}, 100]}, 0.5]}}, 100]}, None]}
            }},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    rows = list(employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": rows}

def leaderboard_pipeline(month, department=None, limit=10):
    start_s, end_s, _, _ = month_range(month)
    pipe = [
      {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
      {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
      {"$unwind": "$employee"}
    ]
    if department is not None: pipe.append({"$match": {"employee.department": department}})
    pipe += [
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}},
      {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipe

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(month, department, limit)))}

def trend_pipeline(department, from_day, to_day):
    start = date.fromisoformat(from_day)
    end = date.fromisoformat(to_day)
    days = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {
            "_id": 0, "department": {"$literal": department},
            "start": {"$dateFromString": {"dateString": from_day}},
            "days": {"$range": [0, days]}
        }},
        {"$unwind": "$days"},
        {"$set": {"day": {"$dateToString": {"date": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$days"}}, "format": "%Y-%m-%d"}}}},
        {"$lookup": {
            "from": "employees", "let": {"dept": "$department", "day": "$day"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", "$$day"]}]}}},
                {"$count": "n"}
            ], "as": "hc"
        }},
        {"$lookup": {
            "from": "attendance_logs", "let": {"day": "$day", "dept": "$department"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
                {"$unwind": "$e"},
                {"$match": {"$expr": {"$eq": ["$e.department", "$$dept"]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE)]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
                }}
            ], "as": "daily"
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$day"}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$day"}}}, 6]}
            ]}
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}]}
        }},
        {"$sort": {"date": 1}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_day: date = Query(..., alias="from"), to_day: date = Query(..., alias="to")):
    if to_day < from_day: raise HTTPException(422, "to must be >= from")
    if (to_day-from_day).days+1 > 92: raise HTTPException(422, "date range must not exceed 92 days")
    if not employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    pipeline = trend_pipeline(department, from_day.isoformat(), to_day.isoformat())
    return {"department": department, "items": list(employees.aggregate(pipeline))}

# ---- explain endpoint -------------------------------------------------------
def explain_query(endpoint, params):
    if endpoint == "attendance_list":
        q = attendance_query(params.get("emp_code"), params.get("date_from"), params.get("date_to"), params.get("status"))
        page, size = params.get("page", 1), params.get("page_size", 20)
        return "attendance_logs", logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page-1)*size).limit(size).explain("executionStats")
    if endpoint == "employee_monthly":
        code, month = params.get("emp_code"), params.get("month")
        if not code or not month: raise HTTPException(422, "emp_code and month are required")
        return "employees", employees.database.command({"explain": {"aggregate": "employees", "pipeline": employee_monthly_pipeline(code, month), "cursor": {}}, "verbosity": "executionStats"})
    if endpoint == "department_summary":
        month = params.get("month");
        if not month: raise HTTPException(422, "month is required")
        return "employees", employees.database.command({"explain": {"aggregate": "employees", "pipeline": department_summary_pipeline(month, params.get("department")), "cursor": {}}, "verbosity": "executionStats"})
    if endpoint == "late_leaderboard":
        month = params.get("month")
        if not month: raise HTTPException(422, "month is required")
        return "attendance_logs", logs.database.command({"explain": {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(month, params.get("department"), params.get("limit", 10)), "cursor": {}}, "verbosity": "executionStats"})
    if endpoint == "department_trend":
        department, start, end = params.get("department"), params.get("from"), params.get("to")
        if not department or not start or not end: raise HTTPException(422, "department, from and to are required")
        if (parse_date(end)-parse_date(start)).days > 91: raise HTTPException(422, "date range must not exceed 92 days")
        return "employees", employees.database.command({"explain": {"aggregate": "employees", "pipeline": trend_pipeline(department, start, end), "cursor": {}}, "verbosity": "executionStats"})
    raise HTTPException(422, "unsupported endpoint")

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[date] = None, date_to: Optional[date] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     from_day: Optional[date] = Query(None, alias="from"), to_day: Optional[date] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    params = {"emp_code": emp_code, "month": month, "department": department, "limit": limit,
              "date_from": date_from.isoformat() if date_from else None, "date_to": date_to.isoformat() if date_to else None,
              "status": status, "from": from_day.isoformat() if from_day else None, "to": to_day.isoformat() if to_day else None,
              "page": page, "page_size": page_size}
    collection, result = explain_query(endpoint, params)
    return {"endpoint": endpoint, "collection": collection, "explain": json_util.loads(json_util.dumps(result))}
