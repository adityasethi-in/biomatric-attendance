from pydantic import BaseModel


class RegisterResponse(BaseModel):
    id: int
    student_code: str
    full_name: str


class AttendanceResponse(BaseModel):
    matched: bool
    student_id: int | None = None
    name: str | None = None
    distance: float | None = None
    attendance_id: int | None = None
    marked_at: str | None = None
    reason: str | None = None