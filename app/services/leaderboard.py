import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_settings import account_settings as acs
from app.models.enums import MissionStatus, QualityStatus
from app.models.measurement import Measurement
from app.models.quota import Mission
from app.models.social import LeaderboardEntry, LeaderboardSnapshot, ReviewVote, UserBadge, UserProfile
from app.models.tenancy import User
from app.services.geo import TZ, to_local

PERIODS = ("week", "month", "season")
LABELS = {"week": "Semaine", "month": "Mois", "season": "Saison (90 j)"}
LEVELS = [(0, "Écouteur"), (100, "Mesureur"), (300, "Observateur"), (700, "Cartographe"),
          (1500, "Expert"), (3000, "Maître du son"), (6000, "Légende")]
BADGES = {  # code: (libellé, description, icône, palier)
    "first_measure": ("Premier relevé", "Une première mesure validée", "flag", 1),
    "measures_50": ("Collecteur", "50 mesures validées sur la saison", "bolt", 2),
    "measures_200": ("Bourreau de travail", "200 mesures validées sur la saison", "bolt", 4),
    "reliable": ("Fiable", "Fiabilité d'au moins 85 % sur 20 mesures", "verified", 3),
    "explorer": ("Explorateur", "25 zones couvertes sur la saison", "explore", 3),
    "regular": ("Régulier", "20 jours d'activité sur la saison", "event", 3),
    "missions_5": ("Missionnaire", "5 missions réussies sur la saison", "flag", 3),
    "sponsor": ("Parrain", "3 filleuls actifs sur la saison", "group", 3),
    "analyst": ("Oreille experte", "20 avis concordants sur la saison", "hearing", 4),
    "podium": ("Podium", "Dans le top 3 de la semaine", "trophy", 5),
}


def level_info(xp: int) -> dict:
    idx = max(i for i, (t, _) in enumerate(LEVELS) if xp >= t)
    nxt = LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None
    prog = 1.0 if nxt is None else (xp - LEVELS[idx][0]) / (nxt - LEVELS[idx][0])
    return {"index": idx, "name": LEVELS[idx][1], "xp": xp, "next_xp": nxt, "progress": round(prog, 3)}


def bounds(kind: str, now: datetime) -> tuple[datetime, datetime, str]:
    local = to_local(now)
    if kind == "week":
        d = local.date() - timedelta(days=local.weekday())
        y, w, _ = d.isocalendar()
        key = f"{y}-W{w:02d}"
    elif kind == "month":
        d = local.date().replace(day=1)
        key = f"{d:%Y-%m}"
    else:
        d = local.date() - timedelta(days=89)
        key = "season"
    return datetime.combine(d, time.min, TZ).astimezone(timezone.utc), now, key


