from datetime import datetime, timezone

import h3

from app.core.account_settings import account_settings as acs
from app.core.config import settings as st
from app.core.road_settings import road_settings as rs
from app.services.leaderboard import BADGES, LEVELS
from app.services.thresholds import build_thresholds
from app.core.survey_settings import survey_settings as sv_

# ------------------------------------------------------------------ briques de contenu
def P(t): return {"type": "p", "text": t}
def H(t): return {"type": "h", "text": t}
def L(*items): return {"type": "list", "items": list(items)}
def S(*steps): return {"type": "steps", "items": [{"title": a, "text": b} for a, b in steps]}
def C(tone, title, text): return {"type": "callout", "tone": tone, "title": title, "text": text}
def EX(title, text): return {"type": "example", "title": title, "text": text}
def FAQ(*qa): return {"type": "faq", "items": [{"q": q, "a": a} for q, a in qa]}
def F(*facts):
    return {"type": "facts", "items": [{"label": f[0], "value": f[1], "hint": f[2] if len(f) > 2 else None,
                                        "live": True} for f in facts]}
def SC(*rows): return {"type": "scale", "items": [{"label": a, "range": b, "color": c, "hint": d} for a, b, c, d in rows]}


def sec(id_, icon, color, title, summary, *blocks):
    return {"id": id_, "icon": icon, "color": color, "title": title, "summary": summary, "blocks": list(blocks)}


def pc(x) -> str: return f"{round(float(x) * 100)} %"
def mn(m) -> str:
    m = float(m)
    return f"{round(m)} min" if m < 120 else f"{m / 60:.0f} h"
def hh(h) -> str:
    h = float(h)
    return f"{round(h)} h" if h < 48 else f"{h / 24:.0f} jours"


