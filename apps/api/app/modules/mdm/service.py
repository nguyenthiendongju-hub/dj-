"""MDM service — departments + employees."""

from __future__ import annotations

import mimetypes
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.modules.audit.service import write_audit
from app.modules.core.models import User
from app.modules.mdm.models import (
    Department,
    Employee,
    EmployeeAssignment,
    EmployeeDocument,
    EmployeeEducation,
    EmployeeExperience,
    EmployeeFamilyMember,
    EmployeeHealthCheck,
    EmployeeResignation,
    EmployeeSalaryHistory,
    EmployeeViolation,
    LabourContract,
    LookupValue,
    Position,
    Team,
)
from app.modules.mdm import employment_status as es
from app.modules.mdm import labour_contract_flow as lcf
from app.modules.mdm.lookup_seed import (
    ADMIN_UNITS_BIRTH_COUNT,
    ADMIN_UNITS_ID_ISSUE_COUNT,
    LOOKUP_VALUES_SEED,
)
from app.modules.payroll.models import EmployeeAllowanceAssignment, PayComponent
from app.modules.mdm.schemas import (
    BulkSalaryRaisePreview,
    BulkSalaryRaiseRequest,
    BulkSalaryRaiseResult,
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    EmployeeAssignmentOut,
    EmployeeCreate,
    EmployeeDocumentCreate,
    EmployeeDocumentOut,
    EmployeeEducationCreate,
    EmployeeEducationOut,
    EmployeeEducationUpdate,
    EmployeeExperienceCreate,
    EmployeeExperienceOut,
    EmployeeExperienceUpdate,
    EmployeeFamilyMemberCreate,
    EmployeeHealthCheckCreate,
    EmployeeHealthCheckOut,
    EmployeeHealthCheckUpdate,
    EmployeeFamilyMemberOut,
    EmployeeFamilyMemberUpdate,
    EmployeeOut,
    EmployeeRehireOut,
    EmployeeRehireRequest,
    EmployeeResignationCreate,
    EmployeeResignationOut,
    EmployeeResignationUpdate,
    EmployeeSalaryHistoryOut,
    EmployeeUpdate,
    EmployeeViolationBoardItem,
    EmployeeViolationCreate,
    EmployeeViolationOut,
    HrMovementOut,
    LabourContractCreate,
    LabourContractOut,
    LabourContractRenewPreview,
    LabourContractRenewRequest,
    LabourContractUpdate,
    LookupValueOut,
    RESIGN_TYPE_CODES,
    ResignationPreviewOut,
    TaxDependentsOut,
    TeamOut,
    TransferTeamPreview,
    TransferTeamRequest,
    TransferTeamResult,
    TransferTeamSkipped,
    UnlockResetPasswordOut,
)
from app.core.security import hash_password
from app.modules.payroll.money import D, money_vnd
from app.modules.worker.service import default_worker_reset_password

RAISE_STATUSES = ("active", "probation")

SALARY_HISTORY_FIELDS = frozenset({"contract_salary", "probation_salary"})
SALARY_FIELD_LABELS = {
    "contract_salary": "Lương HĐ",
    "probation_salary": "Lương thử việc",
}


def _fmt_vnd_display(amount: Decimal) -> str:
    return f"{int(money_vnd(amount)):,}".replace(",", ".") + " đ"


def _record_salary_history(
    db: Session,
    *,
    employee_id: UUID,
    field_code: str,
    effective_from: date,
    old_value: Decimal,
    new_value: Decimal,
    decision_no: str | None = None,
    approved_by: UUID | None = None,
    note: str | None = None,
) -> None:
    """Ghi một dòng lịch sử lương — bỏ qua nếu không đổi (21§21.3)."""
    old_v = money_vnd(old_value or Decimal("0"))
    new_v = money_vnd(new_value or Decimal("0"))
    if old_v == new_v:
        return
    db.add(
        EmployeeSalaryHistory(
            employee_id=employee_id,
            field_code=field_code,
            effective_from=effective_from,
            old_value=old_v,
            new_value=new_v,
            decision_no=decision_no,
            approved_by=approved_by,
            note=note,
        )
    )


PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_PHOTO_BYTES = 5 * 1024 * 1024

VIOLATION_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
MAX_VIOLATION_BYTES = 10 * 1024 * 1024

DOCUMENT_TYPES = VIOLATION_TYPES
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
ALLOWED_DOC_TYPE_CODES = frozenset(
    {"contract", "id_card", "resume", "certificate", "other"}
)

CATEGORIES = {"direct", "prod_indirect", "admin_indirect"}


@lru_cache(maxsize=1)
def _photo_dir() -> Path:
    root = Path(get_settings().upload_dir).expanduser().resolve() / "employee_photos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _employee_photo_file(emp: Employee) -> Path | None:
    """Tìm file ảnh NV trên đĩa.

    photo_path trong DB có thể là tên file (chuẩn) hoặc path tuyệt đối Windows
    do script import chạy trên host — API Docker/Linux phải resolve qua upload_dir.
    """
    if not emp.photo_path:
        return None
    stored = Path(emp.photo_path)
    root = _photo_dir()
    for candidate in (
        stored,
        root / stored.name,
        root / f"{emp.id}.jpg",
        root / f"{emp.id}.jpeg",
        root / f"{emp.id}.png",
    ):
        if candidate.is_file():
            return candidate
    return None


def _account_fields(emp: Employee, user: User | None) -> dict:
    if emp.status == "resigned":
        return {
            "account_status": "resigned",
            "account_status_label": "Nghỉ việc",
            "is_locked": bool(user.is_locked) if user else False,
            "failed_attempts": int(user.failed_attempts) if user else 0,
            "has_worker_account": user is not None,
        }
    if user is not None and (user.is_locked or not user.is_active):
        return {
            "account_status": "locked",
            "account_status_label": "Bị khóa",
            "is_locked": True,
            "failed_attempts": int(user.failed_attempts or 0),
            "has_worker_account": True,
        }
    return {
        "account_status": "active",
        "account_status_label": "Hoạt động",
        "is_locked": False,
        "failed_attempts": int(user.failed_attempts) if user else 0,
        "has_worker_account": user is not None,
    }


def _worker_user_for_employee(db: Session, emp: Employee) -> User | None:
    if emp.id is not None:
        by_id = (
            db.query(User)
            .filter(User.employee_id == emp.id, User.role == "worker")
            .first()
        )
        if by_id is not None:
            return by_id
    return (
        db.query(User)
        .filter(User.username == emp.employee_code, User.role == "worker")
        .first()
    )


def _sync_worker_on_status(db: Session, emp: Employee) -> None:
    """Nghỉ việc → vô hiệu hóa TK công nhân; còn lại thì bật lại nếu chưa bị khóa."""
    user = _worker_user_for_employee(db, emp)
    if user is None:
        return
    if emp.status == "resigned":
        user.is_active = False
        user.is_locked = True
    elif not user.is_locked:
        user.is_active = True
    user.full_name = emp.full_name
    user.employee_id = emp.id


def _seniority_label(join_date: date | None, resign_date: date | None) -> str | None:
    """Thâm niên 'X năm Y tháng' tính tới hôm nay, hoặc tới ngày nghỉ nếu đã thôi việc."""
    if join_date is None:
        return None
    end = resign_date if resign_date is not None else date.today()
    if end < join_date:
        return None
    months_total = (end.year - join_date.year) * 12 + (end.month - join_date.month)
    if end.day < join_date.day:
        months_total -= 1
    months_total = max(0, months_total)
    years, months = divmod(months_total, 12)
    if years == 0 and months == 0:
        return "Dưới 1 tháng"
    if years == 0:
        return f"{months} tháng"
    if months == 0:
        return f"{years} năm"
    return f"{years} năm {months} tháng"


def _active_contract_types(db: Session, emp_ids: list[UUID]) -> dict[UUID, str]:
    if not emp_ids:
        return {}
    rows = (
        db.query(LabourContract)
        .filter(
            LabourContract.employee_id.in_(emp_ids),
            LabourContract.status == "active",
        )
        .order_by(LabourContract.start_date.desc())
        .all()
    )
    out: dict[UUID, str] = {}
    for row in rows:
        if row.employee_id not in out:
            out[row.employee_id] = row.contract_type_code
    return out


def _contract_type_label_for_employee(emp: Employee, active_type: str | None) -> str:
    if active_type:
        return lcf.contract_type_label(active_type)
    return {
        "probation": "Thử việc",
        "maternity": "Thai sản",
        "suspended": "Tạm ngưng",
        "resigned": "Đã nghỉ",
    }.get(emp.status, "Chính thức")


def _allowance_totals_by_employee(db: Session, emp_ids: list[UUID]) -> dict[UUID, Decimal]:
    if not emp_ids:
        return {}
    rows = (
        db.query(
            EmployeeAllowanceAssignment.employee_id,
            EmployeeAllowanceAssignment.amount,
            PayComponent.default_amount,
        )
        .join(PayComponent, PayComponent.id == EmployeeAllowanceAssignment.allowance_type_id)
        .filter(
            EmployeeAllowanceAssignment.employee_id.in_(emp_ids),
            PayComponent.is_active.is_(True),
        )
        .all()
    )
    totals: dict[UUID, Decimal] = {}
    for emp_id, amt, default_amt in rows:
        piece = amt if amt is not None else (default_amt or Decimal("0"))
        totals[emp_id] = totals.get(emp_id, Decimal("0")) + Decimal(str(piece))
    return totals


def employee_to_out(
    emp: Employee,
    user: User | None = None,
    *,
    allowance_total: Decimal | None = None,
    active_contract_type: str | None = None,
    effective_status: str | None = None,
    resolve_photo_on_disk: bool = True,
) -> EmployeeOut:
    dept = emp.department
    team = emp.team
    if resolve_photo_on_disk:
        photo_file = _employee_photo_file(emp)
        has_photo = photo_file is not None
    else:
        has_photo = bool(emp.photo_path)
    acct = _account_fields(emp, user)
    al_total = allowance_total if allowance_total is not None else Decimal("0")
    total_salary = Decimal(str(emp.contract_salary)) + al_total
    eff = effective_status or es.effective_employment_status(
        emp, active_contract_type=active_contract_type
    )
    return EmployeeOut(
        id=emp.id,
        employee_code=emp.employee_code,
        full_name=emp.full_name,
        gender=emp.gender,
        birth_date=emp.birth_date,
        birth_place_code=emp.birth_place_code,
        nationality_code=emp.nationality_code,
        ethnicity_code=emp.ethnicity_code,
        religion_code=emp.religion_code,
        marital_status=emp.marital_status,
        children_count=int(emp.children_count or 0),
        education_code=emp.education_code,
        id_number=emp.id_number,
        id_issue_date=emp.id_issue_date,
        id_issue_place_code=emp.id_issue_place_code,
        permanent_address=emp.permanent_address,
        temporary_address=emp.temporary_address,
        urgent_contact=emp.urgent_contact,
        si_book_no=emp.si_book_no,
        bank_account=emp.bank_account,
        pay_channel=emp.pay_channel,
        department_id=emp.department_id,
        department_code=dept.code if dept else None,
        department_name=dept.name if dept else None,
        team_id=emp.team_id,
        team_code=team.code if team else None,
        team_name=team.name if team else None,
        position_code=emp.position_code,
        position_title=emp.position_title,
        seniority_label=_seniority_label(emp.join_date, emp.resign_date),
        contract_type_label=_contract_type_label_for_employee(emp, active_contract_type),
        join_date=emp.join_date,
        contract_signed_at=emp.contract_signed_at,
        probation_salary=emp.probation_salary,
        contract_salary=emp.contract_salary,
        allowance_total=al_total,
        total_salary=total_salary,
        si_base_override=emp.si_base_override,
        si_enrolled=emp.si_enrolled,
        pit_enrolled=emp.pit_enrolled,
        tax_dependent_count=emp.tax_dependent_count,
        union_fee_override=emp.union_fee_override,
        status=emp.status,
        effective_status=eff,
        status_label=es.status_label(eff),
        resign_date=emp.resign_date,
        phone=emp.phone,
        has_photo=has_photo,
        photo_url=f"/api/employees/{emp.id}/photo" if has_photo else None,
        **acct,
    )


def list_departments(db: Session) -> list[DepartmentOut]:
    rows = db.query(Department).order_by(Department.code.asc()).all()
    return [DepartmentOut.model_validate(r) for r in rows]


