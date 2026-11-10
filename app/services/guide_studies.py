from app.core.study_settings import study_settings as sts
from app.services.guide import C, F, FAQ, H, L, P, S, hh, pc, sec
from app.services.study_rewards import KIND_LABELS as RK
from app.services.study_schema import KIND_LABELS as SK


def sections() -> list[dict]:
    kinds = " · ".join(SK.values())
    rewards = " · ".join(RK.values())
    return [
        sec("etudes", "campaign", "#4F46E5", "Études, sondages et enquêtes commerciales",
            "Répondre pour des entreprises, ONG ou services, avec parfois une récompense.",
            P("En plus des cartes, des organisations peuvent vous proposer des **études** : sondages, études de marché, "
              "tests de produits, études de terrain. Types : " + kinds + "."),
            S(("Choisir une étude", "La liste montre les études qui vous correspondent. Les invitations personnelles sont en tête."),
              ("Lire la fiche", "Vous voyez qui la demande (entreprise, ONG, personne, service), ce qu'elle rapporte, les règles et ce qui est partagé."),
              ("Répondre", "Questions, images, vidéos, notes de produits… Certaines questions vérifient d'abord que l'étude vous concerne."),
              ("Suivre le résultat", "Votre réponse est contrôlée. La récompense apparaît dans « Mes récompenses ».")),
            C("info", "Aucune carte", "Les réponses aux études n'apparaissent sur aucune carte de l'application : "
                                      "elles vont uniquement à l'organisation qui les a demandées, sous pseudonyme."),
            F(("Études max. par jour", f"{sts.ST_MAX_PER_DAY}"), ("Invitations max. par semaine", f"{sts.STT_INVITE_MAX_PER_USER_WEEK}"),
              ("Validité d'une invitation", f"{sts.STT_INVITE_TTL_DAYS} jours"))),
        sec("etudes-ciblage", "gps_fixed", "#0891B2", "Pourquoi cette étude m'est proposée",
            "Un calcul de correspondance, jamais une décision sur vous.",
            P("Chaque étude fixe des critères (zone, âge, activité, niveau d'études, usages numériques…). "
              "L'application compare ces critères à votre **profil dérivé** : compte, réponses aux enquêtes, activité, position récente "
              "et profil « Études » que vous remplissez vous-même."),
            L("Vous recevez l'invitation seulement si vous avez autorisé **« Études »** dans les paramètres.",
              "Votre profil « Études » sert uniquement au ciblage : il n'est jamais transmis tel quel.",
              "Quand une information manque, l'étude peut être proposée ou non selon le choix de l'organisation.",
              f"Les invitations sont triées par score de correspondance ; fiabilité du compte minimale possible."),
            FAQ(("Pourquoi je ne vois aucune étude ?", "Vérifiez l'autorisation « Études », complétez votre profil et gardez la position activée."),
                ("Est-ce que l'organisation connaît mon nom ?", "Non : elle voit un pseudonyme et des tranches (âge, ville…) seulement si l'étude l'annonce."))),
        sec("etudes-recompenses", "card_giftcard", "#DB2777", "Récompenses des études",
            "Ce que vous pouvez gagner, comment, et quand c'est refusé ou perdu.",
            P("Chaque étude indique sa récompense : " + rewards + ". Elle peut être **garantie** (à chaque participation valide) "
              "ou obtenue par **tirage au sort**."),
            H("Comment l'obtenir"),
            L("Terminer le questionnaire avant la clôture.", "Avoir des réponses jugées fiables (score minimum indiqué sur la fiche).",
              "Répondre correctement aux questions de contrôle d'attention quand il y en a.",
              "Une seule participation par personne."),
            H("Refus et perte"),
            L("Refus : réponses trop rapides, au hasard, copiées, contrôle manqué, budget épuisé, conditions de compte non remplies.",
              "Perte : réponses invalidées après vérification, fraude, ou coordonnées non fournies dans le délai.",
              "Le motif est toujours affiché dans « Mes récompenses »."),
            H("Remise"),
            L("Points et crédits : ajoutés automatiquement.", "Bons : le code s'affiche dans l'application.",
              "Argent, crédit téléphonique, cadeaux : l'organisation vous demande un numéro (Wave, Orange Money…) à renseigner dans le délai indiqué."),
            F(("Délai de réclamation par défaut", f"{sts.ST_CLAIM_DEFAULT_DAYS} jours"),
              ("Rétention des numéros transmis", f"{sts.ST_CONTACT_RETENTION_DAYS} jours"),
              ("Identité détachée après clôture", f"{sts.ST_IDENTITY_UNLINK_DAYS_AFTER_CLOSE} jours"))),
    ]