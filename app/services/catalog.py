from dataclasses import dataclass


@dataclass(frozen=True)
class Cat:
    code: str
    label: str
    num: float | None = None


@dataclass(frozen=True)
class Indicator:
    key: str
    domain: str
    label: str
    kind: str                    # number | share | scale | category
    unit: str = ""
    lo: float = 0.0
    hi: float = 1.0
    cats: tuple = ()
    multi: bool = False
    int_: bool = False
    sensitive: bool = False
    polarity: int = 0            # +1 : plus haut = plus favorable ; -1 : défavorable ; 0 : neutre
    anchors: tuple = ()
    viz: str | None = None       # bars | zones | markers | heat
    unit_of: str = "household"


@dataclass(frozen=True)
class Derived:
    key: str
    domain: str
    label: str
    num: tuple
    den: tuple
    factor: float
    unit: str
    lo: float
    hi: float
    viz: str | None = None


@dataclass(frozen=True)
class Domain:
    key: str
    label: str
    icon: str
    color: str
    summary: str
    viz: str
    indicators: tuple


def _b(domain: str):
    def num(key, label, unit, lo, hi, anchors=(), **kw):
        return Indicator(key, domain, label, "number", unit, float(lo), float(hi), anchors=tuple(anchors), **kw)

    def share(key, label, **kw):
        return Indicator(key, domain, label, "share", "%", 0.0, 1.0, **kw)

    def scale(key, label, **kw):
        return Indicator(key, domain, label, "scale", "/5", 1.0, 5.0, **kw)

    def cat(key, label, cats, **kw):
        return Indicator(key, domain, label, "category", "", 0.0, 0.0, cats=tuple(Cat(*c) for c in cats), **kw)
    return num, share, scale, cat


REGIONS = [("dakar", "Dakar"), ("thies", "Thiès"), ("diourbel", "Diourbel"), ("saint_louis", "Saint-Louis"),
           ("louga", "Louga"), ("matam", "Matam"), ("kaolack", "Kaolack"), ("kaffrine", "Kaffrine"),
           ("fatick", "Fatick"), ("tambacounda", "Tambacounda"), ("kedougou", "Kédougou"), ("kolda", "Kolda"),
           ("sedhiou", "Sédhiou"), ("ziguinchor", "Ziguinchor"), ("etranger", "Étranger")]

_BANDS = [("lt50k", "Moins de 50 000 FCFA", 25000), ("k50_100", "50 000 à 100 000", 75000),
          ("k100_200", "100 000 à 200 000", 150000), ("k200_500", "200 000 à 500 000", 350000),
          ("gt500k", "Plus de 500 000", 750000)]


def _demographie():
    num, share, scale, cat = _b("demographie")
    return (
        num("household_persons", "Personnes vivant dans le ménage", "personnes", 1, 60, (1, 3, 5, 8, 12), int_=True),
        num("persons_female", "Femmes et filles du ménage", "personnes", 0, 40, (0, 1, 2, 3, 5), int_=True),
        num("persons_male", "Hommes et garçons du ménage", "personnes", 0, 40, (0, 1, 2, 3, 5), int_=True),
        num("persons_0_14", "Enfants de 0 à 14 ans", "personnes", 0, 30, (0, 1, 2, 4, 6), int_=True),
        num("persons_15_64", "Personnes de 15 à 64 ans", "personnes", 0, 40, (1, 2, 3, 5, 8), int_=True),
        num("persons_65p", "Personnes de 65 ans et plus", "personnes", 0, 15, (0, 1, 2), int_=True),
        num("births_12m", "Naissances dans le ménage (12 derniers mois)", "naissances", 0, 10, (0, 1, 2), int_=True),
        num("deaths_12m", "Décès dans le ménage (12 derniers mois)", "décès", 0, 10, (0, 1), int_=True, sensitive=True),
        scale("neighborhood_density", "Densité de peuplement du quartier (ressentie)", viz="bars"),
        cat("locality_type", "Type de localité", [("urbain_dense", "Ville dense"), ("urbain", "Urbain"),
                                                  ("periurbain", "Périurbain"), ("rural", "Rural")]),
    )