def list_positions(db: Session, *, active_only: bool = True) -> list["PositionOut"]:
    from app.modules.mdm.schemas import PositionOut

    q = db.query(Position).order_by(Position.sort_order.asc(), Position.code.asc())
    if active_only:
        q = q.filter(Position.is_active.is_(True))
    return [PositionOut.model_validate(r) for r in q.all()]


def create_department(db: Session, body: DepartmentCreate) -> DepartmentOut:
    code = body.code.strip().upper()
    if db.query(Department).filter(Department.code == code).first():
        raise HTTPException(status_code=400, detail=f"Trợ Lý AI: mã bộ phận '{code}' đã tồn tại.")
    cat = body.category if body.category in CATEGORIES else "direct"
    row = Department(
        code=code,
        name=body.name.strip(),
        category=cat,
        mitapro_names=list(body.mitapro_names or []),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return DepartmentOut.model_validate(row)


def update_department(db: Session, dept_id: UUID, body: DepartmentUpdate) -> DepartmentOut:
    row = db.get(Department, dept_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bộ phận.")
    if body.name is not None:
        row.name = body.name.strip()
    if body.category is not None:
        if body.category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Trợ Lý AI: category không hợp lệ.")
        row.category = body.category
    if body.mitapro_names is not None:
        row.mitapro_names = list(body.mitapro_names)
    db.commit()
    db.refresh(row)
    return DepartmentOut.model_validate(row)


def delete_department(db: Session, dept_id: UUID) -> dict[str, str]:
    row = db.get(Department, dept_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bộ phận.")
    in_use = (
        db.query(Employee)
        .filter(Employee.department_id == dept_id, Employee.deleted_at.is_(None))
        .count()
    )
    if in_use:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trợ Lý AI: bộ phận «{row.code}» còn {in_use} nhân viên — "
                "hãy chuyển NV sang bộ phận khác trước khi xóa."
            ),
        )
    code = row.code
    db.delete(row)
    db.commit()
    return {"detail": f"Trợ Lý AI: đã xóa bộ phận {code}."}


def get_or_create_department_by_code(db: Session, code: str, name: str | None = None) -> Department:
    """Chỉ dùng import Excel / seed — không dùng cho CRUD NV tay."""
    code_u = code.strip().upper()
    row = db.query(Department).filter(Department.code == code_u).first()
    if row:
        return row
    row = Department(
        code=code_u,
        name=(name or code_u).strip(),
        category="direct",
        mitapro_names=[],
    )
    db.add(row)
    db.flush()
    return row


def get_department_by_code(db: Session, code: str) -> Department:
    code_u = code.strip().upper()
    row = db.query(Department).filter(Department.code == code_u).first()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trợ Lý AI: mã bộ phận «{code_u}» chưa có trong danh mục. "
                "Chỉ Admin thêm bộ phận tại Cấu Hình."
            ),
        )
    return row


def resolve_employee_team(
    db: Session,
    team_id: UUID | None,
    team_code: str | None,
    department_code: str | None,
    *,
    required: bool,
) -> Team | None:
    """Phân giải Tổ cho NV — bộ phận suy ra qua team.department_id, không nhận thẳng.

    team_code KHÔNG duy nhất toàn hệ thống (trùng giữa các bộ phận, 21§21.2) nên khi tạo/
    import bằng mã phải kèm department_code để phân giải đúng; nếu bỏ trống mà mã tổ trùng
    ở nhiều bộ phận thì báo lỗi rõ, không tự chọn đại.
    """
    if team_id is not None:
        team = db.get(Team, team_id)
        if team is None:
            raise HTTPException(status_code=400, detail="Trợ Lý AI: team_id không tồn tại.")
        return team
    if team_code:
        code_u = team_code.strip()
        query = db.query(Team).filter(Team.code == code_u)
        if department_code:
            dept = get_department_by_code(db, department_code)
            query = query.filter(Team.department_id == dept.id)
        matches = query.all()
        if not matches:
            raise HTTPException(
                status_code=400, detail=f"Trợ Lý AI: không tìm thấy tổ có mã '{code_u}'."
            )
        if len(matches) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Trợ Lý AI: mã tổ '{code_u}' trùng ở nhiều bộ phận — "
                    "cần kèm department_code để chọn đúng."
                ),
            )
        return matches[0]
    if required:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: cần chọn Tổ cho nhân viên (team_id hoặc team_code) — "
            "mọi NV phải thuộc về một tổ (23§).",
        )
    return None


def list_teams(db: Session, *, active_only: bool = True) -> list[TeamOut]:
    """Danh mục Tổ cho bộ lọc 'Bộ phận › Tổ' (hạng mục 1.4) — tên tổ có thể trùng giữa bộ phận."""
    query = db.query(Team).options(joinedload(Team.department))
    if active_only:
        today = date.today()
        query = query.filter(or_(Team.effective_to.is_(None), Team.effective_to >= today))
    rows = query.order_by(Team.department_id.asc(), Team.code.asc()).all()
    out: list[TeamOut] = []
    for t in rows:
        dept = t.department
        out.append(
            TeamOut(
                id=t.id,
                code=t.code,
                name=t.name,
                name_local=t.name_local,
                department_id=t.department_id,
                department_code=dept.code if dept else None,
                department_name=dept.name if dept else None,
                is_active=t.is_active,
            )
        )
    return out


def _sync_administrative_units_lookup(db: Session) -> None:
    """Đồng bộ nơi sinh / nơi cấp CCCD theo 34 đơn vị (Chủ chốt 2026-08-11).

    DB cũ seed 63 tỉnh: vô hiệu hóa mã thừa, cập nhật tên 001–034 (và 035 = Cục CS).
    """
    targets = {
        "birth_place": ADMIN_UNITS_BIRTH_COUNT,
        "id_issue_place": ADMIN_UNITS_ID_ISSUE_COUNT,
    }
    changed = False
    for group_code, expected_count in targets.items():
        active = (
            db.query(LookupValue)
            .filter(LookupValue.group_code == group_code, LookupValue.is_active.is_(True))
            .count()
        )
        if active == expected_count:
            first = (
                db.query(LookupValue)
                .filter(
                    LookupValue.group_code == group_code,
                    LookupValue.code == f"{group_code.upper()}001",
                    LookupValue.is_active.is_(True),
                )
                .one_or_none()
            )
            if first and first.name == "Hà Nội":
                continue

        for row in db.query(LookupValue).filter(LookupValue.group_code == group_code).all():
            row.is_active = False
            changed = True

        names = [n for g, _c, n, _o in LOOKUP_VALUES_SEED if g == group_code]
        for i, name in enumerate(names):
            code = f"{group_code.upper()}{i + 1:03d}"
            row = (
                db.query(LookupValue)
                .filter(LookupValue.group_code == group_code, LookupValue.code == code)
                .one_or_none()
            )
            if row is None:
                db.add(
                    LookupValue(
                        group_code=group_code,
                        code=code,
                        name=name,
                        sort_order=i,
                        is_active=True,
                    )
                )
                changed = True
            else:
                row.name = name
                row.sort_order = i
                row.is_active = True
                changed = True

    if changed:
        db.commit()


def seed_lookup_values(db: Session) -> None:
    """Nạp danh mục phẳng nếu còn thiếu (21§21.4, hạng mục 2.1) — không ghi đè dòng đã có,
    tự sửa (sync_portal_tabs từng ghi đè nhầm tên do Admin đã đổi — không lặp lại lỗi đó).
    """
    existing = {(r.group_code, r.code) for r in db.query(LookupValue.group_code, LookupValue.code)}
    added = 0
    for group_code, code, name, sort_order in LOOKUP_VALUES_SEED:
        if (group_code, code) in existing:
            continue
        db.add(LookupValue(group_code=group_code, code=code, name=name, sort_order=sort_order))
        added += 1
    if added:
        db.commit()
    _sync_administrative_units_lookup(db)


def list_lookup_groups(db: Session) -> list[str]:
    """Danh sách các group_code hiện có — cho Admin (2.8) hiển thị danh mục nào đang có."""
    seed_lookup_values(db)
    rows = db.query(LookupValue.group_code).distinct().order_by(LookupValue.group_code.asc()).all()
    return [r[0] for r in rows]


def list_lookup_values(db: Session, group_code: str | None = None) -> list[LookupValueOut]:
    """Đọc danh mục phẳng — dân tộc, tôn giáo, quốc tịch, nơi sinh, nơi cấp CCCD, trình độ.

    Tự seed nếu bảng còn trống (giống seed_leave_types) — không cần bước khởi tạo riêng.
    """
    seed_lookup_values(db)
    query = db.query(LookupValue).filter(LookupValue.is_active.is_(True))
    if group_code:
        query = query.filter(LookupValue.group_code == group_code.strip())
    rows = query.order_by(LookupValue.group_code.asc(), LookupValue.sort_order.asc()).all()
    return [LookupValueOut.model_validate(r) for r in rows]


def list_employees(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    department_id: UUID | None = None,
    team_id: UUID | None = None,
) -> list[EmployeeOut]:
    query = (
        db.query(Employee)
        .options(joinedload(Employee.team).joinedload(Team.department))
        .filter(Employee.deleted_at.is_(None))
        .order_by(Employee.employee_code.asc())
    )
    if status == "resigned":
        query = query.filter(Employee.status == "resigned")
    elif status in ("active", "probation", "maternity"):
        query = query.filter(Employee.status != "resigned")
    elif status:
        query = query.filter(Employee.status == status)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    if team_id:
        query = query.filter(Employee.team_id == team_id)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(Employee.employee_code).like(like),
                func.lower(Employee.full_name).like(like),
            )
        )
    rows = query.all()
    if not rows:
        return []
    ids = [e.id for e in rows]
    codes = [e.employee_code for e in rows]
    users = (
        db.query(User)
        .filter(
            User.role == "worker",
            or_(User.employee_id.in_(ids), User.username.in_(codes)),
        )
        .all()
    )
    by_emp_id = {u.employee_id: u for u in users if u.employee_id is not None}
    by_code = {u.username: u for u in users}
    allowance_totals = _allowance_totals_by_employee(db, ids)
    active_types = _active_contract_types(db, ids)
    maternity_ids = es.maternity_employee_ids(db)
    out: list[EmployeeOut] = []
    for e in rows:
        user = by_emp_id.get(e.id) or by_code.get(e.employee_code)
        act_type = active_types.get(e.id)
        eff = es.effective_employment_status(
            e,
            active_contract_type=act_type,
            on_maternity_leave=e.id in maternity_ids,
        )
        out.append(
            employee_to_out(
                e,
                user,
                allowance_total=allowance_totals.get(e.id, Decimal("0")),
                active_contract_type=act_type,
                effective_status=eff,
                resolve_photo_on_disk=False,
            )
        )
    if status in ("active", "probation", "maternity"):
        if not (q and q.strip()):
            out = [o for o in out if o.effective_status == status]
    return out


def get_employee(db: Session, emp_id: UUID) -> EmployeeOut:
    emp = (
        db.query(Employee)
        .options(joinedload(Employee.team).joinedload(Team.department))
        .filter(Employee.id == emp_id, Employee.deleted_at.is_(None))
        .first()
    )
    if emp is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy nhân viên.")
    al_total = _allowance_totals_by_employee(db, [emp.id]).get(emp.id, Decimal("0"))
    active = lcf.get_active_contract(db, emp.id)
    act_type = active.contract_type_code if active else None
    maternity_ids = es.maternity_employee_ids(db)
    eff = es.effective_employment_status(
        emp,
        active_contract_type=act_type,
        on_maternity_leave=emp.id in maternity_ids,
    )
    return employee_to_out(
        emp,
        _worker_user_for_employee(db, emp),
        allowance_total=al_total,
        active_contract_type=act_type,
        effective_status=eff,
    )


