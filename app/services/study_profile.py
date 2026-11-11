"""Profil études : questionnaire par rubriques, sauvegarde progressive (reprise à tout moment)."""
import math
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.social import UserProfile
from app.models.study import UserStudyProfile
from app.models.tenancy import User

SKIP = "__skip__"
XP_PER_MODULE = 20
LEGACY_STR = ("gender", "occupation_group", "education_level", "income_band")


def _q(key, text, kind, options=(), *, label=None, help=None, lo=None, hi=None, unit=None,
       sensitive=False, max_select=None):
    return {"key": key, "text": text, "kind": kind, "options": list(options), "label": label or text,
            "help": help, "min": lo, "max": hi, "unit": unit, "sensitive": sensitive, "max_select": max_select}


OCC = [("agriculture_peche", "Agriculture / pêche"), ("commerce", "Commerce"), ("artisanat", "Artisanat"),
       ("services", "Services"), ("administration", "Administration"), ("industrie_btp", "Industrie / BTP"),
       ("transport", "Transport"), ("etudiant", "Étudiant(e)"), ("chomeur", "En recherche d'emploi"),
       ("retraite", "Retraité(e)"), ("autre", "Autre")]
EDU = [("aucun", "Aucun"), ("coranique", "Coranique / alphabétisation"), ("primaire", "Primaire"),
       ("college", "Collège"), ("lycee", "Lycée"), ("superieur", "Supérieur")]
INC = [("lt50k", "Moins de 50 000 FCFA"), ("k50_100", "50 000 à 100 000"), ("k100_200", "100 000 à 200 000"),
       ("k200_500", "200 000 à 500 000"), ("gt500k", "Plus de 500 000")]
LANGS = [("wolof", "Wolof"), ("francais", "Français"), ("pulaar", "Pulaar"), ("serere", "Sérère"),
         ("mandinka", "Mandingue"), ("diola", "Diola"), ("soninke", "Soninké"), ("anglais", "Anglais"),
         ("arabe", "Arabe")]
INTERESTS = [("mode", "Mode"), ("alimentation", "Alimentation"), ("telecom", "Télécom"),
             ("banque_mobile", "Banque mobile"), ("sante", "Santé"), ("education", "Éducation"),
             ("agriculture", "Agriculture"), ("immobilier", "Immobilier"), ("transport", "Transport"),
             ("energie", "Énergie"), ("sport", "Sport"), ("musique", "Musique"), ("tech", "Tech"),
             ("beaute", "Beauté"), ("voyage", "Voyage"), ("automobile", "Automobile"),
             ("commerce_en_ligne", "Commerce en ligne")]
NET = [("aucun", "Aucun"), ("mobile", "Données mobiles"), ("fixe", "Wi-Fi / fibre"), ("mobile_fixe", "Mobile et Wi-Fi")]