async def points_for_window(db: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime) -> dict:
    out: dict = {}

    def c(uid):
        return out.setdefault(uid, {"v": 0, "i": 0, "r": 0, "days": 0, "cells": 0, "missions": 0, "units": 0,
                                    "reviews": 0, "invites": 0})
    base = [Measurement.tenant_id == tenant_id, Measurement.user_id.is_not(None),
            Measurement.measured_at >= start, Measurement.measured_at < end]
    keymap = {QualityStatus.validated: "v", QualityStatus.indicative: "i", QualityStatus.rejected: "r"}
    for uid, st, n in (await db.execute(select(Measurement.user_id, Measurement.quality_status, func.count())
                                        .where(*base, Measurement.quality_status != QualityStatus.pending)
                                        .group_by(Measurement.user_id, Measurement.quality_status))).all():
        c(uid)[keymap[st]] += int(n)
    for uid, days, cells in (await db.execute(
            select(Measurement.user_id, func.count(func.distinct(Measurement.local_date)),
                   func.count(func.distinct(Measurement.h3_index)))
            .where(*base, Measurement.quality_status.in_([QualityStatus.validated, QualityStatus.indicative]))
            .group_by(Measurement.user_id))).all():
        c(uid)["days"], c(uid)["cells"] = int(days), int(cells)
    for uid, n, units in (await db.execute(
            select(Mission.user_id, func.count(), func.coalesce(func.sum(Mission.required_count), 0))
            .where(Mission.tenant_id == tenant_id, Mission.status == MissionStatus.completed,
                   Mission.finished_at >= start, Mission.finished_at < end).group_by(Mission.user_id))).all():
        c(uid)["missions"], c(uid)["units"] = int(n), int(units)
    for uid, n in (await db.execute(
            select(ReviewVote.reviewer_id, func.count()).where(
                ReviewVote.tenant_id == tenant_id, ReviewVote.matched.is_(True),
                ReviewVote.voted_at >= start, ReviewVote.voted_at < end).group_by(ReviewVote.reviewer_id))).all():
        c(uid)["reviews"] = int(n)
    for uid, n in (await db.execute(
            select(UserProfile.invited_by, func.count()).where(
                UserProfile.tenant_id == tenant_id, UserProfile.invited_by.is_not(None),
                UserProfile.invite_credited_at >= start, UserProfile.invite_credited_at < end)
            .group_by(UserProfile.invited_by))).all():
        c(uid)["invites"] = int(n)

    s, p0 = acs.LB_RELIABILITY_STRENGTH, acs.LB_RELIABILITY_PRIOR
    for d in out.values():
        n = d["v"] + d["i"] + d["r"]
        d["reliability"] = (d["v"] + 0.5 * d["i"] + s * p0) / (n + s)
        d["raw"] = (d["v"] * acs.LB_PTS_VALIDATED + d["i"] * acs.LB_PTS_INDICATIVE + d["r"] * acs.LB_PTS_REJECTED
                    + d["cells"] * acs.LB_PTS_CELL + d["days"] * acs.LB_PTS_DAY + d["missions"] * acs.LB_PTS_MISSION
                    + d["units"] * acs.LB_PTS_MISSION_UNIT + d["reviews"] * acs.LB_PTS_REVIEW
                    + d["invites"] * acs.LB_PTS_INVITE)
        d["score"] = round(max(0.0, d["raw"]) * (0.6 + 0.8 * d["reliability"]), 1)
    return out


async def credit_invites(db: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> None:
    rows = (await db.execute(select(UserProfile.user_id).where(
        UserProfile.tenant_id == tenant_id, UserProfile.invited_by.is_not(None),
        UserProfile.invite_credited_at.is_(None)).limit(2000))).scalars().all()
    if not rows:
        return
    counts = dict((await db.execute(select(Measurement.user_id, func.count()).where(
        Measurement.tenant_id == tenant_id, Measurement.user_id.in_(rows),
        Measurement.quality_status == QualityStatus.validated).group_by(Measurement.user_id))).all())
    ok = [u for u in rows if counts.get(u, 0) >= 3]
    if ok:
        await db.execute(update(UserProfile).where(UserProfile.user_id.in_(ok)).values(invite_credited_at=now))


async def refresh_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    await credit_invites(db, tenant_id, now)
    visible = dict((await db.execute(select(UserProfile.user_id, UserProfile.show_in_leaderboard).where(
        UserProfile.tenant_id == tenant_id))).all())
    season = {}
    week_ranks: dict = {}
    total = 0
    for kind in PERIODS:
        start, end, key = bounds(kind, now)
        comps = await points_for_window(db, tenant_id, start, now)
        ranked = sorted(((u, d) for u, d in comps.items() if d["score"] > 0),
                        key=lambda t: (-t[1]["score"], -t[1]["v"], str(t[0])))
        rows, rank = [], 0
        for uid, d in ranked:
            shown = visible.get(uid, True)
            rank += 1 if shown else 0
            rows.append({"id": uuid.uuid4(), "tenant_id": tenant_id, "period_kind": kind, "period_key": key,
                         "user_id": uid, "score": d["score"], "rank": rank if shown else 0,
                         "reliability": round(d["reliability"], 4),
                         "components": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()},
                         "updated_at": now})
        await db.execute(delete(LeaderboardEntry).where(
            LeaderboardEntry.tenant_id == tenant_id, LeaderboardEntry.period_kind == kind,
            LeaderboardEntry.period_key == key))
        for i in range(0, len(rows), 1000):
            await db.execute(pg_insert(LeaderboardEntry).values(rows[i:i + 1000]))
        total += len(rows)
        if kind == "season":
            season = comps
        if kind == "week":
            week_ranks = {r["user_id"]: r["rank"] for r in rows if r["rank"] > 0}
        await _snapshot(db, tenant_id, kind, rows, now)
    await _award_badges(db, tenant_id, season, week_ranks)
    return total


