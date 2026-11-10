import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import QualityStatus, RewardStatus
from app.models.quota import UserQuota
from app.models.social import UserProfile
from app.models.study import Study, StudyReward, StudyVoucherCode
from app.services import quota as quota_service
from app.services.study_schema import AUTO_KINDS, PrivacyCfg, RewardCfg

STATUS_LABELS = {"pending": "En vérification", "entered": "Dans le tirage au sort", "granted": "Obtenue",
                 "delivered": "Reçue", "refused": "Refusée", "revoked": "Retirée", "expired": "Expirée"}
REASONS = {
    "quality_low": "Qualité des réponses insuffisante", "status_rejected": "Réponses jugées non fiables",
    "attention_failed": "Question de contrôle manquée", "too_fast": "Réponses données trop rapidement",
    "account_too_new": "Compte trop récent", "reliability_low": "Fiabilité du compte insuffisante",
    "budget_exhausted": "Budget de récompenses épuisé", "max_rewarded_reached": "Nombre maximal de bénéficiaires atteint",
    "not_drawn": "Non tiré au sort", "admin_refused": "Refusée par l'organisateur après vérification",
    "admin_revoked": "Retirée par l'organisateur", "response_invalidated": "Réponses invalidées après vérification",
    "fraud_detected": "Comportement frauduleux détecté", "claim_expired": "Délai de réclamation dépassé"}
KIND_LABELS = {"points": "Points", "credits": "Crédits de mesure", "voucher": "Bon / code", "cash": "Argent",
               "airtime": "Crédit téléphonique", "product": "Produit / cadeau", "custom": "Autre récompense"}


def cfg_of(study: Study) -> RewardCfg:
    try:
        return RewardCfg.model_validate(study.reward or {})
    except Exception:
        return RewardCfg()


def privacy_of(study: Study) -> PrivacyCfg:
    try:
        return PrivacyCfg.model_validate(study.privacy or {})
    except Exception:
        return PrivacyCfg()


def headline(cfg: RewardCfg) -> str:
    if not cfg.enabled:
        return "Sans récompense"
    amt = f"{cfg.amount:g} {cfg.unit or ''}".strip() if cfg.amount else ""
    base = cfg.label or KIND_LABELS.get(cfg.kind, "Récompense")
    base = f"{amt} · {base}" if amt and amt not in base else base
    return f"Tirage au sort : {base}" if cfg.mode == "lottery" else base


def card(cfg: RewardCfg) -> dict:
    return {"enabled": cfg.enabled, "kind": cfg.kind, "kind_label": KIND_LABELS.get(cfg.kind), "mode": cfg.mode,
            "label": cfg.label, "headline": headline(cfg), "validation": cfg.validation,
            "requires_contact": cfg.delivery.requires_contact}


def rules_sections(study: Study, cfg: RewardCfg, privacy: PrivacyCfg) -> list[dict]:
    S = []
    if cfg.enabled:
        it = [headline(cfg)]
        if cfg.description:
            it.append(cfg.description)
        it.append(f"Tirage au sort parmi les participants valides : {cfg.winners} gagnant(s)." if cfg.mode == "lottery"
                  else "Récompense garantie à chaque participation valide.")
        if cfg.max_rewarded:
            it.append(f"Limitée aux {cfg.max_rewarded} premiers participants valides.")
        it.append({"auto": "Attribution automatique dès que vos réponses sont validées.",
                   "manual": "Attribution après vérification manuelle par l'organisateur.",
                   "hybrid": "Attribution automatique si la qualité est excellente, sinon après vérification."}[cfg.validation])
        S.append({"title": "La récompense", "icon": "card_giftcard", "items": it})
        c = cfg.conditions
        cond = [f"Terminer le questionnaire avant la clôture{f' ({study.ends_at:%d/%m/%Y %H:%M} UTC)' if study.ends_at else ''}.",
                f"Réponses {'validées' if c.min_status == 'validated' else 'au moins indicatives'} avec un score d'au moins {round(c.min_score * 100)} %."]
        if c.require_attention:
            cond.append("Répondre correctement aux questions de contrôle d'attention.")
        if c.min_account_age_days:
            cond.append(f"Compte créé depuis au moins {c.min_account_age_days} jour(s).")
        if c.min_reliability:
            cond.append(f"Fiabilité du compte d'au moins {round(c.min_reliability * 100)} %.")
        cond.append("Une seule participation par personne.")
        S.append({"title": "Comment l'obtenir", "icon": "task_alt", "items": cond})
        ref = ["Questionnaire incomplet, réponses trop rapides, au hasard ou copiées.", "Question de contrôle manquée.",
               "Score de qualité sous le seuil requis."]
        if cfg.budget_total or cfg.max_rewarded:
            ref.append("Budget ou nombre de récompenses épuisé avant la validation de votre participation.")
        if cfg.mode == "lottery":
            ref.append("Participation valide mais non tirée au sort.")
        S.append({"title": "Cas de refus", "icon": "block", "items": ref})
        f = cfg.forfeit
        loss = []
        if f.revoke_on_admin_reject:
            loss.append("Réponses invalidées après vérification : la récompense est retirée.")
        if f.revoke_on_fraud:
            loss.append("Fraude détectée (doublons, comptes multiples, réponses copiées) : récompense retirée.")
        if f.claim_deadline_days and cfg.delivery.requires_contact:
            loss.append(f"Coordonnées à renseigner dans les {f.claim_deadline_days} jours, sinon la récompense expire.")
        if f.notes:
            loss.append(f.notes)
        S.append({"title": "Cas de perte", "icon": "report", "items": loss or ["Aucune condition de perte particulière."]})
        d = cfg.delivery
        dl = [d.instructions] if d.instructions else []
        if cfg.kind in AUTO_KINDS:
            dl.append("Ajoutée automatiquement à votre compte.")
        if cfg.kind == "voucher":
            dl.append("Le code s'affiche dans « Mes récompenses ».")
        if d.requires_contact:
            dl.append(f"Vous devrez indiquer : {d.contact_label or 'vos coordonnées'}.")
        if d.valid_until:
            dl.append(f"Valable jusqu'au {d.valid_until:%d/%m/%Y}.")
        S.append({"title": "Remise", "icon": "local_shipping", "items": dl or ["Précisions communiquées dans l'application."]})
    shared = ["Vos réponses sont transmises à l'initiateur de façon pseudonymisée (jamais votre nom ni votre e-mail).",
              "Elles n'apparaissent sur aucune carte de l'application."]
    if privacy.share_profile:
        shared.append("Informations de profil jointes à vos réponses : " + ", ".join(privacy.share_profile).replace("_", " ") + " (par tranches).")
    if privacy.collect_contact:
        shared.append("Votre numéro n'est transmis que si vous l'acceptez explicitement" + (f" ({privacy.contact_purpose})." if privacy.contact_purpose else "."))
    S.append({"title": "Vos données", "icon": "lock", "items": shared})
    return S