MODULES = [
    {"key": "vous", "title": "Vous", "subtitle": "Qui êtes-vous au quotidien ?", "icon": "person",
     "color": "#2563EB", "minutes": 1, "questions": [
        _q("gender", "Vous êtes…", "single",
           [("female", "Une femme"), ("male", "Un homme"), ("other", "Autre")], label="Genre", sensitive=True),
        _q("marital_status", "Votre situation familiale", "single",
           [("celibataire", "Célibataire"), ("couple", "En couple"), ("marie", "Marié(e)"),
            ("separe", "Séparé(e) / divorcé(e)"), ("veuf", "Veuf / veuve")], label="Situation familiale"),
        _q("children_count", "Combien d'enfants avez-vous à charge ?", "single",
           [("0", "Aucun"), ("1", "1"), ("2", "2"), ("3", "3"), ("4p", "4 ou plus")], label="Enfants à charge"),
        _q("household_size", "Combien de personnes vivent dans votre ménage ?", "number",
           lo=1, hi=60, unit="personnes", label="Taille du ménage"),
        _q("housing_status", "Votre logement", "single",
           [("proprietaire", "Propriétaire"), ("locataire", "Locataire"), ("famille", "Logé par la famille"),
            ("employeur", "Logé par l'employeur"), ("autre", "Autre")], label="Statut du logement"),
        _q("housing_type", "Quel type de logement ?", "single",
           [("maison", "Maison"), ("appartement", "Appartement"), ("concession", "Concession / chambre"),
            ("autre", "Autre")], label="Type de logement"),
    ]},
    {"key": "travail", "title": "Travail et revenus", "subtitle": "Votre activité et votre budget",
     "icon": "work", "color": "#7C3AED", "minutes": 1, "questions": [
        _q("occupation_group", "Dans quel secteur travaillez-vous ?", "single", OCC, label="Secteur d'activité"),
        _q("work_status", "Votre statut", "single",
           [("salarie", "Salarié(e)"), ("independant", "Indépendant(e)"), ("informel", "Petite activité informelle"),
            ("stagiaire", "Stagiaire / apprenti"), ("sans_emploi", "Sans activité")], label="Statut professionnel"),
        _q("income_band", "Revenu mensuel de votre ménage", "single", INC,
           label="Revenu mensuel du ménage", sensitive=True),
        _q("savings_habit", "Comment mettez-vous de l'argent de côté ?", "multi",
           [("tontine", "Tontine / groupement"), ("banque", "Banque"), ("mobile", "Mobile money"),
            ("domicile", "À la maison"), ("aucune", "Je n'épargne pas")], label="Épargne", max_select=3),
        _q("mobile_money", "Utilisez-vous Wave, Orange Money ou équivalent ?", "bool",
           label="Utilise le mobile money"),
    ]},
    {"key": "formation", "title": "Études et langues", "subtitle": "Votre parcours et vos langues",
     "icon": "school", "color": "#0891B2", "minutes": 1, "questions": [
        _q("education_level", "Votre plus haut niveau d'études", "single", EDU, label="Niveau d'études"),
        _q("study_field", "Votre domaine de formation", "single",
           [("sciences", "Sciences"), ("gestion", "Gestion / commerce"), ("lettres", "Lettres / droit"),
            ("sante", "Santé"), ("technique", "Technique / ingénierie"), ("agricole", "Agricole"),
            ("religieux", "Religieux"), ("autre", "Autre")], label="Domaine de formation"),
        _q("languages", "Quelles langues parlez-vous ?", "multi", LANGS, label="Langues parlées"),
        _q("training_interest", "Souhaitez-vous suivre une formation cette année ?", "bool",
           label="Intéressé par une formation"),
    ]},
    {"key": "numerique", "title": "Numérique", "subtitle": "Téléphone, internet et paiements",
     "icon": "smartphone", "color": "#4F46E5", "minutes": 2, "questions": [
        _q("phone_type", "Votre téléphone principal", "single",
           [("smartphone", "Smartphone"), ("basique", "Téléphone basique"), ("aucun", "Aucun")],
           label="Type de téléphone"),
        _q("phone_brand", "Quelle marque ?", "single",
           [("tecno", "Tecno"), ("infinix", "Infinix"), ("samsung", "Samsung"), ("iphone", "iPhone"),
            ("xiaomi", "Xiaomi"), ("itel", "itel"), ("autre", "Autre")], label="Marque du téléphone"),
        _q("internet_access", "Comment vous connectez-vous à Internet ?", "single", NET, label="Accès Internet"),
        _q("social_networks", "Quels réseaux utilisez-vous ?", "multi",
           [("whatsapp", "WhatsApp"), ("facebook", "Facebook"), ("tiktok", "TikTok"), ("instagram", "Instagram"),
            ("youtube", "YouTube"), ("x", "X (Twitter)"), ("snapchat", "Snapchat"), ("telegram", "Telegram"),
            ("aucun", "Aucun")], label="Réseaux sociaux"),
        _q("online_hours", "Temps passé en ligne chaque jour", "single",
           [("lt1", "Moins d'1 h"), ("1_3", "1 à 3 h"), ("3_6", "3 à 6 h"), ("gt6", "Plus de 6 h")],
           label="Temps en ligne"),
        _q("online_shopping", "Achetez-vous en ligne ?", "single",
           [("jamais", "Jamais"), ("parfois", "Parfois"), ("souvent", "Souvent")], label="Achats en ligne"),
        _q("payment_methods", "Comment payez-vous le plus souvent ?", "multi",
           [("especes", "Espèces"), ("wave", "Wave"), ("orange_money", "Orange Money"), ("free_money", "Free Money"),
            ("carte", "Carte bancaire")], label="Moyens de paiement"),
    ]},
    {"key": "consommation", "title": "Consommation", "subtitle": "Où et comment vous achetez",
     "icon": "shopping_bag", "color": "#DB2777", "minutes": 1, "questions": [
        _q("shopping_places", "Où faites-vous vos achats ?", "multi",
           [("marche", "Marché"), ("boutique", "Boutique de quartier"), ("supermarche", "Supermarché"),
            ("en_ligne", "En ligne"), ("ambulant", "Vendeurs ambulants")], label="Lieux d'achat"),
        _q("spending_priorities", "Vos 3 principaux postes de dépense", "multi",
           [("alimentation", "Alimentation"), ("transport", "Transport"), ("logement", "Logement"),
            ("sante", "Santé"), ("education", "Éducation"), ("telecom", "Téléphone / internet"),
            ("loisirs", "Loisirs"), ("vetements", "Vêtements")], label="Priorités de dépense", max_select=3),
        _q("buying_influence", "Qui décide des achats du foyer ?", "single",
           [("moi", "Moi seul(e)"), ("conjoint", "Avec mon conjoint"), ("famille", "La famille"),
            ("autre", "Quelqu'un d'autre")], label="Décision d'achat"),
        _q("brand_attitude", "Votre priorité à l'achat", "single",
           [("marques", "Les marques connues"), ("qualite_prix", "Le bon rapport qualité-prix"),
            ("moins_cher", "Le moins cher"), ("local", "Les produits locaux")], label="Attitude face aux marques"),
        _q("next_purchases", "Un achat prévu dans les 12 mois ?", "multi",
           [("telephone", "Téléphone"), ("electromenager", "Électroménager"), ("vehicule", "Véhicule"),
            ("logement", "Terrain / logement"), ("voyage", "Voyage"), ("formation", "Formation"),
            ("rien", "Rien de particulier")], label="Achats prévus"),
    ]},
    {"key": "mobilite", "title": "Déplacements", "subtitle": "Comment vous vous déplacez",
     "icon": "directions_bus", "color": "#F59E0B", "minutes": 1, "questions": [
        _q("transport_modes", "Comment vous déplacez-vous ?", "multi",
           [("pied", "À pied"), ("minibus", "Car rapide / Ndiaga"), ("bus", "Bus / BRT / TER"), ("taxi", "Taxi / VTC"),
            ("moto_taxi", "Moto-taxi"), ("voiture", "Voiture"), ("moto", "Moto"), ("velo", "Vélo")],
           label="Modes de déplacement"),
        _q("commute_time", "Durée d'un trajet quotidien", "single",
           [("lt15", "Moins de 15 min"), ("15_30", "15 à 30 min"), ("30_60", "30 à 60 min"), ("gt60", "Plus d'1 h")],
           label="Durée de trajet"),
        _q("vehicle_owned", "Possédez-vous un véhicule ?", "single",
           [("aucun", "Aucun"), ("moto", "Une moto"), ("voiture", "Une voiture"), ("les_deux", "Les deux")],
           label="Véhicule possédé"),
        _q("travel_frequency", "Voyagez-vous hors de votre ville ?", "single",
           [("jamais", "Jamais"), ("rarement", "Rarement"), ("mensuel", "Chaque mois"), ("hebdo", "Chaque semaine")],
           label="Fréquence de voyage"),
    ]},
    {"key": "quotidien", "title": "Habitudes et intérêts", "subtitle": "Votre quotidien et vos passions",
     "icon": "favorite", "color": "#16A34A", "minutes": 2, "questions": [
        _q("cooking_fuel", "Avec quoi cuisinez-vous ?", "single",
           [("gaz", "Gaz"), ("charbon", "Charbon"), ("bois", "Bois"), ("electricite", "Électricité")],
           label="Énergie de cuisson"),
        _q("news_sources", "Comment vous informez-vous ?", "multi",
           [("tv", "Télévision"), ("radio", "Radio"), ("reseaux", "Réseaux sociaux"), ("sites", "Sites d'actualité"),
            ("entourage", "Mon entourage")], label="Sources d'information"),
        _q("sport_frequency", "Pratiquez-vous un sport ?", "single",
           [("jamais", "Jamais"), ("parfois", "Parfois"), ("1_2", "1 à 2 fois par semaine"),
            ("3p", "3 fois ou plus")], label="Pratique sportive"),
        _q("music_genres", "Vos styles de musique", "multi",
           [("mbalax", "Mbalax"), ("hiphop", "Hip-hop"), ("afrobeats", "Afrobeats"), ("religieux", "Religieux"),
            ("rnb", "R&B"), ("classique", "Classique"), ("autre", "Autre")], label="Styles musicaux"),
        _q("interests", "Vos centres d'intérêt", "multi", INTERESTS, label="Centres d'intérêt", max_select=8),
    ]},
]