def build(tenant_settings: dict | None = None, role: str = "contributor") -> dict:
    th = build_thresholds(tenant_settings)
    cell_m = int(round(h3.average_hexagon_edge_length(st.H3_RESOLUTION, unit="m"), -1))
    zone_km = round(h3.average_hexagon_edge_length(st.MISSION_ZONE_H3_RES, unit="km"), 1)
    parts = (f"matin {st.DAYPART_MORNING_START} h–{st.DAYPART_AFTERNOON_START} h · après-midi "
             f"{st.DAYPART_AFTERNOON_START} h–{st.DAYPART_EVENING_START} h · soirée "
             f"{st.DAYPART_EVENING_START} h–{st.DAYPART_NIGHT_START} h · nuit {st.DAYPART_NIGHT_START} h–"
             f"{st.DAYPART_MORNING_START} h")
    pw = rs.part_weights
    sections = []

    # ============================================================ 1. Bienvenue
    sections.append(sec(
        "bienvenue", "waving_hand", "#0E9F9A", "Bienvenue sur SunuSound",
        "Une carte du bruit construite par tous, avec le micro de nos téléphones.",
        P("SunuSound est une **carte sonore participative**. Chaque personne qui fait une mesure ajoute une "
          "petite pièce au puzzle. Ensemble, nous voyons où il y a du bruit, à quelle heure, et comment cela évolue."),
        S(("Vous mesurez", f"Vous appuyez sur « Mesurer » et le téléphone écoute pendant {st.SESSION_DURATION_SECONDS} secondes."),
          ("Le téléphone calcule", "Il transforme le son en un simple chiffre en décibels. Le son lui-même n'est jamais enregistré ni envoyé."),
          ("Le serveur vérifie", "Plusieurs contrôles automatiques jugent si la mesure est fiable (GPS, durée, saturation…)."),
          ("La carte se met à jour", "Les mesures fiables sont mélangées avec celles des autres pour colorer la carte."),
          ("Vous gagnez des points", "Les bonnes mesures, les missions et les avis rapportent des points, des niveaux et des badges.")),
        C("tip", "À retenir", "Plus vos mesures sont fiables (bon GPS, téléphone immobile, pas de saturation), "
                              "plus elles comptent sur la carte et dans votre classement."),
    ))

    # ============================================================ 2. Mesurer
    sections.append(sec(
        "mesurer", "mic", "#0E9F9A", "Faire une mesure",
        "Ce qui se passe quand vous appuyez sur « Mesurer ».",
        S(("Avant de commencer", "L'application vérifie vos autorisations (micro, position), la précision du GPS et votre solde de crédits."),
          ("Pendant la mesure", f"Restez immobile {st.SESSION_DURATION_SECONDS} secondes, téléphone en main, micro dégagé. Un compte à rebours et un cadran en direct s'affichent."),
          ("Envoi", "Les résultats partent au serveur. Sans réseau, la mesure est gardée dans le téléphone et envoyée plus tard, automatiquement."),
          ("Évaluation", "En quelques secondes, la mesure devient « Validée », « Indicative » ou « Rejetée ». Vous voyez le score et les raisons.")),
        H("Ce que le téléphone calcule"),
        L("**LAeq** : le niveau sonore moyen pendant la mesure, en dB(A). C'est le chiffre principal.",
          "**Lmax / Lmin** : le moment le plus fort et le plus calme (par tranches de 0,125 seconde).",
          "**Saturation** : la part du temps où le micro a été « saturé » (son trop fort, déformé).",
          "**Déplacement** : de combien de mètres vous avez bougé pendant la mesure."),
        H("Ce qui n'est jamais envoyé"),
        L("Aucun enregistrement audio, jamais. Le son est analysé puis oublié dans le téléphone.",
          "Aucune conversation ne peut être reconstituée : seuls quelques chiffres partent."),
        C("info", "Le « dB(A) »", "Le « A » veut dire que le téléphone corrige le son comme notre oreille l'entend : "
                                  "les sons graves très faibles comptent moins, les sons médiums comptent plus."),
        H("Contexte sonore (facultatif)"),
        P("Vous pouvez indiquer ce que vous entendez : circulation, commerce, chantier, musique, rassemblement ou autre. "
          "Ces étiquettes aident à comprendre l'origine du bruit dans les analyses."),
        C("tip", "Pour une mesure réussie", "Attendez que le GPS soit précis (pastille verte), ne marchez pas, ne parlez pas "
                                            "dans le micro et ne couvrez pas le téléphone avec la main."),
    ))

    # ============================================================ 3. Lire la carte
    sections.append(sec(
        "carte", "map", "#2563EB", "Lire la carte du bruit",
        "Couleurs, nuage, vent, confiance et historique animé.",
        P("La carte est **la même pour tout le monde** : elle est calculée par le serveur à partir des mesures de tous "
          "les contributeurs. Vous voyez donc ce que voient les autres."),
        SC(("Calme", "moins de 45 dB", "#22C55E", "Bibliothèque, village la nuit"),
           ("Modéré", "45 à 60 dB", "#FACC15", "Conversation, rue tranquille"),
           ("Bruyant", "60 à 70 dB", "#F97316", "Circulation régulière"),
           ("Très bruyant", "70 à 80 dB", "#EF4444", "Rue animée, klaxons, marché"),
           ("Intense", "80 dB et plus", "#9333EA", "Chantier, concert, grand carrefour")),
        H("Deux façons de voir"),
        L("**Nuage** : le bruit est lissé comme une météo. Il se diffuse autour des zones mesurées et est légèrement "
          "poussé par le vent. C'est indicatif : un nuage n'est pas une mesure exacte en chaque point.",
          f"**Grille** : la ville est découpée en hexagones d'environ {cell_m} m de côté. Chaque hexagone affiche le niveau de ses mesures."),
        H("Pourquoi certaines zones sont vides ?"),
        P(f"Une zone n'apparaît que si **au moins {th.public_min_devices} contributeurs différents** y ont mesuré sur la période. "
          "C'est une protection de la vie privée : on ne montre pas une zone qui permettrait de deviner qui y a mesuré."),
        H("L'indice de confiance"),
        P("Chaque zone a une confiance de 0 à 100 %. Elle monte quand :"),
        L(f"il y a beaucoup de mesures (objectif : {st.CONF_TARGET_MEASUREMENTS}) ; poids {pc(st.CONF_W_COUNT)}",
          f"plusieurs personnes ont mesuré (objectif : {st.CONF_TARGET_DEVICES}) ; poids {pc(st.CONF_W_DEVICES)}",
          f"les téléphones sont calibrés ; poids {pc(st.CONF_W_CALIBRATION)}",
          f"les mesures sont récentes (la confiance baisse peu à peu, environ {hh(st.CONF_RECENCY_TAU_HOURS)} de demi-vie) ; poids {pc(st.CONF_W_RECENCY)}"),
        H("Les filtres"),
        L("**Période** : aujourd'hui, 7 jours, 30 jours.",
          f"**Plage horaire** : {parts}.",
          "**Mesures indicatives** : par défaut la carte n'utilise que les mesures validées. Vous pouvez inclure les indicatives."),
        H("L'historique animé"),
        P("Le bouton « horloge » rejoue l'évolution du bruit jour par jour ou plage par plage. Une zone sans nouvelle mesure "
          "reste visible quelques images en s'estompant, pour ne pas clignoter."),
        C("warn", "Mesures participatives", "Ces mesures sont indicatives et non certifiées. Elles ne remplacent pas "
                                             "une mesure réglementaire faite avec un sonomètre homologué."),
    ))

    # ============================================================ 4. Routes
    sections.append(sec(
        "routes", "route", "#F59E0B", "Les routes sur la carte",
        "Le tracé réel des routes, coloré selon le bruit mesuré dessus.",
        P("En plus des zones, la carte peut afficher **le vrai tracé des routes** (données OpenStreetMap). "
          "Chaque route est découpée en petits tronçons d'environ "
          f"{round(rs.ROAD_SEGMENT_TARGET_M)} m. Chaque tronçon a sa propre couleur."),
        H("Comment une mesure arrive sur une route"),
        S(("Vous mesurez près d'une route", f"Dans les {round(rs.ROAD_MATCH_MAX_M)} mètres autour de vous, le serveur cherche le tronçon le plus proche."),
          ("Rattachement automatique", "Le rattachement se fait en arrière-plan, en quelques minutes, sans rien faire de votre côté."),
          ("Le tronçon se colore", "Comme pour les zones, le niveau du tronçon est la moyenne énergétique de ses mesures fiables.")),
        C("info", "Vert = pas encore mesuré", "Un tronçon sans mesure est affiché en **vert** (niveau de base). "
                                               "Cela ne veut pas dire qu'il est silencieux : il n'a simplement pas encore été mesuré."),
        P("Les routes sont affichées en nuage lumineux comme le reste de la carte (et en traits nets en mode grille). "
          "Touchez une route pour voir son nom, son type, son niveau et ses mesures par plage horaire."),
        H("Les types de routes"),
        L("Autoroutes et nationales, routes principales, secondaires, routes de quartier, routes locales, rues résidentielles.",
          "Zoomez pour voir les petites rues : à grande échelle, seules les grandes routes sont affichées pour que la carte reste lisible."),
        F(("Rattachement mesure → route", f"{round(rs.ROAD_MATCH_MAX_M)} m maximum"),
          ("Longueur d'un tronçon", f"environ {round(rs.ROAD_SEGMENT_TARGET_M)} m")),
    ))

    # ============================================================ 5. Qualité
    sections.append(sec(
        "qualite", "verified", "#16A34A", "Qualité et validation",
        "Comment le serveur décide qu'une mesure est fiable.",
        P("Chaque mesure passe un contrôle automatique. Il y a d'abord des **règles éliminatoires**, puis un **score de 0 à 100 %**."),
        H("Les règles éliminatoires (mesure rejetée)"),
        F(("Niveau plausible", f"entre {round(st.LAEQ_MIN_DB)} et {round(st.LAEQ_MAX_DB)} dB"),
          ("GPS trop imprécis", f"au-delà de {round(th.gps_reject_m)} m"),
          ("Durée utile minimale", f"{pc(th.min_duration_ratio)} de la durée prévue"),
          ("Saturation maximale", pc(th.max_saturation_ratio), "au-delà, le micro déformait le son"),
          ("Déplacement maximal", f"{round(th.max_movement_m)} m pendant la mesure"),
          ("Âge maximal d'une mesure", hh(th.max_age_hours), "envoi hors-ligne inclus"),
          ("Cohérence Lmin / LAeq / Lmax", "les trois chiffres doivent se tenir"),
          ("Date dans le futur", "refusée")),
        H("Le score (6 critères)"),
        F(("GPS", pc(st.QW_GPS), f"parfait sous {round(th.gps_good_m)} m, puis dégressif"),
          ("Durée", pc(st.QW_DURATION)),
          ("Pas de saturation", pc(st.QW_SATURATION)),
          ("Immobilité", pc(st.QW_MOVEMENT)),
          ("Téléphone calibré", pc(st.QW_CALIBRATION), f"sinon seulement {pc(th.uncalibrated_subscore)} sur ce critère"),
          ("Cohérence avec les voisines", pc(st.QW_NEIGHBOR))),
        H("Les trois statuts"),
        F(("Validée", f"score ≥ {pc(th.validated_min_score)}", "utilisée sur la carte"),
          ("Indicative", f"score ≥ {pc(th.indicative_min_score)}", "visible si on active les mesures indicatives"),
          ("Rejetée", "en dessous, ou règle éliminatoire", "non utilisée")),
        C("info", "Téléphone non calibré",
          "Les micros de téléphone ne mesurent pas tous pareil. Un modèle « calibré » a été comparé à un sonomètre de référence "
          "et reçoit une correction en dB. " +
          ("Les mesures d'un téléphone non calibré peuvent être validées dans cette organisation."
           if th.allow_uncalibrated_validated else
           "Tant que votre modèle n'est pas calibré, vos mesures restent « indicatives » au mieux, même avec un excellent score.")),
        H("La comparaison avec les voisines"),
        P(f"Le serveur compare votre niveau à celui des zones voisines, à la même plage horaire, sur les "
          f"{th.neighbor_window_days} derniers jours. Il faut au moins {th.neighbor_min_samples} mesures voisines pour comparer. "
          "Un niveau très éloigné de ses voisines est signalé comme « écart important » et ne peut plus être validé automatiquement. "
          "S'il y a trop peu de voisines, ce critère reste neutre."),
        H("Qui peut changer une décision ?"),
        L("**Les analyseurs** : des contributeurs fiables près de la mesure donnent leur avis en direct (voir « Avis des analyseurs »).",
          "**Un administrateur** : sa décision l'emporte toujours et n'est jamais écrasée par l'automatique."),
        FAQ(("Pourquoi ma mesure est « rejetée » ?", "Ouvrez-la dans « Mesures » : les raisons sont listées avec un conseil (par exemple « restez immobile »)."),
            ("Ma mesure est « en évaluation » depuis longtemps ?", "Elle est reprise automatiquement toutes les quelques minutes. Si elle reste bloquée, tirez pour actualiser.")),
    ))

    # ============================================================ 6. Calcul du bruit
    sections.append(sec(
        "calcul", "calculate", "#7C3AED", "Comment le bruit est calculé",
        "Pourquoi on ne fait pas une moyenne « normale » des décibels.",
        P("Les décibels ne s'additionnent pas comme des kilos. L'échelle est **logarithmique** : +10 dB, c'est dix fois plus d'énergie sonore."),
        L("Deux sources identiques de 60 dB ne font pas 120 dB, mais **63 dB** (+3 dB).",
          "Un bruit de 70 dB est perçu environ deux fois plus fort qu'un bruit de 60 dB."),
        H("La moyenne énergétique"),
        P("Pour faire la moyenne de plusieurs mesures, SunuSound ne fait **jamais** la moyenne simple des dB. "
          "Il transforme chaque niveau en énergie, fait la moyenne pondérée par la durée, puis revient en dB."),
        EX("Un exemple concret",
           "Une rue est à **70 dB** le matin et **50 dB** le soir. La moyenne simple donnerait 60 dB, mais la moyenne "
           "énergétique donne **environ 67 dB** : les moments bruyants pèsent beaucoup plus. C'est cela qu'un riverain ressent vraiment."),
        H("Ce que contient chaque zone"),
        S(("Correction", "Chaque mesure reçoit la correction de son modèle de téléphone, si il est calibré."),
          ("Regroupement", "Les mesures sont regroupées par zone, par jour local (fuseau de Dakar) et par plage horaire."),
          ("Moyenne énergétique", "Le niveau de la zone est la moyenne énergétique, pondérée par la durée de chaque mesure."),
          ("Sur la période choisie", "La carte combine les jours et plages que vous avez filtrés.")),
        F(("Plages horaires", parts), ("Taille d'une zone", f"environ {cell_m} m de côté")),
        H("Le nuage"),
        P("Pour le mode « nuage », chaque zone mesurée devient une source qui rayonne autour d'elle (une cloche douce). "
          "Les sources sont mélangées en énergie et pondérées par leur confiance. Le vent étire le nuage vers l'aval, "
          "l'atténue vers l'amont et le décale un peu. Loin des mesures, le nuage s'éteint : on ne montre pas ce qu'on ne sait pas."),
        C("warn", "Le vent animé est une illustration", "Les petites particules et ondes montrent où le vent pousserait le bruit. "
                                                           "Elles aident à comprendre, mais ne sont pas une mesure."),
    ))

    # ============================================================ 7. Crédits
    sections.append(sec(
        "credits", "bolt", "#F59E0B", "Votre solde de crédits",
        "Pourquoi on ne peut pas mesurer sans limite, et comment le solde se recharge.",
        P("Pour que la carte se remplisse **là où on en a besoin** (et pas toujours aux mêmes endroits), "
          "chaque contributeur a un **solde de crédits**. Chaque mesure en coûte un peu."),
        F(("Solde de départ", f"{st.QUOTA_INITIAL_BALANCE:g} crédits"),
          ("Coût d'une mesure", f"de {st.QUOTA_COST_MIN:g} à {st.QUOTA_COST_MAX:g} crédit(s)",
           "moins cher dans une zone qui manque de mesures"),
          ("Objectif de recharge par jour", f"de {st.QUOTA_MIN_DAILY:g} à {st.QUOTA_MAX_DAILY:g}"),
          ("Plafond du solde", f"au moins {st.QUOTA_MIN_CAP:g}, soit {st.QUOTA_CAP_DAYS:g} jours de recharge"),
          ("Délai avant de remesurer au même endroit", f"de {mn(st.QUOTA_COOLDOWN_MIN_MINUTES)} à {mn(st.QUOTA_COOLDOWN_MAX_MINUTES)}")),
        H("Comment le solde se recharge"),
        L("Il se recharge **heure par heure**, plus vite le jour que la nuit (une heure de nuit compte quatre fois moins).",
          "Il se recharge plus quand le territoire a **besoin** de mesures (peu de mesures récentes).",
          "Votre objectif du jour dépend de votre **fiabilité**, de la façon dont vous **utilisez** votre solde et de votre taux de **réussite aux missions**."),
        H("Le solde s'use si on ne s'en sert pas"),
        P(f"Un solde dormant diminue chaque jour : on conserve entre {pc(st.QUOTA_RETENTION_MIN)} et {pc(st.QUOTA_RETENTION_MAX)} "
          "du solde d'un jour à l'autre, selon le besoin du territoire et votre activité. Si vous êtes inactif, il fond plus vite. "
          "Les crédits sont faits pour être utilisés."),
        H("Zones voisines et délai"),
        P("Après une mesure, vous devez attendre un peu avant de refaire une mesure au même endroit. Le délai est plus court "
          "dans les zones qui manquent de mesures. Dans une zone voisine, le délai est de moitié."),
        H("Mesures gratuites"),
        L("Une mesure faite pour une **proposition acceptée** ne coûte rien.",
          "Une mesure faite dans la **zone d'une mission acceptée** ne coûte rien.",
          "Les administrateurs et analystes du serveur ne sont pas limités."),
        C("info", "Hors-ligne", f"Une mesure faite sans réseau est jugée à son envoi. Si elle a plus de {mn(st.QUOTA_HARD_TOLERANCE_MINUTES)}, "
                                "elle n'est pas refusée : le solde peut simplement passer en négatif."),
        FAQ(("Mon solde est à 0, que faire ?", "Attendez : il se recharge toutes les heures. Les missions donnent aussi des crédits bonus."),
            ("Pourquoi cette limite ?", "Sans limite, quelques personnes mesureraient toujours aux mêmes endroits. Le solde pousse chacun à mesurer là où cela apporte le plus.")),
    ))

    # ============================================================ 8. Propositions
    sections.append(sec(
        "propositions", "auto_awesome", "#0E9F9A", "Propositions de mesure",
        "Comment l'application choisit les zones qu'elle vous suggère.",
        P("Si vous activez la **collecte hybride**, l'application peut vous proposer de mesurer à l'endroit où vous êtes, "
          "quand cet endroit manque d'informations."),
        H("Conditions pour recevoir une proposition"),
        L("Vous avez activé la collecte hybride et donné vos deux autorisations (position, micro).",
          f"Votre batterie est à plus de {st.MIN_BATTERY_LEVEL} %.",
          "Vous êtes dans votre créneau du « planning » (jours et heures que vous avez choisis).",
          f"Pas plus de {st.MAX_PROPOSALS_PER_USER_PER_DAY} propositions par jour et au moins {mn(st.MIN_MINUTES_BETWEEN_PROPOSALS)} entre deux."),
        H("Comment la zone est notée"),
        F(("Ancienneté de la dernière mesure", pc(st.W_FRESHNESS), "plus c'est vieux, plus c'est utile"),
          ("Historique de bruit", pc(st.W_NOISE_HISTORY), "zones bruyantes ou très variables"),
          ("Manque de couverture", pc(st.W_COVERAGE_GAP), "plages horaires sans mesure"),
          ("Pertinence de l'heure", pc(st.W_TIME_RELEVANCE), "cette plage horaire est-elle peu mesurée ?"),
          ("Disponibilité", pc(st.W_AVAILABILITY), "moins on est nombreux sur place, mieux c'est")),
        P("Ces pourcentages sont les valeurs de départ. **Ils s'ajustent tout seuls** : chaque heure, le système regarde quelles "
          "propositions ont donné de bonnes mesures dans des zones qui en avaient vraiment besoin, et donne plus de poids aux critères gagnants."),
        H("La décision"),
        S(("Note de votre zone", "On calcule la note de la zone où vous êtes."),
          ("Votre habitude", "Elle est ajustée selon vos réponses passées à cette heure de la journée (accepter ou refuser)."),
          ("Comparaison", f"Elle doit dépasser {pc(st.PROPOSAL_MIN_SCORE)} et être dans les meilleures zones à {st.CANDIDATE_RING_K} anneaux autour de vous."),
          ("Exploration", f"Dans {pc(st.EXPLORATION_EPSILON)} des cas, une zone moins prioritaire est proposée pour tester et apprendre.")),
        F(("Durée de validité", mn(st.PROPOSAL_TTL_MINUTES)), ("Durée de la mesure", f"{st.SESSION_DURATION_SECONDS} s")),
        C("info", "Ignorer = refuser", "Une proposition qui expire sans réponse compte comme un refus pour l'apprentissage."),
    ))

    # ============================================================ 9. Missions
    sections.append(sec(
        "missions", "flag", "#F59E0B", "Missions",
        "Des objectifs de mesure précis, en zone ou sur une route, avec une récompense.",
        P("Une mission est une demande du système : « il manque des mesures ici ». Elle est **proposée**, vous pouvez "
          "l'accepter ou la refuser. Réussie, elle vous rapporte des **crédits bonus**, des points et des badges."),
        H("Deux types de missions"),
        L(f"**Mission de zone** : plusieurs mesures ({st.MISSION_MIN_COUNT} à {st.MISSION_MAX_COUNT}) dans un quartier d'environ {zone_km} km de côté.",
          "**Mission route** : une mesure en un **point précis d'une vraie route** qui manque de mesures pour une plage horaire donnée."),
        H("Comment une mission de zone est créée"),
        S(("Repérage", f"On compare chaque quartier à l'objectif de {st.TARGET_MEASUREMENTS_PER_CELL} mesures par hexagone sur {st.COVERAGE_WINDOW_DAYS} jours."),
          ("Seuil", f"Un quartier devient « en manque » s'il lui manque au moins {st.MISSION_MIN_ZONE_DEFICIT} mesures et au moins 25 % de son objectif."),
          ("Choix des personnes", f"Jusqu'à {st.MISSION_USERS_PER_ZONE} personnes proches (moins de {st.MISSION_MAX_RADIUS_KM:g} km de leur dernière position connue)."),
          ("Taille", "Plus vous êtes actif et fiable, plus la mission peut être grande.")),
        H("Qui reçoit des missions ?"),
        F(("Activité (14 jours)", pc(st.MS_W_ACTIVITY), f"objectif : {st.MISSION_ACTIVITY_TARGET} mesures"),
          ("Fiabilité de vos mesures", pc(st.MS_W_RELIABILITY)),
          ("Réussite aux missions", pc(st.MS_W_COMPLETION)),
          ("Taux d'acceptation", pc(st.MS_W_ACCEPTANCE)),
          ("Score minimum", pc(st.MISSION_MIN_SCORE))),
        H("Mission route : comment le point est choisi"),
        S(("Besoin du tronçon", f"Pour chaque route proche, on calcule un besoin : poids de la route (grandes routes = plus de trafic attendu) × poids de la plage horaire × manque de mesures (objectif : {rs.ROAD_TARGET_PER_PART} mesures par plage sur {rs.ROAD_COVERAGE_WINDOW_DAYS} jours)."),
          ("Plage atteignable", f"Seules les plages qui commencent dans les {rs.ROAD_LOOKAHEAD_HOURS:g} prochaines heures et durent encore au moins {rs.ROAD_MIN_WINDOW_MINUTES} min sont retenues."),
          ("Choix", f"Les besoins les plus forts, proches de vous (jusqu'à {rs.ROAD_MISSION_MAX_RADIUS_KM:g} km), sont proposés. Deux points proposés sont éloignés d'au moins {round(rs.ROAD_MIN_SPACING_M)} m."),
          ("Pas de répétition", f"Une même route n'est pas reproposée à la même personne pendant {hh(rs.ROAD_REPEAT_COOLDOWN_HOURS)}.")),
        F(("Poids des plages horaires", " · ".join(f"{k} {v:g}" for k, v in pw.items()), "la nuit compte moins"),
          ("Tolérance autour du point", f"{round(rs.ROAD_TARGET_RADIUS_M)} m")),
        H("Itinéraire"),
        P("Pour une mission route, le bouton « Itinéraire » trace sur la carte **le chemin réel par les routes**, "
          "depuis votre position jusqu'au point cible, avec la durée et la distance."),
        H("Valider une mission"),
        L("Seules les mesures faites **après l'acceptation** comptent.",
          f"Mission de zone : mesures dans le cercle de la zone ; mission route : à moins de {round(rs.ROAD_TARGET_RADIUS_M)} m du point, "
          "**pendant la plage horaire visée** et avant la fin de la fenêtre.",
          "Les mesures rejetées ne comptent pas. La mission est réussie quand le nombre demandé de mesures est **validé**.",
          "Pas besoin d'être pile sur le point : la tolérance est là pour cela."),
        F(("Offre valable", hh(st.MISSION_OFFER_HOURS)),
          ("Temps pour accomplir (zone)", hh(st.MISSION_COMPLETION_HOURS)),
          ("Récompense", f"environ {st.MISSION_REWARD_PER_MEASUREMENT:g} crédit par mesure (zone), {rs.ROAD_REWARD_PER_MEASUREMENT:g} (route), plus si le besoin est fort"),
          ("Missions en cours maximum", f"{st.MISSION_MAX_ACTIVE} acceptées, {st.MISSION_MAX_PENDING} en attente"),
          ("Après un refus", f"même zone non reproposée pendant {st.MISSION_DECLINE_COOLDOWN_DAYS} jours")),
        C("tip", "Bonus au-dessus du plafond", "La récompense peut dépasser le plafond de votre solde de 50 %."),
        FAQ(("Une mission échoue, que perd-on ?", "Rien en crédits, mais votre taux de réussite baisse un peu, ce qui influence les prochaines missions."),
            ("Le point est dans un endroit inaccessible ?", "Refusez la mission. Elle ne sera pas reproposée avant un moment.")),
    ))

    # ============================================================ 10. Avis
    sections.append(sec(
        "avis", "hearing", "#7C3AED", "Avis des analyseurs",
        "Des voisins qui confirment, corrigent ou rejettent une mesure en direct.",
        P("Quand une mesure vient d'être faite, des **analyseurs** proches reçoivent une question simple : "
          "« ce niveau correspond-il à ce que vous entendez ici et maintenant ? »"),
        H("Qui peut devenir analyseur ?"),
        F(("Mesures validées (90 jours)", f"au moins {acs.ANALYST_MIN_VALIDATED}"),
          ("Missions réussies", f"au moins {acs.ANALYST_MIN_MISSIONS}"),
          ("Fiabilité", f"au moins {pc(acs.ANALYST_MIN_RELIABILITY)}"),
          ("Ancienneté du compte", f"au moins {acs.ANALYST_MIN_ACCOUNT_DAYS} jours"),
          ("Jours d'activité (60 jours)", f"au moins {acs.ANALYST_MIN_ACTIVE_DAYS}"),
          ("Dernière mesure", f"il y a moins de {acs.ANALYST_MAX_INACTIVE_DAYS} jours")),
        P("Dans votre profil, la carte « Analyseur » montre votre progression pour chaque condition."),
        H("Le déroulement"),
        S(("Envoi", f"La mesure est jugée seulement si elle a moins de {mn(acs.REVIEW_MAX_AGE_MINUTES)}. Jusqu'à {acs.REVIEW_MAX_REVIEWERS} analyseurs à moins de {round(acs.REVIEW_RADIUS_M)} m sont choisis : les plus fiables, les plus proches, les plus souvent d'accord avec le groupe."),
          ("Réponse", f"Chacun a {mn(acs.REVIEW_TTL_MINUTES)} : « Oui », « Plus faible » ou « Plus fort » (avec l'option « écart important »)."),
          ("Décision", f"Il faut au moins {acs.REVIEW_MIN_VOTES} réponses, pondérées par la fiabilité des votants.")),
        F(("Confirmée", f"au moins {pc(acs.REVIEW_CONFIRM_RATIO)} de « oui »"),
          ("Corrigée", f"si au moins {pc(acs.REVIEW_ADJUST_RATIO)} disent « plus faible » ou « plus fort » : ajustement de {acs.REVIEW_ADJUST_STEP_DB:g} à {acs.REVIEW_ADJUST_MAX_DB:g} dB"),
          ("Rejetée", f"si l'écart est jugé important (≥ {pc(acs.REVIEW_STRONG_REJECT_SHARE)} de « écart important ») ou si les avis se contredisent"),
          ("Sans quorum", "moins de réponses que nécessaire : la mesure garde son statut")),
        H("Et les analyseurs eux-mêmes ?"),
        L(f"Votre avis est « concordant » s'il va dans le sens de la décision finale : +{acs.LB_PTS_REVIEW:g} points.",
          f"Si vous êtes d'accord avec le groupe moins de {pc(acs.ANALYST_SUSPEND_AGREEMENT)} du temps (au moins {acs.ANALYST_SUSPEND_MIN_VOTES} avis sur 60 jours), "
          f"le rôle est suspendu {acs.ANALYST_SUSPEND_DAYS} jours.",
          "Gardez l'application ouverte : votre position approximative n'est partagée que quelques minutes pour recevoir des demandes."),
        C("info", "Un administrateur a toujours le dernier mot", "Si un administrateur décide, la demande d'avis est annulée."),
    ))

    # ============================================================ 11. Points
    lv = " · ".join(f"{n} ({x} XP)" for x, n in LEVELS)
    sections.append(sec(
        "points", "emoji_events", "#DB2777", "Points, niveaux et classement",
        "Comment votre score est calculé et ce qu'il récompense.",
        H("Les points"),
        F(("Mesure validée", f"+{acs.LB_PTS_VALIDATED:g}"), ("Mesure indicative", f"+{acs.LB_PTS_INDICATIVE:g}"),
          ("Mesure rejetée", f"{acs.LB_PTS_REJECTED:g}"), ("Nouvelle zone couverte", f"+{acs.LB_PTS_CELL:g}"),
          ("Jour d'activité", f"+{acs.LB_PTS_DAY:g}"), ("Mission réussie", f"+{acs.LB_PTS_MISSION:g}, puis +{acs.LB_PTS_MISSION_UNIT:g} par mesure demandée"),
          ("Avis concordant", f"+{acs.LB_PTS_REVIEW:g}"), ("Filleul devenu actif", f"+{acs.LB_PTS_INVITE:g}", "il a au moins 3 mesures validées")),
        H("Le multiplicateur de fiabilité"),
        P(f"Votre total est multiplié par **0,6 + 0,8 × fiabilité**. La fiabilité = (validées + moitié des indicatives + un petit capital de départ) ÷ (mesures + 5). "
          f"Elle est donc entre ×0,6 (très peu fiable) et ×1,4 (parfait). **La qualité compte plus que la quantité.**"),
        EX("Exemple", "20 mesures validées et 0 rejetée → fiabilité haute (environ 90 %) → ×1,3. "
                      "20 mesures dont 10 rejetées → fiabilité faible (environ 55 %) → ×1,0 et des points perdus."),
        H("Les périodes"),
        L("**Semaine**, **Mois** et **Saison (90 jours)**. Le classement se met à jour toutes les 10 minutes.",
          "Vous pouvez masquer votre rang dans « Modifier le profil » : vous continuez à gagner points et niveaux."),
        H("Niveaux (XP)"),
        P("L'XP est la somme de vos points bruts de chaque jour, et ne baisse jamais."),
        P(lv),
        H("Badges"),
        L(*[f"**{lab}** : {desc}" for lab, desc, _i, _t in BADGES.values()]),
    ))

    from app.services.guide_surveys import sections as _survey_sections
    sections.extend(_survey_sections())
    from app.services.guide_studies import sections as _study_sections
    sections.extend(_study_sections())

    # ============================================================ 12. Vie privée
    sections.append(sec(
        "vie-privee", "lock", "#0F766E", "Vie privée et vos données",
        "Ce qui est gardé, pour combien de temps, et ce que les autres voient.",
        L("**Pas d'audio** : jamais enregistré ni envoyé.",
          "**E-mail** : stocké uniquement sous forme chiffrée (illisible), aucun mail commercial.",
          "**Nom, téléphone, ville** : privés, visibles de vous seul. Les autres voient seulement votre **pseudo public**.",
          "**Administrateurs** : ils voient des mesures anonymisées, jamais les coordonnées ni l'identité."),
        H("Durées de conservation"),
        F(("Position précise", f"{st.LOCATION_RETENTION_DAYS} jours, puis remplacée par le centre de la zone"),
          ("Lien mesure ↔ compte", f"{st.IDENTITY_UNLINK_DAYS} jours, puis supprimé : la mesure devient anonyme"),
          ("Rapports exportés", f"{st.REPORT_RETENTION_DAYS} jours"),
          ("Journal d'audit des administrateurs", f"{st.AUDIT_RETENTION_DAYS} jours"),
          ("Historique de connexions", f"{acs.LOGIN_EVENTS_RETENTION_DAYS} jours")),
        F(("Photos d'enquête", f"{sv_.SV_EVIDENCE_RETENTION_DAYS} jours"),
          ("Réponses d'enquête ↔ compte", f"{st.IDENTITY_UNLINK_DAYS} jours, puis lien supprimé")),
        H("Protections sur la carte"),
        L(f"Une zone ou une route n'est affichée que si au moins **{th.public_min_devices} personnes différentes** y ont contribué.",
          "Pour compter les contributeurs, on utilise un jeton qui **change chaque mois** : impossible de vous suivre dans la durée."),
        H("Vos droits"),
        L("Retirer l'autorisation de position ou de micro à tout moment (Paramètres).",
          "Mettre votre participation en pause.",
          "**Supprimer votre compte** : vos positions sont arrondies, les liens avec vous coupés, puis le compte est effacé."),
    ))

    # ============================================================ 13. Compte
    sections.append(sec(
        "compte", "person", "#2563EB", "Compte, sécurité et invitations",
        "Mot de passe, codes de récupération, sessions et parrainage.",
        H("Mot de passe"),
        L(f"Au moins {st.PASSWORD_MIN_LENGTH} caractères avec lettres et chiffres, et trois types de caractères (ou 14 caractères et plus).",
          "Refusé : mots de passe trop courants ou contenant votre pseudo ou votre nom."),
        H("Codes de récupération"),
        P(f"À l'inscription, vous recevez **{acs.RECOVERY_CODES_COUNT} codes** à garder en lieu sûr. Comme votre e-mail n'est pas stocké en clair, "
          "aucun mail de réinitialisation n'est possible : un code, utilisable une seule fois, permet de changer le mot de passe. "
          "Vous pouvez en générer de nouveaux depuis votre profil (les anciens sont alors annulés)."),
        H("Protection contre les attaques"),
        L(f"Après {st.LOGIN_MAX_ATTEMPTS} échecs, le compte est bloqué {mn(st.LOGIN_LOCK_SECONDS / 60)}.",
          "Vous voyez vos dernières connexions, vos sessions ouvertes et vos appareils dans le profil.",
          "« Déconnecter partout » ferme toutes les sessions."),
        H("Invitations"),
        F(("Codes actifs par personne", f"{acs.INVITE_USER_MAX_ACTIVE}"), ("Utilisations par code", f"{acs.INVITE_USER_MAX_USES}"),
          ("Validité d'un code", f"{acs.INVITE_USER_TTL_DAYS} jours"),
          ("Inscription sur invitation", "obligatoire" if acs.INVITE_REQUIRED else "facultative")),
        P(f"Vous gagnez {acs.LB_PTS_INVITE:g} points quand une personne invitée devient active."),
    ))

    # ============================================================ staff
    if role != "contributor":
        sections.append(sec(
            "equipe", "admin", "#475569", "Espace équipe",
            "Pour les administrateurs et analystes du serveur.",
            L("**Validation** : file des mesures, filtres par score, statut et raison ; décisions groupées (admin).",
              "**Statistiques** : qualité par jour, raisons de rejet, quota, classement des utilisateurs actifs.",
              "**Routes** : avancement de l'import, couverture, et liste des routes qui manquent le plus de mesures.",
              "**Calibration** : l'ajout d'une correction à un modèle de téléphone réévalue ses mesures récentes."),
        ))

    # ============================================================ FAQ + glossaire
    sections.append(sec(
        "faq", "help", "#64748B", "Questions fréquentes",
        "Réponses courtes aux questions les plus posées.",
        FAQ(("Pourquoi mon niveau est différent de celui de mon voisin ?", "Les micros diffèrent, la position et le moment aussi. C'est pour cela que la carte moyenne beaucoup de mesures."),
            ("Puis-je mesurer en intérieur ?", "Techniquement oui, mais le GPS est moins précis et la mesure peut être rejetée. L'idéal est l'extérieur."),
            ("Pourquoi ma mesure est « indicative » ?", "Souvent parce que le modèle de téléphone n'est pas calibré, ou que le score est juste sous le seuil des mesures validées."),
            ("Ça consomme de la batterie ?", f"Une mesure dure {st.SESSION_DURATION_SECONDS} secondes, c'est négligeable. Les propositions interrogent votre position seulement application ouverte."),
            ("Ça consomme des données mobiles ?", "Très peu : quelques chiffres par mesure. Les fonds de carte sont la plus grosse part."),
            ("Les routes sont vertes partout ?", "Normal au début : vert veut dire « pas encore mesuré ». Les routes se colorent au fil des mesures."),
            ("Pourquoi une zone disparaît de la carte ?", f"Elle n'a plus assez de contributeurs différents sur la période choisie (minimum {th.public_min_devices}). Élargissez la période."),
            ("Je n'ai pas reçu de proposition ?", "Vérifiez : collecte hybride activée, planning actif, batterie suffisante, autorisations accordées, et limites quotidiennes.")),
        H("Petit glossaire"),
        L("**dB(A)** : décibel pondéré pour ressembler à l'oreille humaine.", "**LAeq** : niveau moyen (énergétique) sur la durée de la mesure.",
          "**Lmax / Lmin** : niveaux le plus fort et le plus faible.", "**Hexagone / zone** : case de la carte d'environ " + f"{cell_m} m.",
          "**Calibration** : correction d'un modèle de téléphone par comparaison à un sonomètre.", "**Plage horaire** : matin, après-midi, soirée, nuit.",
          "**Indicative** : mesure utilisable mais moins sûre.", "**Tronçon** : petit morceau de route d'environ " + f"{round(rs.ROAD_SEGMENT_TARGET_M)} m.",
          "**Analyseur** : contributeur fiable qui donne son avis sur les mesures proches."),
    ))

    return {"title": "Guide SunuSound",
            "subtitle": "Tout comprendre : mesures, carte, routes, missions, points et vie privée.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Les valeurs marquées d'un point vert sont celles réellement utilisées par le serveur aujourd'hui.",
            "sections": sections}