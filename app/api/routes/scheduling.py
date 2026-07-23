import datetime
import json
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_user
from app.db import session as db
from app.models.schemas import (
    AutoBuildRequest,
    AvailabilityRequest,
    DepartmentCreateRequest,
    DepartmentUpdateRequest,
    EmployeeCreateRequest,
    EmployeeProfileUpdateRequest,
    GenerateShiftsRequest,
    PublishRequest,
    RequirementCreateRequest,
    RequirementUpdateRequest,
    ShiftCreateRequest,
    ShiftUpdateRequest,
    SwapDecisionRequest,
    TemplateSaveRequest,
)
from app.services.scheduling import (
    generate_shifts_from_requirements,
    parse_date,
    parse_time,
    release_shift_if_no_live_swaps,
    resolve_effective_requirements,
    serialize_shift,
    shift_hours,
    validate_assignment,
    week_start,
)

router = APIRouter()


async def _restaurant_id_for(user_id: int) -> int:
    if not db.pool:
        raise HTTPException(503, detail="Database unavailable")
    restaurant_id = await db.pool.fetchval("SELECT restaurant_id FROM users WHERE id = $1", user_id)
    if not restaurant_id:
        raise HTTPException(404, detail="Restaurant membership not found")
    return restaurant_id


@router.get("/api/scheduling/shifts")
async def list_shifts(weekStart: Optional[str] = None, departmentId: Optional[int] = None, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    is_manager = user["role"] == "manager"
    conditions, args = ["resto_id = $1"], [restaurant_id]
    if weekStart:
        monday = week_start(parse_date(weekStart))
        args.append(monday); conditions.append(f"shift_date >= ${len(args)}")
        args.append(monday + datetime.timedelta(days=6)); conditions.append(f"shift_date <= ${len(args)}")
    if not is_manager:
        args.append(user["id"]); conditions.append(f"employee_id = ${len(args)}")
        conditions.append("is_draft = false")
    if departmentId is not None:
        args.append(departmentId); conditions.append(f"department_id = ${len(args)}")
    query = f"SELECT id FROM shifts WHERE {' AND '.join(conditions)} ORDER BY shift_date, start_time"
    rows = await db.pool.fetch(query, *args)
    return [await serialize_shift(db.pool, row) for row in rows]


@router.get("/api/scheduling/departments")
async def list_departments(user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await db.pool.fetch("SELECT id, name, role_category FROM departments WHERE resto_id = $1 ORDER BY name", restaurant_id)
    return [{"id": row["id"], "name": row["name"], "roleCategory": row["role_category"]} for row in rows]


@router.post("/api/scheduling/departments")
async def create_department(payload: DepartmentCreateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can create departments")
    if payload.roleCategory not in ("foh", "boh"):
        raise HTTPException(422, detail="roleCategory must be foh or boh")
    if not payload.name.strip():
        raise HTTPException(422, detail="Department name is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "INSERT INTO departments (resto_id, name, role_category) VALUES ($1, $2, $3) RETURNING id, name, role_category",
        restaurant_id, payload.name.strip(), payload.roleCategory,
    )
    return {"id": row["id"], "name": row["name"], "roleCategory": row["role_category"]}


@router.patch("/api/scheduling/departments/{department_id}")
async def rename_department(department_id: int, payload: DepartmentUpdateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can rename departments")
    if not payload.name.strip():
        raise HTTPException(422, detail="Department name is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "UPDATE departments SET name = $1 WHERE id = $2 AND resto_id = $3 RETURNING id, name, role_category",
        payload.name.strip(), department_id, restaurant_id,
    )
    if not row:
        raise HTTPException(404, detail="Department not found")
    return {"id": row["id"], "name": row["name"], "roleCategory": row["role_category"]}


@router.delete("/api/scheduling/departments/{department_id}")
async def delete_department(department_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can delete departments")
    restaurant_id = await _restaurant_id_for(user["id"])
    # users.department_id and shifts.department_id are ON DELETE SET NULL (employees/shifts just
    # become unassigned), coverage_requirements.department_id is ON DELETE CASCADE (a requirement
    # block can't exist without a department) -- both handled by the FKs, no manual cleanup here.
    row = await db.pool.fetchrow("DELETE FROM departments WHERE id = $1 AND resto_id = $2 RETURNING id", department_id, restaurant_id)
    if not row:
        raise HTTPException(404, detail="Department not found")
    return {"id": department_id, "deleted": True}


_EMPLOYEE_COLUMNS = """u.id, u.name, u.email, u.active, r.name AS role, u.department_id, d.name AS department_name,
                  u.weekly_availability, u.max_hours_per_week, u.min_hours_per_week, u.preferred_hours_per_week,
                  u.scheduling_confidence, u.scheduling_notes, u.auto_schedule_opt_out"""


def _serialize_employee(row: asyncpg.Record) -> dict:
    availability = row["weekly_availability"]
    if isinstance(availability, str):
        availability = json.loads(availability)
    return {
        "id": row["id"], "name": row["name"], "email": row["email"], "active": row["active"], "role": row["role"],
        "departmentId": row["department_id"], "departmentName": row["department_name"],
        "weeklyAvailability": availability or {},
        "maxHoursPerWeek": float(row["max_hours_per_week"]), "minHoursPerWeek": float(row["min_hours_per_week"]),
        "preferredHoursPerWeek": float(row["preferred_hours_per_week"]) if row["preferred_hours_per_week"] is not None else None,
        "schedulingConfidence": row["scheduling_confidence"], "schedulingNotes": row["scheduling_notes"],
        "autoScheduleOptOut": row["auto_schedule_opt_out"],
    }


@router.get("/api/scheduling/employees")
async def scheduling_employees(departmentId: Optional[int] = None, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view the team roster")
    restaurant_id = await _restaurant_id_for(user["id"])
    conditions, args = ["u.restaurant_id = $1", "r.name IN ('foh', 'boh')"], [restaurant_id]
    if departmentId is not None:
        args.append(departmentId); conditions.append(f"u.department_id = ${len(args)}")
    rows = await db.pool.fetch(
        f"""SELECT {_EMPLOYEE_COLUMNS}
            FROM users u JOIN roles r ON r.id = u.role_id
            LEFT JOIN departments d ON d.id = u.department_id
            WHERE {' AND '.join(conditions)} ORDER BY u.active DESC, u.name""", *args,
    )
    return [_serialize_employee(row) for row in rows]


@router.post("/api/scheduling/employees")
async def create_employee(payload: EmployeeCreateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can add employees")
    if payload.roleCategory not in ("foh", "boh"):
        raise HTTPException(422, detail="roleCategory must be foh or boh")
    name, email = payload.name.strip(), payload.email.strip()
    if not name or not email or "@" not in email:
        raise HTTPException(422, detail="A valid name and email are required")
    restaurant_id = await _restaurant_id_for(user["id"])
    if payload.departmentId is not None:
        department = await db.pool.fetchrow("SELECT id FROM departments WHERE id = $1 AND resto_id = $2", payload.departmentId, restaurant_id)
        if not department:
            raise HTTPException(404, detail="Department not found")
    if await db.pool.fetchval("SELECT 1 FROM users WHERE email = $1", email):
        raise HTTPException(409, detail="Email already in use")
    role_id = await db.pool.fetchval("SELECT id FROM roles WHERE name = $1", payload.roleCategory)
    row = await db.pool.fetchrow(
        f"""WITH new_user AS (
                INSERT INTO users (restaurant_id, role_id, department_id, name, email, password_hash)
                VALUES ($1, $2, $3, $4, $5, NULL)
                RETURNING id, name, email, active, department_id, weekly_availability, max_hours_per_week,
                          min_hours_per_week, preferred_hours_per_week, scheduling_confidence, scheduling_notes,
                          auto_schedule_opt_out
            )
            SELECT new_user.*, r.name AS role, d.name AS department_name
            FROM new_user JOIN roles r ON r.id = $2
            LEFT JOIN departments d ON d.id = new_user.department_id""",
        restaurant_id, role_id, payload.departmentId, name, email,
    )
    return _serialize_employee(row)


@router.patch("/api/scheduling/employees/{employee_id}")
async def update_employee_profile(employee_id: int, payload: EmployeeProfileUpdateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can edit team profiles")
    restaurant_id = await _restaurant_id_for(user["id"])
    employee = await db.pool.fetchrow("SELECT * FROM users WHERE id = $1 AND restaurant_id = $2", employee_id, restaurant_id)
    if not employee:
        raise HTTPException(404, detail="Employee not found")
    if payload.schedulingConfidence is not None and not (1 <= payload.schedulingConfidence <= 5):
        raise HTTPException(422, detail="schedulingConfidence must be between 1 and 5")
    max_hours = payload.maxHoursPerWeek if payload.maxHoursPerWeek is not None else float(employee["max_hours_per_week"])
    min_hours = payload.minHoursPerWeek if payload.minHoursPerWeek is not None else float(employee["min_hours_per_week"])
    if min_hours > max_hours:
        raise HTTPException(422, detail="minHoursPerWeek cannot exceed maxHoursPerWeek")
    if payload.departmentId is not None:
        department = await db.pool.fetchrow("SELECT id FROM departments WHERE id = $1 AND resto_id = $2", payload.departmentId, restaurant_id)
        if not department:
            raise HTTPException(404, detail="Department not found")
    await db.pool.execute(
        """UPDATE users SET department_id = COALESCE($1, department_id), max_hours_per_week = $2, min_hours_per_week = $3,
           preferred_hours_per_week = $4, scheduling_confidence = COALESCE($5, scheduling_confidence),
           scheduling_notes = COALESCE($6, scheduling_notes), auto_schedule_opt_out = COALESCE($7, auto_schedule_opt_out),
           active = COALESCE($8, active)
           WHERE id = $9""",
        payload.departmentId, max_hours, min_hours, payload.preferredHoursPerWeek, payload.schedulingConfidence,
        payload.schedulingNotes, payload.autoScheduleOptOut, payload.active, employee_id,
    )
    return {"id": employee_id, "updated": True}


@router.get("/api/scheduling/requirements")
async def list_requirements(weekStart: str, departmentId: Optional[int] = None, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view staffing requirements")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(weekStart))
    blocks = await resolve_effective_requirements(db.pool, restaurant_id, monday)
    if departmentId is not None:
        blocks = [b for b in blocks if b["departmentId"] == departmentId]
    return blocks


@router.post("/api/scheduling/requirements")
async def create_requirement(payload: RequirementCreateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can define staffing requirements")
    if not (0 <= payload.dayOfWeek <= 6):
        raise HTTPException(422, detail="dayOfWeek must be between 0 (Monday) and 6 (Sunday)")
    if payload.countRequired <= 0:
        raise HTTPException(422, detail="countRequired must be positive")
    if payload.minConfidence is not None and not (1 <= payload.minConfidence <= 5):
        raise HTTPException(422, detail="minConfidence must be between 1 and 5")
    restaurant_id = await _restaurant_id_for(user["id"])
    department = await db.pool.fetchrow("SELECT id FROM departments WHERE id = $1 AND resto_id = $2", payload.departmentId, restaurant_id)
    if not department:
        raise HTTPException(404, detail="Department not found")
    start_time, end_time = parse_time(payload.startTime), parse_time(payload.endTime)
    if start_time == end_time:
        raise HTTPException(422, detail="startTime and endTime must differ")
    week_start_override = week_start(parse_date(payload.weekStartOverride)) if payload.weekStartOverride else None
    row = await db.pool.fetchrow(
        """INSERT INTO coverage_requirements (resto_id, department_id, day_of_week, week_start_override, start_time, end_time, count_required, min_confidence, notes)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
        restaurant_id, payload.departmentId, payload.dayOfWeek, week_start_override, start_time, end_time,
        payload.countRequired, payload.minConfidence, payload.notes,
    )
    return {"id": row["id"]}


@router.patch("/api/scheduling/requirements/{requirement_id}")
async def update_requirement(requirement_id: int, payload: RequirementUpdateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can edit staffing requirements")
    if payload.countRequired <= 0:
        raise HTTPException(422, detail="countRequired must be positive")
    if payload.minConfidence is not None and not (1 <= payload.minConfidence <= 5):
        raise HTTPException(422, detail="minConfidence must be between 1 and 5")
    restaurant_id = await _restaurant_id_for(user["id"])
    start_time, end_time = parse_time(payload.startTime), parse_time(payload.endTime)
    if start_time == end_time:
        raise HTTPException(422, detail="startTime and endTime must differ")
    row = await db.pool.fetchrow(
        """UPDATE coverage_requirements SET start_time = $1, end_time = $2, count_required = $3, min_confidence = $4, notes = $5
           WHERE id = $6 AND resto_id = $7 RETURNING id""",
        start_time, end_time, payload.countRequired, payload.minConfidence, payload.notes, requirement_id, restaurant_id,
    )
    if not row:
        raise HTTPException(404, detail="Requirement not found")
    return {"id": row["id"]}


@router.delete("/api/scheduling/requirements/{requirement_id}")
async def delete_requirement(requirement_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can delete staffing requirements")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow("DELETE FROM coverage_requirements WHERE id = $1 AND resto_id = $2 RETURNING id", requirement_id, restaurant_id)
    if not row:
        raise HTTPException(404, detail="Requirement not found")
    return {"id": requirement_id, "deleted": True}


@router.post("/api/scheduling/requirements/generate-shifts")
async def generate_shifts(payload: GenerateShiftsRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can generate shifts")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(payload.weekStart))
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            result = await generate_shifts_from_requirements(connection, restaurant_id, monday, payload.departmentId)
    return result


@router.post("/api/scheduling/shifts")
async def create_shift(payload: ShiftCreateRequest, user: dict = Depends(require_user)):
    """Create an open shift or validate and publish an assigned one."""
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can create shifts")
    restaurant_id = await _restaurant_id_for(user["id"])
    department = await db.pool.fetchrow("SELECT id, role_category FROM departments WHERE id = $1 AND resto_id = $2", payload.departmentId, restaurant_id)
    if not department:
        raise HTTPException(404, detail="Department not found")
    shift_date, start_time, end_time = parse_date(payload.date), parse_time(payload.startTime), parse_time(payload.endTime)
    if payload.employeeId is not None:
        await validate_assignment(db.pool, resto_id=restaurant_id, employee_id=payload.employeeId,
            role_required=department["role_category"], shift_date=shift_date, start_time=start_time, end_time=end_time)
    row = await db.pool.fetchrow(
        """INSERT INTO shifts (resto_id, employee_id, role_required, department_id, shift_date, start_time, end_time, status, is_draft)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true) RETURNING id""",
        restaurant_id, payload.employeeId, department["role_category"], department["id"], shift_date, start_time, end_time,
        "Scheduled" if payload.employeeId is not None else "Open",
    )
    return await serialize_shift(db.pool, row)


@router.get("/api/scheduling/availability")
async def get_availability(user: dict = Depends(require_user)):
    availability = await db.pool.fetchval("SELECT weekly_availability FROM users WHERE id = $1", user["id"])
    if isinstance(availability, str):
        availability = json.loads(availability)
    return {"weeklyAvailability": availability or {}}


@router.patch("/api/scheduling/availability")
async def update_availability(payload: AvailabilityRequest, user: dict = Depends(require_user)):
    valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    if any(day.lower() not in valid_days for day in payload.weeklyAvailability):
        raise HTTPException(422, detail="Availability must be grouped by day of the week")
    for day, windows in payload.weeklyAvailability.items():
        if not isinstance(windows, list):
            raise HTTPException(422, detail=f"Availability for {day} must be a list of time windows")
        for window in windows:
            try:
                if parse_time(window["start"]) >= parse_time(window["end"]):
                    raise HTTPException(422, detail=f"Availability for {day} must end after it starts")
            except (KeyError, TypeError):
                raise HTTPException(422, detail=f"Availability for {day} needs start and end times")
    await db.pool.execute("UPDATE users SET weekly_availability = $1::jsonb WHERE id = $2", json.dumps(payload.weeklyAvailability), user["id"])
    return {"weeklyAvailability": payload.weeklyAvailability}


@router.post("/api/scheduling/auto-build")
async def auto_build_schedule(payload: AutoBuildRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can auto-build schedules")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(payload.weekStart))
    sunday = monday + datetime.timedelta(days=6)
    assignments, unfilled = [], []
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            open_shifts = await connection.fetch(
                """SELECT s.*, cr.min_confidence FROM shifts s LEFT JOIN coverage_requirements cr ON cr.id = s.requirement_id
                   WHERE s.resto_id = $1 AND s.shift_date BETWEEN $2 AND $3 AND s.employee_id IS NULL AND s.status = 'Open'
                   ORDER BY s.shift_date, s.start_time""",
                restaurant_id, monday, sunday,
            )
            for shift in open_shifts:
                candidates = await connection.fetch(
                    """SELECT u.id, u.scheduling_confidence, u.min_hours_per_week FROM users u JOIN roles r ON r.id = u.role_id
                       WHERE u.restaurant_id = $1 AND r.name = $2 AND u.department_id = $3 AND u.auto_schedule_opt_out = false AND u.active = true""",
                    restaurant_id, shift["role_required"], shift["department_id"],
                )
                # Busy blocks can declare a min_confidence (set explicitly by a manager on the
                # requirement, not inferred) -- candidates below it are excluded from Smart Fill
                # entirely, though a manager can still place them manually via drag-and-drop.
                if shift["min_confidence"] is not None:
                    candidates = [c for c in candidates if c["scheduling_confidence"] >= shift["min_confidence"]]
                # Rank by: (1) anyone still under their own weekly minimum first, (2) fewest hours
                # already assigned this week (fairness), (3) higher confidence as a tiebreaker,
                # but only on blocks that actually declared a confidence requirement -- confidence
                # never overrides fairness on an ordinary shift. Hours are re-queried per shift
                # (within the same transaction) so earlier assignments made during this same run
                # are reflected in the next shift's ranking.
                ranked = []
                for candidate in candidates:
                    existing = await connection.fetch(
                        "SELECT shift_date, start_time, end_time FROM shifts WHERE employee_id = $1 AND shift_date BETWEEN $2 AND $3 AND status IN ('Scheduled', 'Pending_Swap')",
                        candidate["id"], monday, sunday,
                    )
                    hours = sum(shift_hours(row["shift_date"], row["start_time"], row["end_time"]) for row in existing)
                    below_minimum = hours < float(candidate["min_hours_per_week"] or 0) - 1e-9
                    confidence_tiebreak = -candidate["scheduling_confidence"] if shift["min_confidence"] is not None else 0
                    ranked.append(((0 if below_minimum else 1, hours, confidence_tiebreak), candidate["id"]))
                ranked.sort(key=lambda pair: pair[0])
                chosen = None
                for _, candidate_id in ranked:
                    try:
                        await validate_assignment(connection, resto_id=restaurant_id, employee_id=candidate_id, role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"])
                        chosen = candidate_id
                        break
                    except HTTPException:
                        continue
                if chosen:
                    row = await connection.fetchrow("UPDATE shifts SET employee_id = $1, status = 'Scheduled' WHERE id = $2 RETURNING id", chosen, shift["id"])
                    assignments.append(await serialize_shift(connection, row))
                else:
                    unfilled.append(shift["id"])
    return {"assigned": assignments, "unfilledShiftIds": unfilled}


@router.patch("/api/scheduling/shifts/{shift_id}")
async def update_shift(shift_id: int, payload: ShiftUpdateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can edit shifts")
    restaurant_id = await _restaurant_id_for(user["id"])
    current = await db.pool.fetchrow("SELECT * FROM shifts WHERE id = $1 AND resto_id = $2", shift_id, restaurant_id)
    if not current:
        raise HTTPException(404, detail="Shift not found")
    department = await db.pool.fetchrow(
        "SELECT id, role_category FROM departments WHERE id = $1 AND resto_id = $2",
        payload.departmentId or current["department_id"], restaurant_id,
    )
    if not department:
        raise HTTPException(404, detail="Department not found")
    shift_date, start_time, end_time = parse_date(payload.date), parse_time(payload.startTime), parse_time(payload.endTime)
    if payload.employeeId is not None:
        await validate_assignment(db.pool, resto_id=restaurant_id, employee_id=payload.employeeId, role_required=department["role_category"], shift_date=shift_date, start_time=start_time, end_time=end_time, exclude_shift_id=shift_id)
    row = await db.pool.fetchrow(
        """UPDATE shifts SET employee_id = $1, department_id = $2, role_required = $3, shift_date = $4, start_time = $5, end_time = $6,
           status = CASE WHEN $1::integer IS NULL THEN 'Open' ELSE 'Scheduled' END, is_draft = true WHERE id = $7 RETURNING id""",
        payload.employeeId, department["id"], department["role_category"], shift_date, start_time, end_time, shift_id,
    )
    return await serialize_shift(db.pool, row)


@router.delete("/api/scheduling/shifts/{shift_id}")
async def delete_shift(shift_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can delete shifts")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow("DELETE FROM shifts WHERE id = $1 AND resto_id = $2 RETURNING id", shift_id, restaurant_id)
    if not row:
        raise HTTPException(404, detail="Shift not found")
    return {"id": shift_id, "deleted": True}


@router.post("/api/scheduling/templates")
async def save_template(payload: TemplateSaveRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can save templates")
    if not payload.name.strip():
        raise HTTPException(422, detail="Template name is required")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(payload.weekStart))
    rows = await db.pool.fetch(
        "SELECT employee_id, department_id, shift_date, start_time, end_time FROM shifts WHERE resto_id = $1 AND shift_date BETWEEN $2 AND $3",
        restaurant_id, monday, monday + datetime.timedelta(days=6),
    )
    shifts = [
        {
            "dayOffset": (row["shift_date"] - monday).days,
            "departmentId": row["department_id"],
            "employeeId": row["employee_id"],
            "startTime": row["start_time"].isoformat(timespec="minutes"),
            "endTime": row["end_time"].isoformat(timespec="minutes"),
        }
        for row in rows
    ]
    row = await db.pool.fetchrow(
        "INSERT INTO schedule_templates (resto_id, name, shifts) VALUES ($1, $2, $3::jsonb) RETURNING id, name, shifts",
        restaurant_id, payload.name.strip(), json.dumps(shifts),
    )
    return {"id": row["id"], "name": row["name"], "shiftCount": len(shifts)}


@router.get("/api/scheduling/templates")
async def list_templates(user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view templates")
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await db.pool.fetch("SELECT id, name, shifts FROM schedule_templates WHERE resto_id = $1 ORDER BY name", restaurant_id)
    result = []
    for row in rows:
        shifts = row["shifts"]
        if isinstance(shifts, str):
            shifts = json.loads(shifts)
        result.append({"id": row["id"], "name": row["name"], "shiftCount": len(shifts)})
    return result


@router.post("/api/scheduling/templates/{template_id}/apply")
async def apply_template(template_id: int, weekStart: str, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can apply templates")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(weekStart))
    template = await db.pool.fetchrow("SELECT shifts FROM schedule_templates WHERE id = $1 AND resto_id = $2", template_id, restaurant_id)
    if not template:
        raise HTTPException(404, detail="Template not found")
    entries = template["shifts"]
    if isinstance(entries, str):
        entries = json.loads(entries)
    applied, skipped = [], 0
    for entry in entries:
        try:
            department = await db.pool.fetchrow("SELECT id, role_category FROM departments WHERE id = $1 AND resto_id = $2", entry["departmentId"], restaurant_id)
            if not department:
                skipped += 1
                continue
            shift_date = monday + datetime.timedelta(days=entry["dayOffset"])
            start_time, end_time = parse_time(entry["startTime"]), parse_time(entry["endTime"])
            employee_id = entry.get("employeeId")
            if employee_id is not None:
                await validate_assignment(db.pool, resto_id=restaurant_id, employee_id=employee_id, role_required=department["role_category"], shift_date=shift_date, start_time=start_time, end_time=end_time)
            row = await db.pool.fetchrow(
                """INSERT INTO shifts (resto_id, employee_id, role_required, department_id, shift_date, start_time, end_time, status, is_draft)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true) RETURNING id""",
                restaurant_id, employee_id, department["role_category"], department["id"], shift_date, start_time, end_time,
                "Scheduled" if employee_id is not None else "Open",
            )
            applied.append(await serialize_shift(db.pool, row))
        except HTTPException:
            skipped += 1
    return {"applied": applied, "skippedCount": skipped}


@router.post("/api/scheduling/publish")
async def publish_shifts(payload: PublishRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can publish schedules")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(payload.weekStart))
    conditions, args = ["resto_id = $1", "shift_date >= $2", "shift_date <= $3", "is_draft = true"], [restaurant_id, monday, monday + datetime.timedelta(days=6)]
    if payload.departmentId is not None:
        args.append(payload.departmentId); conditions.append(f"department_id = ${len(args)}")
    rows = await db.pool.fetch(f"UPDATE shifts SET is_draft = false WHERE {' AND '.join(conditions)} RETURNING id", *args)
    return {"publishedCount": len(rows)}


@router.post("/api/scheduling/drop-shift")
async def drop_shift(shiftId: int, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    shift = await db.pool.fetchrow("SELECT * FROM shifts WHERE id = $1 AND resto_id = $2 AND employee_id = $3 AND is_draft = false", shiftId, restaurant_id, user["id"])
    if not shift:
        raise HTTPException(404, detail="Assigned shift not found")
    if shift["status"] == "Pending_Swap":
        raise HTTPException(409, detail="This shift is already in the swap queue")
    candidates = await db.pool.fetch(
        "SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id WHERE u.restaurant_id = $1 AND r.name = $2 AND u.department_id = $3 AND u.id <> $4 AND u.active = true",
        restaurant_id, shift["role_required"], shift["department_id"], user["id"],
    )
    eligible = []
    for candidate in candidates:
        try:
            await validate_assignment(db.pool, resto_id=restaurant_id, employee_id=candidate["id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"], exclude_shift_id=shiftId)
        except HTTPException:
            continue
        eligible.append(candidate["id"])
    if not eligible:
        return {"shiftId": shiftId, "status": "Scheduled", "matches": []}
    matches = []
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("UPDATE shifts SET status = 'Pending_Swap' WHERE id = $1", shiftId)
            for employee_id in eligible:
                request = await connection.fetchrow("INSERT INTO swap_requests (resto_id, original_shift_id, requesting_employee_id, target_employee_id, status) VALUES ($1, $2, $3, $4, 'Pending_Match') ON CONFLICT (original_shift_id, target_employee_id) DO UPDATE SET status = 'Pending_Match' RETURNING id", restaurant_id, shiftId, user["id"], employee_id)
                matches.append({"swapRequestId": request["id"], "employeeId": employee_id})
    return {"shiftId": shiftId, "status": "Pending_Swap", "matches": matches}


@router.get("/api/scheduling/eligible-shifts")
async def eligible_shifts(user: dict = Depends(require_user)):
    rows = await db.pool.fetch("SELECT sr.id AS swap_request_id, s.id FROM swap_requests sr JOIN shifts s ON s.id = sr.original_shift_id WHERE sr.target_employee_id = $1 AND sr.status = 'Pending_Match' AND s.status = 'Pending_Swap' ORDER BY s.shift_date, s.start_time", user["id"])
    result = []
    for row in rows:
        shift = await serialize_shift(db.pool, row)
        shift["swapRequestId"] = row["swap_request_id"]
        result.append(shift)
    return result


@router.post("/api/scheduling/swap-requests/{request_id}/claim")
async def claim_swap(request_id: int, user: dict = Depends(require_user)):
    """A qualified employee explicitly claims a marketplace shift for manager approval."""
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            request = await connection.fetchrow(
                "SELECT * FROM swap_requests WHERE id = $1 AND target_employee_id = $2 FOR UPDATE",
                request_id, user["id"],
            )
            if not request or request["status"] != "Pending_Match":
                raise HTTPException(404, detail="Eligible shift is no longer available")
            shift = await connection.fetchrow(
                "SELECT * FROM shifts WHERE id = $1 AND status = 'Pending_Swap' FOR UPDATE",
                request["original_shift_id"],
            )
            if not shift:
                raise HTTPException(409, detail="This shift is no longer available")
            # Recheck at claim time: availability or weekly hours may have changed.
            await validate_assignment(connection, resto_id=request["resto_id"], employee_id=user["id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"], exclude_shift_id=shift["id"])
            await connection.execute("UPDATE swap_requests SET status = CASE WHEN id = $1 THEN 'Pending_Approval' ELSE 'Rejected' END WHERE original_shift_id = $2 AND status = 'Pending_Match'", request_id, shift["id"])
    return {"id": request_id, "status": "Pending_Approval"}


@router.get("/api/scheduling/swap-requests")
async def swap_requests(user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view the approval queue")
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await db.pool.fetch("SELECT sr.id, sr.status, s.id AS shift_id, s.shift_date, s.start_time, s.end_time, requester.name AS requesting_name, target.name AS target_name FROM swap_requests sr JOIN shifts s ON s.id = sr.original_shift_id JOIN users requester ON requester.id = sr.requesting_employee_id LEFT JOIN users target ON target.id = sr.target_employee_id WHERE sr.resto_id = $1 AND sr.status = 'Pending_Approval' ORDER BY sr.created_at DESC", restaurant_id)
    return [{"id": r["id"], "status": r["status"], "shiftId": r["shift_id"], "date": r["shift_date"].isoformat(), "startTime": r["start_time"].isoformat(timespec="minutes"), "endTime": r["end_time"].isoformat(timespec="minutes"), "requestingEmployeeName": r["requesting_name"], "targetEmployeeName": r["target_name"]} for r in rows]


@router.post("/api/scheduling/swap-requests/{request_id}/decision")
async def decide_swap(request_id: int, payload: SwapDecisionRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can decide swaps")
    restaurant_id = await _restaurant_id_for(user["id"])
    request = await db.pool.fetchrow("SELECT * FROM swap_requests WHERE id = $1 AND resto_id = $2", request_id, restaurant_id)
    if not request or request["status"] != "Pending_Approval":
        raise HTTPException(404, detail="Active swap request not found")
    if not payload.approve:
        async with db.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("UPDATE swap_requests SET status = 'Rejected' WHERE id = $1", request_id)
                await release_shift_if_no_live_swaps(connection, request["original_shift_id"])
        return {"id": request_id, "status": "Rejected"}
    shift = await db.pool.fetchrow("SELECT * FROM shifts WHERE id = $1", request["original_shift_id"])
    await validate_assignment(db.pool, resto_id=restaurant_id, employee_id=request["target_employee_id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"], exclude_shift_id=shift["id"])
    async with db.pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("UPDATE shifts SET employee_id = $1, status = 'Scheduled' WHERE id = $2", request["target_employee_id"], shift["id"])
            await connection.execute("UPDATE swap_requests SET status = CASE WHEN id = $1 THEN 'Completed' ELSE 'Rejected' END WHERE original_shift_id = $2 AND status = 'Pending_Approval'", request_id, shift["id"])
    return {"id": request_id, "status": "Completed"}