def _menages():
    num, share, scale, cat = _b("menages")
    return (
        cat("household_type", "Composition du ménage",
            [("isole", "Personne seule"), ("couple", "Couple sans enfant"), ("couple_enfants", "Couple avec enfants"),
             ("monoparental", "Un seul parent avec enfants"), ("elargi", "Famille élargie"),
             ("collectif", "Cohabitation / ménage collectif")]),
        num("dependents", "Personnes à charge", "personnes", 0, 40, (0, 2, 4, 6, 10), int_=True, polarity=-1),
        num("households_in_compound", "Ménages dans la concession", "ménages", 1, 20, (1, 2, 4, 8), int_=True),
        scale("living_conditions", "Conditions de vie (ressenti)", polarity=1),
        num("meals_per_day", "Repas pris par jour", "repas", 0, 6, (1, 2, 3), int_=True, polarity=1),
        share("food_insecurity", "Manque de nourriture dans les 12 derniers mois", polarity=-1, sensitive=True),
    )


def _habitat():
    num, share, scale, cat = _b("habitat")
    return (
        cat("dwelling_type", "Type de logement",
            [("maison_dure", "Maison en dur"), ("immeuble", "Appartement / immeuble"),
             ("concession", "Concession à plusieurs logements"), ("baraque", "Baraque / habitat précaire"),
             ("case", "Case traditionnelle"), ("autre", "Autre")]),
        cat("wall_material", "Matériau des murs",
            [("dur", "Béton / briques"), ("banco", "Banco"), ("bois_paille", "Bois / paille"),
             ("tole_zinc", "Tôle / zinc"), ("autre", "Autre")]),
        cat("roof_material", "Matériau du toit",
            [("dalle", "Dalle béton"), ("tole", "Tôle"), ("chaume", "Paille / chaume"), ("tuile", "Tuile"),
             ("autre", "Autre")]),
        num("rooms_count", "Nombre de pièces", "pièces", 1, 30, (1, 2, 3, 5, 8), int_=True),
        cat("water_access", "Accès à l'eau potable",
            [("robinet_interieur", "Robinet dans le logement"), ("robinet_cour", "Robinet dans la cour"),
             ("borne_fontaine", "Borne fontaine"), ("forage_protege", "Forage / puits protégé"),
             ("puits_non_protege", "Puits non protégé"), ("vendeur", "Vendeur / camion citerne"),
             ("surface", "Eau de surface")], polarity=1),
        cat("electricity_access", "Accès à l'électricité",
            [("reseau", "Réseau (Senelec)"), ("solaire", "Solaire"), ("groupe", "Groupe électrogène"),
             ("aucun", "Aucune")], polarity=1),
        cat("sanitation", "Assainissement",
            [("chasse_reseau", "Toilettes avec chasse (réseau)"), ("fosse_septique", "Fosse septique"),
             ("latrine_amelioree", "Latrine améliorée"), ("latrine_simple", "Latrine simple"),
             ("aucune", "Aucune")], polarity=1),
        cat("tenure", "Statut d'occupation",
            [("proprietaire", "Propriétaire"), ("locataire", "Locataire"), ("loge_gratuit", "Logé gratuitement"),
             ("autre", "Autre")]),
        share("flood_risk", "Logement inondé au moins une fois en 5 ans", polarity=-1),
    )


def _emploi():
    num, share, scale, cat = _b("emploi")
    return (
        cat("employment_status", "Situation d'activité",
            [("occupe", "Occupé(e)"), ("chomeur", "À la recherche d'un emploi"), ("inactif", "Inactif(ve)"),
             ("etudiant", "Étudiant(e) / élève"), ("retraite", "Retraité(e)")]),
        cat("occupation_group", "Secteur d'activité",
            [("agriculture_peche", "Agriculture / pêche"), ("commerce", "Commerce"), ("artisanat", "Artisanat"),
             ("services", "Services"), ("administration", "Administration"),
             ("industrie_btp", "Industrie / BTP"), ("transport", "Transport"), ("autre", "Autre")]),
        cat("job_formality", "Type d'emploi", [("formel", "Emploi formel (contrat)"), ("informel", "Emploi informel")]),
        num("persons_employed", "Personnes du ménage ayant un emploi", "personnes", 0, 30, (0, 1, 2, 3), int_=True),
        num("persons_unemployed", "Personnes du ménage sans emploi et en recherche", "personnes", 0, 30, (0, 1, 2),
            int_=True, polarity=-1),
        num("job_search_months", "Durée de recherche d'emploi", "mois", 0, 120, (1, 3, 6, 12, 24), int_=True,
            polarity=-1),
        share("underemployed", "Travaille moins qu'il ne le souhaiterait", polarity=-1),
    )


