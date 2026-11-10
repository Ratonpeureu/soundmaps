import re
from dataclasses import dataclass, field

from app.core.study_settings import study_settings as sts
from app.models.enums import QualityStatus
from app.services.acoustics import clamp01
from app.services.quality import QualityResult


@dataclass
class QFacts:
    duration: float
    expected: float
    ratings: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    att_total: int = 0
    att_failed: int = 0
    trust: float | None = None
    trust_n: int = 0
    dup_count: int = 0


def _junk(t: str) -> bool:
    s = t.strip().lower()
    if len(s) < 3:
        return True
    if len(set(s.replace(" ", ""))) <= 2 and len(s) > 4:
        return True
    return bool(re.fullmatch(r"(.)\1{3,}", s)) or s in {"azerty", "qwerty", "asdf", "test", "rien", "ras", "xxx", "..."}


def evaluate(f: QFacts) -> QualityResult:
    reasons, sub, hard = [], {}, False
    ratio = f.duration / max(f.expected, 1e-6) if f.duration < 7200 else 1.0
    if ratio < sts.STQ_TOO_FAST_HARD:
        hard = True
        reasons.append({"code": "too_fast", "severity": "reject", "ratio": round(ratio, 2)})
        sub["timing"] = 0.0
    else:
        sub["timing"] = clamp01((ratio - 0.25) / 0.5)
        if sub["timing"] < 0.5:
            reasons.append({"code": "fast_answer", "severity": "warn", "ratio": round(ratio, 2)})
    eff = 1.0
    if len(f.ratings) >= 5 and len(set(f.ratings)) == 1:
        eff = 0.3
        reasons.append({"code": "straight_lining", "severity": "warn"})
    sub["effort"] = eff
    cap = False
    if f.texts:
        good = sum(1 for t in f.texts if not _junk(t)) / len(f.texts)
        sub["text"] = good
        if good < 0.5:
            reasons.append({"code": "text_low_effort", "severity": "warn"})
        if f.dup_count >= 1:
            sub["text"] = min(sub["text"], 0.2)
            cap = True
            reasons.append({"code": "copy_pattern", "severity": "warn", "count": f.dup_count})
    else:
        sub["text"] = 1.0
    if f.att_total:
        sub["attention"] = 1 - f.att_failed / f.att_total
        if f.att_failed:
            reasons.append({"code": "attention_failed", "severity": "warn", "failed": f.att_failed})
            cap = True
    else:
        sub["attention"] = 1.0
    if f.trust is not None and f.trust_n >= 3:
        sub["reputation"] = f.trust
    else:
        sub["reputation"] = 0.6
        reasons.append({"code": "new_participant", "severity": "info"})
    w = {"timing": sts.STQ_W_TIMING, "effort": sts.STQ_W_EFFORT, "text": sts.STQ_W_TEXT,
         "attention": sts.STQ_W_ATTENTION, "reputation": sts.STQ_W_REPUTATION}
    score = sum(w[k] * sub[k] for k in w)
    if hard:
        status, score = QualityStatus.rejected, 0.0
    elif score >= sts.STQ_VALIDATED_MIN and not cap:
        status = QualityStatus.validated
    elif score >= sts.STQ_INDICATIVE_MIN:
        status = QualityStatus.indicative
    else:
        status = QualityStatus.rejected
        reasons.append({"code": "low_score", "severity": "reject", "score": round(score, 3)})
    return QualityResult(status=status, score=round(score, 4), sub_scores={k: round(v, 3) for k, v in sub.items()}, reasons=reasons)