ALL = {q["key"]: q for m in MODULES for q in m["questions"]}


def attr_specs() -> dict:
    """Attributs de ciblage dérivés des questions (fusionnés dans study_schema.ATTRS)."""
    out = {}
    for q in ALL.values():
        kind = {"single": "cat", "multi": "multi", "number": "number", "bool": "bool"}[q["kind"]]
        spec = {"label": q["label"], "kind": kind, "sensitive": q["sensitive"]}
        if q["options"]:
            spec["options"] = [(v, l) for v, l in q["options"]]
        if q["unit"]:
            spec["unit"] = q["unit"]
        out[q["key"]] = spec
    return out


def profile_attrs(answers: dict | None) -> dict:
    return {k: v for k, v in (answers or {}).items() if k in ALL and v is not None and v != SKIP}


def public_modules() -> list[dict]:
    return [{"key": m["key"], "title": m["title"], "subtitle": m["subtitle"], "icon": m["icon"],
             "color": m["color"], "minutes": m["minutes"],
             "questions": [{"key": q["key"], "text": q["text"], "help": q["help"], "kind": q["kind"],
                            "options": [{"value": v, "label": l} for v, l in q["options"]],
                            "min": q["min"], "max": q["max"], "unit": q["unit"], "max_select": q["max_select"]}
                           for q in m["questions"]]} for m in MODULES]