async def _snapshot(db: AsyncSession, tenant_id, kind: str, rows: list, now: datetime) -> None:
    today = to_local(now).date()
    snaps = [{"id": uuid.uuid4(), "tenant_id": tenant_id, "user_id": r["user_id"], "period_kind": kind,
              "snap_date": today, "rank": r["rank"], "score": r["score"]} for r in rows if r["rank"] > 0]
    for i in range(0, len(snaps), 1000):
        await db.execute(pg_insert(LeaderboardSnapshot).values(snaps[i:i + 1000]).on_conflict_do_update(
            constraint="uq_lb_snapshot", set_={"rank": pg_insert(LeaderboardSnapshot).excluded.rank,
                                               "score": pg_insert(LeaderboardSnapshot).excluded.score}))


async def _award_badges(db: AsyncSession, tenant_id, comps: dict, week_ranks: dict) -> None:
    earned = []
    for uid, d in comps.items():
        n = d["v"] + d["i"] + d["r"]
        codes = []
        if d["v"] >= 1: codes.append("first_measure")
        if d["v"] >= 50: codes.append("measures_50")
        if d["v"] >= 200: codes.append("measures_200")
        if n >= 20 and d["reliability"] >= 0.85: codes.append("reliable")
        if d["cells"] >= 25: codes.append("explorer")
        if d["days"] >= 20: codes.append("regular")
        if d["missions"] >= 5: codes.append("missions_5")
        if d["invites"] >= 3: codes.append("sponsor")
        if d["reviews"] >= 20: codes.append("analyst")
        earned += [(uid, c) for c in codes]
    earned += [(uid, "podium") for uid, r in week_ranks.items() if r <= 3]
    rows = [{"id": uuid.uuid4(), "tenant_id": tenant_id, "user_id": u, "code": c} for u, c in earned]
    for i in range(0, len(rows), 1000):
        await db.execute(pg_insert(UserBadge).values(rows[i:i + 1000]).on_conflict_do_nothing(constraint="uq_user_badge"))


