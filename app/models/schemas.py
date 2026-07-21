from typing import Optional

from pydantic import BaseModel


class WaitlistEntry(BaseModel):
    name: str
    email: str
    restaurant: str
    role: str


class LoginRequest(BaseModel):
    password: str


class UserLoginRequest(BaseModel):
    email: str
    password: str


class AutoBuildRequest(BaseModel):
    weekStart: str


class PublishRequest(BaseModel):
    weekStart: str
    departmentId: Optional[int] = None


class TemplateSaveRequest(BaseModel):
    name: str
    weekStart: str


class ShiftUpdateRequest(BaseModel):
    employeeId: Optional[int] = None
    departmentId: Optional[int] = None
    date: str
    startTime: str
    endTime: str


class ShiftCreateRequest(BaseModel):
    departmentId: int
    date: str
    startTime: str
    endTime: str
    employeeId: Optional[int] = None


class DepartmentCreateRequest(BaseModel):
    name: str
    roleCategory: str


class DepartmentUpdateRequest(BaseModel):
    name: str


class AvailabilityRequest(BaseModel):
    weeklyAvailability: dict


class SwapDecisionRequest(BaseModel):
    approve: bool


class RequirementCreateRequest(BaseModel):
    departmentId: int
    dayOfWeek: int
    startTime: str
    endTime: str
    countRequired: int
    minConfidence: Optional[int] = None
    notes: str = ""
    weekStartOverride: Optional[str] = None


class RequirementUpdateRequest(BaseModel):
    startTime: str
    endTime: str
    countRequired: int
    minConfidence: Optional[int] = None
    notes: str = ""


class GenerateShiftsRequest(BaseModel):
    weekStart: str
    departmentId: Optional[int] = None


class AnnouncementCreateRequest(BaseModel):
    title: str
    body: str
    pinned: bool = False


class EmployeeProfileUpdateRequest(BaseModel):
    departmentId: Optional[int] = None
    maxHoursPerWeek: Optional[float] = None
    minHoursPerWeek: Optional[float] = None
    preferredHoursPerWeek: Optional[float] = None
    schedulingConfidence: Optional[int] = None
    schedulingNotes: Optional[str] = None
    autoScheduleOptOut: Optional[bool] = None
