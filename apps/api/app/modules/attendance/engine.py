"""
Tính late / early / ot_minutes từ punch thô + lịch công ty (04§4.3, 3.3).
Ca 08:00–17:00, trừ 1 giờ trưa; dung sai trễ/sớm theo giây; OT sau giờ ca.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from app.modules.attendance.ot_split import OtSplitPolicy, default_ot_split_policy, split_weekday_ot_minutes
from app.modules.attendance.punch_dedupe import dedupe_punch_times

VN_TZ = timezone(timedelta(hours=7))


@dataclass(frozen=True)
class Schedule:
    work_weekdays: list[int]  # 1=Mon..7=Sun
    morning_start: time
    morning_end: time
    afternoon_start: time
    afternoon_end: time
    grace_late_minutes: int
    holiday_dates: set[date]
    grace_late_seconds: int = 0
    grace_early_seconds: int = 0


@dataclass
class DayCalcResult:
    work_date: date
    first_in: datetime | None
    last_out: datetime | None
    worked_hours: Decimal
    late_minutes: int
    early_minutes: int
    ot_minutes: int
    ot_on_books_minutes: int
    ot_external_minutes: int
    ot_type: str | None
    punch_count: int
    is_workday: bool


def to_vn(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=VN_TZ)
    return dt.astimezone(VN_TZ)


def combine_vn(d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=VN_TZ)


def is_company_workday(d: date, schedule: Schedule) -> bool:
    if d in schedule.holiday_dates:
        return False
    return d.isoweekday() in set(schedule.work_weekdays)


def _seconds_to_minutes_up(seconds: float) -> int:
    """Dung sai 0 giây — trễ/sớm 1 giây cũng tính ít nhất 1 phút."""
    if seconds <= 0:
        return 0
    return int((seconds + 59) // 60)


def _assign_single_punch(
    punch: datetime,
    schedule: Schedule,
    work_date: date,
) -> tuple[datetime | None, datetime | None]:
    """
    Một lần bấm sau dedupe — gán vào cột vào hoặc ra (HR/AI thấy, không bỏ trống cả hai).

    Từ 13:00 trở đi coi là giờ ra; sáng coi là giờ vào.
    """
    split = combine_vn(work_date, schedule.afternoon_start)
    if punch >= split:
        return None, punch
    return punch, None


def _resolve_in_out_from_times(
    times: list[datetime],
    schedule: Schedule,
    work_date: date,
) -> tuple[datetime | None, datetime | None]:
    """
    Gán giờ vào / ra sau dedupe.

    - Trước giờ vào ca: mọi lần bấm = thử vào (giữ sớm nhất).
    - Từ giờ vào ca đến trước nghỉ trưa (morning_end): vẫn coi là vào, không coi mốc sau là ra.
    - Từ nghỉ trưa trở đi: mốc muộn nhất = ra (nhiều lần bấm chiều gom một).
    """
    shift_start = combine_vn(work_date, schedule.morning_start)
    depart_after = combine_vn(work_date, schedule.morning_end)

    pre_shift = [t for t in times if t < shift_start]
    rest = [t for t in times if t >= shift_start]

    first_in = min(pre_shift) if pre_shift else None

    arrivals = [t for t in rest if t < depart_after]
    departures = [t for t in rest if t >= depart_after]

    if arrivals:
        first_in = min(arrivals) if first_in is None else min(first_in, min(arrivals))

    if not departures:
        return first_in, None

    last_out = departures[-1]
    return first_in, last_out


def _calc_partial_workday(
    *,
    first_in: datetime | None,
    last_out: datetime | None,
    work_date: date,
    schedule: Schedule,
    split_policy: OtSplitPolicy,
) -> tuple[int, int, int, int, int, Decimal, str | None]:
    """Late / early / OT / worked khi thiếu vào hoặc thiếu ra."""
    shift_start = combine_vn(work_date, schedule.morning_start) + timedelta(
        seconds=schedule.grace_late_seconds,
        minutes=schedule.grace_late_minutes,
    )
    shift_end = combine_vn(work_date, schedule.afternoon_end)
    early_deadline = shift_end - timedelta(seconds=schedule.grace_early_seconds)
    ot_qualify_after = shift_end + timedelta(minutes=split_policy.ot_grace_minutes)

    late = 0
    early = 0
    ot_on_books = 0
    ot_external = 0
    ot_type: str | None = None

    if first_in is not None and last_out is None:
        if first_in > shift_start:
            late = _seconds_to_minutes_up((first_in - shift_start).total_seconds())
        worked = Decimal("0")
    elif last_out is not None and first_in is None:
        if last_out < early_deadline:
            early = _seconds_to_minutes_up((early_deadline - last_out).total_seconds())
        if last_out > ot_qualify_after:
            ot_on_books, ot_external = split_weekday_ot_minutes(
                last_out, work_date, shift_end, ot_qualify_after, split_policy
            )
            ot = ot_on_books + ot_external
            ot_type = "weekday" if ot > 0 else None
        worked = Decimal("0")
    else:
        worked = Decimal("0")

    ot = ot_on_books + ot_external
    return late, early, ot, ot_on_books, ot_external, worked, ot_type


def _shift_worked_hours(
    first_in: datetime,
    last_out: datetime,
    schedule: Schedule,
    work_date: date,
) -> Decimal:
    """Giờ công trong khung ca (08:00–17:00), trừ nghỉ trưa — không cộng OT vào công."""
    shift_start = combine_vn(work_date, schedule.morning_start)
    shift_end = combine_vn(work_date, schedule.afternoon_end)
    seg_in = max(first_in, shift_start)
    seg_out = min(last_out, shift_end)
    if seg_out <= seg_in:
        return Decimal("0")
    total = Decimal(str((seg_out - seg_in).total_seconds() / 3600))
    lunch_start = combine_vn(work_date, schedule.morning_end)
    lunch_end = combine_vn(work_date, schedule.afternoon_start)
    overlap_start = max(seg_in, lunch_start)
    overlap_end = min(seg_out, lunch_end)
    if overlap_end > overlap_start:
        lunch_h = Decimal(str((overlap_end - overlap_start).total_seconds() / 3600))
        total -= lunch_h
    if total < 0:
        total = Decimal("0")
    return total.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_day(
    punches: Sequence[datetime],
    work_date: date,
    schedule: Schedule,
    *,
    punch_dedupe_window_seconds: int = 60,
    ot_split: OtSplitPolicy | None = None,
) -> DayCalcResult:
    split_policy = ot_split or default_ot_split_policy()
    times = dedupe_punch_times(punches, window_seconds=punch_dedupe_window_seconds)
    if not times:
        return DayCalcResult(
            work_date=work_date,
            first_in=None,
            last_out=None,
            worked_hours=Decimal("0"),
            late_minutes=0,
            early_minutes=0,
            ot_minutes=0,
            ot_on_books_minutes=0,
            ot_external_minutes=0,
            ot_type=None,
            punch_count=0,
            is_workday=is_company_workday(work_date, schedule),
        )

    # Một mốc sau dedupe — ghi nhận giờ vào HOẶC giờ ra (HR/AI rà soát).
    if len(times) == 1:
        punch = times[0]
        workday = is_company_workday(work_date, schedule)
        first_in, last_out = _assign_single_punch(punch, schedule, work_date)
        late = early = ot = ot_on_books = ot_external = 0
        ot_type: str | None = None
        worked = Decimal("0")
        if workday:
            late, early, ot, ot_on_books, ot_external, worked, ot_type = _calc_partial_workday(
                first_in=first_in,
                last_out=last_out,
                work_date=work_date,
                schedule=schedule,
                split_policy=split_policy,
            )
        return DayCalcResult(
            work_date=work_date,
            first_in=first_in,
            last_out=last_out,
            worked_hours=worked,
            late_minutes=late,
            early_minutes=early,
            ot_minutes=ot,
            ot_on_books_minutes=ot_on_books,
            ot_external_minutes=ot_external,
            ot_type=ot_type,
            punch_count=1,
            is_workday=workday,
        )

    first_in, last_out = _resolve_in_out_from_times(times, schedule, work_date)
    workday = is_company_workday(work_date, schedule)
    late = 0
    early = 0
    ot = 0
    ot_on_books = 0
    ot_external = 0
    ot_type = None

    if workday:
        if first_in is not None and last_out is not None:
            shift_start = combine_vn(work_date, schedule.morning_start) + timedelta(
                seconds=schedule.grace_late_seconds,
                minutes=schedule.grace_late_minutes,
            )
            shift_end = combine_vn(work_date, schedule.afternoon_end)
            early_deadline = shift_end - timedelta(seconds=schedule.grace_early_seconds)
            ot_qualify_after = shift_end + timedelta(minutes=split_policy.ot_grace_minutes)

            if first_in > shift_start:
                late = _seconds_to_minutes_up((first_in - shift_start).total_seconds())
            if last_out < early_deadline:
                early = _seconds_to_minutes_up((early_deadline - last_out).total_seconds())
            if last_out > ot_qualify_after:
                ot_on_books, ot_external = split_weekday_ot_minutes(
                    last_out, work_date, shift_end, ot_qualify_after, split_policy
                )
                ot = ot_on_books + ot_external
                ot_type = "weekday" if ot > 0 else None
            worked = _shift_worked_hours(first_in, last_out, schedule, work_date)
        else:
            late, early, ot, ot_on_books, ot_external, worked, ot_type = _calc_partial_workday(
                first_in=first_in,
                last_out=last_out,
                work_date=work_date,
                schedule=schedule,
                split_policy=split_policy,
            )
    else:
        # Ngày nghỉ (lễ/cuối tuần): toàn bộ thời gian có mặt = OT, không áp
        # logic vào/ra theo khung ca sáng/chiều như ngày công. Dùng trực tiếp
        # mốc bấm đầu–cuối (đã dedupe, đã sort) để không bỏ sót ca chỉ làm buổi
        # sáng (vd. 09:00–11:00) hay chỉ buổi chiều trên ngày nghỉ.
        first_in = times[0]
        last_out = times[-1]
        ot = int((last_out - first_in).total_seconds() // 60)
        if ot < 0:
            ot = 0
        ot_external = ot
        ot_on_books = 0
        ot_type = "holiday" if work_date in schedule.holiday_dates else "weekend"
        worked = Decimal("0")

    return DayCalcResult(
        work_date=work_date,
        first_in=first_in,
        last_out=last_out,
        worked_hours=worked,
        late_minutes=late,
        early_minutes=early,
        ot_minutes=ot,
        ot_on_books_minutes=ot_on_books,
        ot_external_minutes=ot_external,
        ot_type=ot_type,
        punch_count=len(times),
        is_workday=workday,
    )
