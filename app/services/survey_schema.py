import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.survey_settings import survey_settings as sv
from app.services import catalog

KEY_RE = r"^[a-z][a-z0-9_]{1,47}$"
SENSITIVE_AXES = {"ethnie", "religion", "opinion_politique", "politique", "appartenance_ethnique", "parti"}
UNITS = ("person", "household", "dwelling", "enterprise", "farm", "facility", "community")
QKind = Literal["single_choice", "multi_choice", "number", "scale", "yes_no"]
_CMP = {"eq", "ne", "lt", "lte", "gt", "gte"}


class OptionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(pattern=KEY_RE)
    label: str | None = Field(None, max_length=100)
    num: float | None = None


class QuestionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=KEY_RE)
    slot: str = Field(pattern=KEY_RE)
    text: str = Field(min_length=3, max_length=240)
    short: str | None = Field(None, max_length=60)
    help: str | None = Field(None, max_length=300)
    kind: QKind
    indicator: str
    required: bool = True
    options: list[OptionDef] = []
    min: float | None = None
    max: float | None = None
    evidence: Literal["none", "optional", "required"] = "none"
    evidence_hint: str | None = Field(None, max_length=200)
    allow_override: bool = True
    anchors: list[float] = []
    axis: str | None = Field(None, pattern=KEY_RE)
    combine: Literal["first", "sum", "mean", "max", "union"] | None = None


class SlotDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=KEY_RE)
    label: str = Field(min_length=2, max_length=100)
    help: str | None = Field(None, max_length=300)
    multi: bool = False
    allow_none: bool = True
    mode: Literal["profile", "direct"] = "profile"


class RuleDef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = Field(pattern=KEY_RE)
    when: dict | None = None
    assert_: dict = Field(alias="assert")
    severity: Literal["reject", "warn"] = "warn"
    message: str = Field(max_length=200)


class SurveyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: Literal["person", "household", "dwelling", "enterprise", "farm", "facility", "community"] = "household"
    slots: list[SlotDef] = Field(min_length=1)
    questions: list[QuestionDef] = Field(min_length=1)
    rules: list[RuleDef] = []
    dimensions: list[str] = []

    @model_validator(mode="after")
    def _check(self):
        if len(self.slots) > sv.SV_MAX_SLOTS or len(self.questions) > sv.SV_MAX_QUESTIONS:
            raise ValueError("survey_too_large")
        slot_keys = [s.key for s in self.slots]
        qkeys = [q.key for q in self.questions]
        if len(set(slot_keys)) != len(slot_keys):
            raise ValueError("duplicate_slot")
        if len(set(qkeys)) != len(qkeys):
            raise ValueError("duplicate_question")
        if len({q.indicator for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate_indicator")
        for s in self.slots:
            if not any(q.slot == s.key for q in self.questions):
                raise ValueError(f"empty_slot:{s.key}")
        for q in self.questions:
            if q.slot not in slot_keys:
                raise ValueError(f"unknown_slot:{q.key}")
            ind = catalog.get(q.indicator)
            if ind is None:
                raise ValueError(f"unknown_indicator:{q.indicator}")
            ok = (q.kind == "single_choice" or (q.kind == "multi_choice" and ind.multi)) if ind.kind == "category" \
                else q.kind == {"number": "number", "share": "yes_no", "scale": "scale"}[ind.kind]
            if not ok:
                raise ValueError(f"kind_mismatch:{q.key}")
            if ind.kind == "category":
                codes = {c.code: c for c in ind.cats}
                if not q.options:
                    q.options = [OptionDef(value=c.code) for c in ind.cats]
                if len(q.options) < 2 or len({o.value for o in q.options}) != len(q.options):
                    raise ValueError(f"bad_options:{q.key}")
                for o in q.options:
                    c = codes.get(o.value)
                    if c is None:
                        raise ValueError(f"option_not_in_indicator:{q.key}:{o.value}")
                    o.label = o.label or c.label
                    o.num = c.num if o.num is None else o.num
            elif q.options:
                raise ValueError(f"options_not_allowed:{q.key}")
            if ind.kind == "number":
                if (q.min is not None and q.min < ind.lo) or (q.max is not None and q.max > ind.hi):
                    raise ValueError(f"bounds_outside_indicator:{q.key}")
            if q.axis and q.axis in SENSITIVE_AXES:
                pass   # contrôlé au niveau du service (flag allow_sensitive_axes)
        for r in self.rules:
            if r.when is not None:
                _check_expr(r.when, set(qkeys))
            _check_expr(r.assert_, set(qkeys))
        return self


def _check_operand(o, qkeys, depth=0):
    if depth > 5 or not isinstance(o, dict) or len(o) != 1:
        raise ValueError("bad_operand")
    (k, v), = o.items()
    if k == "q":
        if v not in qkeys:
            raise ValueError(f"rule_unknown_question:{v}")
    elif k == "const":
        pass
    elif k in ("sum", "sub", "mul", "div"):
        if not isinstance(v, list) or not v or (k != "sum" and len(v) != 2):
            raise ValueError("bad_operand")
        for x in v:
            _check_operand(x, qkeys, depth + 1)
    else:
        raise ValueError("bad_operand")


def _check_expr(e, qkeys, depth=0):
    if depth > 6 or not isinstance(e, dict) or "op" not in e:
        raise ValueError("bad_expression")
    op = e["op"]
    if op in ("and", "or"):
        if not isinstance(e.get("args"), list) or not e["args"]:
            raise ValueError("bad_expression")
        for a in e["args"]:
            _check_expr(a, qkeys, depth + 1)
    elif op == "not":
        if not isinstance(e.get("args"), list) or len(e["args"]) != 1:
            raise ValueError("bad_expression")
        _check_expr(e["args"][0], qkeys, depth + 1)
    elif op == "answered":
        if e.get("q") not in qkeys:
            raise ValueError("bad_expression")
    elif op in _CMP:
        _check_operand(e.get("left"), qkeys)
        _check_operand(e.get("right"), qkeys)
    elif op in ("in", "not_in"):
        _check_operand(e.get("left"), qkeys)
        if not isinstance(e.get("right"), list):
            raise ValueError("bad_expression")
    elif op == "has":
        _check_operand(e.get("left"), qkeys)
        if "right" not in e:
            raise ValueError("bad_expression")
    else:
        raise ValueError("bad_expression")


# ------------------------------------------------------------------ évaluation des règles

def _operand(o, a):
    (k, v), = o.items()
    if k == "q":
        x = a.get(v)
        return int(x) if isinstance(x, bool) else x
    if k == "const":
        return v
    vals = [_operand(x, a) for x in v]
    if any(x is None or isinstance(x, (list, str)) for x in vals):
        return None
    if k == "sum":
        return sum(vals)
    if k == "sub":
        return vals[0] - vals[1]
    if k == "mul":
        return vals[0] * vals[1]
    return None if vals[1] == 0 else vals[0] / vals[1]


def eval_expr(e: dict, a: dict) -> bool | None:
    op = e["op"]
    if op in ("and", "or"):
        res = [eval_expr(x, a) for x in e["args"]]
        if op == "and":
            return False if any(r is False for r in res) else (None if any(r is None for r in res) else True)
        return True if any(r is True for r in res) else (None if any(r is None for r in res) else False)
    if op == "not":
        r = eval_expr(e["args"][0], a)
        return None if r is None else (not r)
    if op == "answered":
        return a.get(e["q"]) is not None
    left = _operand(e["left"], a)
    if left is None:
        return None
    if op in ("in", "not_in"):
        r = left in e["right"]
        return r if op == "in" else not r
    if op == "has":
        return isinstance(left, list) and e["right"] in left
    right = _operand(e["right"], a)
    if right is None:
        return None
    if isinstance(left, list) or isinstance(right, list) or isinstance(left, str) or isinstance(right, str):
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
        return None
    if op == "eq":
        return abs(left - right) < 1e-9
    if op == "ne":
        return abs(left - right) >= 1e-9
    return {"lt": left < right, "lte": left <= right + 1e-9, "gt": left > right, "gte": left >= right - 1e-9}[op]


def builtin_rules(defn: SurveyDefinition) -> list[RuleDef]:
    qk = {q.indicator: q.key for q in defn.questions}
    out = []
    for parts, whole, rel, msg in catalog.PART_RULES:
        if whole in qk and all(p in qk for p in parts):
            total = {"sum": [{"q": qk[p]} for p in parts]} if len(parts) > 1 else {"q": qk[parts[0]]}
            out.append(RuleDef(id=f"b_{parts[0]}_{rel}"[:48], when=None, severity="reject", message=msg,
                               assert_={"op": rel if rel == "eq" else "lte", "left": total, "right": {"q": qk[whole]}}))
    return out


def all_rules(defn: SurveyDefinition) -> list[RuleDef]:
    return [*builtin_rules(defn), *defn.rules]


def check_rules(rules: list[RuleDef], answers: dict) -> list[dict]:
    hits = []
    for r in rules:
        if r.when is not None and eval_expr(r.when, answers) is not True:
            continue
        if eval_expr(r.assert_, answers) is False:
            hits.append({"id": r.id, "severity": r.severity, "message": r.message})
    return hits


# ------------------------------------------------------------------ réponses

def questions_of(defn: SurveyDefinition, slot: str) -> list[QuestionDef]:
    return [q for q in defn.questions if q.slot == slot]


def coerce_answer(q: QuestionDef, raw: Any) -> Any:
    ind = catalog.INDICATORS[q.indicator]
    if q.kind == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise ValueError("not_a_number")
        x = float(raw)
        lo = max(q.min if q.min is not None else ind.lo, ind.lo)
        hi = min(q.max if q.max is not None else ind.hi, ind.hi)
        if x < lo or x > hi:
            raise ValueError("out_of_range")
        if ind.int_:
            if x != int(x):
                raise ValueError("not_integer")
            return int(x)
        return x
    if q.kind == "scale":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw or not 1 <= int(raw) <= 5:
            raise ValueError("out_of_range")
        return int(raw)
    if q.kind == "yes_no":
        if not isinstance(raw, bool):
            raise ValueError("not_a_boolean")
        return raw
    values = [o.value for o in q.options]
    if q.kind == "single_choice":
        if not isinstance(raw, str) or raw not in values:
            raise ValueError("invalid_option")
        return raw
    if not isinstance(raw, list) or not raw or len(set(raw)) != len(raw) or any(x not in values for x in raw):
        raise ValueError("invalid_option")
    return [v for v in values if v in raw]


def validate_answers(defn: SurveyDefinition, answers: dict, *, partial: bool = False) -> dict:
    qm = {q.key: q for q in defn.questions}
    out = {}
    for k, raw in answers.items():
        q = qm.get(k)
        if q is None:
            raise ValueError(f"unknown_question:{k}")
        out[k] = coerce_answer(q, raw)
    return out


def merge_profiles(defn: SurveyDefinition, slot: str, answer_sets: list[dict]) -> dict:
    if len(answer_sets) == 1:
        return dict(answer_sets[0])
    out: dict = {}
    for q in questions_of(defn, slot):
        vals = [a[q.key] for a in answer_sets if q.key in a]
        if not vals:
            continue
        mode = q.combine or ("union" if q.kind == "multi_choice" else "mean" if q.kind in ("number", "scale") else "first")
        if mode == "union":
            lst = vals if q.kind == "multi_choice" else [[v] for v in vals]
            seen = {x for v in lst for x in v}
            out[q.key] = [o.value for o in q.options if o.value in seen]
        elif mode == "sum":
            out[q.key] = sum(vals)
        elif mode == "max":
            out[q.key] = max(vals)
        elif mode == "mean":
            m = sum(vals) / len(vals)
            ind = catalog.INDICATORS[q.indicator]
            out[q.key] = int(round(m)) if (ind.int_ or q.kind == "scale") else round(m, 2)
        else:
            out[q.key] = vals[0]
    return out


def answer_rows(defn: SurveyDefinition, answers: dict) -> list[dict]:
    rows = []
    qm = {q.key: q for q in defn.questions}
    for k, v in answers.items():
        q = qm[k]
        base = {"indicator_key": q.indicator, "question_key": k}
        if q.kind in ("number", "scale"):
            rows.append({**base, "value_num": float(v), "value_cat": None})
        elif q.kind == "yes_no":
            rows.append({**base, "value_num": 1.0 if v else 0.0, "value_cat": None})
        else:
            nums = {o.value: o.num for o in q.options}
            rows.append({**base, "value_num": None, "value_cat": "__all__"})
            for c in ([v] if q.kind == "single_choice" else v):
                rows.append({**base, "value_num": nums.get(c), "value_cat": c})
    return rows


def describe_answer(q: QuestionDef, v: Any) -> str:
    ind = catalog.INDICATORS[q.indicator]
    if q.kind == "yes_no":
        return "Oui" if v else "Non"
    if q.kind == "scale":
        return f"{v}/5"
    if q.kind == "number":
        return f"{v:g} {ind.unit}".strip()
    lab = {o.value: o.label for o in q.options}
    return ", ".join(lab.get(x, x) for x in ([v] if q.kind == "single_choice" else v))


def summarize(defn: SurveyDefinition, slot: str, answers: dict) -> list[dict]:
    return [{"question": q.short or q.text, "answer": describe_answer(q, answers[q.key])}
            for q in questions_of(defn, slot) if q.key in answers]


def definition_errors(defn: SurveyDefinition, domain: str, allow_sensitive: bool) -> list[str]:
    errs = []
    for q in defn.questions:
        ind = catalog.INDICATORS[q.indicator]
        if not catalog.allowed_for_domain(ind, domain):
            errs.append(f"indicator_wrong_domain:{q.indicator}")
        if q.axis in SENSITIVE_AXES and not allow_sensitive:
            errs.append(f"sensitive_axis_not_allowed:{q.axis}")
    for d in defn.dimensions:
        if d in SENSITIVE_AXES and not allow_sensitive:
            errs.append(f"sensitive_axis_not_allowed:{d}")
    return errs


def public_view(defn: SurveyDefinition) -> dict:
    return {"unit": defn.unit,
            "slots": [s.model_dump() for s in defn.slots],
            "questions": [{**q.model_dump(exclude={"anchors", "combine"}),
                           "unit": catalog.INDICATORS[q.indicator].unit,
                           "indicator_label": catalog.INDICATORS[q.indicator].label,
                           "min": q.min if q.min is not None else catalog.INDICATORS[q.indicator].lo,
                           "max": q.max if q.max is not None else catalog.INDICATORS[q.indicator].hi}
                          for q in defn.questions]}