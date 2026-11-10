import math
import re
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.study_settings import study_settings as sts
from app.services import catalog

KEY_RE = r"^[a-z][a-z0-9_]{1,47}$"
VAL_RE = r"^[a-z0-9][a-z0-9_\-]{0,47}$"
ANSWERABLE = {"single_choice", "multi_choice", "yes_no", "number", "slider", "scale", "stars", "nps", "text",
              "long_text", "ranking", "matrix", "date", "product_rating"}
Kind = Literal["single_choice", "multi_choice", "yes_no", "number", "slider", "scale", "stars", "nps", "text",
               "long_text", "ranking", "matrix", "date", "product_rating", "info", "media"]
KIND_LABELS = {"survey": "Enquête", "poll": "Sondage", "market_study": "Étude de marché", "field_study": "Étude terrain",
               "commercial": "Enquête commerciale", "product_test": "Test produit"}
SPONSOR_LABELS = {"company": "Entreprise", "ngo": "ONG / Association", "person": "Personne", "service": "Service public",
                  "institution": "Institution"}


# ====================================================================== définition du questionnaire
class MediaDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: uuid.UUID | None = None
    url: str | None = Field(None, max_length=400, pattern=r"^https://")
    kind: Literal["image", "video"] = "image"
    caption: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _one(self):
        if (self.asset_id is None) == (self.url is None):
            raise ValueError("media_needs_asset_or_url")
        return self


class OptionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(pattern=VAL_RE)
    label: str = Field(min_length=1, max_length=160)
    media: MediaDef | None = None
    exclusive: bool = False
    other: bool = False


class ItemDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=VAL_RE)
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(None, max_length=400)
    media: list[MediaDef] = Field(default_factory=list, max_length=8)


class CondAtom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str
    op: Literal["eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "answered", "not_answered", "has", "has_any"]
    value: Any = None


class CondDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "any"] = "all"
    rules: list[CondAtom] = Field(min_length=1, max_length=8)


class ScreenDef(BaseModel):
    """Question filtre : hors cible => la personne est remerciée et sort de l'étude."""
    model_config = ConfigDict(extra="forbid")
    allow: list[Any] = Field(default_factory=list, max_length=60)
    deny: list[Any] = Field(default_factory=list, max_length=60)
    message: str | None = Field(None, max_length=300)


class AttentionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correct: Any


def _uniq(vals, code, mn):
    if len(vals) < mn or len(set(vals)) != len(vals):
        raise ValueError(code)


class BlockDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=KEY_RE)
    kind: Kind
    text: str | None = Field(None, max_length=600)
    help: str | None = Field(None, max_length=400)
    required: bool = True
    media: list[MediaDef] = Field(default_factory=list, max_length=8)
    options: list[OptionDef] = Field(default_factory=list, max_length=sts.ST_MAX_OPTIONS)
    rows: list[OptionDef] = Field(default_factory=list, max_length=40)
    items: list[ItemDef] = Field(default_factory=list, max_length=20)
    criteria: list[OptionDef] = Field(default_factory=list, max_length=8)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    integer: bool = False
    unit: str | None = Field(None, max_length=20)
    low_label: str | None = Field(None, max_length=60)
    high_label: str | None = Field(None, max_length=60)
    min_select: int | None = Field(None, ge=0, le=60)
    max_select: int | None = Field(None, ge=1, le=60)
    min_length: int | None = Field(None, ge=0, le=2000)
    max_length: int | None = Field(None, ge=1, le=5000)
    display: Literal["list", "dropdown", "images", "buttons"] | None = None
    randomize: bool = False
    top_n: int | None = Field(None, ge=1, le=60)
    allow_comment: bool = False
    placeholder: str | None = Field(None, max_length=120)
    show_if: CondDef | None = None
    screen: ScreenDef | None = None
    attention: AttentionDef | None = None

    @model_validator(mode="after")
    def _kind(self):
        k, key = self.kind, self.key
        if k in ANSWERABLE and len((self.text or "").strip()) < 2:
            raise ValueError(f"text_required:{key}")
        if k in ("single_choice", "multi_choice", "ranking"):
            _uniq([o.value for o in self.options], f"bad_options:{key}", 2)
        elif k == "matrix":
            _uniq([o.value for o in self.options], f"bad_columns:{key}", 2)
            _uniq([o.value for o in self.rows], f"bad_rows:{key}", 1)
        elif self.options:
            raise ValueError(f"options_not_allowed:{key}")
        if k == "product_rating":
            _uniq([i.key for i in self.items], f"bad_items:{key}", 1)
            if not self.criteria:
                self.criteria = [OptionDef(value="overall", label="Note globale")]
            _uniq([c.value for c in self.criteria], f"bad_criteria:{key}", 1)
            self.min = 1 if self.min is None else self.min
            self.max = 5 if self.max is None else self.max
        if k == "scale":
            self.min = 1 if self.min is None else self.min
            self.max = 5 if self.max is None else self.max
            if not 1 <= self.max - self.min <= 10:
                raise ValueError(f"bad_scale:{key}")
        if k == "stars":
            self.min, self.max = 1, int(self.max or 5)
            if not 3 <= self.max <= 10:
                raise ValueError(f"bad_stars:{key}")
        if k == "nps":
            self.min, self.max = 0, 10
        if k == "slider":
            if self.min is None or self.max is None or self.min >= self.max:
                raise ValueError(f"bad_slider:{key}")
            self.step = self.step or 1.0
        if k == "number" and self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"bad_bounds:{key}")
        if k == "info" and not (self.text or "").strip():
            raise ValueError(f"text_required:{key}")
        if k == "media" and not self.media:
            raise ValueError(f"media_required:{key}")
        if self.screen and k not in ("single_choice", "multi_choice", "yes_no", "number", "slider", "scale", "stars", "nps"):
            raise ValueError(f"screen_not_allowed:{key}")
        if self.attention and k not in ("single_choice", "yes_no", "number", "scale", "stars", "nps"):
            raise ValueError(f"attention_not_allowed:{key}")
        return self


class PageDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=KEY_RE)
    title: str | None = Field(None, max_length=160)
    description: str | None = Field(None, max_length=600)
    blocks: list[BlockDef] = Field(min_length=1, max_length=40)
    show_if: CondDef | None = None


class IntroDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, max_length=160)
    text: str | None = Field(None, max_length=1500)
    media: list[MediaDef] = Field(default_factory=list, max_length=6)


class StudyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intro: IntroDef | None = None
    outro: IntroDef | None = None
    pages: list[PageDef] = Field(min_length=1, max_length=30)
    show_progress: bool = True
    allow_back: bool = True

    @model_validator(mode="after")
    def _check(self):
        keys, pkeys, seen, answerable = set(), set(), set(), 0
        total = 0
        for p in self.pages:
            if p.key in pkeys:
                raise ValueError(f"duplicate_page:{p.key}")
            pkeys.add(p.key)
            if p.show_if:
                _refs(p.show_if, seen)
            for b in p.blocks:
                total += 1
                if b.key in keys or b.key.endswith("_other"):
                    raise ValueError(f"duplicate_block:{b.key}")
                keys.add(b.key)
                if b.show_if:
                    _refs(b.show_if, seen)
                if b.kind in ANSWERABLE:
                    seen.add(b.key)
                    answerable += 1
        if total > sts.ST_MAX_BLOCKS:
            raise ValueError("study_too_large")
        if answerable == 0:
            raise ValueError("no_question")
        return self


def _refs(c: CondDef, seen: set):
    for a in c.rules:
        if a.q not in seen:
            raise ValueError(f"condition_unknown_question:{a.q}")


def all_blocks(d: StudyDefinition) -> list[BlockDef]:
    return [b for p in d.pages for b in p.blocks]


def asset_ids(d: StudyDefinition) -> set:
    out = set()

    def add(m):
        if m is not None and m.asset_id:
            out.add(m.asset_id)
    for sec in (d.intro, d.outro):
        for m in (sec.media if sec else []):
            add(m)
    for b in all_blocks(d):
        for m in b.media:
            add(m)
        for o in b.options:
            add(o.media)
        for i in b.items:
            for m in i.media:
                add(m)
    return out


def public_view(d: StudyDefinition) -> dict:
    raw = d.model_dump(mode="json", exclude_none=True)
    for p in raw["pages"]:
        for b in p["blocks"]:
            if b.pop("screen", None) is not None:
                b["screening"] = True
            b.pop("attention", None)
    return raw


