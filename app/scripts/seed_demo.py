"""
Seed de démonstration SunuSound (à exécuter UNE fois, dans le conteneur de l'API).

    python -m app.scripts.seed_demo --users 2200 --days 90
    python -m app.scripts.seed_demo --reset            # purge les données démo puis recrée tout
    python -m app.scripts.seed_demo --purge            # purge seulement

Principe : aucune règle métier n'est cassée. On saute seulement les workers Celery :
  * mesures  : qualité calculée par la VRAIE fonction pure quality.evaluate (statuts/scores/raisons cohérents),
               avis des analyseurs décidés par la VRAIE reviews.decide, agrégats par la VRAIE aggregate_dirty,
               anonymisation de position > LOCATION_RETENTION_DAYS appliquée comme le fait privacy.
  * enquêtes : définitions validées par SurveyDefinition, profils générés par survey_profiles,
               réponses fusionnées/contrôlées par les règles du catalogue, qualité par survey_quality.evaluate,
               évolution des profils par survey_profiles.evolve, confiance par survey_trust.refresh_user.
  * quotas / classement / XP / badges / poids adaptatifs : fonctions réelles du service.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import struct
import time
import unicodedata
import uuid
import zlib
from collections import defaultdict, namedtuple
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

import h3
from fastapi import HTTPException
from geoalchemy2 import WKTElement
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.redis import new_redis
from app.core.security import contributor_token, hash_device, hash_email, hash_password, make_pseudonym, new_code
from app.db.session import task_session_factory
from app.models import (CollectionProposal, CollectionSchedule, Device, DeviceCalibration, DeviceModel, Invitation,
                        LeaderboardSnapshot, LoginEvent, Measurement, MeasurementQuality, MeasurementReview, Mission,
                        NoiseAggregation, ProfileEvent, QuotaDay, ReferenceValue, ReviewVote, Survey, SurveyAnswer,
                        SurveyEvidence, SurveyProfile, SurveyResponse, SurveyResponseQuality, Tenant,
                        TenantAlgorithmState, User, UserConsent, UserProfile, UserQuota, UserResponseStat)
from app.models.enums import (CalibrationStatus, ConsentType, EvidenceStatus, MissionStatus, NoiseContext,
                              ProposalStatus, QualityStatus, Role, ScheduleMode, SurveyDomain, SurveyStatus,
                              TriggerMode)
from app.schemas.schemas import TenantCreate
from app.schemas.surveys import SurveyCreate
from app.services import aggregation, catalog
from app.services import collection as collection_service
from app.services import leaderboard as lb
from app.services import quota as quota_service
from app.services import reviews as reviews_service
from app.services import survey_admin as adm
from app.services import survey_profiles as sprof
from app.services import survey_quality as sq
from app.services import survey_trust
from app.services import tenant as tenant_service
from app.services.accounts import display_key
from app.services.audit import audit
from app.services.geo import TZ, day_part_of, to_local
from app.services.quality import MeasurementFacts
from app.services.quality import evaluate as quality_evaluate
from app.services.survey_schema import all_rules, check_rules, coerce_answer, questions_of, answer_rows
from app.services.thresholds import build_thresholds

UTC = timezone.utc
DEMO_DOMAIN = "demo.sunusound.sn"
DEMO_PASSWORD = "Demo#Sunu2026!"
MARK = " [démo]"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def demo_email(i: int) -> str:
    return f"demo{i:05d}@{DEMO_DOMAIN}"


STAFF = [f"analyste{i}@{DEMO_DOMAIN}" for i in (1, 2)]


@asynccontextmanager
async def tdb(factory, tid):
    async with factory() as db:
        db.info["tenant_id"] = str(tid)
        yield db


async def bulk(db, model, rows, size=2000):
    for c in chunks(rows, size):
        await db.execute(insert(model), c)


# ============================================================================ géographie
D = namedtuple("D", "region city name lat lon w db kind wealth")
_DIST = [
    ("dakar", "Dakar", "Plateau", 14.6708, -17.4381, 3.0, 68, "urbain_dense", .80),
    ("dakar", "Dakar", "Médina", 14.6853, -17.4491, 3.0, 67, "urbain_dense", .45),
    ("dakar", "Dakar", "Grand Yoff", 14.7255, -17.4560, 3.2, 64, "urbain_dense", .40),
    ("dakar", "Dakar", "Parcelles Assainies", 14.7600, -17.4400, 3.5, 63, "urbain_dense", .40),
    ("dakar", "Dakar", "Ouakam", 14.7200, -17.4850, 2.2, 63, "urbain", .55),
    ("dakar", "Dakar", "Yoff", 14.7500, -17.4900, 2.2, 62, "urbain", .50),
    ("dakar", "Dakar", "Ngor", 14.7510, -17.5120, 1.0, 58, "urbain", .65),
    ("dakar", "Dakar", "Almadies", 14.7430, -17.5190, 1.2, 57, "urbain", .90),
    ("dakar", "Dakar", "Mermoz", 14.7040, -17.4690, 1.6, 62, "urbain", .80),
    ("dakar", "Dakar", "HLM", 14.7110, -17.4400, 2.0, 66, "urbain_dense", .45),
    ("dakar", "Dakar", "Fann", 14.6930, -17.4670, 1.2, 61, "urbain", .85),
    ("dakar", "Pikine", "Pikine Centre", 14.7550, -17.3900, 3.2, 66, "urbain_dense", .30),
    ("dakar", "Pikine", "Thiaroye", 14.7600, -17.3500, 2.0, 64, "urbain_dense", .30),
    ("dakar", "Guédiawaye", "Guédiawaye Centre", 14.7750, -17.3900, 2.8, 65, "urbain_dense", .30),
    ("dakar", "Keur Massar", "Keur Massar", 14.7800, -17.3100, 2.2, 60, "periurbain", .30),
    ("dakar", "Rufisque", "Rufisque Centre", 14.7167, -17.2667, 1.8, 62, "urbain", .40),
    ("dakar", "Bargny", "Bargny", 14.6950, -17.2300, .6, 58, "periurbain", .30),
    ("dakar", "Diamniadio", "Diamniadio", 14.7200, -17.1800, .6, 57, "periurbain", .50),
    ("thies", "Thiès", "Thiès Centre", 14.7886, -16.9260, 1.6, 61, "urbain", .45),
    ("thies", "Thiès", "Thiès Randoulène", 14.8000, -16.9450, .8, 58, "periurbain", .35),
    ("thies", "Mbour", "Mbour Centre", 14.4198, -16.9640, 1.3, 62, "urbain", .45),
    ("thies", "Mbour", "Saly", 14.4474, -17.0094, .8, 58, "urbain", .75),
    ("thies", "Tivaouane", "Tivaouane", 14.9500, -16.8167, .6, 57, "urbain", .35),
    ("thies", "Joal-Fadiouth", "Joal", 14.1667, -16.8333, .4, 55, "urbain", .35),
    ("diourbel", "Touba", "Touba Centre", 14.8500, -15.8833, 2.6, 65, "urbain_dense", .30),
    ("diourbel", "Touba", "Touba Ndamatou", 14.8300, -15.9100, 1.2, 60, "periurbain", .25),
    ("diourbel", "Mbacké", "Mbacké", 14.7900, -15.9083, .9, 60, "urbain", .30),
    ("diourbel", "Diourbel", "Diourbel Centre", 14.6553, -16.2316, 1.0, 59, "urbain", .35),
    ("kaolack", "Kaolack", "Kaolack Centre", 14.1652, -16.0753, 1.6, 62, "urbain", .40),
    ("kaolack", "Kaolack", "Médina Baye", 14.1580, -16.0900, .8, 59, "urbain", .30),
    ("kaffrine", "Kaffrine", "Kaffrine", 14.1059, -15.5508, .5, 56, "urbain", .30),
    ("fatick", "Fatick", "Fatick", 14.3390, -16.4111, .5, 55, "urbain", .30),
    ("fatick", "Gossas", "Gossas rural", 14.4900, -16.2700, .3, 44, "rural", .20),
    ("saint_louis", "Saint-Louis", "Île de Saint-Louis", 16.0300, -16.5000, 1.0, 60, "urbain", .45),
    ("saint_louis", "Saint-Louis", "Sor", 16.0100, -16.4700, .9, 58, "urbain", .35),
    ("saint_louis", "Richard-Toll", "Richard-Toll", 16.4625, -15.7011, .5, 56, "urbain", .40),
    ("saint_louis", "Podor", "Podor", 16.6500, -14.9667, .3, 48, "rural", .20),
    ("louga", "Louga", "Louga Centre", 15.6144, -16.2246, .9, 58, "urbain", .35),
    ("louga", "Linguère", "Linguère", 15.3944, -15.1181, .3, 47, "rural", .20),
    ("matam", "Matam", "Matam", 15.6559, -13.2554, .4, 52, "urbain", .25),
    ("matam", "Ourossogui", "Ourossogui", 15.6000, -13.3200, .3, 46, "rural", .20),
    ("tambacounda", "Tambacounda", "Tambacounda Centre", 13.7707, -13.6673, .8, 58, "urbain", .30),
    ("tambacounda", "Bakel", "Bakel", 14.9000, -12.4667, .25, 47, "rural", .20),
    ("kedougou", "Kédougou", "Kédougou", 12.5579, -12.1743, .3, 52, "urbain", .25),
    ("kolda", "Kolda", "Kolda Centre", 12.8833, -14.9500, .6, 56, "urbain", .30),
    ("kolda", "Vélingara", "Vélingara", 13.1500, -14.1100, .3, 48, "rural", .20),
    ("sedhiou", "Sédhiou", "Sédhiou", 12.7081, -15.5569, .3, 50, "urbain", .25),
    ("ziguinchor", "Ziguinchor", "Ziguinchor Centre", 12.5833, -16.2719, .9, 58, "urbain", .40),
    ("ziguinchor", "Ziguinchor", "Boucotte", 12.5700, -16.2900, .5, 54, "periurbain", .30),
    ("ziguinchor", "Bignona", "Bignona", 12.8100, -16.2260, .3, 48, "rural", .20),
    ("ziguinchor", "Cap Skirring", "Cap Skirring", 12.3930, -16.7440, .3, 52, "urbain", .55),
]
DISTRICTS = [D(*t) for t in _DIST]
CITY_DISTRICTS: dict[str, list[D]] = defaultdict(list)
for _d in DISTRICTS:
    CITY_DISTRICTS[_d.city].append(_d)
REGION_CODES = ["dakar", "thies", "diourbel", "saint_louis", "louga", "matam", "kaolack", "kaffrine", "fatick",
                "tambacounda", "kedougou", "kolda", "sedhiou", "ziguinchor"]

FIRST_M = ["Moussa", "Abdoulaye", "Ibrahima", "Mamadou", "Cheikh", "Ousmane", "Modou", "Pape", "Serigne", "Alioune",
           "Babacar", "Lamine", "Idrissa", "Samba", "Mbaye", "Assane", "Seydou", "Boubacar", "Omar", "Demba"]
FIRST_F = ["Fatou", "Aminata", "Awa", "Khady", "Mariama", "Ndeye", "Aissatou", "Rokhaya", "Coumba", "Sokhna", "Binta",
           "Dieynaba", "Marieme", "Astou", "Yacine", "Bineta", "Adja", "Mame", "Nabou", "Safiatou"]
LAST = ["Diop", "Ndiaye", "Fall", "Gueye", "Sow", "Sarr", "Ba", "Diallo", "Faye", "Mbaye", "Sy", "Cisse", "Thiam",
        "Camara", "Seck", "Diouf", "Niang", "Kane", "Sene", "Badji", "Sagna", "Diatta", "Coly", "Mendy", "Tall",
        "Ndour", "Lo", "Samb", "Wade", "Gaye", "Konate", "Traore", "Sonko"]

MODELS = [  # fabricant, modèle, android, part, calibré, correction dB
    ("Tecno", "Spark 10", "13", .12, True, 2.1), ("Tecno", "Camon 20", "13", .06, True, 1.4),
    ("Tecno", "Spark Go 2023", "12", .07, False, 0), ("Infinix", "Hot 30", "13", .11, True, -1.8),
    ("Infinix", "Smart 7", "12", .07, False, 0), ("Infinix", "Note 30", "13", .04, True, 0.9),
    ("Samsung", "Galaxy A14", "13", .09, True, -3.2), ("Samsung", "Galaxy A03", "11", .06, False, 0),
    ("Samsung", "Galaxy A54", "14", .04, True, -2.4), ("Samsung", "Galaxy S21", "13", .02, True, 0.6),
    ("Xiaomi", "Redmi 12C", "13", .07, True, 3.7), ("Xiaomi", "Redmi Note 11", "12", .04, True, 1.1),
    ("itel", "A60s", "12", .04, False, 0), ("itel", "P40", "11", .03, False, 0),
    ("Oppo", "A17", "12", .03, True, -0.7), ("Huawei", "Y9 Prime", "10", .02, False, 0),
    ("Google", "Pixel 6a", "14", .01, True, 0.2), ("Realme", "C53", "13", .03, True, 2.8),
]
WEEK = [1, 1, 1, 1, 1, .8, .6]


# ============================================================================ contexte et utilisateurs
@dataclass
class U:
    i: int
    id: uuid.UUID
    email: str
    handle: str
    first: str
    last: str
    phone: str
    birth: int
    d: D
    home: tuple
    work: tuple
    joined: datetime
    last_active: datetime
    arche: str
    trait: str
    model: dict
    install: str
    hybrid: bool
    analyst: bool
    paused: bool
    hours: list
    n_meas: int
    aff: float
    has_firm: bool
    has_farm: bool
    photo: bool
    latent: dict
    role: Role = Role.contributor
    invited_by: int | None = None
    ums: list = field(default_factory=list)       # (ts, lat, lon, status, zone7)
    validated: int = 0


@dataclass
class Ctx:
    tid: uuid.UUID
    admin_id: uuid.UUID
    now: datetime
    start: datetime
    days: int
    th: object
    pw_hash: str
    models: list
    rng: random.Random
    cellmap: dict = field(default_factory=dict)
    new_cells: list = field(default_factory=list)
    users: list = field(default_factory=list)
    meas: list = field(default_factory=list)
    qual: list = field(default_factory=list)
    props: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    votes: list = field(default_factory=list)
    qdays: dict = field(default_factory=dict)
    rstats: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    missions: list = field(default_factory=list)
    token_cache: dict = field(default_factory=dict)
    analysts_by_region: dict = field(default_factory=lambda: defaultdict(list))


def ascii_(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def pick(rng, pairs):
    return rng.choices([p[0] for p in pairs], [p[1] for p in pairs])[0]


def clampi(x, lo, hi):
    return int(max(lo, min(hi, round(x))))


def binom(rng, n, p):
    return sum(rng.random() < p for _ in range(max(0, n)))


def gauss_pt(rng, lat, lon, sigma):
    return (lat + rng.gauss(0, sigma) / 110574.0,
            lon + rng.gauss(0, sigma) / (111320.0 * math.cos(math.radians(lat))))


def cell_of(C: Ctx, lat: float, lon: float) -> tuple[str, uuid.UUID]:
    h9 = h3.latlng_to_cell(lat, lon, settings.H3_RESOLUTION)
    cid = C.cellmap.get(h9)
    if cid is None:
        cid = uuid.uuid4()
        C.cellmap[h9] = cid
        la, lo = h3.cell_to_latlng(h9)
        C.new_cells.append({"id": cid, "tid": C.tid, "h3": h9, "res": settings.H3_RESOLUTION, "lat": la, "lon": lo})
    return h9, cid


def token(C: Ctx, u_id, ts: datetime) -> str:
    k = (u_id, f"{to_local(ts):%Y-%m}")
    v = C.token_cache.get(k)
    if v is None:
        v = C.token_cache[k] = contributor_token(C.tid, u_id, k[1])
    return v


# ============================================================================ foyer latent (cohérent entre enquêtes)
def make_latent(rng: random.Random, d: D, has_firm: bool, has_farm: bool) -> dict:
    k = d.kind
    rural, dense, peri = k == "rural", k == "urbain_dense", k == "periurbain"
    w = max(0.0, min(1.0, rng.gauss(d.wealth, .17)))
    n = clampi(rng.gauss(10 if rural else 8.5 if dense else 7.5 if peri else 7, 3.8), 1, 25)
    fem = binom(rng, n, .51)
    u15 = binom(rng, n, .45 if rural else .38)
    o65 = binom(rng, n - u15, .07)
    a64 = n - u15 - o65
    adults = a64 + o65
    L: dict = {}
    L.update(household_persons=n, persons_female=fem, persons_male=n - fem, persons_0_14=u15, persons_15_64=a64,
             persons_65p=o65, births_12m=min(n, int(rng.random() < .32) + int(rng.random() < .04)),
             deaths_12m=int(rng.random() < .035), neighborhood_density=clampi({"urbain_dense": 4.4, "urbain": 3.2, "periurbain": 2.5, "rural": 1.5}[k] + rng.gauss(0, .7), 1, 5),
             locality_type=k if rng.random() > .08 else pick(rng, [("urbain", 1), ("periurbain", 1)]))
    # ménages
    ht = "isole" if n == 1 else pick(rng, [("couple", .6), ("monoparental", .4)]) if n == 2 else \
        pick(rng, [("couple_enfants", .5), ("monoparental", .2), ("elargi", .3)]) if n <= 5 else \
        pick(rng, [("elargi", .6), ("couple_enfants", .3), ("collectif", .1)])
    L.update(household_type=ht, dependents=clampi(u15 + o65 + rng.random() * 2, 0, 40),
             households_in_compound=pick(rng, [(1, .35), (2, .3), (3, .2), (4, .1), (6, .05)]),
             living_conditions=clampi(1.2 + 3.6 * w + rng.gauss(0, .6), 1, 5),
             meals_per_day=3 if (w > .35 and rng.random() < .85) else pick(rng, [(2, .7), (1, .15), (3, .15)]),
             food_insecurity=rng.random() < max(.03, .5 - .5 * w + (.08 if rural else 0)))
    # habitat
    if w > .7:
        dw = pick(rng, [("maison_dure", .45), ("immeuble", .35), ("concession", .1), ("autre", .1)])
    elif rural:
        dw = pick(rng, [("case", .3), ("maison_dure", .25), ("concession", .35), ("autre", .1)])
    elif dense:
        dw = pick(rng, [("concession", .4), ("maison_dure", .3), ("immeuble", .12), ("baraque", .15), ("autre", .03)])
    else:
        dw = pick(rng, [("maison_dure", .45), ("concession", .25), ("baraque", .12), ("immeuble", .1), ("case", .03), ("autre", .05)])
    wall = "dur" if dw in ("maison_dure", "immeuble") else pick(rng, [("banco", .6), ("bois_paille", .4)]) if dw == "case" \
        else pick(rng, [("tole_zinc", .6), ("bois_paille", .4)]) if dw == "baraque" \
        else pick(rng, [("dur", .7), ("banco", .2), ("autre", .1)]) if dw == "concession" else "autre"
    roof = pick(rng, [("dalle", .6), ("tole", .35), ("tuile", .05)]) if wall == "dur" else \
        pick(rng, [("chaume", .6), ("tole", .4)]) if dw == "case" else "tole"
    water = pick(rng, [("forage_protege", .35), ("puits_non_protege", .15), ("robinet_cour", .15), ("borne_fontaine", .25), ("surface", .05), ("vendeur", .05)]) if rural else \
        pick(rng, [("robinet_interieur", .7), ("robinet_cour", .25), ("borne_fontaine", .05)]) if w > .55 else \
        pick(rng, [("robinet_cour", .4), ("borne_fontaine", .25), ("robinet_interieur", .2), ("vendeur", .1), ("forage_protege", .05)])
    elec = pick(rng, [("reseau", .5), ("solaire", .28), ("groupe", .02), ("aucun", .2)]) if rural else \
        pick(rng, [("reseau", .93), ("solaire", .03), ("groupe", .02), ("aucun", .02)])
    sani = pick(rng, [("chasse_reseau", .45), ("fosse_septique", .45), ("latrine_amelioree", .1)]) if w > .55 else \
        pick(rng, [("latrine_simple", .4), ("latrine_amelioree", .25), ("aucune", .25), ("fosse_septique", .1)]) if rural else \
        pick(rng, [("fosse_septique", .35), ("chasse_reseau", .25), ("latrine_amelioree", .2), ("latrine_simple", .15), ("aucune", .05)])
    L.update(dwelling_type=dw, wall_material=wall, roof_material=roof,
             rooms_count=clampi(n / 2.6 + 2 * w + rng.gauss(0, 1), 1, 15), water_access=water, electricity_access=elec,
             sanitation=sani, tenure=pick(rng, [("proprietaire", .4 if dense else .6), ("locataire", .4 if dense else .25), ("loge_gratuit", .12), ("autre", .03)]),
             flood_risk=rng.random() < {"urbain_dense": .28, "periurbain": .4, "urbain": .12, "rural": .08}[k])
    # emploi
    emp = min(a64, clampi(a64 * rng.uniform(.2, .6), 0, 40))
    une = min(a64 - emp, int(rng.random() < .35) + int(rng.random() < .1))
    occ = pick(rng, [("agriculture_peche", .55), ("commerce", .15), ("artisanat", .1), ("services", .05), ("transport", .05), ("autre", .1)]) if rural else \
        pick(rng, [("commerce", .3), ("services", .25), ("artisanat", .1), ("administration", .1), ("industrie_btp", .1), ("transport", .1), ("autre", .05)])
    L.update(employment_status=pick(rng, [("occupe", .5), ("chomeur", .14), ("inactif", .2), ("etudiant", .1), ("retraite", .06)]),
             occupation_group=occ, job_formality="formel" if (occ == "administration" and rng.random() < .8) or rng.random() < .1 else "informel",
             persons_employed=emp, persons_unemployed=une, job_search_months=0 if une == 0 else clampi(rng.lognormvariate(2.2, .8), 1, 120),
             underemployed=rng.random() < .3)
    # entreprise
    size = pick(rng, [("individuelle", .55), ("micro", .3), ("petite", .1), ("moyenne", .04), ("grande", .01)])
    emps = {"individuelle": 1, "micro": rng.randint(2, 5), "petite": rng.randint(6, 20), "moyenne": rng.randint(21, 100), "grande": rng.randint(101, 400)}[size]
    turn = {"individuelle": [("lt500k", .5), ("m05_2", .5)], "micro": [("m05_2", .5), ("m2_10", .5)], "petite": [("m2_10", .6), ("m10_50", .4)],
            "moyenne": [("m10_50", .6), ("gt50m", .4)], "grande": [("gt50m", 1)]}[size]
    L.update(firm_activity=pick(rng, [("commerce", .4), ("services", .15), ("transport", .08), ("artisanat", .12), ("restauration", .1), ("industrie", .04), ("btp", .05), ("agro_transfo", .04), ("autre", .02)]),
             firm_size=size, firm_employees=emps, firm_age_years=clampi(rng.lognormvariate(1.7, .8), 0, 60),
             firm_turnover_band=pick(rng, turn), firm_registered=rng.random() < {"individuelle": .12, "micro": .35, "petite": .7, "moyenne": .95, "grande": 1}[size],
             firm_outlook=clampi(3 + rng.gauss(0, 1), 1, 5))
    # agriculture
    crop = pick(rng, [("riz", .5), ("arachide", .15), ("mil_sorgho", .15), ("maraichage", .1), ("mais", .1)]) if d.region in ("ziguinchor", "kolda", "sedhiou") else \
        pick(rng, [("maraichage", .45), ("arboriculture", .15), ("mil_sorgho", .2), ("arachide", .2)]) if d.region in ("dakar", "thies") else \
        pick(rng, [("arachide", .4), ("mil_sorgho", .35), ("niebe", .1), ("mais", .1), ("manioc", .05)])
    area = rng.lognormvariate(.6, .9) if rural else rng.lognormvariate(-1.2, .7)
    L.update(farm_area_ha=round(min(area, 500), 2), main_crop=crop, cattle_count=clampi(rng.expovariate(1 / 6), 0, 200) if rng.random() < (.5 if rural else .1) else 0,
             small_ruminants_count=clampi(rng.expovariate(1 / 8), 0, 300) if rng.random() < (.65 if rural else .15) else 0,
             poultry_count=clampi(rng.expovariate(1 / 25), 0, 2000) if rng.random() < .5 else 0,
             harvest_kg=clampi(area * rng.uniform(300, 2200), 0, 10_000_000), irrigated=rng.random() < (.7 if crop == "maraichage" else .12),
             farm_inputs_access=clampi(2.8 + rng.gauss(0, 1), 1, 5))
    # éducation
    c616 = min(u15, clampi(n * .26 + rng.gauss(0, .8), 0, 12))
    L.update(children_6_16=c616, children_enrolled=binom(rng, c616, (.62 if rural else .88) * (.8 + .2 * w)),
             adults_15p=adults, adults_literate=binom(rng, adults, (.42 if rural else .72) * (.8 + .3 * w)),
             education_level_max=pick(rng, [("aucun", max(.02, .25 - .2 * w)), ("coranique", .1), ("primaire", .25), ("college", .18), ("lycee", .12 + .1 * w), ("superieur", .05 + .35 * w)]),
             school_distance_km=round(min(50, rng.lognormvariate(.4, .7) if rural else rng.lognormvariate(-1, .6)), 1),
             school_type=pick(rng, [("public", .65), ("prive_laic", .15), ("prive_confessionnel", .06), ("communautaire", .06), ("daara", .06), ("aucune", .02 + (.05 if rural else 0))]),
             school_quality=clampi(3 + rng.gauss(0, 1), 1, 5))
    # santé
    dist = round(max(.1, min(100, rng.lognormvariate(1.3, .6) if rural else rng.lognormvariate(-.2, .7))), 1)
    L.update(health_facility_type=pick(rng, [("case_sante", .25), ("poste_sante", .5), ("centre_sante", .15), ("hopital", .03), ("aucune", .07)]) if rural else
             pick(rng, [("poste_sante", .2), ("centre_sante", .3), ("hopital", .15), ("clinique_privee", .1), ("pharmacie", .2), ("case_sante", .02), ("aucune", .03)]),
             facility_distance_km=dist, time_to_care_min=clampi(dist * (9 if rural else 5) * rng.uniform(.8, 1.5) + rng.uniform(3, 12), 1, 600),
             care_cost_barrier=rng.random() < max(.05, .5 - .4 * w),
             health_coverage=pick(rng, [("aucune", max(.1, .55 - .4 * w)), ("mutuelle", .2), ("cmu", .15), ("privee", .02 + .1 * w), ("employeur", .03 + .06 * w), ("autre", .04)]),
             children_vaccinated=rng.random() < (.65 if rural else .8), facility_quality=clampi(3 + rng.gauss(0, 1), 1, 5),
             facility_stockout=rng.random() < .4)
    # économie
    z = w + rng.gauss(0, .15)
    ii = 0 if z < .2 else 1 if z < .4 else 2 if z < .62 else 3 if z < .85 else 4
    si = max(0, min(4, ii - int(rng.random() < .4) + int(rng.random() < .1)))
    codes = ["lt50k", "k50_100", "k100_200", "k200_500", "gt500k"]
    remote = d.region in ("kedougou", "matam", "tambacounda") or d.name in ("Podor", "Bakel")
    L.update(household_income_band=codes[ii], household_spending_band=codes[si],
             price_rice_kg=clampi(rng.gauss(480 + (70 if remote else 0), 50), 100, 2000), price_oil_l=clampi(rng.gauss(1350, 150), 300, 4000),
             price_transport_ride=clampi(rng.gauss({"urbain_dense": 250, "urbain": 300, "periurbain": 400, "rural": 700}[k], 80 if not rural else 300), 50, 10000),
             purchasing_power_trend=pick(rng, [("baisse", .55), ("stable", .35), ("hausse", .1)]), household_debt=rng.random() < .3 + .1 * (1 - w))
    # migration
    moved = rng.random() < (.45 if peri else .3 if dense else .22)
    ab = min(n, pick(rng, [(0, .75), (1, .14), (2, .07), (3, .03), (5, .01)]))
    L.update(moved_last_5y=moved, origin_region=rng.choice(REGION_CODES) if moved else d.region,
             move_reason=pick(rng, [("travail", .35), ("famille", .25), ("etudes", .12), ("logement", .1), ("climat", .08), ("insecurite", .04), ("autre", .06)]),
             members_abroad=ab, remittances_received=ab > 0 and rng.random() < .7, intends_emigrate=rng.random() < .16)
    # genre
    w1564 = min(fem, int(round(fem * a64 / max(1, n))))
    L.update(female_headed=rng.random() < (.3 if dense else .22), women_15_64=w1564, women_employed=binom(rng, w1564, .33))
    L["woman_income_activity"] = L["women_employed"] > 0 or rng.random() < .15
    L.update(woman_entrepreneur=rng.random() < .12 + (.2 if has_firm and w > .3 else 0), woman_land_access=rng.random() < .1,
             woman_decides_spending=clampi(2.5 + rng.gauss(0, 1), 1, 5))
    # numérique
    uses = [c for c, p in (("reseaux_sociaux", .6), ("commerce", .25 + .2 * w), ("education", .25), ("administration", .15), ("sante", .1),
                           ("banque_mobile", .35 + .3 * w), ("divertissement", .4)) if rng.random() < p] or ["reseaux_sociaux"]
    L.update(internet_access=pick(rng, [("aucun", max(.03, .35 - .3 * w)), ("mobile", .5), ("fixe", .05 + .1 * w), ("mobile_fixe", .05 + .3 * w)]),
             smartphones_count=clampi(n * (.2 + .4 * w) * rng.uniform(.5, 1.1), 0, n), computers_count=int(rng.random() < .1 + .5 * w) + int(rng.random() < .1 * w),
             mobile_money_use=rng.random() < .55 + .4 * w, internet_hours_day=round(max(0, min(24, rng.gauss(.8 + 4 * w, 1.3))), 1),
             digital_uses=uses, network_quality=clampi(3 + rng.gauss(0, 1) - (1 if rural else 0), 1, 5))
    for key, ind in catalog.INDICATORS.items():          # nettoyage final : bornes, entiers
        v = L.get(key)
        if v is None:
            L[key] = ind.lo if ind.kind == "number" else (1 if ind.kind == "scale" else False if ind.kind == "share" else ind.cats[0].code)
        elif ind.kind == "number":
            v = max(ind.lo, min(ind.hi, float(v)))
            L[key] = int(round(v)) if ind.int_ else round(v, 2)
    return L


def drift(L: dict, rng: random.Random, frac: float) -> dict:
    """Évolution lente du foyer au fil des mois (uniquement des champs sans règle comptable)."""
    X = dict(L)
    X["price_rice_kg"] = clampi(L["price_rice_kg"] * (1 + .05 * frac + rng.gauss(0, .012)), 100, 2000)
    X["price_oil_l"] = clampi(L["price_oil_l"] * (1 + .06 * frac + rng.gauss(0, .015)), 300, 4000)
    X["price_transport_ride"] = clampi(L["price_transport_ride"] * (1 + .03 * frac + rng.gauss(0, .02)), 50, 10000)
    if rng.random() < .1 * frac and L["internet_access"] == "mobile":
        X["internet_access"] = "mobile_fixe"
    if rng.random() < .08:
        X["food_insecurity"] = not L["food_insecurity"]
    if rng.random() < .1:
        X["network_quality"] = clampi(L["network_quality"] + rng.choice([-1, 1]), 1, 5)
    if rng.random() < .06:
        X["household_debt"] = not L["household_debt"]
    return X


def build_users(C: Ctx, n: int) -> list[U]:
    rng = C.rng
    weights = [d.w for d in DISTRICTS]
    handles: set = set()
    users: list[U] = []
    for i in range(n):
        d = rng.choices(DISTRICTS, weights)[0]
        fem = rng.random() < .48
        first, last = rng.choice(FIRST_F if fem else FIRST_M), rng.choice(LAST)
        while True:
            h = f"{ascii_(first)[:9]}_{ascii_(last)[:7]}{rng.randint(1, 999)}"
            if h not in handles:
                break
        handles.add(h)
        sig = 600 if d.kind == "rural" else 380
        home = gauss_pt(rng, d.lat, d.lon, sig)
        wd = rng.choice(CITY_DISTRICTS[d.city]) if rng.random() < .5 else d
        work = gauss_pt(rng, wd.lat, wd.lon, sig)
        joined = C.start + timedelta(days=C.days * .92 * (rng.random() ** 1.7), seconds=rng.randint(0, 86399))
        joined = min(joined, C.now - timedelta(hours=6))
        r = rng.random()
        arche = "heavy" if r < .04 else "regular" if r < .25 else "casual" if r < .70 else "dormant"
        lo, hi = {"heavy": (120, 300), "regular": (28, 85), "casual": (5, 26), "dormant": (0, 5)}[arche]
        frac = max(.05, (C.now - joined).total_seconds() / 86400 / C.days)
        nm = int(rng.uniform(lo, hi) * frac ** .8)
        if arche == "heavy":
            nm = max(nm, 25)
        last_active = C.now if rng.random() > .15 else joined + (C.now - joined) * rng.uniform(.3, .8)
        t = rng.random()
        trait = "careful" if t < .70 else "average" if t < .90 else "hasty" if t < .97 else "noisy"
        has_firm = rng.random() < (.15 if d.kind == "rural" else .38)
        has_farm = rng.random() < {"rural": .7, "periurbain": .25}.get(d.kind, .08)
        m = pick(rng, [(x, x["share"]) for x in C.models])
        hours = rng.choice([[7, 8, 9], [12, 13], [17, 18, 19, 20], [21, 22, 23], [10, 11], [14, 15, 16]]) + \
            rng.choice([[7, 8, 9], [12, 13], [17, 18, 19, 20], [21, 22, 23]])
        users.append(U(
            i=i, id=uuid.uuid4(), email=demo_email(i), handle=h, first=first, last=last,
            phone="+22177" + "".join(str(rng.randint(0, 9)) for _ in range(7)), birth=rng.randint(1958, 2008), d=d,
            home=home, work=work, joined=joined, last_active=last_active, arche=arche, trait=trait, model=m,
            install=str(uuid.uuid4()), hybrid=rng.random() < .5, analyst=False, paused=rng.random() < .03, hours=hours,
            n_meas=nm, aff=0.0 if rng.random() < .12 else rng.betavariate(2, 3), has_firm=has_firm, has_farm=has_farm,
            photo=rng.random() < .3, latent=make_latent(rng, d, has_firm, has_farm)))
    # parrainage et analyseurs
    for u in users[20:]:
        if rng.random() < .14:
            cands = [x for x in rng.sample(users[:u.i], min(30, u.i)) if x.joined < u.joined]
            if cands:
                u.invited_by = max(cands, key=lambda x: (x.arche in ("heavy", "regular"), rng.random())).i
    pool = [u for u in users if u.trait == "careful" and u.arche in ("heavy", "regular") and (C.now - u.joined).days >= 35]
    for u in rng.sample(pool, min(len(pool), max(60, n // 16))):
        u.analyst = True
        C.analysts_by_region[u.d.region].append(u)
    return users


# ============================================================================ mesures de bruit
def _u(s: str) -> float:
    return (zlib.crc32(s.encode()) % 10000) / 10000.0


def expected_level(d: D, h9: str, dp: str, dow: int) -> float:
    off = -3 + 13 * (_u(h9) ** 2.3) + (_u(h3.cell_to_parent(h9, 8)) - .5) * 5
    night = -6 if d.kind in ("urbain_dense", "urbain") else -10
    return d.db + off + {"morning": 1.5, "afternoon": 2.5, "evening": 1.0, "night": night}[dp] + (-2 if dow == 6 else 1 if dow == 4 else 0)


def z7(lat: float, lon: float) -> str:
    return h3.latlng_to_cell(lat, lon, settings.MISSION_ZONE_H3_RES)


def gen_user_meas(C: Ctx, u: U) -> None:
    rng = C.rng
    if u.n_meas <= 0 or u.role != Role.contributor:
        return
    t0, t1 = max(u.joined, C.start), u.last_active
    span = (t1 - t0).total_seconds()
    if span < 3600:
        return
    sessions = []
    for _ in range(max(1, round(u.n_meas / 2.2))):
        for _t in range(6):
            ts = t0 + timedelta(seconds=rng.random() * span)
            if rng.random() < WEEK[ts.weekday()]:
                break
        ts = ts.replace(hour=rng.choice(u.hours), minute=rng.randrange(60), second=rng.randrange(60), microsecond=0)
        if ts > C.now - timedelta(minutes=5):
            ts = C.now - timedelta(minutes=rng.randint(6, 600))
        sessions.append(max(ts, t0 + timedelta(minutes=5)))
    made = 0
    for ts0 in sorted(sessions):
        r = rng.random()
        if r < .6:
            base, dist = u.home, u.d
        elif r < .85:
            base, dist = u.work, u.d
        else:
            dist = rng.choice(CITY_DISTRICTS[u.d.city]) if rng.random() < .96 else rng.choice(DISTRICTS)
            base = gauss_pt(rng, dist.lat, dist.lon, 500)
        pos = gauss_pt(rng, base[0], base[1], 150)
        ts = ts0
        for _ in range(1 + min(4, int(rng.expovariate(1 / 1.2)))):
            if made >= u.n_meas + 3 or ts > C.now - timedelta(minutes=2):
                break
            one_meas(C, u, ts, pos[0], pos[1], dist)
            made += 1
            ts += timedelta(minutes=rng.randint(2, 12))
            pos = gauss_pt(rng, pos[0], pos[1], 90)


def one_meas(C: Ctx, u: U, ts: datetime, lat: float, lon: float, dist: D) -> None:
    rng = C.rng
    local = to_local(ts)
    dp = day_part_of(local)
    h9, cid = cell_of(C, lat, lon)
    exp = expected_level(dist, h9, dp.value, local.weekday())
    spike = rng.random() < .035
    L = min(115.0, max(30.0, exp + rng.gauss(0, 2.8) + (rng.uniform(8, 18) if spike else 0)))
    m = u.model
    corr = m["corr"] if m["calibrated"] else 0.0
    raw = L - corr
    lmax = raw + abs(rng.gauss(7, 3)) + 1.5
    lmin = max(15.0, raw - abs(rng.gauss(6, 2.2)) - 1.0)
    sloppy = u.trait in ("hasty", "noisy")
    acc = rng.lognormvariate(math.log(22 if sloppy else 11), .55)
    if rng.random() < .025:
        acc = rng.uniform(100, 260)
    eff = 15.0 * (rng.uniform(.6, .88) if rng.random() < (.05 if sloppy else .004) else rng.uniform(.985, 1.0))
    sat = rng.uniform(.05, .25) if rng.random() < (.08 if sloppy else .03) else rng.uniform(0, .012)
    moved = rng.uniform(35, 90) if rng.random() < (.05 if sloppy else .012) else abs(rng.gauss(8 if sloppy else 2.5, 3))
    nb = [exp + rng.gauss(0, 3.2) for _ in range(8 if (dist.w >= 1.0 or rng.random() < .3) else 3)]
    res = quality_evaluate(MeasurementFacts(raw, lmax, lmin, acc, 15.0, eff, sat, moved, ts, m["calibrated"], corr), nb, C.th,
                           now=ts + timedelta(seconds=25))
    status, score, reasons, evaluator, override = res.status, res.score, list(res.reasons), "auto", None
    evaluated_at = ts + timedelta(seconds=25)
    applied_corr = corr
    if rng.random() < .005:                                             # décision d'un administrateur
        status = rng.choice([s for s in (QualityStatus.validated, QualityStatus.indicative, QualityStatus.rejected) if s != status])
        reasons.append({"code": "admin_override", "severity": "info", "reason": "Vérification manuelle (démo)"})
        evaluator, override, evaluated_at = "admin", C.admin_id, ts + timedelta(hours=rng.randint(1, 30))
    mid = uuid.uuid4()
    pid = None
    if u.hybrid and rng.random() < .4:
        pid = make_proposal(C, u, ts, h9, cid)
    review = None
    if evaluator == "auto" and status in (QualityStatus.validated, QualityStatus.indicative) and rng.random() < .07:
        pool = [a for a in C.analysts_by_region.get(u.d.region, []) if a.id != u.id and a.joined < ts - timedelta(days=20)]
        if len(pool) >= 3:
            review = (pool, spike or status == QualityStatus.indicative)
    if review:
        status, score, reasons, evaluator, applied_corr, evaluated_at = gen_review(C, u, mid, ts, raw + corr, review, status, score, reasons, applied_corr)
    anon = ts < C.now - timedelta(days=settings.LOCATION_RETENTION_DAYS)
    clat, clon = h3.cell_to_latlng(h9) if anon else (lat, lon)
    tags = [NoiseContext.construction, NoiseContext.gathering, NoiseContext.music] if spike else \
        [NoiseContext.traffic] * (6 if _u(h9) > .45 else 3) + [NoiseContext.commerce] * 2 + [NoiseContext.music, NoiseContext.construction, NoiseContext.gathering, NoiseContext.other, None, None, None, None]
    C.meas.append({
        "id": mid, "tenant_id": C.tid, "user_id": u.id, "device_id": u.install, "device_model_id": m["id"], "proposal_id": pid,
        "cell_id": cid, "client_measurement_id": uuid.uuid4(), "contributor_hash": token(C, u.id, ts), "measured_at": ts,
        "local_date": local.date(), "day_part": dp, "latitude": clat, "longitude": clon,
        "geom": WKTElement(f"POINT({clon} {clat})", srid=4326), "h3_index": h9, "gps_accuracy": acc, "location_anonymized": anon,
        "duration_seconds": 15.0, "effective_duration_seconds": eff, "sample_rate_hz": 44100, "laeq": raw, "lmax": lmax,
        "lmin": lmin, "saturation_ratio": sat, "moved_distance_m": moved, "noise_context": rng.choice(tags),
        "correction_db_applied": applied_corr, "calibration_status": CalibrationStatus.calibrated if m["calibrated"] else CalibrationStatus.uncalibrated,
        "quality_status": status, "audio_retained": False, "aggregated_at": None, "created_at": ts + timedelta(seconds=3)})
    C.qual.append({"id": uuid.uuid4(), "tenant_id": C.tid, "measurement_id": mid, "status": status, "score": score,
                   "sub_scores": res.sub_scores, "reasons": reasons, "evaluator": evaluator, "override_by": override,
                   "evaluated_at": evaluated_at})
    u.ums.append((ts, lat, lon, status, z7(lat, lon)))
    if status == QualityStatus.validated:
        u.validated += 1
    qd = C.qdays.setdefault((u.id, local.date()), {"n": 0, "used": 0.0})
    qd["n"] += 1
    qd["used"] += rng.uniform(.6, 1.6)


def make_proposal(C: Ctx, u: U, ts: datetime, h9: str, cid, status=ProposalStatus.completed):
    rng = C.rng
    created = ts - timedelta(minutes=rng.randint(3, 20))
    feats = {k: round(rng.random(), 3) for k in collection_service.FEATURES}
    w = collection_service.base_weights()
    prio = collection_service.score_of(feats, w)
    pid = uuid.uuid4()
    done = status == ProposalStatus.completed
    C.props.append({
        "id": pid, "tenant_id": C.tid, "user_id": u.id, "cell_id": cid, "h3_index": h9,
        "trigger": rng.choice([TriggerMode.scheduled, TriggerMode.contextual]), "status": status, "priority_score": prio,
        "decision_score": prio * (.5 + .5 * rng.uniform(.3, .9)), "is_exploration": rng.random() < .05, "features": feats,
        "weights_used": w, "confidence_before": round(rng.uniform(0, .8), 3), "expires_at": created + timedelta(minutes=30),
        "responded_at": created + timedelta(minutes=rng.randint(1, 6)) if status != ProposalStatus.expired else created + timedelta(minutes=30),
        "completed_at": ts + timedelta(seconds=20) if done else None,
        "learned_at": ts + timedelta(hours=1) if done and ts < C.now - timedelta(days=3) else None, "created_at": created})
    st = C.rstats[(u.id, to_local(created).hour)]
    st[0 if status in (ProposalStatus.completed, ProposalStatus.accepted) else 1] += 1
    return pid if done else None


def gen_review(C, u, mid, ts, level, review, status, score, reasons, corr):
    rng = C.rng
    pool, suspicious = review
    requested = rng.randint(3, min(7, len(pool)))
    voters = rng.sample(pool, requested)
    n_voted = requested - (rng.randint(0, requested - 3) if rng.random() < .15 else 0)
    p_agree = .35 if suspicious else .78
    rid = uuid.uuid4()
    votes = []
    for a in voters[:n_voted]:
        r = rng.random()
        v = "agree" if r < p_agree else "down" if r < p_agree + (1 - p_agree) * (.7 if suspicious else .5) else "up"
        votes.append((a, v, v != "agree" and rng.random() < .3, rng.uniform(.9, 1.8)))
    res = reviews_service.decide([(v, s, w) for _, v, s, w in votes])
    out = res["outcome"]
    closed = ts + timedelta(minutes=rng.randint(2, 9))
    C.reviews.append({"id": rid, "tenant_id": C.tid, "measurement_id": mid, "status": "closed", "outcome": out,
                      "adjust_db": res["adjust"], "requested": requested, "votes": n_voted,
                      "agree_ratio": round(res["agree"], 3), "down_ratio": round(res["down"], 3), "up_ratio": round(res["up"], 3),
                      "expires_at": ts + timedelta(minutes=10), "closed_at": closed, "created_at": ts + timedelta(seconds=30)})
    for a, v, s, w in votes:
        matched = (v == "agree") if out == "confirmed" else (v != "agree") if out == "rejected" else (v == out.split("_")[1])
        C.votes.append({"id": uuid.uuid4(), "tenant_id": C.tid, "review_id": rid, "reviewer_id": a.id, "status": "voted", "vote": v,
                        "strong": s, "level_db": round(level, 1), "measured_at": ts, "distance_m": round(rng.uniform(40, 480), 1),
                        "weight": w, "matched": matched, "expires_at": ts + timedelta(minutes=10),
                        "voted_at": ts + timedelta(minutes=rng.randint(1, 8)), "created_at": ts + timedelta(seconds=30)})
    for a in voters[n_voted:]:
        C.votes.append({"id": uuid.uuid4(), "tenant_id": C.tid, "review_id": rid, "reviewer_id": a.id, "status": "expired", "vote": None,
                        "strong": False, "level_db": round(level, 1), "measured_at": ts, "distance_m": round(rng.uniform(40, 480), 1),
                        "weight": 1.0, "matched": None, "expires_at": ts + timedelta(minutes=10), "voted_at": None,
                        "created_at": ts + timedelta(seconds=30)})
    reason = {"code": "peer_confirmed" if out == "confirmed" else f"peer_{out}", "severity": "info", "votes": n_voted,
              "agree": round(res["agree"], 3), "adjust_db": res["adjust"]}
    if out == "rejected":
        reason["severity"] = "reject"
        return QualityStatus.rejected, 0.0, [*reasons, reason], "peer", corr, closed
    if res["adjust"]:
        corr = corr + res["adjust"]
    return QualityStatus.validated, max(score, settings.VALIDATED_MIN_SCORE), [*reasons, reason], "peer", corr, closed


# ============================================================================ missions
def gen_missions(C: Ctx, u: U) -> None:
    rng = C.rng
    if u.role != Role.contributor:
        return
    ums = sorted(u.ums)
    usable = [x for x in ums if x[3] != QualityStatus.rejected]
    quota = {"heavy": rng.randint(6, 12), "regular": rng.randint(2, 6), "casual": rng.randint(0, 2) if rng.random() < .4 else 0, "dormant": 0}[u.arche]
    if u.analyst:
        quota = max(quota, 3)
    seen: set = set()
    forced_done = 3 if u.analyst else 0

    def add(status, z, created, accepted, deadline, finished, req, done, conf, reward_need):
        lat, lon = h3.cell_to_latlng(z)
        hl = quota_service.haversine_m(u.home[0], u.home[1], lat, lon) / 1000
        C.missions.append({
            "id": uuid.uuid4(), "tenant_id": C.tid, "user_id": u.id, "kind": "zone", "status": status, "zone_h3": z,
            "center_lat": lat, "center_lon": lon, "radius_m": 1500.0, "distance_km": round(hl, 1), "required_count": req,
            "done_count": done, "confirmed_count": conf,
            "reward_credits": round(req * settings.MISSION_REWARD_PER_MEASUREMENT * (.75 + .5 * reward_need), 1),
            "priority": round(100 * min(1, .6 * reward_need + .4 * rng.random()), 1),
            "features": {"zone_deficit": rng.randint(8, 60), "zone_need": round(reward_need, 3), "score": round(rng.uniform(.4, .9), 3), "fit": round(rng.random(), 3)},
            "offer_expires_at": created + timedelta(hours=settings.MISSION_OFFER_HOURS), "accepted_at": accepted,
            "deadline_at": deadline, "finished_at": finished, "created_at": created})

    for _ in range(quota * 3):
        if quota <= 0 or not usable:
            break
        a = rng.choice(usable)
        if (a[4], a[0].date()) in seen:
            continue
        group = [x for x in usable if x[4] == a[4] and timedelta(0) <= x[0] - a[0] < timedelta(hours=60)
                 and quota_service.haversine_m(x[1], x[2], *h3.cell_to_latlng(a[4])) <= 1500]
        need = rng.uniform(.3, .9)
        accepted = a[0] - timedelta(minutes=rng.randint(20, 180))
        created = accepted - timedelta(hours=rng.randint(1, 30))
        deadline = accepted + timedelta(hours=settings.MISSION_COMPLETION_HOURS)
        if len(group) >= 2 and (forced_done > 0 or rng.random() < .65):
            req = rng.randint(2, min(6, len(group)))
            add(MissionStatus.completed, a[4], created, accepted, deadline, group[req - 1][0] + timedelta(minutes=3), req, req, req, need)
            forced_done -= 1
        elif deadline < C.now - timedelta(hours=2):
            req = rng.randint(3, 6)
            got = min(len(group), req - 1)
            add(MissionStatus.failed, a[4], created, accepted, deadline, deadline, req, got, got, need)
        else:
            continue
        seen.add((a[4], a[0].date()))
        quota -= 1
    if u.arche in ("heavy", "regular", "casual"):
        for _ in range(rng.randint(0, 3)):                                # refusées / expirées
            t = u.joined + (C.now - u.joined) * rng.random()
            if t > C.now - timedelta(hours=50):
                continue
            z = z7(*gauss_pt(rng, u.home[0], u.home[1], 3000))
            st = rng.choice([MissionStatus.declined, MissionStatus.expired])
            add(st, z, t, None, None, t + timedelta(hours=rng.randint(1, 47)) if st == MissionStatus.declined else t + timedelta(hours=settings.MISSION_OFFER_HOURS), rng.randint(2, 6), 0, 0, rng.uniform(.3, .9))
        if u.arche != "casual" and rng.random() < .35:                    # missions en cours aujourd'hui
            z = z7(*gauss_pt(rng, u.home[0], u.home[1], 2500))
            if rng.random() < .5:
                add(MissionStatus.offered, z, C.now - timedelta(hours=rng.randint(1, 20)), None, None, None, rng.randint(2, 6), 0, 0, rng.uniform(.4, .9))
            else:
                acc = C.now - timedelta(hours=rng.randint(1, 30))
                add(MissionStatus.accepted, z, acc - timedelta(hours=2), acc, acc + timedelta(hours=settings.MISSION_COMPLETION_HOURS), None, rng.randint(2, 6), 0, 0, rng.uniform(.4, .9))


# ============================================================================ enquêtes (définitions)
EVID = {"dwelling_type": "Photo de la façade du logement", "water_access": "Photo du point d'eau"}
SURVEYS = [
    dict(domain="demographie", title="Composition et structure du ménage", unit="household", cooldown=45, pop=.9, launch=0, slots=[
        ("composition", "Composition du ménage", "Les totaux doivent se tenir (femmes + hommes, tranches d'âge).",
         ["household_persons", "persons_female", "persons_male", "persons_0_14", "persons_15_64", "persons_65p", "births_12m", "deaths_12m"], {"household_persons": "taille_menage"}),
        ("quartier", "Votre quartier", None, ["neighborhood_density", "locality_type"], {"locality_type": "milieu"})]),
    dict(domain="menages", title="Conditions de vie du ménage", unit="household", cooldown=30, pop=.8, launch=2, slots=[
        ("structure", "Type de ménage", None, ["household_type", "dependents", "households_in_compound"], {"household_type": "type_menage"}),
        ("niveau_de_vie", "Niveau de vie", None, ["living_conditions", "meals_per_day", "food_insecurity"], {})]),
    dict(domain="habitat", title="Logement, eau et énergie", unit="household", cooldown=30, pop=1.0, launch=3, slots=[
        ("logement", "Votre logement", None, ["dwelling_type", "wall_material", "roof_material", "rooms_count"], {"dwelling_type": "logement"}),
        ("services", "Eau, électricité, assainissement", None, ["water_access", "electricity_access", "sanitation", "tenure"], {"water_access": "eau"}),
        ("risques", "Risques", None, ["flood_risk"], {})]),
    dict(domain="emploi", title="Emploi et activité", unit="household", cooldown=30, pop=.85, launch=5, slots=[
        ("activite", "Votre activité", None, ["employment_status", "occupation_group", "job_formality"], {"employment_status": "activite", "occupation_group": "secteur"}),
        ("foyer", "Emploi dans le ménage", None, ["household_persons", "persons_employed", "persons_unemployed", "job_search_months", "underemployed"], {})]),
    dict(domain="entreprises", title="Petites entreprises locales", unit="enterprise", cooldown=60, pop=.9, launch=7, slots=[
        ("profil", "Votre entreprise", None, ["firm_activity", "firm_size", "firm_employees", "firm_age_years"], {"firm_activity": "activite", "firm_size": "taille"}),
        ("performance", "Activité et perspectives", None, ["firm_turnover_band", "firm_registered", "firm_outlook"], {})]),
    dict(domain="agriculture", title="Exploitations agricoles", unit="farm", cooldown=60, pop=.9, launch=9, slots=[
        ("terres", "Vos terres", None, ["farm_area_ha", "main_crop", "irrigated", "farm_inputs_access"], {"main_crop": "culture"}),
        ("elevage", "Élevage et récolte", None, ["cattle_count", "small_ruminants_count", "poultry_count", "harvest_kg"], {})]),
    dict(domain="education", title="École et alphabétisation", unit="household", cooldown=45, pop=.75, launch=12, slots=[
        ("scolarisation", "Scolarisation et lecture", None, ["children_6_16", "children_enrolled", "adults_15p", "adults_literate"], {}),
        ("ecole", "École la plus proche", None, ["education_level_max", "school_distance_km", "school_type", "school_quality"], {"education_level_max": "niveau"})]),
    dict(domain="sante", title="Accès aux soins", unit="household", cooldown=45, pop=.8, launch=14, slots=[
        ("acces", "Structure de santé proche", None, ["health_facility_type", "facility_distance_km", "time_to_care_min", "facility_quality", "facility_stockout"], {"health_facility_type": "structure"}),
        ("couverture", "Couverture et coûts", None, ["health_coverage", "care_cost_barrier", "children_vaccinated"], {"health_coverage": "couverture"})]),
    dict(domain="economie", title="Revenus, dépenses et prix", unit="household", cooldown=30, pop=.95, launch=17, slots=[
        ("budget", "Budget du ménage", None, ["household_income_band", "household_spending_band", "household_debt", "purchasing_power_trend"], {"household_income_band": "revenu"}),
        ("prix", "Prix constatés", None, ["price_rice_kg", "price_oil_l", "price_transport_ride"], {})]),
    dict(domain="migration", title="Mobilité et migration", unit="household", cooldown=60, pop=.7, launch=20, slots=[
        ("parcours", "Votre parcours", None, ["moved_last_5y", "origin_region", "move_reason"], {"move_reason": "motif"}),
        ("international", "Famille à l'étranger", None, ["members_abroad", "remittances_received", "intends_emigrate"], {})]),
    dict(domain="genre", title="Genre et autonomie économique", unit="household", cooldown=45, pop=.7, launch=23, slots=[
        ("foyer", "Femmes du ménage", None, ["women_15_64", "women_employed", "female_headed"], {"female_headed": "chef_menage"}),
        ("autonomie", "Autonomie économique", None, ["woman_income_activity", "woman_entrepreneur", "woman_land_access", "woman_decides_spending"], {})]),
    dict(domain="numerique", title="Internet et équipements", unit="household", cooldown=30, pop=.95, launch=26, slots=[
        ("acces", "Accès et équipements", None, ["internet_access", "smartphones_count", "computers_count", "mobile_money_use"], {"internet_access": "connexion"}),
        ("usages", "Usages", None, ["internet_hours_day", "digital_uses", "network_quality"], {})]),
]


def make_def(spec: dict) -> dict:
    slots, qs, dims = [], [], set()
    for key, label, help_, inds, axes in spec["slots"]:
        slots.append({"key": key, "label": label, "help": help_, "multi": False, "allow_none": True, "mode": "profile"})
        for k in inds:
            ind = catalog.INDICATORS[k]
            kind = {"number": "number", "share": "yes_no", "scale": "scale"}.get(ind.kind) or ("multi_choice" if ind.multi else "single_choice")
            q = {"key": k, "slot": key, "text": ind.label, "kind": kind, "indicator": k, "required": True}
            if k in axes:
                q["axis"] = axes[k]
                dims.add(axes[k])
            if k in EVID:
                q["evidence"], q["evidence_hint"] = "optional", EVID[k]
            qs.append(q)
    return {"unit": spec["unit"], "slots": slots, "questions": qs, "rules": [], "dimensions": sorted(dims)}


def tiny_png(rng) -> bytes:
    w = h = 16
    raw = b"".join(b"\x00" + bytes(rng.randrange(256) for _ in range(w * 3)) for _ in range(h))

    def ch(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")


# ============================================================================ persistance : cœur
async def flush_cells(db, C: Ctx):
    sql = text("INSERT INTO geographic_cells (id, tenant_id, h3_index, resolution, center) VALUES "
               "(:id, :tid, :h3, :res, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) ON CONFLICT (tenant_id, h3_index) DO NOTHING")
    for c in chunks(C.new_cells, 2000):
        await db.execute(sql, c)
    C.new_cells.clear()
    await db.commit()


async def ensure_tenant(factory):
    async with factory() as db:
        t = (await db.execute(select(Tenant).where(Tenant.slug == settings.DEFAULT_TENANT_SLUG))).scalar_one_or_none()
        if t is None:
            t = await tenant_service.create_tenant(db, TenantCreate(
                slug=settings.DEFAULT_TENANT_SLUG, name="SunuSound Sénégal (démo)", allow_registration=True,
                admin_email=f"admin@{DEMO_DOMAIN}", admin_password="Admin#Sunu2026!"))
            log(f"Tenant créé : {t.slug} (admin admin@{DEMO_DOMAIN} / Admin#Sunu2026!)")
        tid, tset = t.id, dict(t.settings or {})
    async with tdb(factory, tid) as db:
        admin = (await db.execute(select(User).where(User.tenant_id == tid, User.role == Role.tenant_admin).limit(1))).scalar_one_or_none()
        if admin is None:
            admin = User(id=uuid.uuid4(), tenant_id=tid, email_hash=hash_email(tid, f"admin@{DEMO_DOMAIN}"),
                         password_hash=hash_password("Admin#Sunu2026!"), role=Role.tenant_admin, pseudonym="Contrib-admindemo")
            db.add(admin)
            await db.commit()
        return tid, admin.id, tset


async def ensure_models(factory, tid, admin_id) -> list[dict]:
    out = []
    async with tdb(factory, tid) as db:
        for man, mod, av, share, cal, corr in MODELS:
            dm = (await db.execute(select(DeviceModel).where(
                DeviceModel.tenant_id == tid, DeviceModel.manufacturer == man, DeviceModel.model == mod,
                DeviceModel.android_version == av, DeviceModel.microphone == "internal"))).scalar_one_or_none()
            if dm is None:
                dm = DeviceModel(tenant_id=tid, manufacturer=man, model=mod, android_version=av, microphone="internal",
                                 calibration_status=CalibrationStatus.calibrated if cal else CalibrationStatus.uncalibrated,
                                 correction_db=corr if cal else None)
                db.add(dm)
                await db.flush()
                if cal:
                    db.add(DeviceCalibration(tenant_id=tid, device_model_id=dm.id, correction_db=corr, calibrated_by=admin_id, is_active=True,
                                             reference_instrument="Sonomètre de référence classe 2 (démo)",
                                             conditions="Calibration de démonstration en chambre silencieuse, bruit rose 94 dB."))
            is_cal = dm.calibration_status == CalibrationStatus.calibrated and dm.correction_db is not None
            out.append({"id": dm.id, "share": share, "calibrated": is_cal, "corr": float(dm.correction_db or 0.0)})
        await db.commit()
    return out


async def persist_core(factory, C: Ctx):
    rng = C.rng
    now = C.now
    async with tdb(factory, C.tid) as db:
        # --- utilisateurs
        urows, prows, crows, lrows, drows, srows = [], [], [], [], [], []
        invs: dict[int, dict] = {}
        for u in C.users:
            urows.append({"id": u.id, "tenant_id": C.tid, "email_hash": hash_email(C.tid, u.email), "password_hash": C.pw_hash,
                          "role": u.role, "pseudonym": make_pseudonym(C.tid, u.id), "is_active": True,
                          "participation_paused": u.paused, "hybrid_collection_enabled": u.hybrid,
                          "last_login_at": u.last_active - timedelta(minutes=rng.randint(1, 3000)), "created_at": u.joined})
        await bulk(db, User, urows)
        by_i = {u.i: u for u in C.users}
        for u in C.users:
            if u.invited_by is not None and u.invited_by in by_i and u.invited_by not in invs:
                code = new_code(10)
                invs[u.invited_by] = {"id": uuid.uuid4(), "tenant_id": C.tid, "code": code, "created_by": by_i[u.invited_by].id, "kind": "user",
                                      "max_uses": 5, "uses": 0, "expires_at": now + timedelta(days=10), "active": True, "note": None,
                                      "created_at": by_i[u.invited_by].joined + timedelta(days=1)}
            if u.invited_by in invs:
                invs[u.invited_by]["uses"] = min(5, invs[u.invited_by]["uses"] + 1)
        await bulk(db, Invitation, list(invs.values()))
        for u in C.users:
            inv = invs.get(u.invited_by) if u.invited_by is not None else None
            xp_done = u.validated >= 3 and inv is not None
            prows.append({"user_id": u.id, "tenant_id": C.tid, "display_name": u.handle, "display_key": display_key(u.handle),
                          "first_name": u.first, "last_name": u.last, "phone": u.phone, "city": u.d.city, "district": u.d.name,
                          "birth_year": u.birth, "invited_by": by_i[u.invited_by].id if inv else None, "invitation_id": inv["id"] if inv else None,
                          "invite_credited_at": u.joined + timedelta(days=rng.randint(4, 9)) if xp_done and u.joined + timedelta(days=9) < now else None,
                          "terms_accepted_at": u.joined, "show_in_leaderboard": rng.random() > .08, "analyst_enabled": u.analyst,
                          "analyst_since": u.joined + timedelta(days=30) if u.analyst else None, "xp": 0, "created_at": u.joined})
            for ct, ok in ((ConsentType.geolocation, True), (ConsentType.microphone, True),
                           (ConsentType.survey_data, u.aff > 0), (ConsentType.photo, u.photo and u.aff > 0)):
                crows.append({"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": u.id, "consent_type": ct, "granted": ok,
                              "policy_version": settings.CONSENT_POLICY_VERSION, "created_at": u.joined + timedelta(minutes=2)})
            for k, when in enumerate((u.joined, u.last_active - timedelta(minutes=rng.randint(1, 3000)))):
                lrows.append({"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": u.id, "success": True, "user_agent": "okhttp/4.12.0",
                              "platform": "Application", "ip_hash": None, "created_at": when})
            if u.role == Role.contributor:
                drows.append({"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": u.id, "device_model_id": u.model["id"],
                              "device_hash": hash_device(C.tid, u.install), "sample_rate_hz": 44100, "last_seen_at": u.last_active, "created_at": u.joined})
                u.install = drows[-1]["id"]
                if u.hybrid:
                    days = sorted(rng.sample(range(7), rng.randint(3, 7)))
                    srows.append({"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": u.id, "days_of_week": days, "window_start": dtime(7, 0),
                                  "window_end": dtime(21, 0), "preferred_times": [dtime(h, 0) for h in rng.sample([8, 12, 13, 17, 18, 20], 2)],
                                  "mode": rng.choice([ScheduleMode.both, ScheduleMode.both, ScheduleMode.scheduled, ScheduleMode.contextual]),
                                  "timezone": settings.TIMEZONE, "active": True, "created_at": u.joined})
        await bulk(db, UserProfile, prows)
        await bulk(db, UserConsent, crows)
        await bulk(db, LoginEvent, lrows)
        await bulk(db, Device, drows)
        await bulk(db, CollectionSchedule, srows)
        await db.commit()
    log(f"  {len(C.users)} comptes, {len(drows)} appareils, {len(invs)} codes d'invitation")
    for m in C.meas:                                                 # remplace install_id par l'id réel du Device
        pass
    dev = {u.id: u.install for u in C.users}
    for m in C.meas:
        m["device_id"] = dev[m["user_id"]]
    async with tdb(factory, C.tid) as db:
        await flush_cells(db, C)
        log(f"  cellules H3 : {len(C.cellmap)}")
        await bulk(db, CollectionProposal, C.props)
        await bulk(db, Measurement, C.meas, 1500)
        await db.commit()
        log(f"  {len(C.meas)} mesures, {len(C.props)} propositions")
        await bulk(db, MeasurementQuality, C.qual, 3000)
        await bulk(db, MeasurementReview, C.reviews)
        await bulk(db, ReviewVote, C.votes)
        rs = [{"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": uid, "hour": h, "accepted": a, "declined": d} for (uid, h), (a, d) in C.rstats.items()]
        await bulk(db, UserResponseStat, rs)
        await bulk(db, Mission, C.missions)
        await db.commit()
        log(f"  {len(C.reviews)} avis analyseurs ({len(C.votes)} votes), {len(C.missions)} missions")
        # quotas : lignes initiales anciennes pour que rebalance_tenant les calcule avec les vraies formules
        qrows, qd = [], []
        for u in C.users:
            if u.role == Role.contributor:
                qrows.append({"user_id": u.id, "tenant_id": C.tid, "balance": round(rng.uniform(.5, 6), 2), "daily_target": 0.0,
                              "updated_at": now - timedelta(minutes=45)})
        for (uid, d), v in C.qdays.items():
            qd.append({"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": uid, "local_date": d, "credited": round(v["used"] * rng.uniform(1.0, 1.25) + .5, 2),
                       "used": round(v["used"], 2), "decayed": round(rng.uniform(0, .6), 2), "measurements": v["n"], "target": round(rng.uniform(4, 12), 2)})
        await bulk(db, UserQuota, qrows)
        await bulk(db, QuotaDay, qd)
        await db.commit()


async def run_aggregation(factory, tid):
    total = 0
    while True:
        async with tdb(factory, tid) as db:
            n = await aggregation.aggregate_dirty(db, tid, limit=20000)
            await db.commit()
        if n == 0:
            break
        total += n
    async with tdb(factory, tid) as db:
        await db.execute(text(
            "UPDATE geographic_cells g SET last_measurement_at = s.m FROM (SELECT cell_id, max(measured_at) m FROM measurements "
            "WHERE tenant_id = :t AND quality_status <> 'rejected' GROUP BY cell_id) s WHERE g.id = s.cell_id"), {"t": tid})
        await db.commit()
    log(f"  agrégation terminée ({total} mesures agrégées)")


# ============================================================================ enquêtes : génération et persistance
class R:   # contexte des enquêtes
    pass


def peer_stats(R_, u: U, keys):
    gk = u.d.name if R_.dcount[u.d.name] >= 15 else u.d.city
    out = {}
    for k in keys:
        st = R_.peer_cache.get((gk, k))
        if st is None:
            ind = catalog.INDICATORS[k]
            vals = [m[k] for m in R_.members[gk]][:400]
            if ind.kind in ("number", "scale"):
                st = sq.PeerStat("num", len(vals), values=[float(x) for x in vals])
            else:
                cnt: dict = defaultdict(int)
                for v in vals:
                    for c in (v if isinstance(v, list) else [v]):
                        cnt[str(c)] += 1
                st = sq.PeerStat("cat", len(vals), counts=dict(cnt))
            R_.peer_cache[(gk, k)] = st
        if st.n:
            out[k] = st
    return out


def build_response(R_, sv, u: U, ts: datetime):
    rng = R_.rng
    defn = sv["defn"]
    frac = max(0.0, min(1.0, (ts - R_.start) / (R_.now - R_.start)))
    Lt = drift(u.latent, rng, frac)
    target = {}
    for q in defn.questions:
        try:
            target[q.key] = coerce_answer(q, Lt[q.indicator])
        except Exception:
            return None
    picks, answers, overrides, none_slots, direct_keys, offered, picked_slots = {}, {}, [], [], [], {}, {}

    def go_direct(sl, qs):
        none_slots.append(sl.key)
        for q in qs:
            answers[q.key] = target[q.key]
            direct_keys.append(q.key)

    for sl in defn.slots:
        qs = questions_of(defn, sl.key)
        profs = R_.pools.get((sv["id"], sl.key), [])
        if not profs:
            go_direct(sl, qs)
            continue
        shown = profs if len(profs) <= 12 else rng.sample(profs, 12)
        offered[sl.key] = [p["id"] for p in shown]
        vt = sprof.encode(qs, target)
        best = min(shown, key=lambda p: sprof.dist(sprof.encode(qs, p["answers"]), vt))
        diffs = [q.key for q in qs if best["answers"].get(q.key) != target[q.key]]
        r = rng.random()
        if u.trait == "noisy":
            best, diffs, lazy = rng.choice(shown), [], True
        elif u.trait == "hasty":
            lazy = True
        elif not diffs:
            lazy = False
        elif len(diffs) <= max(2, len(qs) // 3):
            lazy = r < .25
        else:
            lazy = False
            if r < .75:
                go_direct(sl, qs)
                continue
        picks[sl.key] = [best["id"]]
        picked_slots[sl.key] = [best["id"]]
        for q in qs:
            answers[q.key] = best["answers"][q.key] if (lazy or q.key not in diffs) else target[q.key]
            if not lazy and q.key in diffs:
                overrides.append(q.key)
    if any(h["severity"] == "reject" for h in check_rules(all_rules(defn), answers)):     # repli : réponses directes
        picks, picked_slots, overrides, none_slots, direct_keys = {}, {}, [], [], []
        for sl in defn.slots:
            go_direct(sl, questions_of(defn, sl.key))
    hits = [h for h in check_rules(all_rules(defn), answers) if h["severity"] == "warn"]
    custom = {k: answers[k] for k in overrides + direct_keys}
    mode = "direct" if not picks else ("mixed" if custom else "profile")
    expected = sq.sv.SQ_BASE_SECONDS + sq.sv.SQ_SECONDS_PER_SLOT * len(picks) + sq.sv.SQ_SECONDS_PER_OVERRIDE * len(overrides) + sq.sv.SQ_SECONDS_PER_DIRECT * len(direct_keys)
    ratio = rng.uniform(.08, .3) if (u.trait == "hasty" and rng.random() < .6) else rng.lognormvariate(math.log(1.5), .35)
    duration = expected * ratio
    lat, lon = gauss_pt(rng, u.home[0], u.home[1], 60)
    h9, cid = cell_of(R_.C, lat, lon)
    acc = rng.uniform(100, 400) if rng.random() < .02 else rng.lognormvariate(math.log(14), .5)
    qm = {q.key: q for q in defn.questions}
    scale_vals = [v for k, v in answers.items() if qm[k].kind == "scale"]
    yes_vals = [v for k, v in answers.items() if qm[k].kind == "yes_no"]
    # preuve photo facultative
    evid = []
    if u.photo and rng.random() < .55:
        for q in defn.questions:
            if q.evidence == "optional" and q.key in answers:
                evid.append(q.key)
    prior = {"careful": .88, "average": .72, "hasty": .45, "noisy": .35}[u.trait] + rng.uniform(-.06, .06)
    hist = R_.hist[(u.id)]
    keys = list(dict.fromkeys(qm[k].indicator for k in answers))
    facts = sq.Facts(
        values={qm[k].indicator: v for k, v in answers.items()}, kinds={qm[k].indicator: qm[k].kind for k in answers},
        duration_seconds=duration, expected_seconds=expected, gps_accuracy_m=acc, speed_kmh=None,
        fingerprint_dups=rng.randint(3, 5) if (u.trait == "hasty" and len(custom) >= 3 and rng.random() < .15) else 0,
        scale_values=scale_vals, yes_values=yes_vals, own_median_duration=statistics.median(hist) if hist else None, own_n=len(hist),
        trust=prior if len(hist) >= 3 else None, trust_n=len(hist), evidence_required=[],
        evidence={k: [sq.EvFacts(k, has_exif=True)] for k in evid}, q_by_ind={qm[k].indicator: k for k in answers},
        peers=peer_stats(R_, u, keys), refs={}, rule_warns=hits)
    res = sq.evaluate(facts)
    status, score, reasons, evaluator, override = res.status, res.score, list(res.reasons), "auto", None
    evaluated = ts + timedelta(seconds=rng.randint(8, 60))
    if rng.random() < .006:
        status = rng.choice([s for s in (QualityStatus.validated, QualityStatus.indicative, QualityStatus.rejected) if s != status])
        reasons.append({"code": "admin_override", "severity": "info", "reason": "Vérification manuelle (démo)"})
        evaluator, override, evaluated = "admin", R_.admin_id, ts + timedelta(hours=rng.randint(1, 40))
    if evaluator == "admin":
        weight = {QualityStatus.validated: 1.0, QualityStatus.indicative: .5}.get(status, 0.0)
    else:
        weight = round(score * (.5 + .5 * prior), 4) if status != QualityStatus.rejected else 0.0
    hist.append(duration)
    rid = uuid.uuid4()
    ft = {"n_slots_picked": len(picks), "n_overrides": len(overrides), "n_direct": len(direct_keys), "n_questions": len(answers),
          "extreme_share": round(sum(v in (1, 5) for v in scale_vals) / len(scale_vals), 3) if scale_vals else None,
          "yes_share": round(sum(yes_vals) / len(yes_vals), 3) if yes_vals else None, "scale_n": len(scale_vals)}
    resp = {"id": rid, "tenant_id": R_.tid, "survey_id": sv["id"], "survey_version": 1, "user_id": u.id,
            "contributor_hash": token(R_.C, u.id, ts), "client_response_id": uuid.uuid4(), "unit_ref_hash": None,
            "started_at": ts - timedelta(seconds=duration), "submitted_at": ts, "local_date": to_local(ts).date(), "h3_index": h9,
            "gps_accuracy": acc, "duration_seconds": duration, "mode": mode,
            "profile_picks": {k: v for k, v in picks.items()}, "answers": answers, "overrides": overrides, "none_slots": none_slots,
            "custom_fingerprint": hashlib.sha1(json.dumps(custom, sort_keys=True, default=str).encode()).hexdigest()[:40] if len(custom) >= 3 else None,
            "flags": hits, "features": ft, "needs_evidence": False, "evidence_finalized_at": ts + timedelta(minutes=2) if evid else None,
            "quality_status": status, "weight": weight, "evaluated_at": evaluated, "created_at": ts + timedelta(seconds=2)}
    qual = {"id": uuid.uuid4(), "tenant_id": R_.tid, "response_id": rid, "status": status, "score": score, "sub_scores": res.sub_scores,
            "reasons": reasons, "evaluator": evaluator, "override_by": override, "evaluated_at": evaluated}
    proved = bool(evid) and status != QualityStatus.rejected
    arows = [{"tenant_id": R_.tid, "response_id": rid, "survey_id": sv["id"], "domain": sv["domain"], "h3_index": h9,
              "local_date": resp["local_date"], "answered_at": ts, "contributor_hash": resp["contributor_hash"], "unit_ref_hash": None,
              "status": status, "weight": weight, "proved": proved, **row} for row in answer_rows(defn, answers)]
    evrows = []
    for k in evid:
        data = tiny_png(rng)
        clat, clon = h3.cell_to_latlng(h9)
        evrows.append({"id": uuid.uuid4(), "tenant_id": R_.tid, "response_id": rid, "question_key": k, "sha256": hashlib.sha256(data).hexdigest(),
                       "mime": "image/png", "size_bytes": len(data), "content": data,
                       "meta": {"taken_at": (ts - timedelta(seconds=90)).astimezone(TZ).isoformat(), "lat": clat, "lon": clon, "width": 16, "height": 16},
                       "status": EvidenceStatus.accepted if status != QualityStatus.rejected else EvidenceStatus.rejected,
                       "created_at": ts + timedelta(minutes=1)})
    counters = {"shown": [i for ids in offered.values() for i in ids], "picked": [i for ids in picked_slots.values() for i in ids],
                "over": [picked_slots[s][0] for s in picked_slots if any(qm[k].slot == s for k in overrides)]}
    return {"resp": resp, "h9": h9, "qual": qual, "arows": arows, "evid": evrows, "c": counters}


async def run_surveys(factory, C: Ctx, a):
    log("Enquêtes : création des 12 thèmes…")
    rng = random.Random(a.seed + 7)
    R_ = R()
    R_.rng, R_.tid, R_.admin_id, R_.start, R_.now, R_.C = rng, C.tid, C.admin_id, C.start, C.now, C
    R_.pools, R_.hist, R_.peer_cache = {}, defaultdict(list), {}
    contribs = [u for u in C.users if u.role == Role.contributor and u.aff > 0]
    R_.members, R_.dcount = defaultdict(list), defaultdict(int)
    for u in contribs:
        R_.members[u.d.name].append(u.latent)
        R_.members[u.d.city].append(u.latent)
        R_.dcount[u.d.name] += 1
    svs = []
    async with tdb(factory, C.tid) as db:
        admin = await db.get(User, C.admin_id)
        for spec in SURVEYS:
            launch = C.start + timedelta(days=min(spec["launch"], max(0, C.days - 35)), hours=8)
            payload = SurveyCreate(domain=SurveyDomain(spec["domain"]), title=spec["title"], description=f"Enquête de démonstration : {spec['title'].lower()}.{MARK}",
                                   definition=make_def(spec), cooldown_days=spec["cooldown"], target_responses=1500)
            s = await adm.create_survey(db, admin, payload)
            try:
                await adm.set_status(db, s, "active", use_ai=False)
            except HTTPException as e:
                log(f"  ! {spec['domain']} : {e.detail} (activation forcée)")
                s.status = SurveyStatus.active
            await db.flush()
            defn = await adm.definition_of(db, s)
            for tbl in ("surveys", "survey_versions", "survey_profiles"):
                col = "id" if tbl == "surveys" else "survey_id"
                await db.execute(text(f"UPDATE {tbl} SET created_at = :t WHERE {col} = :i"), {"t": launch, "i": s.id})
            svs.append({"id": s.id, "domain": spec["domain"], "spec": spec, "defn": defn, "launch": launch})
        await db.commit()

    async def load_pools():
        async with tdb(factory, C.tid) as db:
            rows = (await db.execute(select(SurveyProfile.id, SurveyProfile.survey_id, SurveyProfile.slot, SurveyProfile.answers).where(
                SurveyProfile.survey_id.in_([x["id"] for x in svs]), SurveyProfile.status == "active"))).all()
        R_.pools = defaultdict(list)
        for pid, sid, slot, ans in rows:
            R_.pools[(sid, slot)].append({"id": str(pid), "answers": ans})
    await load_pools()
    # plans de réponse
    plans = []
    for sv in svs:
        sp = sv["spec"]
        for u in contribs:
            if sp["domain"] == "entreprises" and not u.has_firm or sp["domain"] == "agriculture" and not u.has_farm:
                continue
            if rng.random() > min(1.0, u.aff * 1.6 * sp["pop"]):
                continue
            t = max(u.joined, sv["launch"]) + timedelta(days=rng.expovariate(1 / 8), hours=rng.random() * 6)
            while t < C.now - timedelta(hours=2):
                tt = t.replace(hour=rng.choice(u.hours), minute=rng.randrange(60), second=rng.randrange(60), microsecond=0)
                if tt < C.now - timedelta(minutes=10):
                    plans.append((tt, sv, u))
                if rng.random() > .5:
                    break
                t += timedelta(days=sp["cooldown"] * rng.uniform(1.0, 1.8))
    plans.sort(key=lambda p: p[0])
    log(f"  {len(plans)} réponses planifiées")
    bounds = [C.start + (C.now - C.start) * f for f in (1 / 3, 2 / 3, 1.0000001)]
    total = 0
    for ph, end in enumerate(bounds):
        batch = [p for p in plans if (bounds[ph - 1] if ph else C.start) <= p[0] < end]
        bundles = [b for ts, sv, u in batch if (b := build_response(R_, sv, u, ts))]
        async with tdb(factory, C.tid) as db:
            await flush_cells(db, C)
            for b in bundles:
                b["resp"]["cell_id"] = C.cellmap[b["h9"]]
                for r in b["arows"]:
                    r["cell_id"] = C.cellmap[b["h9"]]
            await bulk(db, SurveyResponse, [b["resp"] for b in bundles], 1500)
            await bulk(db, SurveyResponseQuality, [b["qual"] for b in bundles], 3000)
            await bulk(db, SurveyAnswer, [r for b in bundles for r in b["arows"]], 3000)
            await bulk(db, SurveyEvidence, [e for b in bundles for e in b["evid"]], 500)
            cnt: dict = defaultdict(lambda: [0, 0, 0])
            for b in bundles:
                for i in b["c"]["shown"]: cnt[i][0] += 1
                for i in b["c"]["picked"]: cnt[i][1] += 1
                for i in b["c"]["over"]: cnt[i][2] += 1
            upd = text("UPDATE survey_profiles SET shown_count = shown_count + :s, picked_count = picked_count + :p, "
                       "override_count = override_count + :o WHERE id = :id")
            await db.execute(upd, [{"id": uuid.UUID(i), "s": v[0], "p": v[1], "o": v[2]} for i, v in cnt.items()]) if cnt else None
            await db.commit()
        total += len(bundles)
        log(f"  phase {ph + 1}/3 : {len(bundles)} réponses insérées")
        if ph < 2:                                                    # évolution réelle des profils (données observées)
            async with tdb(factory, C.tid) as db:
                for sv in svs:
                    s = await db.get(Survey, sv["id"])
                    n = (await db.execute(select(func.count()).select_from(SurveyResponse).where(SurveyResponse.survey_id == s.id))).scalar_one()
                    if n < 60:
                        continue
                    res = await sprof.evolve(db, s, sv["defn"], use_ai=False, rng=rng)
                    g = res["generation"]
                    await db.execute(update(SurveyProfile).where(SurveyProfile.survey_id == s.id, SurveyProfile.generation == g).values(created_at=end))
                    await db.execute(update(ProfileEvent).where(ProfileEvent.survey_id == s.id, ProfileEvent.generation == g).values(created_at=end))
                    s.last_evolved_at = end
                await db.commit()
            await load_pools()
    # références fournies par l'organisation (illustratives)
    refs = [("household_persons", "mean", None, 8.4), ("rooms_count", "mean", None, 3.6), ("school_distance_km", "mean", None, 1.9),
            ("persons_0_14", "mean", None, 3.3), ("electricity_access", "share", "reseau", .78), ("water_access", "share", "robinet_interieur", .38),
            ("mobile_money_use", "share", None, .7), ("internet_access", "share", "mobile", .55), ("female_headed", "share", None, .28)]
    async with tdb(factory, C.tid) as db:
        await bulk(db, ReferenceValue, [{"id": uuid.uuid4(), "tenant_id": C.tid, "indicator_key": k, "stat": st, "category": c, "value": v,
                                          "scope_h3": None, "scope_res": None, "period_year": 2023,
                                          "source": f"Valeur illustrative de démonstration{MARK}", "created_by": C.admin_id,
                                          "created_at": C.start} for k, st, c, v in refs])
        await db.commit()
    # confiance des répondants (vraie fonction)
    redis = new_redis()
    try:
        async with tdb(factory, C.tid) as db:
            ids = (await db.execute(select(SurveyResponse.user_id).where(SurveyResponse.tenant_id == C.tid, SurveyResponse.user_id.is_not(None)).distinct())).scalars().all()
            for k, uid in enumerate(ids):
                await survey_trust.refresh_user(db, redis, C.tid, uid)
                if k % 200 == 199:
                    await db.commit()
            await db.commit()
    finally:
        await redis.aclose()
    log(f"  {total} réponses d'enquête, {len(ids)} profils de confiance calculés")


# ============================================================================ finalisation
async def leaderboard_history(factory, C: Ctx):
    async with tdb(factory, C.tid) as db:
        vis = dict((await db.execute(select(UserProfile.user_id, UserProfile.show_in_leaderboard).where(UserProfile.tenant_id == C.tid))).all())
        days = list(range(1, 11)) + list(range(12, C.days - 3, 3))
        today = to_local(C.now).date()
        for k in days:
            dd = to_local(C.now - timedelta(days=k)).date()
            end = datetime.combine(dd + timedelta(days=1), dtime.min, TZ).astimezone(UTC)
            for kind in lb.PERIODS:
                start, _, _ = lb.bounds(kind, end - timedelta(seconds=1))
                comps = await lb.points_for_window(db, C.tid, max(start, C.start), end)
                ranked = sorted(((u, d) for u, d in comps.items() if d["score"] > 0), key=lambda t: (-t[1]["score"], -t[1]["v"], str(t[0])))
                rows, rank = [], 0
                for uid, d in ranked:
                    if vis.get(uid, True):
                        rank += 1
                        rows.append({"id": uuid.uuid4(), "tenant_id": C.tid, "user_id": uid, "period_kind": kind, "snap_date": dd, "rank": rank, "score": d["score"]})
                for c in chunks(rows, 1000):
                    await db.execute(pg_insert(LeaderboardSnapshot).values(c).on_conflict_do_nothing(constraint="uq_lb_snapshot"))
            await db.commit()
        # XP = points bruts de la saison (jusqu'à hier)
        yest = today - timedelta(days=1)
        end = datetime.combine(today, dtime.min, TZ).astimezone(UTC)
        comps = await lb.points_for_window(db, C.tid, C.start, end)
        for c in chunks([{"i": u, "x": int(round(max(0.0, d["raw"])))} for u, d in comps.items()], 1000):
            await db.execute(text("UPDATE user_profiles SET xp = :x, xp_through = :y WHERE user_id = :i"), [{**r, "y": yest} for r in c])
        await db.commit()


async def finalize(factory, C: Ctx, tset: dict):
    redis = new_redis()
    try:
        log("Quotas (formules réelles)…")
        async with tdb(factory, C.tid) as db:
            n = await quota_service.rebalance_tenant(db, redis, C.tid)
            await db.commit()
        log(f"  {n} soldes calculés")
        log("Poids adaptatifs de l'algorithme de propositions…")
        async with tdb(factory, C.tid) as db:
            if await db.get(TenantAlgorithmState, C.tid) is None:
                db.add(TenantAlgorithmState(tenant_id=C.tid, weights=collection_service.base_weights(), samples_used=0, updated_at=C.start))
                await db.commit()
            for _ in range(3):
                if await collection_service.adapt_tenant_weights(db, C.tid) is None:
                    break
                await db.commit()
        log("Classement, niveaux, badges…")
        await leaderboard_history(factory, C)
        async with tdb(factory, C.tid) as db:
            n = await lb.refresh_tenant(db, C.tid)
            admin = await db.get(User, C.admin_id)
            await audit(db, admin, "demo.seed", "tenant", C.tid, {"users": len([u for u in C.users if u.role == Role.contributor]), "days": C.days})
            await db.commit()
            ids = [u.id for u in C.users if u.analyst]
            el = await reviews_service.eligibility_bulk(db, C.tid, ids, C.now)
            log(f"  {n} lignes de classement ; analyseurs réellement éligibles : {sum(v['eligible'] for v in el.values())}/{len(ids)}")
    finally:
        await redis.aclose()


async def purge(factory, tid):
    log("Purge des données de démonstration…")
    emails = [demo_email(i) for i in range(40000)] + STAFF
    hashes = {hash_email(tid, e) for e in emails}
    async with tdb(factory, tid) as db:
        ids = [i for i, h in (await db.execute(select(User.id, User.email_hash).where(User.tenant_id == tid))).all() if h in hashes]
    async with tdb(factory, tid) as db:
        for c in chunks(ids, 1000):
            await db.execute(delete(Measurement).where(Measurement.user_id.in_(c)))
            await db.execute(delete(SurveyResponse).where(SurveyResponse.user_id.in_(c)))
        await db.execute(delete(Survey).where(Survey.tenant_id == tid, Survey.description.like(f"%{MARK}")))
        await db.execute(delete(ReferenceValue).where(ReferenceValue.tenant_id == tid, ReferenceValue.source.like(f"%{MARK}")))
        for c in chunks(ids, 1000):
            await db.execute(delete(Invitation).where(Invitation.created_by.in_(c)))
            await db.execute(delete(User).where(User.id.in_(c)))
        await db.execute(delete(NoiseAggregation).where(NoiseAggregation.tenant_id == tid))
        await db.execute(update(Measurement).where(Measurement.tenant_id == tid).values(aggregated_at=None))
        await db.commit()
    await run_aggregation(factory, tid)
    log(f"  {len(ids)} comptes de démonstration supprimés")


async def report(factory, tid):
    tabs = ["users", "measurements", "measurement_quality", "noise_aggregations", "geographic_cells", "collection_proposals",
            "measurement_reviews", "review_votes", "missions", "surveys", "survey_profiles", "survey_responses", "survey_answers",
            "survey_evidence", "respondent_trust", "leaderboard_entries", "user_badges"]
    async with tdb(factory, tid) as db:
        log("Résumé :")
        for t in tabs:
            n = (await db.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()
            print(f"    {t:24s} {n}")
        for st, n in (await db.execute(text("SELECT quality_status, count(*) FROM measurements GROUP BY 1 ORDER BY 2 DESC"))).all():
            print(f"    mesures {st:12s} {n}")


# ============================================================================ main
async def run(a):
    t0 = time.time()
    engine, factory = task_session_factory()
    try:
        tid, admin_id, tset = await ensure_tenant(factory)
        if a.reset or a.purge:
            await purge(factory, tid)
            if a.purge:
                return
        async with tdb(factory, tid) as db:
            exists = (await db.execute(select(User.id).where(User.tenant_id == tid, User.email_hash == hash_email(tid, demo_email(0))))).first()
        if exists:
            log("Des données démo existent déjà. Relancez avec --reset pour les recréer.")
            return
        rng = random.Random(a.seed)
        days = min(a.days, settings.IDENTITY_UNLINK_DAYS - 1)
        now = datetime.now(UTC).replace(microsecond=0)
        log("Préparation (modèles de téléphones, hachage du mot de passe)…")
        models = await ensure_models(factory, tid, admin_id)
        C = Ctx(tid=tid, admin_id=admin_id, now=now, start=now - timedelta(days=days), days=days, th=build_thresholds(tset),
                pw_hash=hash_password(DEMO_PASSWORD), models=models, rng=rng)
        async with tdb(factory, tid) as db:
            C.cellmap = dict((await db.execute(select(GeographicCellRef.h3_index, GeographicCellRef.id))).all())
        log(f"Génération de {a.users} utilisateurs sur {days} jours…")
        C.users = build_users(C, a.users)
        for k, email in enumerate(STAFF):                                    # comptes équipe (analystes du serveur)
            d = DISTRICTS[0]
            base = C.users[0]
            C.users.append(U(i=a.users + k, id=uuid.uuid4(), email=email, handle=f"analyste_demo{k + 1}", first="Analyste", last="Démo",
                             phone="+221770000000", birth=1985, d=d, home=(d.lat, d.lon), work=(d.lat, d.lon), joined=C.start, last_active=now,
                             arche="dormant", trait="careful", model=base.model, install=str(uuid.uuid4()), hybrid=False, analyst=False,
                             paused=False, hours=[9, 14], n_meas=0, aff=0.0, has_firm=False, has_farm=False, photo=False, latent={},
                             role=Role.analyst))
        t1 = time.time()
        for k, u in enumerate(C.users):
            gen_user_meas(C, u)
            if k % 500 == 499:
                log(f"  {k + 1} utilisateurs simulés, {len(C.meas)} mesures")
        # propositions refusées / expirées
        for u in C.users:
            if u.hybrid and u.ums:
                for _ in range(int(len(u.ums) * .5)):
                    ts = rng.choice(u.ums)[0] + timedelta(hours=rng.randint(-60, 60))
                    if C.start < ts < now - timedelta(hours=1):
                        h9, cid = cell_of(C, *gauss_pt(rng, u.home[0], u.home[1], 300))
                        make_proposal(C, u, ts, h9, cid, rng.choice([ProposalStatus.declined, ProposalStatus.expired]))
        for u in C.users:
            gen_missions(C, u)
        log(f"  {len(C.meas)} mesures générées en {time.time() - t1:.0f}s")
        await persist_core(factory, C)
        log("Agrégation du bruit (aggregate_dirty)…")
        await run_aggregation(factory, tid)
        if not a.skip_surveys:
            await run_surveys(factory, C, a)
        await finalize(factory, C, tset)
        await report(factory, tid)
        log(f"Terminé en {time.time() - t0:.0f}s. Connexion : {demo_email(0)} / {DEMO_PASSWORD} "
            f"(analyste staff : {STAFF[0]} / {DEMO_PASSWORD})")
    finally:
        await engine.dispose()


class GeographicCellRef:                      # import tardif pour éviter un doublon en tête de fichier
    from app.models.geo import GeographicCell as _G
    h3_index = _G.h3_index
    id = _G.id


def main():
    ap = argparse.ArgumentParser(description="Seed de démonstration SunuSound")
    ap.add_argument("--users", type=int, default=2200)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", action="store_true", help="purge les données démo puis recrée")
    ap.add_argument("--purge", action="store_true", help="purge les données démo et s'arrête")
    ap.add_argument("--skip-surveys", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()