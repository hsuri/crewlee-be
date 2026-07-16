"""Scheduling rules and persistence operations.

The service deliberately owns the labour-rule checks so auto-building, swaps,
and manager edits all make the same decision.
"""
import json
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from fastapi import HTTPException

QUEBEC_WEEKLY_LIMIT = 40
MIN_REST_HOURS = 11


def parse_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="Times must use HH:MM or HH:MM:SS format")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="Date must use YYYY-MM-DD format")


def shift_bounds(shift_date: date, start: time, end: time) -> tuple[datetime, datetime]:
    begins = datetime.combine(shift_date, start)
    finishes = datetime.combine(shift_date, end)
    if finishes <= begins:  # supports shifts that run past midnight
        finishes += timedelta(days=1)
    return begins, finishes


def shift_hours(shift_date: date, start: time, end: time) -> float:
    begins, finishes = shift_bounds(shift_date, start, end)
    return (finishes - begins).total_seconds() / 3600


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def availability_allows(availability: Any, shift_date: date, start: time, end: time) -> bool:
    """Supports either {"monday": [{start, end}]} or [{day, start, end}]."""
    # asyncpg can return jsonb as an already-decoded object or as JSON text,
    # depending on connection codecs. Normalize once at this boundary.
    if isinstance(availability, str):
        try:
            availability = json.loads(availability)
        except json.JSONDecodeError:
            return False
    if not availability:
        return False
    day_name = shift_date.strftime("%A").lower()
    slots = availability.get(day_name, []) if isinstance(availability, dict) else [
        item for item in availability if isinstance(item, dict) and str(item.get("day", "")).lower() in (day_name, str(shift_date))
    ]
    shift_start, shift_end = shift_bounds(shift_date, start, end)
    for slot in slots:
        try:
            available_start, available_end = shift_bounds(
                shift_date, parse_time(slot["start"]), parse_time(slot["end"])
            )
            if available_start <= shift_start and available_end >= shift_end:
                return True
        except (KeyError, TypeError):
            continue
    return False


async def validate_assignment(
    pool, *, resto_id: int, employee_id: int, role_required: str,
    shift_date: date, start_time: time, end_time: time, exclude_shift_id: Optional[int] = None,
) -> dict:
    employee = await pool.fetchrow(
        """SELECT u.id, u.restaurant_id, u.weekly_availability, u.max_hours_per_week,
                  r.name AS role
           FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = $1""", employee_id
    )
    if not employee or employee["restaurant_id"] != resto_id:
        raise HTTPException(400, detail="Employee does not belong to this restaurant")
    if employee["role"] != role_required:
        raise HTTPException(400, detail=f"Role mismatch: this shift requires {role_required.upper()} staff")
    if not availability_allows(employee["weekly_availability"], shift_date, start_time, end_time):
        raise HTTPException(400, detail="Employee is not available for this shift window")

    monday = week_start(shift_date)
    sunday = monday + timedelta(days=6)
    existing = await pool.fetch(
        """SELECT id, shift_date, start_time, end_time FROM shifts
           WHERE employee_id = $1 AND shift_date BETWEEN $2 AND $3
             AND status IN ('Scheduled', 'Pending_Swap')""", employee_id, monday, sunday
    )
    if exclude_shift_id:
        existing = [s for s in existing if s["id"] != exclude_shift_id]
    proposed_hours = shift_hours(shift_date, start_time, end_time)
    assigned_hours = sum(shift_hours(s["shift_date"], s["start_time"], s["end_time"]) for s in existing)
    limit = min(float(employee["max_hours_per_week"] or QUEBEC_WEEKLY_LIMIT), QUEBEC_WEEKLY_LIMIT)
    # Quebec overtime protection: never schedule above the lower of profile and 40h cap.
    if assigned_hours + proposed_hours > limit + 1e-9:
        raise HTTPException(400, detail=f"Overtime limit reached: this assignment would exceed {limit:g} hours this week")

    proposed_start, proposed_end = shift_bounds(shift_date, start_time, end_time)
    for scheduled in existing:
        current_start, current_end = shift_bounds(scheduled["shift_date"], scheduled["start_time"], scheduled["end_time"])
        if proposed_start < current_end and current_start < proposed_end:
            raise HTTPException(400, detail="Employee already has an overlapping shift")
        # Minimum 11-hour rest prevents close-to-open (and any short turnaround).
        gap = (proposed_start - current_end).total_seconds() / 3600 if current_end <= proposed_start else (current_start - proposed_end).total_seconds() / 3600
        if gap < MIN_REST_HOURS:
            raise HTTPException(400, detail="Quebec rest-period rule requires at least 11 hours between shifts")
    return dict(employee)


async def release_shift_if_no_live_swaps(connection, shift_id: int) -> None:
    """Revert a shift to 'Scheduled' once no swap_requests remain live for it."""
    live = await connection.fetchval(
        """SELECT 1 FROM swap_requests
           WHERE original_shift_id = $1 AND status IN ('Pending_Match', 'Pending_Approval') LIMIT 1""",
        shift_id,
    )
    if not live:
        await connection.execute(
            "UPDATE shifts SET status = 'Scheduled' WHERE id = $1 AND status = 'Pending_Swap'", shift_id
        )


async def serialize_shift(pool, shift) -> dict:
    row = await pool.fetchrow(
        """SELECT s.*, u.name AS employee_name FROM shifts s
           LEFT JOIN users u ON u.id = s.employee_id WHERE s.id = $1""", shift["id"]
    )
    return {
        "id": row["id"], "restoId": row["resto_id"], "employeeId": row["employee_id"],
        "employeeName": row["employee_name"], "roleRequired": row["role_required"],
        "date": row["shift_date"].isoformat(), "startTime": row["start_time"].isoformat(timespec="minutes"),
        "endTime": row["end_time"].isoformat(timespec="minutes"), "status": row["status"],
    }
