import csv
import io
import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import ReportKind, ReportStatus
from app.models.insights import Report
from app.schemas.schemas import TimeFilter
from app.services import ai as ai_service
from app.services.map import cell_rows

COLUMNS = ["h3_index", "laeq_db", "lmax_db", "measurement_count", "device_count", "confidence",
           "calibrated_share", "last_measurement_at"]


def build_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r["h3_index"], r["laeq_db"], r["lmax_db"], r["measurement_count"], r["device_count"],
                    r["confidence"], r["calibrated_share"], r["last_measurement_at"].isoformat()])
    return buf.getvalue().encode("utf-8-sig")


def build_pdf(rows: list[dict], params: dict, narrative: dict | None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Rapport Data Map")
    story = [Paragraph("Rapport SunuSound — cartographie sonore indicative", styles["Title"]),
             Paragraph(f"Période : {params['period']} — plage : {params['day_part']} — "
                       f"indicatives incluses : {'oui' if params['include_indicative'] else 'non'}", styles["Normal"]),
             Paragraph("Mesures participatives : indicatives, non certifiées réglementairement.", styles["Italic"]),
             Spacer(1, 12)]
    if narrative:
        story.append(Paragraph("Synthèse (générée par IA)", styles["Heading2"]))
        story.append(Paragraph(narrative["executive_summary"], styles["Normal"]))
        for title, key in (("Constats", "key_findings"), ("Recommandations", "recommendations"),
                           ("Limites", "limitations")):
            if narrative.get(key):
                story.append(Paragraph(title, styles["Heading3"]))
                for item in narrative[key]:
                    story.append(Paragraph(f"• {item}", styles["Normal"]))
        story.append(Spacer(1, 12))
    data = [["Cellule H3", "LAeq (dB)", "Lmax (dB)", "Mesures", "Appareils", "Confiance"]]
    for r in rows[:2000]:
        data.append([r["h3_index"], r["laeq_db"], r["lmax_db"], r["measurement_count"], r["device_count"], r["confidence"]])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("FONTSIZE", (0, 0), (-1, -1), 8),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.grey)]))
    story.append(table)
    doc.build(story)
    return buf.getvalue()


async def generate(db: AsyncSession, redis: Redis, tenant_id: uuid.UUID, report_id: uuid.UUID) -> None:
    report = await db.get(Report, report_id, with_for_update=True)
    if report is None or report.status not in (ReportStatus.pending, ReportStatus.failed):
        return
    report.status = ReportStatus.running
    await db.commit()
    try:
        p = report.params
        tf = TimeFilter(period=p["period"], date_from=_d(p.get("date_from")), date_to=_d(p.get("date_to")),
                        day_part=p["day_part"])
        rows = await cell_rows(db, tenant_id, tf, p["include_indicative"], min_devices=1)
        rows.sort(key=lambda r: r["laeq_db"], reverse=True)
        if report.kind == ReportKind.csv:
            report.content, report.content_type = build_csv(rows), "text/csv"
            report.filename = f"sunusound-{report.id}.csv"
        else:
            narrative = None
            if p.get("include_ai_summary") and rows:
                narrative = await ai_service.summarize_report(redis, tenant_id, {
                    "period": {"period": p["period"], "day_part": p["day_part"]},
                    "cells_count": len(rows),
                    "top_cells": [{k: r[k] for k in ("laeq_db", "lmax_db", "measurement_count", "device_count", "confidence")}
                                  for r in rows[:20]],
                    "overall_median_laeq_db": sorted(r["laeq_db"] for r in rows)[len(rows) // 2]})
            report.content, report.content_type = build_pdf(rows, p, narrative), "application/pdf"
            report.filename = f"sunusound-{report.id}.pdf"
        report.status = ReportStatus.done
    except Exception as exc:                       # message court, sans donnée sensible
        report.status, report.error = ReportStatus.failed, f"{type(exc).__name__}"
    report.finished_at = datetime.now(timezone.utc)
    await db.commit()


def _d(value):
    from datetime import date
    return date.fromisoformat(value) if value else None