def _entreprises():
    num, share, scale, cat = _b("entreprises")
    u = dict(unit_of="enterprise", viz="markers")
    return (
        cat("firm_activity", "Activité de l'entreprise",
            [("commerce", "Commerce"), ("services", "Services"), ("transport", "Transport"),
             ("artisanat", "Artisanat"), ("restauration", "Restauration"), ("industrie", "Industrie"),
             ("btp", "BTP"), ("agro_transfo", "Transformation agroalimentaire"), ("autre", "Autre")], **u),
        cat("firm_size", "Taille de l'entreprise",
            [("individuelle", "Activité individuelle"), ("micro", "2 à 5 personnes"), ("petite", "6 à 20 personnes"),
             ("moyenne", "21 à 100 personnes"), ("grande", "Plus de 100 personnes")], **u),
        num("firm_employees", "Personnes employées", "personnes", 0, 5000, (1, 3, 10, 40, 150), int_=True, **u),
        num("firm_age_years", "Ancienneté de l'entreprise", "ans", 0, 100, (1, 3, 7, 15, 30), int_=True, **u),
        cat("firm_turnover_band", "Chiffre d'affaires annuel",
            [("lt500k", "Moins de 500 000 FCFA", 250000), ("m05_2", "500 000 à 2 millions", 1250000),
             ("m2_10", "2 à 10 millions", 6000000), ("m10_50", "10 à 50 millions", 30000000),
             ("gt50m", "Plus de 50 millions", 100000000)], sensitive=True, **u),
        share("firm_registered", "Entreprise déclarée (NINEA / RCCM)", polarity=1, **u),
        scale("firm_outlook", "Perspective d'activité pour l'année à venir", polarity=1, **u),
    )


def _agriculture():
    num, share, scale, cat = _b("agriculture")
    u = dict(unit_of="farm")
    return (
        num("farm_area_ha", "Superficie exploitée", "ha", 0, 10000, (0.5, 1, 3, 10, 50), **u),
        cat("main_crop", "Culture principale",
            [("mil_sorgho", "Mil / sorgho"), ("arachide", "Arachide"), ("riz", "Riz"), ("mais", "Maïs"),
             ("niebe", "Niébé"), ("manioc", "Manioc"), ("maraichage", "Maraîchage"),
             ("arboriculture", "Arboriculture"), ("autre", "Autre")], **u),
        num("cattle_count", "Bovins", "têtes", 0, 5000, (0, 3, 10, 30, 100), int_=True, **u),
        num("small_ruminants_count", "Ovins et caprins", "têtes", 0, 5000, (0, 5, 15, 40, 100), int_=True, **u),
        num("poultry_count", "Volailles", "têtes", 0, 100000, (0, 10, 30, 100, 500), int_=True, **u),
        num("harvest_kg", "Production de la dernière campagne", "kg", 0, 10_000_000,
            (200, 1000, 5000, 20000, 100000), **u),
        share("irrigated", "Exploitation irriguée", polarity=1, **u),
        scale("farm_inputs_access", "Accès aux intrants (semences, engrais)", polarity=1, **u),
    )


def _education():
    num, share, scale, cat = _b("education")
    return (
        num("children_6_16", "Enfants de 6 à 16 ans", "enfants", 0, 20, (0, 1, 2, 4), int_=True),
        num("children_enrolled", "Enfants de 6 à 16 ans scolarisés", "enfants", 0, 20, (0, 1, 2, 4), int_=True),
        num("adults_15p", "Adultes de 15 ans et plus", "personnes", 0, 40, (1, 2, 3, 5), int_=True),
        num("adults_literate", "Adultes sachant lire et écrire", "personnes", 0, 40, (0, 1, 2, 3), int_=True),
        cat("education_level_max", "Plus haut niveau d'études dans le ménage",
            [("aucun", "Aucun"), ("coranique", "Coranique / alphabétisation"), ("primaire", "Primaire"),
             ("college", "Collège"), ("lycee", "Lycée"), ("superieur", "Supérieur")], polarity=1),
        num("school_distance_km", "Distance à l'école la plus proche", "km", 0, 50, (0.3, 1, 3, 8), polarity=-1),
        cat("school_type", "Type d'école la plus proche",
            [("public", "Publique"), ("prive_laic", "Privée laïque"), ("prive_confessionnel", "Privée confessionnelle"),
             ("communautaire", "Communautaire"), ("daara", "Daara"), ("aucune", "Aucune à proximité")],
            unit_of="facility", viz="markers"),
        scale("school_quality", "Qualité de l'école la plus proche", polarity=1, unit_of="facility"),
    )


