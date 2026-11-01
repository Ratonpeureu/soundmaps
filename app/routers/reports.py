import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_roles
from app.core.rate_limit import limit_by_user
from app.models.enums import ReportStatus, Role
from app.models.insights import Report
from app.models.tenancy import User
from app.schemas.schemas import ReportCreate, ReportOut
from app.services.audit import audit
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/reports", tags=["reports"],
                   dependencies=[Depends(limit_by_user("reports", settings.RATE_LIMIT_API_PER_MINUTE))])
_staff = require_roles(Role.tenant_admin, Role.analyst)


@router.post("", response_model=ReportOut, status_code=202)
async def create(payload: ReportCreate, request: Request, user: User = Depends(_staff),
                 db: AsyncSession = Depends(get_db)):
    params = payload.model_dump(mode="json", exclude={"kind"})
    report = Report(tenant_id=user.tenant_id, requested_by=user.id, kind=payload.kind,
                    status=ReportStatus.pending, params=params)
    db.add(report)
    await audit(db, user, "report.create", "report", report.id, {"kind": payload.kind.value}, request)
    await db.commit()
    celery_app.send_task("reports.generate", args=[str(user.tenant_id), str(report.id)])
    return report


@router.get("", response_model=list[ReportOut])
async def list_reports(limit: int = Query(50, ge=1, le=200), user: User = Depends(_staff),
                       db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Report).where(Report.tenant_id == user.tenant_id)
                             .order_by(Report.created_at.desc()).limit(limit))).scalars().all()


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: uuid.UUID, user: User = Depends(_staff), db: AsyncSession = Depends(get_db)):
    report = await db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "report_not_found")
    return report


@router.get("/{report_id}/download")
async def download(report_id: uuid.UUID, request: Request, user: User = Depends(_staff),
                   db: AsyncSession = Depends(get_db)):
    report = await db.get(Report, report_id)
    if report is None or report.status != ReportStatus.done or report.content is None:
        raise HTTPException(404, "report_not_available")
    await audit(db, user, "report.download", "report", report.id, request=request)
    await db.commit()
    return Response(report.content, media_type=report.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{report.filename}"'})