def _merged(p: UserStudyProfile | None, answers: dict | None = None) -> dict:
    a = dict(answers if answers is not None else ((p.answers or {}) if p else {}))
    if p is not None:
        for k in LEGACY_STR:
            v = getattr(p, k)
            if v is not None and k not in a:
                a[k] = v
        if p.household_size is not None and "household_size" not in a:
            a["household_size"] = p.household_size
        if p.languages and "languages" not in a:
            a["languages"] = list(p.languages)
        if p.interests and "interests" not in a:
            a["interests"] = list(p.interests)
    return {k: v for k, v in a.items() if k in ALL}


def progress(answers: dict) -> dict:
    mods, total, done, nxt = {}, 0, 0, None
    for m in MODULES:
        qs = m["questions"]
        a = sum(1 for q in qs if q["key"] in answers)
        mods[m["key"]] = {"answered": a, "total": len(qs), "done": a == len(qs)}
        total += len(qs)
        done += a
        if nxt is None:
            for q in qs:
                if q["key"] not in answers:
                    nxt = {"module": m["key"], "key": q["key"]}
                    break
    return {"overall": round(done / total, 3) if total else 0.0, "answered": done, "total": total,
            "modules": mods, "next": nxt}


def _bad(key: str):
    return HTTPException(422, {"code": "invalid_value", "field": key})


def _clean(q: dict, v):
    if v is None:
        return None
    if v == SKIP:
        return SKIP
    k, vals = q["kind"], [o[0] for o in q["options"]]
    if k == "single":
        if not isinstance(v, str) or v not in vals:
            raise _bad(q["key"])
        return v
    if k == "multi":
        if (not isinstance(v, list) or not v or len(set(map(str, v))) != len(v)
                or any(x not in vals for x in v) or (q["max_select"] and len(v) > q["max_select"])):
            raise _bad(q["key"])
        return [x for x in vals if x in v]
    if k == "bool":
        if not isinstance(v, bool):
            raise _bad(q["key"])
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise _bad(q["key"])
    x = int(round(float(v)))
    if (q["min"] is not None and x < q["min"]) or (q["max"] is not None and x > q["max"]):
        raise _bad(q["key"])
    return x


def _sync_legacy(p: UserStudyProfile, k: str, v) -> None:
    empty = v is None or v == SKIP
    if k in LEGACY_STR:
        setattr(p, k, None if empty else v)
    elif k == "household_size":
        p.household_size = None if empty else int(v)
    elif k in ("languages", "interests"):
        setattr(p, k, [] if empty else list(v))


async def get_schema(db: AsyncSession, user: User) -> dict:
    p = await db.get(UserStudyProfile, user.id)
    a = _merged(p)
    return {"modules": public_modules(), "answers": a, "progress": progress(a)}


async def save_answers(db: AsyncSession, user: User, patch: dict) -> dict:
    if not patch or len(patch) > 40:
        raise HTTPException(422, "invalid_patch")
    p = await db.get(UserStudyProfile, user.id)
    if p is None:
        p = UserStudyProfile(user_id=user.id, tenant_id=user.tenant_id, languages=[], interests=[], answers={})
        db.add(p)
    answers = dict(p.answers or {})
    for k, raw in patch.items():
        q = ALL.get(k)
        if q is None:
            raise HTTPException(422, {"code": "unknown_question", "field": k})
        v = _clean(q, raw)
        if v is None:
            answers.pop(k, None)
        else:
            answers[k] = v
        _sync_legacy(p, k, v)
    after = progress(_merged(p, answers))
    awarded = list(answers.get("_xp", []))
    xp = 0
    for mk, st in after["modules"].items():
        if st["done"] and mk not in awarded:
            awarded.append(mk)
            xp += XP_PER_MODULE
    if xp:
        answers["_xp"] = awarded
        await db.execute(update(UserProfile).where(UserProfile.user_id == user.id)
                         .values(xp=UserProfile.xp + xp))
    p.answers = answers
    p.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"progress": after, "xp_awarded": xp}