def _sante():
    num, share, scale, cat = _b("sante")
    return (
        cat("health_facility_type", "Structure de santé la plus proche",
            [("case_sante", "Case de santé"), ("poste_sante", "Poste de santé"), ("centre_sante", "Centre de santé"),
             ("hopital", "Hôpital"), ("clinique_privee", "Clinique privée"), ("pharmacie", "Pharmacie"),
             ("aucune", "Aucune à proximité")], unit_of="facility", viz="markers"),
        num("facility_distance_km", "Distance à la structure de santé", "km", 0, 100, (0.5, 2, 5, 15), polarity=-1),
        num("time_to_care_min", "Temps pour accéder aux soins", "min", 0, 600, (10, 30, 60, 120), int_=True,
            polarity=-1),
        share("care_cost_barrier", "A renoncé à des soins pour raison financière", polarity=-1, sensitive=True),
        cat("health_coverage", "Couverture sanitaire",
            [("aucune", "Aucune"), ("mutuelle", "Mutuelle de santé"), ("cmu", "Couverture maladie universelle"),
             ("privee", "Assurance privée"), ("employeur", "Prise en charge employeur"), ("autre", "Autre")],
            sensitive=True, polarity=1),
        share("children_vaccinated", "Enfants du ménage à jour de leurs vaccins", sensitive=True, polarity=1),
        scale("facility_quality", "Qualité de l'accueil et des soins", polarity=1, unit_of="facility"),
        share("facility_stockout", "Rupture de médicaments constatée", polarity=-1, unit_of="facility"),
    )


def _economie():
    num, share, scale, cat = _b("economie")
    return (
        cat("household_income_band", "Revenu mensuel du ménage", _BANDS, sensitive=True, polarity=1),
        cat("household_spending_band", "Dépenses mensuelles du ménage", _BANDS, sensitive=True),
        num("price_rice_kg", "Prix du riz observé", "FCFA/kg", 100, 2000, (400, 450, 500, 600, 700),
            unit_of="community", polarity=-1),
        num("price_oil_l", "Prix de l'huile observé", "FCFA/L", 300, 4000, (1000, 1300, 1600, 2000),
            unit_of="community", polarity=-1),
        num("price_transport_ride", "Prix d'un trajet urbain", "FCFA", 50, 10000, (150, 250, 500, 1000, 2500),
            unit_of="community", polarity=-1),
        cat("purchasing_power_trend", "Évolution du pouvoir d'achat sur un an",
            [("baisse", "En baisse", -1), ("stable", "Stable", 0), ("hausse", "En hausse", 1)], polarity=1),
        share("household_debt", "Ménage endetté", sensitive=True, polarity=-1),
    )


def _migration():
    num, share, scale, cat = _b("migration")
    return (
        share("moved_last_5y", "A changé de lieu de résidence dans les 5 dernières années"),
        cat("origin_region", "Région d'origine / de provenance", REGIONS),
        cat("move_reason", "Raison principale du déplacement",
            [("travail", "Travail"), ("etudes", "Études"), ("famille", "Raisons familiales"),
             ("insecurite", "Insécurité"), ("climat", "Climat / terres"), ("logement", "Logement"),
             ("autre", "Autre")]),
        num("members_abroad", "Membres du ménage vivant à l'étranger", "personnes", 0, 20, (0, 1, 2, 4), int_=True),
        share("remittances_received", "Reçoit des transferts d'argent de la migration", sensitive=True),
        share("intends_emigrate", "Un membre envisage d'émigrer", sensitive=True),
    )


def _genre():
    num, share, scale, cat = _b("genre")
    return (
        share("female_headed", "Ménage dirigé par une femme"),
        num("women_15_64", "Femmes de 15 à 64 ans dans le ménage", "personnes", 0, 30, (0, 1, 2, 4), int_=True),
        num("women_employed", "dont femmes ayant un emploi", "personnes", 0, 30, (0, 1, 2), int_=True, polarity=1),
        share("woman_income_activity", "Au moins une femme a une activité génératrice de revenus", polarity=1),
        share("woman_entrepreneur", "Une femme du ménage dirige une entreprise ou un commerce", polarity=1),
        share("woman_land_access", "Une femme du ménage possède ou contrôle un bien (terre, logement)", polarity=1),
        scale("woman_decides_spending", "Part des femmes dans les décisions de dépenses", sensitive=True,
              polarity=1),
    )