def reward_out(rw: StudyReward, study_title: str | None = None, sponsor: str | None = None) -> dict:
    needs_claim = False
    if rw.status == RewardStatus.granted:
        cfg = RewardCfg.model_validate((rw.snapshot or {}).get("cfg") or {"enabled": True, "label": rw.label})
        needs_claim = cfg.delivery.requires_contact and not (rw.delivery or {}).get("phone")
    return {"id": str(rw.id), "study_id": str(rw.study_id), "study_title": study_title, "sponsor_name": sponsor,
            "status": rw.status.value, "status_label": STATUS_LABELS[rw.status.value], "kind": rw.kind,
            "kind_label": KIND_LABELS.get(rw.kind), "label": rw.label, "amount": rw.amount, "unit": rw.unit, "mode": rw.mode,
            "headline": f"{rw.amount:g} {rw.unit or ''} · {rw.label}".strip() if rw.amount and (rw.unit or rw.kind in ("points", "credits")) else rw.label,
            "score": rw.score, "reason_code": rw.reason_code, "reason_label": REASONS.get(rw.reason_code or ""),
            "reason_text": rw.reason_text, "code": rw.code if rw.status in (RewardStatus.granted, RewardStatus.delivered) else None,
            "needs_claim": needs_claim, "claim_deadline_at": rw.claim_deadline_at, "valid_until": rw.valid_until,
            "delivered_at": rw.delivered_at, "created_at": rw.created_at, "note": (rw.delivery or {}).get("note")}


# ------------------------------------------------------------------ cycle de vie
async def _budget(db: AsyncSession, study_id) -> tuple[float, int]:
    s, n = (await db.execute(select(func.coalesce(func.sum(StudyReward.amount), 0.0), func.count()).where(
        StudyReward.study_id == study_id, StudyReward.status.in_(
            [RewardStatus.pending, RewardStatus.granted, RewardStatus.delivered])))).one()
    return float(s), int(n)


async def decide(db: AsyncSession, study: Study, resp, user_id, cfg: RewardCfg, prof: dict) -> StudyReward | None:
    """Crée la ligne de récompense d'une participation terminée et applique les règles automatiques."""
    if not cfg.enabled or user_id is None:
        return None
    now = datetime.now(timezone.utc)
    c = cfg.conditions
    rw = StudyReward(tenant_id=study.tenant_id, study_id=study.id, response_id=resp.id, user_id=user_id, kind=cfg.kind,
                     mode=cfg.mode, label=cfg.label or KIND_LABELS[cfg.kind], amount=cfg.amount, unit=cfg.unit,
                     status=RewardStatus.pending, score=resp.score, snapshot={"cfg": cfg.model_dump(mode="json")},
                     valid_until=cfg.delivery.valid_until)
    db.add(rw)
    codes = {r["code"] for r in resp.reasons or []}
    code = None
    if resp.quality_status == QualityStatus.rejected:
        code = "too_fast" if "too_fast" in codes else "attention_failed" if "attention_failed" in codes else "status_rejected"
    elif c.require_attention and "attention_failed" in codes:
        code = "attention_failed"
    elif c.min_status == "validated" and resp.quality_status != QualityStatus.validated and cfg.validation == "auto":
        code = "quality_low"
    elif resp.score < c.min_score and cfg.validation != "manual":
        code = "quality_low"
    elif prof.get("account_age_days", 0) < c.min_account_age_days:
        code = "account_too_new"
    elif prof.get("reliability", 1) < c.min_reliability:
        code = "reliability_low"
    else:
        spent, n = await _budget(db, study.id)
        if cfg.max_rewarded and n >= cfg.max_rewarded:
            code = "max_rewarded_reached"
        elif cfg.budget_total and spent + cfg.amount > cfg.budget_total:
            code = "budget_exhausted"
    await db.flush()
    if code:
        return await refuse(db, rw, code)
    if cfg.mode == "lottery":
        rw.status = RewardStatus.entered
        return rw
    if cfg.validation == "auto" or (cfg.validation == "hybrid" and resp.score >= c.auto_grant_score
                                    and resp.quality_status == QualityStatus.validated):
        return await grant(db, rw, study, cfg)
    return rw