# ====================================================================== logique d'affichage et validation des réponses
def _atom(a: CondAtom, ans: dict) -> bool:
    v, op, ref = ans.get(a.q), a.op, a.value
    if op == "answered":
        return v is not None
    if op == "not_answered":
        return v is None
    if v is None:
        return False
    try:
        if op == "eq":
            return v == ref
        if op == "ne":
            return v != ref
        if op in ("in", "nin"):
            vs = v if isinstance(v, list) else [v]
            hit = any(x in ref for x in vs)
            return hit if op == "in" else not hit
        if op == "has":
            return isinstance(v, list) and ref in v
        if op == "has_any":
            return isinstance(v, list) and any(x in v for x in ref)
        x, y = float(v), float(ref)
        return {"gt": x > y, "gte": x >= y, "lt": x < y, "lte": x <= y}[op]
    except (TypeError, ValueError):
        return False


def cond_ok(c: CondDef | None, ans: dict) -> bool:
    if c is None:
        return True
    res = [_atom(a, ans) for a in c.rules]
    return all(res) if c.mode == "all" else any(res)


def visible_blocks(d: StudyDefinition, ans: dict) -> list[BlockDef]:
    return [b for p in d.pages if cond_ok(p.show_if, ans) for b in p.blocks if cond_ok(b.show_if, ans)]