def _numerique():
    num, share, scale, cat = _b("numerique")
    return (
        cat("internet_access", "Accès à Internet",
            [("aucun", "Aucun"), ("mobile", "Données mobiles"), ("fixe", "Connexion fixe (wifi / fibre)"),
             ("mobile_fixe", "Mobile et fixe")], polarity=1),
        num("smartphones_count", "Smartphones dans le ménage", "appareils", 0, 30, (0, 1, 2, 4), int_=True),
        num("computers_count", "Ordinateurs dans le ménage", "appareils", 0, 20, (0, 1, 2), int_=True),
        share("mobile_money_use", "Utilise le mobile money", polarity=1),
        num("internet_hours_day", "Heures d'Internet par jour", "h", 0, 24, (0, 1, 3, 6)),
        cat("digital_uses", "Usages du numérique",
            [("reseaux_sociaux", "Réseaux sociaux"), ("education", "Éducation / formation"),
             ("administration", "Démarches administratives"), ("commerce", "Commerce / vente"),
             ("sante", "Santé"), ("banque_mobile", "Paiements et banque mobile"),
             ("divertissement", "Divertissement")], multi=True),
        scale("network_quality", "Qualité du réseau mobile dans le quartier", polarity=1, unit_of="community"),
    )


def _D(key, label, icon, color, summary, viz, inds):
    return Domain(key, label, icon, color, summary, viz, inds)


DOMAINS: dict[str, Domain] = {d.key: d for d in (
    _D("demographie", "Démographie", "groups", "#2563EB",
       "Population, âge, sexe, répartition géographique, natalité, mortalité.", "bars", _demographie()),
    _D("menages", "Ménages", "home", "#0E9F9A",
       "Taille, composition familiale, conditions de vie.", "zones", _menages()),
    _D("habitat", "Habitat", "apartment", "#F59E0B",
       "Logements, construction, eau, électricité, assainissement.", "zones", _habitat()),
    _D("emploi", "Emploi", "work", "#7C3AED",
       "Population active, chômage, métiers, secteurs d'activité.", "bars", _emploi()),
    _D("entreprises", "Entreprises", "storefront", "#DB2777",
       "Nombre, localisation, taille, activité, chiffre d'affaires.", "markers", _entreprises()),
    _D("agriculture", "Agriculture", "agriculture", "#16A34A",
       "Exploitations, superficies, cultures, cheptel, production.", "zones", _agriculture()),
    _D("education", "Éducation", "school", "#0891B2",
       "Scolarisation, alphabétisation, établissements, niveaux d'études.", "zones", _education()),
    _D("sante", "Santé", "local_hospital", "#DC2626",
       "Infrastructures sanitaires, couverture, accès aux soins.", "markers", _sante()),
    _D("economie", "Économie", "payments", "#CA8A04",
       "Revenus, consommation, prix, pouvoir d'achat.", "heat", _economie()),
    _D("migration", "Migration", "flight_takeoff", "#475569",
       "Mobilité intérieure, immigration, émigration.", "zones", _migration()),
    _D("genre", "Genre", "diversity_1", "#C026D3",
       "Situation économique des femmes, emploi féminin, entrepreneuriat.", "zones", _genre()),
    _D("numerique", "Numérique", "devices", "#4F46E5",
       "Accès à Internet, équipements, usages numériques.", "zones", _numerique()),
)}

INDICATORS: dict[str, Indicator] = {i.key: i for d in DOMAINS.values() for i in d.indicators}
BASE_INDICATORS = {"household_persons"}          # utilisable dans toute enquête (contexte du ménage)
MULTI_DOMAIN_OK = BASE_INDICATORS


def _dv(key, dom, label, num, den, factor, unit, lo, hi, viz=None):
    return Derived(key, dom, label, tuple(num), tuple(den), factor, unit, lo, hi, viz)