def create_employee(db: Session, body: EmployeeCreate) -> EmployeeOut:
    from app.modules.mdm import employee_validation as ev

    issues = ev.validate_employee_create(db, body)
    ev.raise_on_errors(issues)

    code = body.employee_code.strip()
    team = resolve_employee_team(
        db, body.team_id, body.team_code, body.department_code, required=True
    )
    position_code = body.position_code
    position_title = body.position_title
    if position_code:
        pos = db.get(Position, position_code)
        if pos is None:
            raise HTTPException(
                status_code=400, detail=f"Trợ Lý AI: không tìm thấy chức vụ '{position_code}'."
            )
        position_title = pos.name

    emp = Employee(
        employee_code=code,
        full_name=body.full_name.strip(),
        gender=body.gender,
        birth_date=body.birth_date,
        birth_place_code=body.birth_place_code,
        nationality_code=body.nationality_code,
        ethnicity_code=body.ethnicity_code,
        religion_code=body.religion_code,
        marital_status=body.marital_status,
        children_count=max(0, int(body.children_count or 0)),
        education_code=body.education_code,
        id_number=body.id_number,
        id_issue_date=body.id_issue_date,
        id_issue_place_code=body.id_issue_place_code,
        permanent_address=body.permanent_address,
        temporary_address=body.temporary_address,
        urgent_contact=body.urgent_contact,
        si_book_no=body.si_book_no,
        bank_account=body.bank_account,
        pay_channel=body.pay_channel,
        team_id=team.id if team else None,
        position_code=position_code,
        position_title=position_title,
        join_date=body.join_date,
        contract_signed_at=body.contract_signed_at,
        probation_salary=body.probation_salary or Decimal("0"),
        contract_salary=body.contract_salary or Decimal("0"),
        si_base_override=body.si_base_override,
        si_enrolled=body.si_enrolled,
        pit_enrolled=body.pit_enrolled,
        tax_dependent_count=0,
        union_fee_override=body.union_fee_override,
        status=body.status,
        resign_date=body.resign_date,
        phone=body.phone,
    )
    db.add(emp)
    db.flush()
    effective = body.join_date or date.today()
    if money_vnd(body.contract_salary or Decimal("0")) > 0:
        _record_salary_history(
            db,
            employee_id=emp.id,
            field_code="contract_salary",
            effective_from=effective,
            old_value=Decimal("0"),
            new_value=body.contract_salary or Decimal("0"),
            note="Khởi tạo hồ sơ",
        )
    if money_vnd(body.probation_salary or Decimal("0")) > 0:
        _record_salary_history(
            db,
            employee_id=emp.id,
            field_code="probation_salary",
            effective_from=effective,
            old_value=Decimal("0"),
            new_value=body.probation_salary or Decimal("0"),
            note="Khởi tạo hồ sơ",
        )
    if emp.status == "probation" and emp.join_date:
        lcf.bootstrap_first_contract(db, emp, sign_date=body.contract_signed_at)
    db.commit()
    return get_employee(db, emp.id)


def validate_employee_form(
    db: Session,
    *,
    is_new: bool,
    employee_id: UUID | None,
    payload: dict,
) -> dict:
    from app.modules.mdm import employee_validation as ev

    issues = ev.validate_employee_payload(
        db, is_new=is_new, employee_id=employee_id, payload=payload
    )
    result = ev.validation_result(issues)
    if is_new:
        result["suggested_code"] = ev.suggest_employee_code(db)
    return result


def get_suggested_employee_code(db: Session) -> str:
    from app.modules.mdm import employee_validation as ev

    return ev.suggest_employee_code(db)


def update_employee(db: Session, emp_id: UUID, body: EmployeeUpdate) -> EmployeeOut:
    from app.modules.mdm import employee_validation as ev

    emp = db.get(Employee, emp_id)
    if emp is None or emp.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy nhân viên.")

    issues = ev.validate_employee_update(db, emp_id, body)
    ev.raise_on_errors(issues)

    was_resigned = emp.status == "resigned"
    data = body.model_dump(exclude_unset=True)
    dept_code = data.pop("department_code", None)
    team_id_in = data.pop("team_id", None)
    team_code_in = data.pop("team_code", None)
    position_code_in = data.pop("position_code", None)
    # Giảm trừ gia cảnh tính từ employee_family_members — không sửa tay (21§21.3).
    data.pop("tax_dependent_count", None)
    if team_id_in is not None or team_code_in is not None:
        team = resolve_employee_team(db, team_id_in, team_code_in, dept_code, required=True)
        emp.team_id = team.id if team else emp.team_id
    if position_code_in is not None:
        if position_code_in == "":
            emp.position_code = None
        else:
            pos = db.get(Position, position_code_in)
            if pos is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Trợ Lý AI: không tìm thấy chức vụ '{position_code_in}'.",
                )
            emp.position_code = position_code_in
            emp.position_title = pos.name
    for key, val in data.items():
        if key == "pay_channel" and val is not None:
            val = str(val).upper()
            if val not in ("ATM", "CASH"):
                raise HTTPException(status_code=400, detail="Trợ Lý AI: pay_channel chỉ ATM hoặc CASH.")
        if key == "full_name" and isinstance(val, str):
            val = val.strip()
        if key == "children_count" and val is not None:
            val = max(0, int(val))
        if key in SALARY_HISTORY_FIELDS and val is not None:
            old = getattr(emp, key) or Decimal("0")
            new = D(val)
            if money_vnd(old) != money_vnd(new):
                _record_salary_history(
                    db,
                    employee_id=emp.id,
                    field_code=key,
                    effective_from=date.today(),
                    old_value=old,
                    new_value=new,
                    note="Cập nhật hồ sơ",
                )
            val = new
        setattr(emp, key, val)
    if data.get("status") == "resigned" and not was_resigned:
        _finalize_resignation_on_status_change(db, emp)
    _sync_worker_on_status(db, emp)
    db.commit()
    return get_employee(db, emp_id)


def unlock_and_reset_worker_password(
    db: Session, emp_id: UUID, actor: User
) -> UnlockResetPasswordOut:
    """HR mở khóa + đặt lại mật khẩu Worker + 4 số cuối CCCD."""
    emp = (
        db.query(Employee)
        .options(joinedload(Employee.team).joinedload(Team.department))
        .filter(Employee.id == emp_id, Employee.deleted_at.is_(None))
        .first()
    )
    if emp is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy nhân viên.")
    if emp.status == "resigned":
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: nhân viên đã nghỉ việc — không mở khóa đăng nhập.",
        )

    new_password = default_worker_reset_password()
    user = _worker_user_for_employee(db, emp)
    if user is None:
        user = User(
            username=emp.employee_code,
            full_name=emp.full_name,
            password_hash=hash_password(new_password),
            role="worker",
            employee_id=emp.id,
            must_change_password=True,
            is_active=True,
            is_locked=False,
            failed_attempts=0,
            failed_login_count=0,
        )
        db.add(user)
    else:
        user.password_hash = hash_password(new_password)
        user.is_locked = False
        user.failed_attempts = 0
        user.failed_login_count = 0
        user.locked_until = None
        user.is_active = True
        user.must_change_password = True
        user.full_name = emp.full_name
        user.employee_id = emp.id

    write_audit(
        db,
        actor=actor,
        action="worker.unlock_reset_password",
        entity_type="employee",
        entity_id=str(emp.id),
        summary=f"Mở khóa & reset mật khẩu MSNV {emp.employee_code}",
        commit=False,
    )
    db.commit()
    acct = _account_fields(emp, user)
    return UnlockResetPasswordOut(
        detail=(
            f"Trợ Lý AI: đã mở khóa và đặt lại mật khẩu cho {emp.full_name} (MSNV {emp.employee_code}). "
            f"Mật khẩu mới: {new_password}"
        ),
        employee_id=emp.id,
        employee_code=emp.employee_code,
        new_password=new_password,
        account_status=acct["account_status"],
        account_status_label=acct["account_status_label"],
    )


def _get_employee_row(db: Session, emp_id: UUID) -> Employee:
    emp = db.get(Employee, emp_id)
    if emp is None or emp.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy nhân viên.")
    return emp


def upload_employee_photo(
    db: Session,
    emp_id: UUID,
    content: bytes,
    content_type: str | None,
    filename: str | None,
) -> EmployeeOut:
    emp = _get_employee_row(db, emp_id)
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in PHOTO_TYPES:
        # đoán từ tên file nếu trình duyệt gửi generic
        guessed, _ = mimetypes.guess_type(filename or "")
        ctype = (guessed or "").lower()
    if ctype not in PHOTO_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: ảnh hồ sơ chỉ nhận JPG, PNG hoặc WEBP.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: file ảnh trống.")
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: ảnh hồ sơ tối đa 5MB.",
        )

    ext = PHOTO_TYPES[ctype]
    dest = _photo_dir() / f"{emp.id}{ext}"
    # xóa ảnh cũ khác đuôi
    if emp.photo_path:
        old = Path(emp.photo_path)
        if old.is_file() and old.resolve() != dest.resolve():
            try:
                old.unlink()
            except OSError:
                pass
    dest.write_bytes(content)
    emp.photo_path = dest.name
    db.commit()
    return get_employee(db, emp_id)


def resolve_employee_photo_file(db: Session, emp_id: UUID) -> tuple[Path, str]:
    emp = _get_employee_row(db, emp_id)
    path = _employee_photo_file(emp)
    if path is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy file ảnh hồ sơ.")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, ctype


def _raise_target_query(db: Session, body: BulkSalaryRaiseRequest):
    q = (
        db.query(Employee)
        .options(joinedload(Employee.team).joinedload(Team.department))
        .filter(
            Employee.deleted_at.is_(None),
            Employee.status.in_(RAISE_STATUSES),
        )
    )
    dept: Department | None = None
    if body.scope == "department":
        code = (body.department_code or "").strip().upper()
        if not code:
            raise HTTPException(
                status_code=400,
                detail="Trợ Lý AI: chọn bộ phận hoặc chọn toàn bộ công ty.",
            )
        dept = db.query(Department).filter(Department.code == code).first()
        if dept is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trợ Lý AI: không tìm thấy bộ phận '{code}'.",
            )
        q = q.filter(Employee.department_id == dept.id)
    elif body.scope == "employees":
        ids = list(dict.fromkeys(body.employee_ids or []))
        if not ids:
            raise HTTPException(
                status_code=400,
                detail="Trợ Lý AI: chọn ít nhất một nhân viên.",
            )
        q = q.filter(Employee.id.in_(ids))
    return q, dept


def _resolve_raise_target(db: Session, body: BulkSalaryRaiseRequest) -> tuple[str, str, PayComponent | None]:
    if body.target == "contract_salary":
        return body.target, "Lương HĐ", None
    if body.target == "probation_salary":
        return body.target, "Lương thử việc", None
    code = (body.allowance_code or "").strip().upper()
    if not code:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: chọn mã phụ cấp cần tăng.",
        )
    at = (
        db.query(PayComponent)
        .filter(PayComponent.code == code, PayComponent.is_active.is_(True))
        .one_or_none()
    )
    if at is None:
        raise HTTPException(
            status_code=404,
            detail=f"Trợ Lý AI: không tìm thấy phụ cấp '{code}'.",
        )
    return body.target, f"Phụ cấp {at.code} — {at.name}", at


def _bump_allowance(db: Session, emp: Employee, at: PayComponent, delta: Decimal) -> None:
    row = (
        db.query(EmployeeAllowanceAssignment)
        .filter(
            EmployeeAllowanceAssignment.employee_id == emp.id,
            EmployeeAllowanceAssignment.allowance_type_id == at.id,
        )
        .one_or_none()
    )
    if row is None:
        base = at.default_amount or Decimal("0")
        db.add(
            EmployeeAllowanceAssignment(
                employee_id=emp.id,
                allowance_type_id=at.id,
                amount=base + delta,
            )
        )
        return
    base = row.amount if row.amount is not None else (at.default_amount or Decimal("0"))
    row.amount = base + delta


def preview_salary_raise(db: Session, body: BulkSalaryRaiseRequest) -> BulkSalaryRaisePreview:
    q, dept = _raise_target_query(db, body)
    _target, target_label, at = _resolve_raise_target(db, body)
    count = q.count()
    if body.scope == "employees":
        scope_label = f"{count} NV được chọn"
    elif dept:
        scope_label = f"bộ phận {dept.code} — {dept.name}"
    else:
        scope_label = "toàn bộ công ty (NV đang làm)"
    return BulkSalaryRaisePreview(
        scope=body.scope,
        department_code=dept.code if dept else None,
        department_name=dept.name if dept else None,
        target=body.target,
        target_label=target_label,
        allowance_code=at.code if at else None,
        amount=body.amount,
        affected_count=count,
        message=(
            f"Sẽ tăng {target_label} thêm {body.amount:,.0f} VND cho {count} NV ({scope_label}). "
            "Chỉ áp dụng trạng thái đang làm / thử việc."
        ).replace(",", "."),
    )