def _int_in(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer() or not lo <= v <= hi:
        raise ValueError("out_of_range")
    return int(v)


def coerce(b: BlockDef, v: Any, partial: bool = False) -> Any:
    k, vals = b.kind, [o.value for o in b.options]
    if k == "single_choice":
        if not isinstance(v, str) or v not in vals:
            raise ValueError("invalid_option")
        return v
    if k == "multi_choice":
        if not isinstance(v, list) or len(set(map(str, v))) != len(v) or any(x not in vals for x in v):
            raise ValueError("invalid_option")
        if (b.min_select and len(v) < b.min_select) or (b.max_select and len(v) > b.max_select):
            raise ValueError("selection_count")
        ex = {o.value for o in b.options if o.exclusive}
        if len(v) > 1 and any(x in ex for x in v):
            raise ValueError("exclusive_option")
        return [x for x in vals if x in v]
    if k == "yes_no":
        if not isinstance(v, bool):
            raise ValueError("not_a_boolean")
        return v
    if k in ("number", "slider"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ValueError("not_a_number")
        if (b.min is not None and v < b.min) or (b.max is not None and v > b.max):
            raise ValueError("out_of_range")
        if b.integer:
            if not float(v).is_integer():
                raise ValueError("not_integer")
            return int(v)
        return float(v)
    if k in ("scale", "stars", "nps"):
        return _int_in(v, b.min, b.max)
    if k in ("text", "long_text"):
        if not isinstance(v, str):
            raise ValueError("not_text")
        s = v.strip()
        if len(s) < (b.min_length or 0) or len(s) > (b.max_length or (300 if k == "text" else 4000)):
            raise ValueError("text_length")
        return s
    if k == "ranking":
        n = b.top_n or len(vals)
        if not isinstance(v, list) or len(set(map(str, v))) != len(v) or any(x not in vals for x in v) or len(v) != min(n, len(vals)):
            raise ValueError("invalid_ranking")
        return v
    if k == "matrix":
        rows, cols = [r.value for r in b.rows], vals
        if not isinstance(v, dict) or any(r not in rows or c not in cols for r, c in v.items()):
            raise ValueError("invalid_matrix")
        if not partial and any(r not in v for r in rows):
            raise ValueError("incomplete_matrix")
        return {r: v[r] for r in rows if r in v}
    if k == "date":
        try:
            return date.fromisoformat(str(v)).isoformat()
        except ValueError:
            raise ValueError("invalid_date")
    if k == "product_rating":
        items, crits = [i.key for i in b.items], [c.value for c in b.criteria]
        if not isinstance(v, dict) or any(i not in items or not isinstance(x, dict) for i, x in v.items()):
            raise ValueError("invalid_rating")
        out = {}
        for i, x in v.items():
            if any(c not in crits and not (c == "comment" and b.allow_comment) for c in x):
                raise ValueError("invalid_rating")
            o = {c: _int_in(x[c], b.min, b.max) for c in crits if c in x}
            if "comment" in x and str(x["comment"]).strip():
                o["comment"] = str(x["comment"]).strip()[:500]
            if not partial and any(c not in o for c in crits):
                raise ValueError("incomplete_rating")
            out[i] = o
        if not partial and any(i not in out for i in items):
            raise ValueError("incomplete_rating")
        return out
    raise ValueError("unsupported")


def clean_answers(d: StudyDefinition, raw: dict, partial: bool = False) -> tuple[dict, list[str]]:
    keys = {b.key for b in all_blocks(d) if b.kind in ANSWERABLE}
    for k in raw:
        if k not in keys and not (k.endswith("_other") and k[:-6] in keys):
            raise ValueError(f"unknown_question:{k}")
    clean, missing = {}, []
    for p in d.pages:
        if not cond_ok(p.show_if, clean):
            continue
        for b in p.blocks:
            if b.kind not in ANSWERABLE or not cond_ok(b.show_if, clean):
                continue
            v = raw.get(b.key)
            if v is None or v == "" or v == [] or v == {}:
                if b.required and not partial:
                    missing.append(b.key)
                continue
            try:
                clean[b.key] = coerce(b, v, partial)
            except ValueError as e:
                raise ValueError(f"{b.key}:{e}")
            o = raw.get(b.key + "_other")
            if o and any(op.other for op in b.options):
                clean[b.key + "_other"] = str(o).strip()[:200]
    return clean, missing


def _in(kind, vs, items) -> bool:
    if kind in ("number", "slider"):
        return any(isinstance(r, list) and len(r) == 2 and r[0] <= x <= r[1] for x in vs for r in items)
    return any(x in items for x in vs)


def screen_check(d: StudyDefinition, ans: dict) -> tuple[bool, str | None]:
    for b in all_blocks(d):
        s = b.screen
        if s is None or b.key not in ans:
            continue
        v = ans[b.key]
        vs = v if isinstance(v, list) else [v]
        if (s.allow and not _in(b.kind, vs, s.allow)) or (s.deny and _in(b.kind, vs, s.deny)):
            return False, s.message or "Cette étude ne correspond pas à votre situation. Merci d'avoir répondu !"
    return True, None


def attention_stats(d: StudyDefinition, ans: dict) -> tuple[int, int]:
    n = f = 0
    for b in all_blocks(d):
        if b.attention is not None and b.key in ans:
            n += 1
            f += int(ans[b.key] != b.attention.correct)
    return n, f


_SEC = {"single_choice": 4, "multi_choice": 7, "yes_no": 3, "number": 5, "slider": 5, "scale": 3, "stars": 3, "nps": 3,
        "text": 10, "long_text": 25, "date": 6, "media": 8}


def expected_seconds(d: StudyDefinition, ans: dict) -> float:
    t = 6.0
    for b in visible_blocks(d, ans):
        if b.kind == "matrix":
            s = 3 * len(b.rows) + 2
        elif b.kind == "ranking":
            s = 3 + 2 * len(b.options)
        elif b.kind == "product_rating":
            s = 8 * len(b.items) * math.sqrt(max(1, len(b.criteria)))
        elif b.kind == "info":
            s = len(b.text or "") / 18
        else:
            s = _SEC.get(b.kind, 4)
        s += sum(5 if m.kind == "video" else 2 for m in b.media)
        t += s * 0.6
    return t


# ---------------------------------------------------------------- lecture humaine et export
def _lab(b):
    return {o.value: o.label for o in b.options}


def describe(b: BlockDef, v: Any) -> str:
    lab, k = _lab(b), b.kind
    if v is None:
        return ""
    if k == "yes_no":
        return "Oui" if v else "Non"
    if k in ("single_choice",):
        return lab.get(v, str(v))
    if k in ("multi_choice", "ranking"):
        return (" > " if k == "ranking" else " | ").join(lab.get(x, str(x)) for x in v)
    if k == "matrix":
        rl = {r.value: r.label for r in b.rows}
        return " ; ".join(f"{rl.get(r, r)}: {lab.get(c, c)}" for r, c in v.items())
    if k == "product_rating":
        return " ; ".join(f"{next((i.label for i in b.items if i.key == i_), i_)}: " + ", ".join(f"{c}={x}" for c, x in o.items())
                          for i_, o in v.items())
    return str(v)


def export_columns(d: StudyDefinition) -> list[tuple[str, Any]]:
    cols = []
    for b in all_blocks(d):
        if b.kind not in ANSWERABLE:
            continue
        t = (b.text or b.key)[:60]
        if b.kind == "matrix":
            for r in b.rows:
                cols.append((f"{b.key}.{r.value} | {t} - {r.label}", lambda a, b=b, r=r: _lab(b).get((a.get(b.key) or {}).get(r.value))))
        elif b.kind == "product_rating":
            for i in b.items:
                for c in b.criteria:
                    cols.append((f"{b.key}.{i.key}.{c.value} | {t} - {i.label} ({c.label})",
                                 lambda a, b=b, i=i, c=c: ((a.get(b.key) or {}).get(i.key) or {}).get(c.value)))
                if b.allow_comment:
                    cols.append((f"{b.key}.{i.key}.comment | {i.label} (commentaire)",
                                 lambda a, b=b, i=i: ((a.get(b.key) or {}).get(i.key) or {}).get("comment")))
        else:
            cols.append((f"{b.key} | {t}", lambda a, b=b: describe(b, a.get(b.key)) if b.key in a else None))
            if any(o.other for o in b.options):
                cols.append((f"{b.key}_other | {t} (autre)", lambda a, b=b: a.get(b.key + "_other")))
    return cols


# ====================================================================== attributs de ciblage
GENDERS = [("female", "Femme"), ("male", "Homme"), ("other", "Autre")]
OCC = [("agriculture_peche", "Agriculture / pêche"), ("commerce", "Commerce"), ("artisanat", "Artisanat"),
       ("services", "Services"), ("administration", "Administration"), ("industrie_btp", "Industrie / BTP"),
       ("transport", "Transport"), ("etudiant", "Étudiant(e)"), ("chomeur", "En recherche d'emploi"),
       ("retraite", "Retraité(e)"), ("autre", "Autre")]
EDU = [(c.code, c.label) for c in catalog.INDICATORS["education_level_max"].cats]
INC = [(c.code, c.label) for c in catalog.INDICATORS["household_income_band"].cats]
NET = [(c.code, c.label) for c in catalog.INDICATORS["internet_access"].cats]
LANGS = [("wolof", "Wolof"), ("francais", "Français"), ("pulaar", "Pulaar"), ("serere", "Sérère"), ("mandinka", "Mandingue"),
         ("diola", "Diola"), ("soninke", "Soninké"), ("anglais", "Anglais"), ("arabe", "Arabe")]
INTERESTS = [(k, k.replace("_", " ").capitalize()) for k in (
    "mode", "alimentation", "telecom", "banque_mobile", "sante", "education", "agriculture", "immobilier", "transport",
    "energie", "sport", "musique", "tech", "beaute", "voyage", "automobile", "commerce_en_ligne")]

ATTRS: dict[str, dict] = {
    "age": dict(label="Âge", kind="number", unit="ans", sensitive=False),
    "gender": dict(label="Genre", kind="cat", options=GENDERS, sensitive=True),
    "city": dict(label="Ville", kind="cat", free=True),
    "district": dict(label="Quartier", kind="cat", free=True),
    "occupation_group": dict(label="Secteur d'activité", kind="cat", options=OCC),
    "education_level": dict(label="Niveau d'études", kind="cat", options=EDU),
    "income_band": dict(label="Revenu mensuel du ménage", kind="cat", options=INC, sensitive=True),
    "household_size": dict(label="Taille du ménage", kind="number", unit="personnes"),
    "languages": dict(label="Langues parlées", kind="multi", options=LANGS),
    "interests": dict(label="Centres d'intérêt", kind="multi", options=INTERESTS),
    "mobile_money": dict(label="Utilise le mobile money", kind="bool"),
    "internet_access": dict(label="Accès Internet", kind="cat", options=NET),
    "account_age_days": dict(label="Ancienneté du compte", kind="number", unit="jours"),
    "reliability": dict(label="Fiabilité (0 à 1)", kind="number"),
    "validated_count": dict(label="Mesures validées (90 j)", kind="number"),
    "level": dict(label="Niveau (0 à 6)", kind="number"),
    "analyst": dict(label="Est analyseur", kind="bool"),
    "studies_done": dict(label="Études déjà réalisées", kind="number"),
    "study_trust": dict(label="Score moyen en études (0 à 1)", kind="number"),
}


def attrs_public() -> list[dict]:
    return [{"key": k, "label": v["label"], "kind": v["kind"], "unit": v.get("unit"), "free": bool(v.get("free")),
             "sensitive": bool(v.get("sensitive")), "options": [{"value": a, "label": b} for a, b in v.get("options", [])]}
            for k, v in ATTRS.items()]


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attr: str
    op: Literal["eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "between", "has", "has_any", "true", "false"]
    value: Any = None
    mode: Literal["must", "prefer"] = "must"
    weight: float = Field(1.0, ge=0.1, le=10)

    @model_validator(mode="after")
    def _ok(self):
        spec = ATTRS.get(self.attr)
        if spec is None:
            raise ValueError(f"unknown_attribute:{self.attr}")
        allowed = {"number": {"eq", "ne", "gt", "gte", "lt", "lte", "between"}, "cat": {"eq", "ne", "in", "nin"},
                   "multi": {"has", "has_any"}, "bool": {"true", "false"}}[spec["kind"]]
        if self.op not in allowed:
            raise ValueError(f"bad_operator:{self.attr}")
        if self.op == "between" and not (isinstance(self.value, list) and len(self.value) == 2):
            raise ValueError(f"bad_value:{self.attr}")
        if self.op not in ("true", "false") and self.value in (None, []):
            raise ValueError(f"bad_value:{self.attr}")
        return self


class Circle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(gt=0, le=500)
    label: str | None = Field(None, max_length=80)


class GeoDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    circles: list[Circle] = Field(default_factory=list, max_length=20)
    cities: list[str] = Field(default_factory=list, max_length=50)
    h3_res: int | None = Field(None, ge=3, le=9)
    h3_cells: list[str] = Field(default_factory=list, max_length=500)
    mode: Literal["must", "prefer"] = "must"


class SamplingDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["open", "invite_only"] = "open"
    oversample: float | None = Field(None, ge=1, le=10)
    max_invites: int | None = Field(None, ge=1, le=100000)


class Targeting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include: list[Criterion] = Field(default_factory=list, max_length=20)
    exclude: list[Criterion] = Field(default_factory=list, max_length=20)
    geo: GeoDef | None = None
    strict_unknown: bool = False
    min_reliability: float = Field(0.0, ge=0, le=1)
    sampling: SamplingDef = Field(default_factory=SamplingDef)


# ====================================================================== récompense et confidentialité
RewardKind = Literal["points", "credits", "voucher", "cash", "airtime", "product", "custom"]
AUTO_KINDS = {"points", "credits"}


class RewardConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_status: Literal["validated", "indicative"] = "validated"
    min_score: float = Field(0.7, ge=0, le=1)
    require_attention: bool = True
    min_account_age_days: int = Field(0, ge=0, le=365)
    min_reliability: float = Field(0.0, ge=0, le=1)
    auto_grant_score: float = Field(0.8, ge=0, le=1)


class Forfeit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_deadline_days: int | None = Field(None, ge=1, le=365)
    revoke_on_admin_reject: bool = True
    revoke_on_fraud: bool = True
    notes: str | None = Field(None, max_length=400)


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requires_contact: bool = False
    contact_label: str | None = Field("Numéro Wave / Orange Money", max_length=80)
    instructions: str | None = Field(None, max_length=600)
    valid_until: AwareDatetime | None = None


class RewardCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    kind: RewardKind = "points"
    mode: Literal["guaranteed", "lottery"] = "guaranteed"
    label: str | None = Field(None, max_length=160)
    description: str | None = Field(None, max_length=600)
    amount: float = Field(0, ge=0, le=10_000_000)
    unit: str | None = Field(None, max_length=20)
    winners: int = Field(0, ge=0, le=10000)
    validation: Literal["auto", "manual", "hybrid"] = "auto"
    budget_total: float | None = Field(None, ge=0)
    max_rewarded: int | None = Field(None, ge=1)
    conditions: RewardConditions = Field(default_factory=RewardConditions)
    forfeit: Forfeit = Field(default_factory=Forfeit)
    delivery: Delivery = Field(default_factory=Delivery)

    @model_validator(mode="after")
    def _ok(self):
        if not self.enabled:
            return self
        if not (self.label or "").strip():
            raise ValueError("reward_label_required")
        if self.kind in ("points", "credits", "cash", "airtime") and self.amount <= 0:
            raise ValueError("reward_amount_required")
        if self.mode == "lottery" and self.winners < 1:
            raise ValueError("lottery_winners_required")
        return self


SHARE_FIELDS = ("age_band", "gender", "city", "occupation_group", "education_level", "income_band", "household_size")


class PrivacyCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share_profile: list[Literal["age_band", "gender", "city", "occupation_group", "education_level", "income_band",
                                "household_size"]] = Field(default_factory=list)
    collect_contact: bool = False
    contact_purpose: str | None = Field(None, max_length=200)