async def grant(db: AsyncSession, rw: StudyReward, study: Study, cfg: RewardCfg | None = None, by=None) -> StudyReward:
    cfg = cfg or RewardCfg.model_validate((rw.snapshot or {}).get("cfg") or {"enabled": True, "label": rw.label})
    now = datetime.now(timezone.utc)
    rw.decided_by, rw.decided_at, rw.reason_code, rw.reason_text = by, now, None, None
    if rw.kind == "points":
        await db.execute(update(UserProfile).where(UserProfile.user_id == rw.user_id).values(xp=UserProfile.xp + int(rw.amount)))
        rw.status, rw.delivered_at = RewardStatus.delivered, now
    elif rw.kind == "credits":
        await quota_service.grant(db, rw.tenant_id, rw.user_id, float(rw.amount), now)
        rw.status, rw.delivered_at = RewardStatus.delivered, now
    elif rw.kind == "voucher":
        v = (await db.execute(select(StudyVoucherCode).where(StudyVoucherCode.study_id == rw.study_id, StudyVoucherCode.reward_id.is_(None))
                              .order_by(StudyVoucherCode.created_at).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if v is not None:
            v.reward_id, v.assigned_at = rw.id, now
            rw.code, rw.status, rw.delivered_at = v.code, RewardStatus.delivered, now
        else:
            rw.status = RewardStatus.granted                     # en attente de stock : le code arrivera à l'ajout de codes
            rw.delivery = {**(rw.delivery or {}), "note": "Code en préparation."}
    else:
        rw.status = RewardStatus.granted
        d = cfg.forfeit.claim_deadline_days
        if cfg.delivery.requires_contact and d:
            rw.claim_deadline_at = now + timedelta(days=d)
    return rw


async def refuse(db: AsyncSession, rw: StudyReward, code: str, text: str | None = None, by=None) -> StudyReward:
    rw.status, rw.reason_code, rw.reason_text, rw.decided_by, rw.decided_at = RewardStatus.refused, code, text, by, datetime.now(timezone.utc)
    return rw


async def revoke(db: AsyncSession, rw: StudyReward, code: str, text: str | None = None, by=None, expired: bool = False) -> StudyReward:
    if rw.status in (RewardStatus.granted, RewardStatus.delivered):
        if rw.kind == "points":
            await db.execute(update(UserProfile).where(UserProfile.user_id == rw.user_id)
                             .values(xp=func.greatest(UserProfile.xp - int(rw.amount), 0)))
        elif rw.kind == "credits":
            await db.execute(update(UserQuota).where(UserQuota.user_id == rw.user_id).values(balance=UserQuota.balance - float(rw.amount)))
        elif rw.kind == "voucher" and rw.code:
            await db.execute(update(StudyVoucherCode).where(StudyVoucherCode.reward_id == rw.id).values(code=StudyVoucherCode.code + "#VOID"))
    rw.status = RewardStatus.expired if expired else RewardStatus.revoked
    rw.reason_code, rw.reason_text, rw.decided_by, rw.decided_at, rw.code = code, text, by, datetime.now(timezone.utc), None
    return rw


async def deliver(db: AsyncSession, rw: StudyReward, note: str | None, reference: str | None, by) -> StudyReward:
    rw.status, rw.delivered_at, rw.decided_by = RewardStatus.delivered, datetime.now(timezone.utc), by
    rw.delivery = {**(rw.delivery or {}), "note": note, "reference": reference}
    return rw


async def fill_waiting_vouchers(db: AsyncSession, study: Study) -> int:
    waiting = (await db.execute(select(StudyReward).where(StudyReward.study_id == study.id, StudyReward.kind == "voucher",
                                                          StudyReward.status == RewardStatus.granted, StudyReward.code.is_(None))
                                .order_by(StudyReward.created_at))).scalars().all()
    n = 0
    for rw in waiting:
        await grant(db, rw, study, None, rw.decided_by)
        n += int(rw.status == RewardStatus.delivered)
    return n