def apply_salary_raise(
    db: Session, body: BulkSalaryRaiseRequest, actor: User
) -> BulkSalaryRaiseResult:
    if not body.confirm or not body.confirm_again:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: cần xác nhận đủ 2 lần trước khi tăng lương hàng loạt.",
        )
    preview = preview_salary_raise(db, body)
    if preview.affected_count == 0:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: không có nhân viên nào trong phạm vi để tăng lương.",
        )
    q, dept = _raise_target_query(db, body)
    _target, target_label, at = _resolve_raise_target(db, body)
    rows = q.all()
    effective = body.effective_from or date.today()
    scope_label = dept.code if dept else ("SELECTED" if body.scope == "employees" else "ALL")
    for emp in rows:
        if body.target == "contract_salary":
            old = emp.contract_salary or Decimal("0")
            new = old + body.amount
            _record_salary_history(
                db,
                employee_id=emp.id,
                field_code="contract_salary",
                effective_from=effective,
                old_value=old,
                new_value=new,
                approved_by=actor.id,
                note=f"Tăng lương hàng loạt · {scope_label} · +{body.amount:,.0f}".replace(",", "."),
            )
            emp.contract_salary = new
        elif body.target == "probation_salary":
            old = emp.probation_salary or Decimal("0")
            new = old + body.amount
            _record_salary_history(
                db,
                employee_id=emp.id,
                field_code="probation_salary",
                effective_from=effective,
                old_value=old,
                new_value=new,
                approved_by=actor.id,
                note=f"Tăng lương thử việc hàng loạt · {scope_label} · +{body.amount:,.0f}".replace(",", "."),
            )
            emp.probation_salary = new
        else:
            assert at is not None
            _bump_allowance(db, emp, at, body.amount)
    db.commit()

    scope_label = dept.code if dept else "ALL"
    write_audit(
        db,
        actor=actor,
        action="employee.salary_raise_bulk",
        entity_type="employee",
        entity_id=scope_label,
        summary=(
            f"Tăng {target_label} +{body.amount} VND · phạm vi {scope_label} · "
            f"{len(rows)} NV"
        ),
        meta={
            "scope": body.scope,
            "department_code": dept.code if dept else None,
            "target": body.target,
            "allowance_code": at.code if at else None,
            "increase_amount": str(body.amount),
            "affected_count": len(rows),
        },
    )
    return BulkSalaryRaiseResult(
        scope=body.scope,
        department_code=dept.code if dept else None,
        target=body.target,
        target_label=target_label,
        allowance_code=at.code if at else None,
        amount=body.amount,
        affected_count=len(rows),
        message=(
            f"Đã tăng {target_label} thêm {body.amount:,.0f} VND cho {len(rows)} nhân viên "
            f"({preview.department_name or 'toàn công ty'})."
        ).replace(",", "."),
    )


def _transfer_team_classify(
    db: Session, employee_ids: list[UUID], team_id: UUID, effective_from: date
) -> tuple[Team, list[Employee], list[TransferTeamSkipped]]:
    """Lọc danh sách NV được chọn thành: hợp lệ để chuyển / bị loại khỏi lô + lý do rõ.

    Theo 23§143/145 — không im lặng bỏ qua, luôn báo rõ dòng nào bị loại và vì sao.
    """
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: không tìm thấy tổ.")

    unique_ids = list(dict.fromkeys(employee_ids))
    rows = (
        db.query(Employee)
        .options(joinedload(Employee.team))
        .filter(Employee.id.in_(unique_ids), Employee.deleted_at.is_(None))
        .all()
    )
    by_id = {e.id: e for e in rows}

    targets: list[Employee] = []
    skipped: list[TransferTeamSkipped] = []
    for eid in unique_ids:
        emp = by_id.get(eid)
        if emp is None:
            skipped.append(
                TransferTeamSkipped(employee_code=str(eid), full_name="—", reason="Không tìm thấy nhân viên")
            )
            continue
        if emp.status == "resigned":
            skipped.append(
                TransferTeamSkipped(
                    employee_code=emp.employee_code, full_name=emp.full_name, reason="Đã nghỉ việc"
                )
            )
            continue
        if emp.team_id == team.id:
            skipped.append(
                TransferTeamSkipped(
                    employee_code=emp.employee_code,
                    full_name=emp.full_name,
                    reason=f"Đã ở tổ {team.code} — {team.name}",
                )
            )
            continue
        open_row = (
            db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.employee_id == emp.id, EmployeeAssignment.effective_to.is_(None))
            .one_or_none()
        )
        if open_row is not None and effective_from <= open_row.effective_from:
            skipped.append(
                TransferTeamSkipped(
                    employee_code=emp.employee_code,
                    full_name=emp.full_name,
                    reason=(
                        f"Ngày hiệu lực phải sau lần chuyển tổ gần nhất "
                        f"({open_row.effective_from.isoformat()})"
                    ),
                )
            )
            continue
        targets.append(emp)
    return team, targets, skipped


def preview_transfer_team(db: Session, body: TransferTeamRequest) -> TransferTeamPreview:
    if body.position_code and db.get(Position, body.position_code) is None:
        raise HTTPException(
            status_code=400, detail=f"Trợ Lý AI: không tìm thấy chức vụ '{body.position_code}'."
        )
    team, targets, skipped = _transfer_team_classify(
        db, body.employee_ids, body.team_id, body.effective_from
    )
    dept = team.department
    msg = f"Sẽ chuyển {len(targets)}/{len(set(body.employee_ids))} NV sang tổ {team.code} — {team.name}"
    msg += f", {len(skipped)} bị loại khỏi lô (xem danh sách)." if skipped else "."
    return TransferTeamPreview(
        team_id=team.id,
        team_code=team.code,
        team_name=team.name,
        department_code=dept.code if dept else None,
        effective_from=body.effective_from,
        total_selected=len(set(body.employee_ids)),
        affected_count=len(targets),
        skipped=skipped,
        message=msg,
    )


def apply_transfer_team(db: Session, body: TransferTeamRequest, actor: User) -> TransferTeamResult:
    """Chuyển tổ hàng loạt — 1 giao dịch, ghi employee_assignments trước khi đổi team_id (20§N4)."""
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail="Trợ Lý AI: cần xác nhận trước khi chuyển tổ hàng loạt."
        )
    if body.position_code and db.get(Position, body.position_code) is None:
        raise HTTPException(
            status_code=400, detail=f"Trợ Lý AI: không tìm thấy chức vụ '{body.position_code}'."
        )
    team, targets, skipped = _transfer_team_classify(
        db, body.employee_ids, body.team_id, body.effective_from
    )
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: không có nhân viên hợp lệ nào trong lô để chuyển tổ.",
        )

    for emp in targets:
        open_row = (
            db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.employee_id == emp.id, EmployeeAssignment.effective_to.is_(None))
            .one_or_none()
        )
        if open_row is not None:
            open_row.effective_to = body.effective_from - timedelta(days=1)
        db.add(
            EmployeeAssignment(
                employee_id=emp.id,
                team_id=team.id,
                position_code=body.position_code or emp.position_code,
                job_code=emp.job_code,
                effective_from=body.effective_from,
                decision_no=(body.decision_no or "").strip() or None,
                reason_code=(body.reason_code or "").strip() or None,
                approved_by=actor.id,
            )
        )
        emp.team_id = team.id
        # department suy ra qua team_id (hybrid_property) — không gán department_id riêng.
        if body.position_code:
            emp.position_code = body.position_code
    db.commit()

    write_audit(
        db,
        actor=actor,
        action="employee.transfer_team_bulk",
        entity_type="team",
        entity_id=str(team.id),
        summary=(
            f"Chuyển tổ hàng loạt {len(targets)} NV sang {team.code} — {team.name}, "
            f"hiệu lực {body.effective_from}"
        ),
        meta={
            "team_id": str(team.id),
            "team_code": team.code,
            "effective_from": str(body.effective_from),
            "employee_codes": [e.employee_code for e in targets],
            "skipped_count": len(skipped),
        },
    )

    msg = (
        f"Đã chuyển {len(targets)} nhân viên sang tổ {team.code} — {team.name}, "
        f"hiệu lực từ {body.effective_from}."
    )
    if skipped:
        msg += f" {len(skipped)} bị loại khỏi lô (xem danh sách)."
    return TransferTeamResult(
        team_id=team.id,
        team_code=team.code,
        team_name=team.name,
        effective_from=body.effective_from,
        affected_count=len(targets),
        skipped=skipped,
        message=msg,
    )


def team_at_date(db: Session, employee_id: UUID, as_of: date) -> Team | None:
    """Tổ NV tại một ngày — tra employee_assignments (21§21.3, hạng mục 1.3)."""
    row = (
        db.query(EmployeeAssignment)
        .options(joinedload(EmployeeAssignment.team))
        .filter(
            EmployeeAssignment.employee_id == employee_id,
            EmployeeAssignment.effective_from <= as_of,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= as_of),
        )
        .order_by(EmployeeAssignment.effective_from.desc())
        .first()
    )
    if row is not None:
        return row.team
    emp = db.get(Employee, employee_id)
    if emp is not None and emp.team_id is not None:
        return db.get(Team, emp.team_id)
    return None


def backfill_initial_assignments(db: Session) -> int:
    """NV có team_id nhưng chưa có lịch sử — seed 1 dòng từ join_date (nạp GenusSuite)."""
    count = 0
    emps = (
        db.query(Employee)
        .filter(Employee.deleted_at.is_(None), Employee.team_id.isnot(None))
        .all()
    )
    for emp in emps:
        has_row = (
            db.query(EmployeeAssignment.id)
            .filter(EmployeeAssignment.employee_id == emp.id)
            .limit(1)
            .first()
        )
        if has_row:
            continue
        db.add(
            EmployeeAssignment(
                employee_id=emp.id,
                team_id=emp.team_id,
                position_code=emp.position_code,
                job_code=emp.job_code,
                effective_from=emp.join_date or date(2020, 1, 1),
                effective_to=None,
                reason_code="backfill",
            )
        )
        count += 1
    if count:
        db.commit()
    return count


def list_employee_assignments(db: Session, emp_id: UUID) -> list[EmployeeAssignmentOut]:
    """Lịch sử đổi tổ / chức vụ của một NV — mới nhất trước (21§21.3)."""
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeAssignment)
        .options(joinedload(EmployeeAssignment.team))
        .filter(EmployeeAssignment.employee_id == emp.id)
        .order_by(EmployeeAssignment.effective_from.desc())
        .all()
    )
    approver_ids = {r.approved_by for r in rows if r.approved_by is not None}
    approvers = (
        {u.id: u.full_name for u in db.query(User).filter(User.id.in_(approver_ids)).all()}
        if approver_ids
        else {}
    )
    out: list[EmployeeAssignmentOut] = []
    for r in rows:
        out.append(
            EmployeeAssignmentOut(
                id=r.id,
                employee_id=r.employee_id,
                team_id=r.team_id,
                team_code=r.team.code if r.team else None,
                team_name=r.team.name if r.team else None,
                position_code=r.position_code,
                effective_from=r.effective_from,
                effective_to=r.effective_to,
                decision_no=r.decision_no,
                reason_code=r.reason_code,
                approved_by_name=approvers.get(r.approved_by),
                created_at=r.created_at,
            )
        )
    return out


def list_employee_salary_history(db: Session, emp_id: UUID) -> list[EmployeeSalaryHistoryOut]:
    """Lịch sử thay đổi lương của một NV — mới nhất trước (21§21.3)."""
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeSalaryHistory)
        .filter(EmployeeSalaryHistory.employee_id == emp.id)
        .order_by(EmployeeSalaryHistory.effective_from.desc(), EmployeeSalaryHistory.created_at.desc())
        .all()
    )
    approver_ids = {r.approved_by for r in rows if r.approved_by is not None}
    approvers = (
        {u.id: u.full_name for u in db.query(User).filter(User.id.in_(approver_ids)).all()}
        if approver_ids
        else {}
    )
    return [
        EmployeeSalaryHistoryOut(
            id=r.id,
            employee_id=r.employee_id,
            field_code=r.field_code,
            effective_from=r.effective_from,
            old_value=r.old_value,
            new_value=r.new_value,
            decision_no=r.decision_no,
            approved_by_name=approvers.get(r.approved_by),
            note=r.note,
            created_at=r.created_at,
        )
        for r in rows
    ]


