import h3

from app.core.config import settings as st
from app.core.survey_settings import survey_settings as sv
from app.services import catalog
from app.services.guide import C, EX, F, FAQ, H, L, P, S, SC, pc, sec


def sections() -> list[dict]:
    doms = [f"**{d.label}** : {d.summary}" for d in catalog.DOMAINS.values()]
    res_km = {r: round(h3.average_hexagon_edge_length(r, unit="km"), 1) for r in (5, 6, 7, 8, 9)}
    out = []

    out.append(sec(
        "donnees", "dataset", "#2563EB", "Sunu Données : les 12 thèmes",
        "En plus du bruit, vous aidez à cartographier la vie quotidienne : population, logement, emploi, santé…",
        P("Sunu Données applique à la société **la même méthode que pour le bruit** : de petites contributions "
          "citoyennes, vérifiées, regroupées par zone, puis affichées sur une carte. Les thèmes :"),
        L(*doms),
        C("info", "Rien n'est inventé", "Une carte n'affiche que ce que des personnes ont réellement répondu. "
                                         "Sans réponses suffisantes dans une zone, la zone reste vide."),
    ))

    out.append(sec(
        "enquetes", "assignment", "#0E9F9A", "Répondre à une enquête",
        "Choisir des profils qui vous ressemblent au lieu de tout saisir.",
        S(("Choisir une enquête", "La liste montre les enquêtes ouvertes près de vous. Une même enquête ne se refait qu'après un délai de repos."),
          ("Choisir un profil par rubrique", "Chaque rubrique propose des profils : un ensemble de réponses cohérentes (par exemple « Maison en dur, 5 pièces, eau au robinet, électricité du réseau »). Choisissez celui qui vous ressemble le plus."),
          ("Ajuster si besoin", "Un détail est différent ? Corrigez seulement ce détail. Plusieurs profils peuvent être combinés quand la rubrique le permet."),
          ("« Aucun ne me correspond »", "Répondez directement. Ce signal est précieux : il sert à créer de meilleurs profils."),
          ("Photos (si demandées)", f"Certaines réponses demandent une preuve photo, à ajouter dans les {sv.SV_EVIDENCE_WINDOW_MINUTES} minutes."),
          ("Envoi et vérification", "Votre réponse est contrôlée automatiquement en quelques secondes.")),
        C("tip", "Gain de temps", "Choisir un profil prend quelques secondes. Il n'y a aucune donnée personnelle à saisir."),
        F(("Réponses max. par jour", f"{sv.SV_MAX_PER_DAY}"), ("Profils affichés par rubrique", f"jusqu'à {sv.SV_SHOWN_MAX}"),
          ("Photos max. par réponse", f"{sv.SV_EVIDENCE_PER_RESPONSE}")),
    ))

    out.append(sec(
        "profils", "psychology", "#7C3AED", "Comment les profils sont construits",
        "Des archétypes vérifiés, qui évoluent avec les vrais choix des gens.",
        P("Un profil n'est **pas une statistique** : c'est un exemple plausible de réponses. Chaque profil passe des contrôles "
          "(types, valeurs possibles, cohérence des totaux) avant d'être proposé."),
        S(("Au départ", "Le système crée des profils variés qui couvrent la diversité des situations (milieu urbain ou rural, taille du ménage, revenus, types de logement…), adaptés au thème de l'enquête."),
          ("Avec l'IA", "Une IA peut proposer des profils supplémentaires, mais seulement avec les questions et les valeurs autorisées de l'enquête. Son travail est revérifié par le serveur."),
          ("Avec les vraies réponses", "Quand assez de personnes ont répondu, les profils sont recalculés à partir des réponses réelles (regroupement de situations semblables)."),
          ("Profils qui montent, profils qui descendent", f"Les profils très rarement choisis sont retirés (si la couverture reste bonne). Si trop de gens répondent « aucun ne me correspond » (plus de {pc(sv.SV_NONE_RATE_TRIGGER)}), de nouveaux profils sont créés."),
          ("Personnalisation", "L'ordre des profils s'adapte à votre secteur et à vos réponses passées. Vos données ne sont jamais envoyées à l'IA.")),
        C("info", "Pas de profils sensibles", "Ethnie, religion et opinion politique sont bloquées par défaut. Elles ne peuvent être étudiées que si l'organisation l'autorise explicitement pour une enquête précise."),
    ))

    out.append(sec(
        "verification", "fact_check", "#16A34A", "Comment les réponses sont vérifiées",
        "Cohérence, rythme, recoupement avec les voisins et preuves photo.",
        P("Chaque réponse reçoit un **score de 0 à 100 %**, puis un statut (validée, indicative, rejetée). "
          "Le but est de protéger la carte, **pas d'accuser les personnes** : une réponse douteuse est déclassée, c'est tout."),
        F(("Cohérence interne", pc(sv.SQ_W_CONSISTENCY), "les totaux se tiennent (femmes + hommes = personnes…)"),
          ("Rythme de saisie", pc(sv.SQ_W_TIMING), "réponses trop rapides"),
          ("Effort", pc(sv.SQ_W_EFFORT), "mêmes notes partout, « oui » partout"),
          ("Lieu", pc(sv.SQ_W_LOCATION), "précision GPS, déplacements possibles"),
          ("Recoupement avec les voisins", pc(sv.SQ_W_PEERS), "écart avec les réponses validées autour"),
          ("Valeurs de référence", pc(sv.SQ_W_REFERENCE), "uniquement si l'organisation en a fourni"),
          ("Preuves photo", pc(sv.SQ_W_EVIDENCE)),
          ("Fiabilité du répondant", pc(sv.SQ_W_REPUTATION)),
          ("Comportement", pc(sv.SQ_W_BEHAVIOR), "changement brutal, réponses copiées")),
        F(("Validée", f"score ≥ {pc(sv.SQ_VALIDATED_MIN)}"), ("Indicative", f"score ≥ {pc(sv.SQ_INDICATIVE_MIN)}"),
          ("Rejetée", "en dessous ou règle éliminatoire")),
        H("Les minorités ne sont pas pénalisées"),
        P("Une réponse rare n'est pas une réponse fausse. Un écart avec les voisins ne la déclasse que s'il est très marqué, "
          "et jamais pour une catégorie simplement peu fréquente."),
        H("Photos"),
        L("Les photos n'ont de valeur que si elles sont **prises pendant la réponse**, **dans la zone** et **inédites** (une même photo ne peut pas servir à deux personnes).",
          f"Elles sont conservées {sv.SV_EVIDENCE_RETENTION_DAYS} jours, vues uniquement par les administrateurs, puis supprimées.",
          "Sans consentement « photo », aucune photo n'est demandée."),
        EX("Exemple", "Un ménage déclare 6 personnes : 4 femmes et 4 hommes. Le total ne tombe pas juste, la réponse est refusée dès la saisie avec le message à corriger."),
    ))

    out.append(sec(
        "confiance-donnees", "shield_person", "#DB2777", "Votre fiabilité de répondant",
        "Comment le système apprend votre manière de répondre.",
        P("À partir de **vos propres réponses passées uniquement**, le système forme un profil de comportement : "
          "attentif, pressé, réponses très uniformes, réponses tranchées, régulier… Il ne compare jamais votre identité à celle des autres."),
        L("Plus vos réponses sont validées, plus elles pèsent sur la carte.",
          "Un conseil personnalisé vous est donné dans votre profil si une habitude vous pénalise.",
          "Votre fiabilité se reconstruit : elle repose sur les 90 derniers jours."),
        FAQ(("Suis-je accusé de mentir ?", "Non. Le score mesure l'incohérence et l'écart avec les tendances, pas l'honnêteté."),
            ("Mes mauvaises réponses restent-elles pour toujours ?", "Non, seuls les 90 derniers jours comptent.")),
    ))

    out.append(sec(
        "cartes-donnees", "layers", "#CA8A04", "Lire les cartes de données",
        "Zones colorées, barres, marqueurs et niveaux de confiance.",
        P("Chaque thème a sa carte. Le rendu dépend de la nature de l'information :"),
        L("**Barres sur les zones** : populations, emploi. La hauteur montre la valeur la plus forte.",
          "**Zones colorées** : logement, éducation, agriculture, numérique… la teinte montre l'intensité.",
          "**Marqueurs** : structures de santé, écoles, entreprises, qui sont des lieux précis.",
          "**Chaleur** : revenus, prix, pouvoir d'achat."),
        H("Taille des zones"),
        F(*[(f"Niveau {r}", f"environ {k} km de côté") for r, k in res_km.items()]),
        P("En zoomant, les zones deviennent plus petites. Toucher une zone affiche toutes les valeurs de son thème."),
        H("Ce que signifie « valeur »"),
        L("Moyenne, pourcentage, catégorie dominante ou total déclaré, selon l'indicateur.",
          "Le **total déclaré** n'est jamais une estimation de population : c'est la somme des réponses reçues.",
          "Chaque zone affiche son nombre de réponses, de contributeurs et sa **confiance**."),
        H("Protection de la vie privée"),
        F(("Contributeurs minimum par zone", f"{sv.SM_MIN_CONTRIBUTORS}"),
          ("Sujets sensibles (revenus, santé, dettes…)", f"{sv.SM_MIN_CONTRIBUTORS_SENSITIVE}"),
          ("Lieux publics (écoles, centres de santé)", f"{sv.SM_MIN_CONTRIBUTORS_FACILITY}")),
        P("Une personne compte pour une seule voix dans une zone, même si elle a répondu plusieurs fois. "
          "Les valeurs de référence officielles (recensement…) ne s'affichent que si l'organisation les a chargées."),
        C("warn", "Échantillon participatif", "Les cartes sont indicatives : elles reflètent les personnes qui ont répondu, pas toute la population."),
    ))
    return out