async def xp_catchup(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Crédite l'XP jour par jour (fenêtres locales complètes), sans dépendre de la rétention des identités."""
    today = to_local(datetime.now(timezone.utc)).date()
    yesterday = today - timedelta(days=1)
    rows = (await db.execute(select(UserProfile.user_id, UserProfile.xp_through, UserProfile.created_at)
                             .where(UserProfile.tenant_id == tenant_id))).all()
    pending = [(u, t or (to_local(c).date() - timedelta(days=1))) for u, t, c in rows]
    pending = [(u, t) for u, t in pending if t < yesterday]
    if not pending:
        return 0
    first = max(min(t for _, t in pending) + timedelta(days=1), yesterday - timedelta(days=6))
    delta: dict = {}
    d = first
    while d <= yesterday:
        start = datetime.combine(d, time.min, TZ).astimezone(timezone.utc)
        comps = await points_for_window(db, tenant_id, start, start + timedelta(days=1))
        for u, t in pending:
            if t < d and u in comps:
                delta[u] = delta.get(u, 0.0) + max(0.0, comps[u]["raw"])
        d += timedelta(days=1)
    for u, _ in pending:
        await db.execute(update(UserProfile).where(UserProfile.user_id == u).values(
            xp=UserProfile.xp + int(round(delta.get(u, 0.0))), xp_through=yesterday))
    return len(pending)


# ------------------------------------------------------------------ lecture

def _badge_codes(codes: list[str]) -> list[str]:
    return sorted(set(codes), key=lambda c: -BADGES.get(c, ("", "", "", 0))[3])


async def board(db: AsyncSession, user: User, kind: str, limit: int) -> dict:
    if kind not in PERIODS:
        kind = "week"
    limit = max(1, min(limit, acs.LB_MAX_LIMIT))
    now = datetime.now(timezone.utc)
    _, _, key = bounds(kind, now)
    base = (select(LeaderboardEntry, User.pseudonym, UserProfile.display_name, UserProfile.xp)
            .join(User, User.id == LeaderboardEntry.user_id)
            .outerjoin(UserProfile, UserProfile.user_id == LeaderboardEntry.user_id)
            .where(LeaderboardEntry.tenant_id == user.tenant_id, LeaderboardEntry.period_kind == kind,
                   LeaderboardEntry.period_key == key))
    top = (await db.execute(base.where(LeaderboardEntry.rank > 0, LeaderboardEntry.rank <= limit)
                            .order_by(LeaderboardEntry.rank))).all()
    me = (await db.execute(base.where(LeaderboardEntry.user_id == user.id))).first()
    total = (await db.execute(select(func.count()).select_from(LeaderboardEntry).where(
        LeaderboardEntry.tenant_id == user.tenant_id, LeaderboardEntry.period_kind == kind,
        LeaderboardEntry.period_key == key, LeaderboardEntry.rank > 0))).scalar_one()
    around = []
    if me and me[0].rank > limit:
        around = (await db.execute(base.where(LeaderboardEntry.rank.between(me[0].rank - 2, me[0].rank + 2))
                                   .order_by(LeaderboardEntry.rank))).all()
    rows = list({r[0].user_id: r for r in [*top, *around, *([me] if me else [])]}.values())
    ids = [r[0].user_id for r in rows]
    prev_date = (await db.execute(select(func.max(LeaderboardSnapshot.snap_date)).where(
        LeaderboardSnapshot.tenant_id == user.tenant_id, LeaderboardSnapshot.period_kind == kind,
        LeaderboardSnapshot.snap_date < to_local(now).date()))).scalar_one()
    prev = {}
    if prev_date and ids:
        prev = dict((await db.execute(select(LeaderboardSnapshot.user_id, LeaderboardSnapshot.rank).where(
            LeaderboardSnapshot.tenant_id == user.tenant_id, LeaderboardSnapshot.period_kind == kind,
            LeaderboardSnapshot.snap_date == prev_date, LeaderboardSnapshot.user_id.in_(ids)))).all())
    badges: dict = {}
    if ids:
        for uid, code in (await db.execute(select(UserBadge.user_id, UserBadge.code).where(
                UserBadge.user_id.in_(ids)))).all():
            badges.setdefault(uid, []).append(code)

    def entry(r):
        e, pseudo, dname, xp = r
        comp = e.components or {}
        p = prev.get(e.user_id)
        return {"rank": e.rank, "display_name": dname or pseudo, "level": level_info(xp or 0), "score": e.score,
                "reliability": round(e.reliability, 3), "delta": (p - e.rank) if p and e.rank > 0 else None,
                "is_me": e.user_id == user.id, "badges": _badge_codes(badges.get(e.user_id, []))[:4],
                "validated": comp.get("v", 0), "missions": comp.get("missions", 0), "days": comp.get("days", 0)}
    return {"kind": kind, "label": LABELS[kind], "total": int(total), "entries": [entry(r) for r in top],
            "around": [entry(r) for r in around], "me": entry(me) if me else None}


async def my_detail(db: AsyncSession, user: User) -> dict:
    from app.services.accounts import ensure_profile
    now = datetime.now(timezone.utc)
    profile = await ensure_profile(db, user)
    periods = {}
    for kind in PERIODS:
        _, _, key = bounds(kind, now)
        e = (await db.execute(select(LeaderboardEntry).where(
            LeaderboardEntry.user_id == user.id, LeaderboardEntry.period_kind == kind,
            LeaderboardEntry.period_key == key))).scalar_one_or_none()
        periods[kind] = None if e is None else {"rank": e.rank, "score": e.score, "reliability": e.reliability,
                                                "components": e.components}
    have = dict((await db.execute(select(UserBadge.code, UserBadge.awarded_at).where(UserBadge.user_id == user.id))).all())
    badges = [{"code": c, "label": v[0], "description": v[1], "icon": v[2], "tier": v[3],
               "earned": c in have, "awarded_at": have.get(c)} for c, v in BADGES.items()]
    hist = (await db.execute(select(LeaderboardSnapshot.snap_date, LeaderboardSnapshot.rank, LeaderboardSnapshot.score)
                             .where(LeaderboardSnapshot.user_id == user.id, LeaderboardSnapshot.period_kind == "season")
                             .order_by(LeaderboardSnapshot.snap_date.desc()).limit(60))).all()
    return {"level": level_info(profile.xp or 0), "periods": periods, "badges": badges,
            "history": [{"date": d.isoformat(), "rank": r, "score": s} for d, r, s in reversed(hist)]}