def _get_or_create_default_team(db: Session, dept: Department) -> Team:
    """Tổ mặc định "T1" của một bộ phận test — chỉ dùng cho fixture seed_mdm."""
    team = db.query(Team).filter(Team.department_id == dept.id, Team.code == "T1").first()
    if team:
        return team
    team = Team(department_id=dept.id, code="T1", name=f"{dept.name} — Tổ 1")
    db.add(team)
    db.flush()
    return team


def seed_mdm(db: Session) -> dict[str, int]:
    depts = [
        ("SW1", "May 1", "direct"),
        ("B01", "Văn phòng", "admin_indirect"),
        ("QC1", "QC", "prod_indirect"),
    ]
    for code, name, cat in depts:
        if not db.query(Department).filter(Department.code == code).first():
            db.add(Department(code=code, name=name, category=cat, mitapro_names=[name]))
    db.flush()

    # Fixture regression Oct/2025 (file 08§8.5)
    samples = [
        ("1514", "Nguyễn Văn A", "SW1", "Công nhân", Decimal("4840750"), Decimal("5675000")),
        ("1643", "Trần Thị B", "SW1", "Công nhân", Decimal("4840750"), Decimal("5675000")),
        ("5290", "Lê Văn C", "SW1", "Công nhân", Decimal("4840750"), Decimal("5675000")),
        ("5321", "Phạm Thị D", "QC1", "QC", Decimal("4840750"), Decimal("5675000")),
        ("1732", "Hoàng Văn E", "B01", "Nhân viên", Decimal("5000000"), Decimal("6500000")),
    ]
    created = 0
    for code, name, dcode, pos, prob, contract in samples:
        if db.query(Employee).filter(Employee.employee_code == code).first():
            continue
        dept = get_or_create_department_by_code(db, dcode)
        team = _get_or_create_default_team(db, dept)
        db.add(
            Employee(
                employee_code=code,
                full_name=name,
                gender="M" if "Văn" in name else "F",
                pay_channel="ATM",
                team_id=team.id,
                position_title=pos,
                join_date=date(2020, 1, 15),
                contract_signed_at=date(2020, 4, 15),
                probation_salary=prob,
                contract_salary=contract,
                status="active",
                si_enrolled=True,
            )
        )
        created += 1
    db.commit()
    return {
        "departments": db.query(Department).count(),
        "employees": db.query(Employee).filter(Employee.deleted_at.is_(None)).count(),
        "seeded_new_employees": created,
    }


def _violation_dir(emp_id: UUID) -> Path:
    root = Path(get_settings().upload_dir).expanduser().resolve() / "violations" / str(emp_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _violation_to_out(row: EmployeeViolation, emp: Employee) -> EmployeeViolationOut:
    has = bool(row.attachment_path)
    return EmployeeViolationOut(
        id=row.id,
        employee_id=emp.id,
        employee_code=emp.employee_code,
        full_name=emp.full_name,
        occurred_at=row.occurred_at,
        title=row.title,
        description=row.description or "",
        penalty=row.penalty or "",
        has_attachment=has,
        attachment_url=f"/api/employees/{emp.id}/violations/{row.id}/attachment" if has else None,
        created_at=row.created_at,
    )


def list_violations(db: Session, emp_id: UUID) -> list[EmployeeViolationOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeViolation)
        .filter(EmployeeViolation.employee_id == emp.id)
        .order_by(EmployeeViolation.occurred_at.desc())
        .all()
    )
    return [_violation_to_out(r, emp) for r in rows]


def create_violation(
    db: Session,
    emp_id: UUID,
    body: EmployeeViolationCreate,
    *,
    actor: User | None,
    file_content: bytes | None = None,
    content_type: str | None = None,
    filename: str | None = None,
) -> EmployeeViolationOut:
    emp = _get_employee_row(db, emp_id)
    occurred = body.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)

    row = EmployeeViolation(
        employee_id=emp.id,
        occurred_at=occurred,
        title=body.title.strip(),
        description=(body.description or "").strip(),
        penalty=(body.penalty or "").strip(),
        created_by_user_id=actor.id if actor else None,
    )
    db.add(row)
    db.flush()

    if file_content:
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype not in VIOLATION_TYPES:
            guessed, _ = mimetypes.guess_type(filename or "")
            ctype = (guessed or "").lower()
        if ctype not in VIOLATION_TYPES:
            raise HTTPException(
                status_code=400,
                detail="Trợ Lý AI: biên bản chỉ nhận PDF, JPG, PNG hoặc WEBP.",
            )
        if len(file_content) > MAX_VIOLATION_BYTES:
            raise HTTPException(
                status_code=400,
                detail="Trợ Lý AI: file biên bản tối đa 10MB.",
            )
        ext = VIOLATION_TYPES[ctype]
        dest = _violation_dir(emp.id) / f"{row.id}{ext}"
        dest.write_bytes(file_content)
        row.attachment_path = str(dest)

    db.commit()
    db.refresh(row)
    if actor:
        write_audit(
            db,
            actor=actor,
            action="employee.violation.create",
            entity_type="employee_violation",
            entity_id=str(row.id),
            summary=f"Ghi vi phạm {emp.employee_code}: {row.title[:80]}",
            meta={"employee_code": emp.employee_code, "has_attachment": bool(row.attachment_path)},
        )
    return _violation_to_out(row, emp)