DERIVED: dict[str, Derived] = {d.key: d for d in (
    _dv("sex_ratio", "demographie", "Rapport de masculinité (hommes pour 100 femmes)", ["persons_male"],
        ["persons_female"], 100, "H/100 F", 0, 300, "bars"),
    _dv("female_share", "demographie", "Part des femmes", ["persons_female"], ["household_persons"], 100, "%", 0, 100),
    _dv("youth_share", "demographie", "Part des moins de 15 ans", ["persons_0_14"], ["household_persons"], 100, "%", 0, 100),
    _dv("elderly_share", "demographie", "Part des 65 ans et plus", ["persons_65p"], ["household_persons"], 100, "%", 0, 100),
    _dv("dependency_ratio", "demographie", "Rapport de dépendance", ["persons_0_14", "persons_65p"],
        ["persons_15_64"], 100, "%", 0, 400),
    _dv("birth_rate", "demographie", "Taux de natalité (pour 1000 personnes)", ["births_12m"], ["household_persons"],
        1000, "‰", 0, 100),
    _dv("death_rate", "demographie", "Taux de mortalité (pour 1000 personnes)", ["deaths_12m"], ["household_persons"],
        1000, "‰", 0, 100),
    _dv("unemployment_rate", "emploi", "Taux de chômage", ["persons_unemployed"],
        ["persons_employed", "persons_unemployed"], 100, "%", 0, 100, "bars"),
    _dv("school_enrolment_rate", "education", "Taux de scolarisation (6-16 ans)", ["children_enrolled"],
        ["children_6_16"], 100, "%", 0, 100),
    _dv("literacy_rate", "education", "Taux d'alphabétisation des adultes", ["adults_literate"], ["adults_15p"],
        100, "%", 0, 100),
    _dv("female_employment_rate", "genre", "Taux d'emploi des femmes", ["women_employed"], ["women_15_64"],
        100, "%", 0, 100),
)}

# Règles d'identité comptable : (parties, total, relation) ; appliquées si tous les indicateurs sont dans l'enquête.
PART_RULES = [
    (("persons_female", "persons_male"), "household_persons", "eq", "Femmes + hommes doivent égaler le nombre de personnes."),
    (("persons_0_14", "persons_15_64", "persons_65p"), "household_persons", "eq",
     "Les tranches d'âge doivent totaliser le nombre de personnes."),
    (("births_12m",), "household_persons", "lte", "Les naissances ne peuvent pas dépasser le nombre de personnes."),
    (("deaths_12m",), "household_persons", "lte", "Les décès ne peuvent pas dépasser le nombre de personnes."),
    (("persons_employed", "persons_unemployed"), "household_persons", "lte",
     "Actifs + chômeurs ne peuvent pas dépasser le nombre de personnes."),
    (("children_enrolled",), "children_6_16", "lte", "Les enfants scolarisés ne peuvent pas dépasser les enfants de 6 à 16 ans."),
    (("children_6_16",), "household_persons", "lte", "Les enfants de 6-16 ans ne peuvent pas dépasser le nombre de personnes."),
    (("adults_literate",), "adults_15p", "lte", "Les adultes alphabétisés ne peuvent pas dépasser les adultes."),
    (("adults_15p",), "household_persons", "lte", "Les adultes ne peuvent pas dépasser le nombre de personnes."),
    (("women_employed",), "women_15_64", "lte", "Les femmes en emploi ne peuvent pas dépasser les femmes de 15-64 ans."),
    (("women_15_64",), "persons_female", "lte", "Les femmes de 15-64 ans ne peuvent pas dépasser les femmes du ménage."),
    (("members_abroad",), "household_persons", "lte", "Les membres à l'étranger ne peuvent pas dépasser le nombre de personnes."),
    (("smartphones_count",), "household_persons", "lte", "Plus de smartphones que de personnes : vérifiez."),
]


def get(key: str) -> Indicator | None:
    return INDICATORS.get(key)


def allowed_for_domain(ind: Indicator, domain: str) -> bool:
    return ind.domain == domain or ind.key in BASE_INDICATORS


def public() -> dict:
    def ind(i: Indicator) -> dict:
        return {"key": i.key, "label": i.label, "kind": i.kind, "unit": i.unit, "lo": i.lo, "hi": i.hi,
                "multi": i.multi, "sensitive": i.sensitive, "polarity": i.polarity, "viz": i.viz,
                "unit_of": i.unit_of, "anchors": list(i.anchors),
                "categories": [{"code": c.code, "label": c.label} for c in i.cats]}
    return {"domains": [{"key": d.key, "label": d.label, "icon": d.icon, "color": d.color, "summary": d.summary,
                         "viz": d.viz, "indicators": [ind(i) for i in d.indicators],
                         "derived": [{"key": x.key, "label": x.label, "unit": x.unit, "lo": x.lo, "hi": x.hi,
                                      "viz": x.viz or d.viz} for x in DERIVED.values() if x.domain == d.key]}
                        for d in DOMAINS.values()]}