def delete_violation(db: Session, emp_id: UUID, violation_id: UUID, *, actor: User | None) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeViolation)
        .filter(
            EmployeeViolation.id == violation_id,
            EmployeeViolation.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy biên bản vi phạm.")
    if row.attachment_path:
        path = Path(row.attachment_path)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
    db.delete(row)
    db.commit()
    if actor:
        write_audit(
            db,
            actor=actor,
            action="employee.violation.delete",
            entity_type="employee_violation",
            entity_id=str(violation_id),
            summary=f"Xóa vi phạm của {emp.employee_code}",
            meta={"employee_code": emp.employee_code},
        )
    return {"detail": "Trợ Lý AI: đã xóa biên bản vi phạm."}


def resolve_violation_attachment(db: Session, emp_id: UUID, violation_id: UUID) -> tuple[Path, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeViolation)
        .filter(
            EmployeeViolation.id == violation_id,
            EmployeeViolation.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None or not row.attachment_path:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không có file biên bản.")
    path = Path(row.attachment_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy file biên bản.")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, ctype


def list_violation_board(db: Session) -> list[EmployeeViolationBoardItem]:
    rows = (
        db.query(
            Employee.id,
            Employee.employee_code,
            Employee.full_name,
            Department.code,
            Employee.status,
            func.count(EmployeeViolation.id),
            func.max(EmployeeViolation.occurred_at),
        )
        .join(EmployeeViolation, EmployeeViolation.employee_id == Employee.id)
        .outerjoin(Department, Department.id == Employee.department_id)
        .filter(Employee.deleted_at.is_(None))
        .group_by(
            Employee.id,
            Employee.employee_code,
            Employee.full_name,
            Department.code,
            Employee.status,
        )
        .order_by(func.max(EmployeeViolation.occurred_at).desc())
        .all()
    )
    return [
        EmployeeViolationBoardItem(
            employee_id=r[0],
            employee_code=r[1],
            full_name=r[2],
            department_code=r[3],
            status=r[4],
            violation_count=int(r[5] or 0),
            last_occurred_at=r[6],
        )
        for r in rows
    ]


def _document_dir(emp_id: UUID) -> Path:
    root = Path(get_settings().upload_dir).expanduser().resolve() / "documents" / str(emp_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _document_to_out(row: EmployeeDocument, emp: Employee) -> EmployeeDocumentOut:
    return EmployeeDocumentOut(
        id=row.id,
        employee_id=emp.id,
        employee_code=emp.employee_code,
        full_name=emp.full_name,
        doc_type=row.doc_type,
        title=row.title,
        note=row.note or "",
        file_url=f"/api/employees/{emp.id}/documents/{row.id}/file",
        created_at=row.created_at,
    )


def list_documents(db: Session, emp_id: UUID) -> list[EmployeeDocumentOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeDocument)
        .filter(EmployeeDocument.employee_id == emp.id)
        .order_by(EmployeeDocument.created_at.desc())
        .all()
    )
    return [_document_to_out(r, emp) for r in rows]


def create_document(
    db: Session,
    emp_id: UUID,
    body: EmployeeDocumentCreate,
    *,
    actor: User | None,
    file_content: bytes,
    content_type: str | None = None,
    filename: str | None = None,
) -> EmployeeDocumentOut:
    emp = _get_employee_row(db, emp_id)
    doc_type = (body.doc_type or "other").strip().lower()
    if doc_type not in ALLOWED_DOC_TYPE_CODES:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: loại hồ sơ không hợp lệ.")
    if not file_content:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: cần ảnh/PDF hồ sơ giấy.")

    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in DOCUMENT_TYPES:
        guessed, _ = mimetypes.guess_type(filename or "")
        ctype = (guessed or "").lower()
    if ctype not in DOCUMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: hồ sơ giấy chỉ nhận PDF, JPG, PNG hoặc WEBP.",
        )
    if len(file_content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: file hồ sơ tối đa 10MB.")

    row = EmployeeDocument(
        employee_id=emp.id,
        doc_type=doc_type,
        title=body.title.strip(),
        note=(body.note or "").strip(),
        file_path="",
        created_by_user_id=actor.id if actor else None,
    )
    db.add(row)
    db.flush()

    ext = DOCUMENT_TYPES[ctype]
    dest = _document_dir(emp.id) / f"{row.id}{ext}"
    dest.write_bytes(file_content)
    row.file_path = str(dest)

    db.commit()
    db.refresh(row)
    if actor:
        write_audit(
            db,
            actor=actor,
            action="employee.document.create",
            entity_type="employee_document",
            entity_id=str(row.id),
            summary=f"Lưu hồ sơ giấy {emp.employee_code}: {row.title[:80]}",
            meta={"employee_code": emp.employee_code, "doc_type": row.doc_type},
        )
    return _document_to_out(row, emp)


def delete_document(db: Session, emp_id: UUID, document_id: UUID, *, actor: User | None) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.id == document_id,
            EmployeeDocument.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy hồ sơ giấy.")
    if row.file_path:
        path = Path(row.file_path)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
    db.delete(row)
    db.commit()
    if actor:
        write_audit(
            db,
            actor=actor,
            action="employee.document.delete",
            entity_type="employee_document",
            entity_id=str(document_id),
            summary=f"Xóa hồ sơ giấy của {emp.employee_code}",
            meta={"employee_code": emp.employee_code},
        )
    return {"detail": "Trợ Lý AI: đã xóa hồ sơ giấy."}


def resolve_document_file(db: Session, emp_id: UUID, document_id: UUID) -> tuple[Path, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.id == document_id,
            EmployeeDocument.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None or not row.file_path:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không có file hồ sơ.")
    path = Path(row.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy file hồ sơ.")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, ctype


# --- 5.2 labour_contracts ---


def _contract_ranges_overlap(
    start_a: date,
    end_a: date | None,
    start_b: date,
    end_b: date | None,
) -> bool:
    """Hai khoảng ngày chồng lấn nếu giao nhau (end NULL = vô thời hạn)."""
    far = date(9999, 12, 31)
    a_end = end_a or far
    b_end = end_b or far
    return start_a <= b_end and start_b <= a_end


def _next_contract_seq(db: Session, employee_id: UUID) -> int:
    max_seq = (
        db.query(func.max(LabourContract.seq_no))
        .filter(LabourContract.employee_id == employee_id)
        .scalar()
    )
    return int(max_seq or 0) + 1


def _validate_contract_no_overlap(
    db: Session,
    employee_id: UUID,
    start_date: date,
    end_date: date | None,
    *,
    exclude_id: UUID | None = None,
) -> None:
    q = db.query(LabourContract).filter(LabourContract.employee_id == employee_id)
    if exclude_id is not None:
        q = q.filter(LabourContract.id != exclude_id)
    for existing in q.all():
        if _contract_ranges_overlap(start_date, end_date, existing.start_date, existing.end_date):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Trợ Lý AI: khoảng ngày HĐ chồng lấn với HĐ khác của cùng nhân viên "
                    f"(từ {existing.start_date} đến {existing.end_date or 'vô thời hạn'})."
                ),
            )


def _contract_to_out(row: LabourContract, emp: Employee) -> LabourContractOut:
    days_until: int | None = None
    if row.status == "active" and row.end_date is not None:
        days_until = (row.end_date - date.today()).days
    ctype = row.contract_type_code.upper()
    return LabourContractOut(
        id=row.id,
        employee_id=emp.id,
        employee_code=emp.employee_code,
        full_name=emp.full_name,
        contract_type_code=ctype,
        contract_type_label=lcf.contract_type_label(ctype),
        contract_no=lcf.format_contract_no(emp.employee_code, ctype),
        times_label=lcf.times_label(int(row.seq_no), ctype),
        seq_no=int(row.seq_no),
        sign_date=row.sign_date,
        start_date=row.start_date,
        end_date=row.end_date,
        base_salary=row.base_salary,
        position_code=row.position_code,
        team_id=row.team_id,
        status=row.status,
        file_path=row.file_path,
        days_until_expiry=days_until,
        created_at=row.created_at,
    )


def list_labour_contracts(
    db: Session,
    *,
    employee_id: UUID | None = None,
    expiring_within_days: int | None = None,
) -> list[LabourContractOut]:
    q = (
        db.query(LabourContract, Employee)
        .join(Employee, Employee.id == LabourContract.employee_id)
        .filter(Employee.deleted_at.is_(None))
    )
    if employee_id is not None:
        q = q.filter(LabourContract.employee_id == employee_id)
    if expiring_within_days is not None:
        today = date.today()
        deadline = today + timedelta(days=expiring_within_days)
        q = q.filter(
            LabourContract.status == "active",
            LabourContract.end_date.isnot(None),
            LabourContract.end_date >= today,
            LabourContract.end_date <= deadline,
        )
    rows = q.order_by(LabourContract.end_date.asc().nullslast(), LabourContract.start_date.desc()).all()
    return [_contract_to_out(lc, emp) for lc, emp in rows]


def get_labour_contract(db: Session, contract_id: UUID) -> LabourContractOut:
    row = db.get(LabourContract, contract_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy hợp đồng lao động.")
    emp = _get_employee_row(db, row.employee_id)
    return _contract_to_out(row, emp)


def create_labour_contract(db: Session, body: LabourContractCreate) -> LabourContractOut:
    emp = _get_employee_row(db, body.employee_id)
    if body.end_date is not None and body.end_date < body.start_date:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: ngày hết HĐ không được trước ngày bắt đầu.")
    _validate_contract_no_overlap(db, emp.id, body.start_date, body.end_date)
    seq = body.seq_no if body.seq_no is not None else _next_contract_seq(db, emp.id)
    row = LabourContract(
        employee_id=emp.id,
        contract_type_code=body.contract_type_code,
        seq_no=seq,
        sign_date=body.sign_date,
        start_date=body.start_date,
        end_date=body.end_date,
        base_salary=body.base_salary,
        position_code=body.position_code,
        team_id=body.team_id,
        status=body.status,
        file_path=body.file_path,
    )
    db.add(row)
    lcf.sync_employee_after_contract(emp, row.contract_type_code)
    db.commit()
    db.refresh(row)
    return _contract_to_out(row, emp)


def preview_renew_labour_contract(db: Session, employee_id: UUID) -> LabourContractRenewPreview:
    emp = _get_employee_row(db, employee_id)
    data = lcf.build_renew_preview(db, emp)
    return LabourContractRenewPreview(**data)


def renew_labour_contract(
    db: Session, body: LabourContractRenewRequest
) -> LabourContractOut:
    emp = _get_employee_row(db, body.employee_id)
    preview = lcf.build_renew_preview(db, emp)
    ctype = (body.contract_type_code or preview["suggested_contract_type_code"]).upper()
    allowed = preview["allowed_contract_type_codes"]
    if ctype not in allowed and ctype != preview["suggested_contract_type_code"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trợ Lý AI: sau {preview['previous_contract_type_code'] or '—'} "
                f"chỉ được ký {', '.join(allowed)}."
            ),
        )
    start = body.start_date or preview["suggested_start_date"]
    end = body.end_date if body.end_date is not None else preview["suggested_end_date"]
    if end is None and ctype in ("HD1", "HD2", "TV"):
        end = lcf.contract_end_date(start, ctype)
    if end is not None and end < start:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: ngày hết HĐ không được trước ngày bắt đầu.")
    _validate_contract_no_overlap(db, emp.id, start, end)
    lcf.expire_superseded(db, emp.id, start)
    seq = preview["suggested_seq_no"]
    base = body.base_salary if body.base_salary is not None else preview["suggested_base_salary"]
    sign = body.sign_date or preview["suggested_sign_date"]
    row = LabourContract(
        employee_id=emp.id,
        contract_type_code=ctype,
        seq_no=seq,
        sign_date=sign,
        start_date=start,
        end_date=end,
        base_salary=base,
        position_code=emp.position_code,
        team_id=emp.team_id,
        status="active",
    )
    db.add(row)
    lcf.sync_employee_after_contract(emp, ctype)
    if ctype in ("HD1", "HD2", "VTH"):
        emp.contract_salary = base
    db.commit()
    db.refresh(row)
    return _contract_to_out(row, emp)


def update_labour_contract(
    db: Session, contract_id: UUID, body: LabourContractUpdate
) -> LabourContractOut:
    row = db.get(LabourContract, contract_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy hợp đồng lao động.")
    emp = _get_employee_row(db, row.employee_id)
    start = body.start_date if body.start_date is not None else row.start_date
    end = body.end_date if body.end_date is not None else row.end_date
    if body.end_date is not None or body.start_date is not None:
        if end is not None and end < start:
            raise HTTPException(
                status_code=400, detail="Trợ Lý AI: ngày hết HĐ không được trước ngày bắt đầu."
            )
        _validate_contract_no_overlap(db, emp.id, start, end, exclude_id=row.id)
    if body.contract_type_code is not None:
        row.contract_type_code = body.contract_type_code
    if body.seq_no is not None:
        row.seq_no = body.seq_no
    if body.sign_date is not None:
        row.sign_date = body.sign_date
    if body.start_date is not None:
        row.start_date = body.start_date
    if body.end_date is not None:
        row.end_date = body.end_date
    if body.base_salary is not None:
        row.base_salary = body.base_salary
    if body.position_code is not None:
        row.position_code = body.position_code
    if body.team_id is not None:
        row.team_id = body.team_id
    if body.status is not None:
        row.status = body.status
    if body.file_path is not None:
        row.file_path = body.file_path
    db.commit()
    db.refresh(row)
    return _contract_to_out(row, emp)


def delete_labour_contract(db: Session, contract_id: UUID) -> dict[str, str]:
    row = db.get(LabourContract, contract_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy hợp đồng lao động.")
    db.delete(row)
    db.commit()
    return {"detail": "Trợ Lý AI: đã xóa hợp đồng lao động."}


# --- 5.3 employee_family_members ---


def _is_dependent_effective(member: EmployeeFamilyMember, as_of: date) -> bool:
    if not member.is_tax_dependent:
        return False
    if member.dependent_from is not None and member.dependent_from > as_of:
        return False
    if member.dependent_to is not None and member.dependent_to < as_of:
        return False
    return True


def _family_member_to_out(
    row: EmployeeFamilyMember, *, as_of: date | None = None
) -> EmployeeFamilyMemberOut:
    ref = as_of or date.today()
    return EmployeeFamilyMemberOut(
        id=row.id,
        employee_id=row.employee_id,
        relationship_code=row.relationship_code,
        full_name=row.full_name,
        birth_date=row.birth_date,
        id_number=row.id_number,
        is_tax_dependent=row.is_tax_dependent,
        dependent_from=row.dependent_from,
        dependent_to=row.dependent_to,
        is_effective=_is_dependent_effective(row, ref),
        created_at=row.created_at,
    )


def list_family_members(db: Session, emp_id: UUID) -> list[EmployeeFamilyMemberOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeFamilyMember)
        .filter(EmployeeFamilyMember.employee_id == emp.id)
        .order_by(EmployeeFamilyMember.full_name.asc())
        .all()
    )
    return [_family_member_to_out(r) for r in rows]


def compute_tax_dependents(
    db: Session, emp_id: UUID, *, as_of: date | None = None
) -> TaxDependentsOut:
    emp = _get_employee_row(db, emp_id)
    ref = as_of or date.today()
    rows = (
        db.query(EmployeeFamilyMember)
        .filter(EmployeeFamilyMember.employee_id == emp.id)
        .order_by(EmployeeFamilyMember.full_name.asc())
        .all()
    )
    effective = [_family_member_to_out(r, as_of=ref) for r in rows if _is_dependent_effective(r, ref)]
    return TaxDependentsOut(
        employee_id=emp.id,
        as_of_date=ref,
        effective_count=len(effective),
        members=effective,
    )


def resolve_tax_dependent_count(
    db: Session, employee_id: UUID, *, as_of: date | None = None
) -> int:
    """Số người phụ thuộc hiệu lực — nguồn duy nhất cho PIT (5.3)."""
    ref = as_of or date.today()
    return compute_tax_dependents(db, employee_id, as_of=ref).effective_count


def sync_employee_tax_dependent_count(
    db: Session, employee_id: UUID, *, as_of: date | None = None
) -> int:
    """Đồng bộ cột cache employees.tax_dependent_count từ bảng thân nhân."""
    count = resolve_tax_dependent_count(db, employee_id, as_of=as_of)
    emp = _get_employee_row(db, employee_id)
    emp.tax_dependent_count = count
    db.flush()
    return count


def create_family_member(
    db: Session, emp_id: UUID, body: EmployeeFamilyMemberCreate
) -> EmployeeFamilyMemberOut:
    emp = _get_employee_row(db, emp_id)
    if body.dependent_from and body.dependent_to and body.dependent_to < body.dependent_from:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: dependent_to không được trước dependent_from.",
        )
    row = EmployeeFamilyMember(
        employee_id=emp.id,
        relationship_code=body.relationship_code,
        full_name=body.full_name.strip(),
        birth_date=body.birth_date,
        id_number=body.id_number,
        is_tax_dependent=body.is_tax_dependent,
        dependent_from=body.dependent_from,
        dependent_to=body.dependent_to,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    sync_employee_tax_dependent_count(db, emp.id)
    db.commit()
    return _family_member_to_out(row)


def update_family_member(
    db: Session, emp_id: UUID, member_id: UUID, body: EmployeeFamilyMemberUpdate
) -> EmployeeFamilyMemberOut:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeFamilyMember)
        .filter(
            EmployeeFamilyMember.id == member_id,
            EmployeeFamilyMember.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy thân nhân.")
    if body.relationship_code is not None:
        row.relationship_code = body.relationship_code
    if body.full_name is not None:
        row.full_name = body.full_name.strip()
    if body.birth_date is not None:
        row.birth_date = body.birth_date
    if body.id_number is not None:
        row.id_number = body.id_number
    if body.is_tax_dependent is not None:
        row.is_tax_dependent = body.is_tax_dependent
    if body.dependent_from is not None:
        row.dependent_from = body.dependent_from
    if body.dependent_to is not None:
        row.dependent_to = body.dependent_to
    dep_from = row.dependent_from
    dep_to = row.dependent_to
    if dep_from and dep_to and dep_to < dep_from:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: dependent_to không được trước dependent_from.",
        )
    db.commit()
    db.refresh(row)
    sync_employee_tax_dependent_count(db, emp.id)
    db.commit()
    return _family_member_to_out(row)


def delete_family_member(db: Session, emp_id: UUID, member_id: UUID) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeFamilyMember)
        .filter(
            EmployeeFamilyMember.id == member_id,
            EmployeeFamilyMember.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy thân nhân.")
    db.delete(row)
    db.commit()
    sync_employee_tax_dependent_count(db, emp.id)
    db.commit()
    return {"detail": "Trợ Lý AI: đã xóa thân nhân."}


# --- 5.4 resignation preview ---


def preview_resignation(
    db: Session,
    emp_id: UUID,
    *,
    resign_type_code: str,
    last_working_date: date,
) -> ResignationPreviewOut:
    from app.modules.attendance.annual_leave_ledger import annual_leave_remaining

    emp = _get_employee_row(db, emp_id)
    up = resign_type_code.strip().upper()
    if up not in RESIGN_TYPE_CODES:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: resign_type_code phải là DPR, AFL, LWA, CID hoặc DIS.",
        )
    tenure_years = 0
    if emp.join_date and last_working_date >= emp.join_date:
        tenure_years = (last_working_date - emp.join_date).days // 365
    severance_months = 0
    severance_amount = Decimal("0")
    salary = D(emp.contract_salary or 0)
    if up == "AFL" and tenure_years > 0:
        # 0,5 tháng lương / năm thâm niên (BLV 2019 — ước lượng vận hành).
        severance_months = tenure_years
        severance_amount = money_vnd(salary * Decimal("0.5") * Decimal(tenure_years))
    elif up == "CID":
        severance_months = max(1, tenure_years) if tenure_years > 0 else 0
        severance_amount = money_vnd(salary * Decimal(severance_months))
    leave_remaining = annual_leave_remaining(db, emp.id, last_working_date)
    return ResignationPreviewOut(
        employee_id=emp.id,
        employee_code=emp.employee_code,
        full_name=emp.full_name,
        resign_type_code=up,
        last_working_date=last_working_date,
        tenure_years=tenure_years,
        severance_months=severance_months,
        severance_amount=severance_amount,
        annual_leave_remaining=leave_remaining,
        account_will_lock=True,
    )


# --- 5.4 employee_resignations ---


def _next_resignation_seq(db: Session, employee_id: UUID) -> int:
    max_seq = (
        db.query(func.max(EmployeeResignation.seq_no))
        .filter(EmployeeResignation.employee_id == employee_id)
        .scalar()
    )
    return int(max_seq or 0) + 1


def _resignation_to_out(row: EmployeeResignation, emp: Employee) -> EmployeeResignationOut:
    return EmployeeResignationOut(
        id=row.id,
        employee_id=emp.id,
        employee_code=emp.employee_code,
        full_name=emp.full_name,
        seq_no=int(row.seq_no),
        resign_type_code=row.resign_type_code,
        applied_date=row.applied_date,
        last_working_date=row.last_working_date,
        reason=row.reason,
        severance_months=int(row.severance_months),
        severance_amount=row.severance_amount,
        handover_done=row.handover_done,
        rehired_at=row.rehired_at,
        rehire_mode=row.rehire_mode,
        rehire_reason=row.rehire_reason,
        created_at=row.created_at,
    )


def list_resignations(db: Session, emp_id: UUID) -> list[EmployeeResignationOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeResignation)
        .filter(EmployeeResignation.employee_id == emp.id)
        .order_by(EmployeeResignation.seq_no.asc())
        .all()
    )
    return [_resignation_to_out(r, emp) for r in rows]


def create_resignation(
    db: Session, emp_id: UUID, body: EmployeeResignationCreate
) -> EmployeeResignationOut:
    emp = _get_employee_row(db, emp_id)
    seq = body.seq_no if body.seq_no is not None else _next_resignation_seq(db, emp.id)
    existing = (
        db.query(EmployeeResignation)
        .filter(
            EmployeeResignation.employee_id == emp.id,
            EmployeeResignation.seq_no == seq,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Trợ Lý AI: seq_no {seq} đã tồn tại cho nhân viên này.",
        )
    row = EmployeeResignation(
        employee_id=emp.id,
        seq_no=seq,
        resign_type_code=body.resign_type_code,
        applied_date=body.applied_date,
        last_working_date=body.last_working_date,
        reason=body.reason,
        severance_months=body.severance_months,
        severance_amount=body.severance_amount,
        handover_done=body.handover_done,
        rehired_at=body.rehired_at,
    )
    db.add(row)
    if body.finalize:
        row.snapshot_json = _build_resignation_snapshot(db, emp)
        emp.status = "resigned"
        emp.resign_date = body.last_working_date
        _sync_worker_on_status(db, emp)
    db.commit()
    db.refresh(row)
    return _resignation_to_out(row, emp)


def update_resignation(
    db: Session, emp_id: UUID, resignation_id: UUID, body: EmployeeResignationUpdate
) -> EmployeeResignationOut:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeResignation)
        .filter(
            EmployeeResignation.id == resignation_id,
            EmployeeResignation.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy lần nghỉ việc.")
    new_seq = body.seq_no if body.seq_no is not None else row.seq_no
    if body.seq_no is not None and body.seq_no != row.seq_no:
        clash = (
            db.query(EmployeeResignation)
            .filter(
                EmployeeResignation.employee_id == emp.id,
                EmployeeResignation.seq_no == new_seq,
                EmployeeResignation.id != row.id,
            )
            .first()
        )
        if clash is not None:
            raise HTTPException(
                status_code=400,
                detail=f"Trợ Lý AI: seq_no {new_seq} đã tồn tại cho nhân viên này.",
            )
        row.seq_no = new_seq
    if body.resign_type_code is not None:
        row.resign_type_code = body.resign_type_code
    if body.applied_date is not None:
        row.applied_date = body.applied_date
    if body.last_working_date is not None:
        row.last_working_date = body.last_working_date
    if body.reason is not None:
        row.reason = body.reason
    if body.severance_months is not None:
        row.severance_months = body.severance_months
    if body.severance_amount is not None:
        row.severance_amount = body.severance_amount
    if body.handover_done is not None:
        row.handover_done = body.handover_done
    if body.rehired_at is not None:
        row.rehired_at = body.rehired_at
    db.commit()
    db.refresh(row)
    return _resignation_to_out(row, emp)


def delete_resignation(db: Session, emp_id: UUID, resignation_id: UUID) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeResignation)
        .filter(
            EmployeeResignation.id == resignation_id,
            EmployeeResignation.employee_id == emp.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy lần nghỉ việc.")
    db.delete(row)
    db.commit()
    return {"detail": "Trợ Lý AI: đã xóa lần nghỉ việc."}


def _build_resignation_snapshot(db: Session, emp: Employee) -> dict:
    """Lưu mốc lương/PC/join_date trước khi chốt nghỉ — phục vụ tái tuyển giữ quyền lợi."""
    rows = (
        db.query(EmployeeAllowanceAssignment, PayComponent)
        .join(PayComponent, PayComponent.id == EmployeeAllowanceAssignment.allowance_type_id)
        .filter(EmployeeAllowanceAssignment.employee_id == emp.id)
        .order_by(PayComponent.code.asc())
        .all()
    )
    return {
        "join_date": emp.join_date.isoformat() if emp.join_date else None,
        "contract_salary": str(emp.contract_salary or Decimal("0")),
        "probation_salary": str(emp.probation_salary or Decimal("0")),
        "pay_channel": emp.pay_channel,
        "allowances": [
            {"code": pc.code, "amount": str(asg.amount or Decimal("0"))} for asg, pc in rows
        ],
    }


def _clear_employee_allowances(db: Session, employee_id: UUID) -> None:
    db.query(EmployeeAllowanceAssignment).filter(
        EmployeeAllowanceAssignment.employee_id == employee_id
    ).delete(synchronize_session=False)


def _restore_allowances_from_snapshot(db: Session, emp: Employee, snapshot: dict) -> None:
    for item in snapshot.get("allowances") or []:
        code = str(item.get("code") or "").strip().upper()
        if not code:
            continue
        pc = (
            db.query(PayComponent)
            .filter(PayComponent.code == code, PayComponent.is_active.is_(True))
            .one_or_none()
        )
        if pc is None:
            continue
        amount = D(item.get("amount") or "0")
        row = (
            db.query(EmployeeAllowanceAssignment)
            .filter(
                EmployeeAllowanceAssignment.employee_id == emp.id,
                EmployeeAllowanceAssignment.allowance_type_id == pc.id,
            )
            .one_or_none()
        )
        if row is None:
            row = EmployeeAllowanceAssignment(
                employee_id=emp.id,
                allowance_type_id=pc.id,
                amount=amount,
            )
            db.add(row)
        else:
            row.amount = amount


def _ensure_open_resignation_record(db: Session, emp: Employee) -> EmployeeResignation:
    """NV «Đã nghỉ» nhưng chưa có lần nghỉ (import/legacy) — tạo hồi tố để tái tuyển."""

    last = (
        db.query(EmployeeResignation)
        .filter(EmployeeResignation.employee_id == emp.id)
        .order_by(EmployeeResignation.seq_no.desc())
        .first()
    )
    if last is not None:
        if last.snapshot_json is None and last.rehired_at is None:
            last.snapshot_json = _build_resignation_snapshot(db, emp)
        return last

    lwd = emp.resign_date or date.today()
    row = EmployeeResignation(
        employee_id=emp.id,
        seq_no=_next_resignation_seq(db, emp.id),
        resign_type_code="DPR",
        last_working_date=lwd,
        reason="Ghi nhận hồi tố (import/legacy)",
        snapshot_json=_build_resignation_snapshot(db, emp),
    )
    db.add(row)
    db.flush()
    return row


def _finalize_resignation_on_status_change(db: Session, emp: Employee) -> None:
    """Khi HR chuyển trạng thái sang «Đã nghỉ» — ghi nhận lần nghỉ + snapshot."""

    open_row = (
        db.query(EmployeeResignation)
        .filter(
            EmployeeResignation.employee_id == emp.id,
            EmployeeResignation.rehired_at.is_(None),
        )
        .order_by(EmployeeResignation.seq_no.desc())
        .first()
    )
    if open_row is not None:
        if open_row.snapshot_json is None:
            open_row.snapshot_json = _build_resignation_snapshot(db, emp)
        return

    lwd = emp.resign_date or date.today()
    db.add(
        EmployeeResignation(
            employee_id=emp.id,
            seq_no=_next_resignation_seq(db, emp.id),
            resign_type_code="DPR",
            last_working_date=lwd,
            reason="Ghi nhận qua hồ sơ NV",
            snapshot_json=_build_resignation_snapshot(db, emp),
        )
    )


def rehire_employee(db: Session, emp_id: UUID, body: EmployeeRehireRequest) -> EmployeeRehireOut:
    """Tái tuyển NV đã nghỉ — fresh_start (mặc định) hoặc continuity (ưu ái)."""

    emp = _get_employee_row(db, emp_id)
    if emp.status != "resigned":
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: chỉ tái tuyển nhân viên đang ở trạng thái «Đã nghỉ».",
        )

    last_resign = (
        db.query(EmployeeResignation)
        .filter(EmployeeResignation.employee_id == emp.id)
        .order_by(EmployeeResignation.seq_no.desc())
        .first()
    )
    if last_resign is None:
        last_resign = _ensure_open_resignation_record(db, emp)
    elif last_resign.snapshot_json is None and last_resign.rehired_at is None:
        last_resign.snapshot_json = _build_resignation_snapshot(db, emp)
    if last_resign.rehired_at is not None:
        raise HTTPException(
            status_code=400,
            detail="Trợ Lý AI: lần nghỉ gần nhất đã được đánh dấu tái tuyển.",
        )

    team = db.get(Team, body.team_id)
    if team is None:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: Tổ không tồn tại.")
    today = date.today()
    if team.effective_to is not None and team.effective_to < today:
        raise HTTPException(status_code=400, detail=f"Trợ Lý AI: Tổ '{team.code}' không còn hiệu lực.")

    mode = body.rehire_mode
    snap = last_resign.snapshot_json or {}

    if mode == "continuity":
        reason = (body.rehire_reason or "").strip()
        if not reason:
            raise HTTPException(
                status_code=400,
                detail="Trợ Lý AI: tái tuyển giữ quyền lợi cần ghi lý do (ưu ái).",
            )
        if snap.get("join_date"):
            emp.join_date = date.fromisoformat(str(snap["join_date"]))
        emp.contract_salary = D(snap.get("contract_salary") or emp.contract_salary)
        emp.probation_salary = D(snap.get("probation_salary") or emp.probation_salary)
        emp.pay_channel = snap.get("pay_channel") or emp.pay_channel
        _clear_employee_allowances(db, emp.id)
        _restore_allowances_from_snapshot(db, emp, snap)
        msg = f"Trợ Lý AI: tái tuyển giữ quyền lợi MSNV {emp.employee_code}."
        last_resign.rehire_reason = reason
    else:
        if body.contract_salary is None or money_vnd(body.contract_salary) <= 0:
            raise HTTPException(
                status_code=400,
                detail="Trợ Lý AI: tái tuyển thường cần lương HĐ mới > 0.",
            )
        emp.join_date = body.rehire_date
        emp.contract_salary = body.contract_salary
        emp.probation_salary = body.probation_salary or Decimal("0")
        _clear_employee_allowances(db, emp.id)
        _record_salary_history(
            db,
            employee_id=emp.id,
            field_code="contract_salary",
            effective_from=body.rehire_date,
            old_value=Decimal("0"),
            new_value=body.contract_salary,
            note="Tái tuyển — lương mới",
        )
        if money_vnd(emp.probation_salary) > 0:
            _record_salary_history(
                db,
                employee_id=emp.id,
                field_code="probation_salary",
                effective_from=body.rehire_date,
                old_value=Decimal("0"),
                new_value=emp.probation_salary,
                note="Tái tuyển — lương TV",
            )
        msg = f"Trợ Lý AI: tái tuyển thường MSNV {emp.employee_code} — thâm niên & phụ cấp tính lại."

    emp.status = body.status
    emp.resign_date = None
    emp.team_id = team.id
    last_resign.rehired_at = body.rehire_date
    last_resign.rehire_mode = mode
    if mode == "fresh_start":
        last_resign.rehire_reason = None

    _sync_worker_on_status(db, emp)
    if emp.status == "probation" and emp.join_date:
        lcf.bootstrap_first_contract(db, emp, sign_date=body.rehire_date)

    write_audit(
        db,
        actor=None,
        action="employee.rehire",
        entity_type="employees",
        entity_id=str(emp.id),
        summary=f"Tái tuyển {emp.employee_code} mode={mode}",
        meta={"rehire_mode": mode, "rehire_date": str(body.rehire_date)},
        commit=False,
    )
    db.commit()
    out = get_employee(db, emp.id)
    return EmployeeRehireOut(employee=out, rehire_mode=mode, message=msg)


# --- 5.1 employee profile subrecords ---


def list_educations(db: Session, emp_id: UUID) -> list[EmployeeEducationOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeEducation)
        .filter(EmployeeEducation.employee_id == emp.id)
        .order_by(EmployeeEducation.from_date.desc().nullslast(), EmployeeEducation.created_at.desc())
        .all()
    )
    return [EmployeeEducationOut.model_validate(r) for r in rows]


def create_education(
    db: Session, emp_id: UUID, body: EmployeeEducationCreate
) -> EmployeeEducationOut:
    emp = _get_employee_row(db, emp_id)
    if body.from_date and body.to_date and body.to_date < body.from_date:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: to_date không được trước from_date.")
    row = EmployeeEducation(
        employee_id=emp.id,
        from_date=body.from_date,
        to_date=body.to_date,
        school_name=body.school_name.strip(),
        major=body.major,
        degree_code=body.degree_code,
        note=body.note or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return EmployeeEducationOut.model_validate(row)


def update_education(
    db: Session, emp_id: UUID, row_id: UUID, body: EmployeeEducationUpdate
) -> EmployeeEducationOut:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeEducation)
        .filter(EmployeeEducation.id == row_id, EmployeeEducation.employee_id == emp.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bản ghi đào tạo.")
    if body.from_date is not None:
        row.from_date = body.from_date
    if body.to_date is not None:
        row.to_date = body.to_date
    if body.school_name is not None:
        row.school_name = body.school_name.strip()
    if body.major is not None:
        row.major = body.major
    if body.degree_code is not None:
        row.degree_code = body.degree_code
    if body.note is not None:
        row.note = body.note
    fd, td = row.from_date, row.to_date
    if fd and td and td < fd:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: to_date không được trước from_date.")
    db.commit()
    db.refresh(row)
    return EmployeeEducationOut.model_validate(row)


def delete_education(db: Session, emp_id: UUID, row_id: UUID) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeEducation)
        .filter(EmployeeEducation.id == row_id, EmployeeEducation.employee_id == emp.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bản ghi đào tạo.")
    db.delete(row)
    db.commit()
    return {"detail": "Trợ Lý AI: đã xóa bản ghi đào tạo."}


def list_experiences(db: Session, emp_id: UUID) -> list[EmployeeExperienceOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeExperience)
        .filter(EmployeeExperience.employee_id == emp.id)
        .order_by(EmployeeExperience.from_date.desc().nullslast(), EmployeeExperience.created_at.desc())
        .all()
    )
    return [EmployeeExperienceOut.model_validate(r) for r in rows]


def create_experience(
    db: Session, emp_id: UUID, body: EmployeeExperienceCreate
) -> EmployeeExperienceOut:
    emp = _get_employee_row(db, emp_id)
    if body.from_date and body.to_date and body.to_date < body.from_date:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: to_date không được trước from_date.")
    row = EmployeeExperience(
        employee_id=emp.id,
        from_date=body.from_date,
        to_date=body.to_date,
        company_name=body.company_name.strip(),
        position_title=body.position_title,
        note=body.note or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return EmployeeExperienceOut.model_validate(row)


def update_experience(
    db: Session, emp_id: UUID, row_id: UUID, body: EmployeeExperienceUpdate
) -> EmployeeExperienceOut:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeExperience)
        .filter(EmployeeExperience.id == row_id, EmployeeExperience.employee_id == emp.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bản ghi kinh nghiệm.")
    if body.from_date is not None:
        row.from_date = body.from_date
    if body.to_date is not None:
        row.to_date = body.to_date
    if body.company_name is not None:
        row.company_name = body.company_name.strip()
    if body.position_title is not None:
        row.position_title = body.position_title
    if body.note is not None:
        row.note = body.note
    fd, td = row.from_date, row.to_date
    if fd and td and td < fd:
        raise HTTPException(status_code=400, detail="Trợ Lý AI: to_date không được trước from_date.")
    db.commit()
    db.refresh(row)
    return EmployeeExperienceOut.model_validate(row)


def delete_experience(db: Session, emp_id: UUID, row_id: UUID) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeExperience)
        .filter(EmployeeExperience.id == row_id, EmployeeExperience.employee_id == emp.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bản ghi kinh nghiệm.")
    db.delete(row)
    db.commit()
    return {"detail": "Trợ Lý AI: đã xóa bản ghi kinh nghiệm."}


def list_health_checks(db: Session, emp_id: UUID) -> list[EmployeeHealthCheckOut]:
    emp = _get_employee_row(db, emp_id)
    rows = (
        db.query(EmployeeHealthCheck)
        .filter(EmployeeHealthCheck.employee_id == emp.id)
        .order_by(EmployeeHealthCheck.check_date.desc(), EmployeeHealthCheck.created_at.desc())
        .all()
    )
    return [EmployeeHealthCheckOut.model_validate(r) for r in rows]


def create_health_check(
    db: Session, emp_id: UUID, body: EmployeeHealthCheckCreate
) -> EmployeeHealthCheckOut:
    emp = _get_employee_row(db, emp_id)
    row = EmployeeHealthCheck(
        employee_id=emp.id,
        check_date=body.check_date,
        facility_name=body.facility_name,
        result_summary=body.result_summary,
        note=body.note or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return EmployeeHealthCheckOut.model_validate(row)


def update_health_check(
    db: Session, emp_id: UUID, row_id: UUID, body: EmployeeHealthCheckUpdate
) -> EmployeeHealthCheckOut:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeHealthCheck)
        .filter(EmployeeHealthCheck.id == row_id, EmployeeHealthCheck.employee_id == emp.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bản ghi khám sức khỏe.")
    if body.check_date is not None:
        row.check_date = body.check_date
    if body.facility_name is not None:
        row.facility_name = body.facility_name
    if body.result_summary is not None:
        row.result_summary = body.result_summary
    if body.note is not None:
        row.note = body.note
    db.commit()
    db.refresh(row)
    return EmployeeHealthCheckOut.model_validate(row)


def delete_health_check(db: Session, emp_id: UUID, row_id: UUID) -> dict[str, str]:
    emp = _get_employee_row(db, emp_id)
    row = (
        db.query(EmployeeHealthCheck)
        .filter(EmployeeHealthCheck.id == row_id, EmployeeHealthCheck.employee_id == emp.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Trợ Lý AI: không tìm thấy bản ghi khám sức khỏe.")
    db.delete(row)
    db.commit()
    return {"detail": "Trợ Lý AI: đã xóa bản ghi khám sức khỏe."}


# --- HR biến động (23§23.4) ---


def list_hr_movements(
    db: Session,
    *,
    employee_id: UUID | None = None,
    limit: int = 300,
) -> list[HrMovementOut]:
    """Hợp nhất assignment + salary_history + vi phạm (23§23.4)."""
    cap = max(1, min(limit, 500))
    items: list[HrMovementOut] = []

    assign_q = (
        db.query(EmployeeAssignment, Employee)
        .join(Employee, Employee.id == EmployeeAssignment.employee_id)
        .filter(Employee.deleted_at.is_(None))
    )
    if employee_id is not None:
        assign_q = assign_q.filter(EmployeeAssignment.employee_id == employee_id)
    assign_rows = (
        assign_q.options(joinedload(EmployeeAssignment.team))
        .order_by(EmployeeAssignment.effective_from.desc())
        .limit(cap)
        .all()
    )

    sal_q = (
        db.query(EmployeeSalaryHistory, Employee)
        .join(Employee, Employee.id == EmployeeSalaryHistory.employee_id)
        .filter(Employee.deleted_at.is_(None))
    )
    if employee_id is not None:
        sal_q = sal_q.filter(EmployeeSalaryHistory.employee_id == employee_id)
    sal_rows = (
        sal_q.order_by(
            EmployeeSalaryHistory.effective_from.desc(),
            EmployeeSalaryHistory.created_at.desc(),
        )
        .limit(cap)
        .all()
    )

    viol_q = (
        db.query(EmployeeViolation, Employee)
        .join(Employee, Employee.id == EmployeeViolation.employee_id)
        .filter(Employee.deleted_at.is_(None))
    )
    if employee_id is not None:
        viol_q = viol_q.filter(EmployeeViolation.employee_id == employee_id)
    viol_rows = viol_q.order_by(EmployeeViolation.occurred_at.desc()).limit(cap).all()

    approver_ids: set[UUID] = set()
    for a, _ in assign_rows:
        if a.approved_by:
            approver_ids.add(a.approved_by)
    for s, _ in sal_rows:
        if s.approved_by:
            approver_ids.add(s.approved_by)
    approvers = (
        {u.id: u.full_name for u in db.query(User).filter(User.id.in_(approver_ids)).all()}
        if approver_ids
        else {}
    )

    for a, emp in assign_rows:
        team_label = a.team.name if a.team else "—"
        items.append(
            HrMovementOut(
                id=f"asg-{a.id}",
                movement_type="assignment",
                occurred_at=a.effective_from,
                employee_id=emp.id,
                employee_code=emp.employee_code,
                full_name=emp.full_name,
                summary=f"Chuyển tổ → {team_label}",
                value_before=None,
                value_after=team_label,
                decision_no=a.decision_no,
                approved_by_name=approvers.get(a.approved_by),
            )
        )

    for s, emp in sal_rows:
        label = SALARY_FIELD_LABELS.get(s.field_code, s.field_code)
        items.append(
            HrMovementOut(
                id=f"sal-{s.id}",
                movement_type="salary",
                occurred_at=s.effective_from,
                employee_id=emp.id,
                employee_code=emp.employee_code,
                full_name=emp.full_name,
                summary=f"Điều chỉnh {label}" + (f" — {s.note}" if s.note else ""),
                value_before=_fmt_vnd_display(s.old_value),
                value_after=_fmt_vnd_display(s.new_value),
                decision_no=s.decision_no,
                approved_by_name=approvers.get(s.approved_by),
            )
        )

    for v, emp in viol_rows:
        items.append(
            HrMovementOut(
                id=f"viol-{v.id}",
                movement_type="violation",
                occurred_at=v.occurred_at,
                employee_id=emp.id,
                employee_code=emp.employee_code,
                full_name=emp.full_name,
                summary=v.title,
                value_before=None,
                value_after=v.penalty or None,
                decision_no=None,
                approved_by_name=None,
            )
        )

    def _sort_key(m: HrMovementOut) -> datetime:
        if isinstance(m.occurred_at, datetime):
            return m.occurred_at
        return datetime.combine(m.occurred_at, datetime.min.time()).replace(tzinfo=timezone.utc)

    items.sort(key=_sort_key, reverse=True)
    return items[:cap]
