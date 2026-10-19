# KBW B2 Monitor

Surveillance des disponibilités d’examens TELC B2, avec un tableau de bord
Streamlit, un historique en base de données et des alertes Telegram.

## Fonctionnement

- Le monitor interroge l’API pour cinq catégories d’examens.
- Pour le rattrapage écrit de Yaoundé, il utilise Playwright et Chromium pour
  lire les disponibilités dans la page.
- Les sessions et leurs changements de disponibilité sont enregistrés dans
  SQLite en local ou dans PostgreSQL/Supabase si `SUPABASE_DB_URL` est défini.
- Telegram envoie une alerte lorsqu’une session passe à « disponible ».
- Le tableau de bord affiche les sessions et l’historique des changements.

Une première observation d’une session initialise son état dans la base ; elle
ne déclenche pas d’alerte. Les vérifications suivantes enregistrent les
transitions détectées. La détection peut survenir au prochain passage du
scheduler.

## Démarrage local

Le workflow GitHub utilise Python 3.12. Pour reproduire cet environnement :

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Renseignez les variables nécessaires dans `.env` (voir la section
[Configuration](#configuration)). Le fichier `.env` est ignoré par Git :
ne publiez jamais ses valeurs.

Dans un terminal, lancez une vérification ponctuelle :

```bash
python -m monitor
```

Dans un autre terminal, démarrez le tableau de bord :

```bash
streamlit run app.py
```

Pour installer également les dépendances système de Chromium sur une
distribution Linux compatible, utilisez :

```bash
python -m playwright install --with-deps chromium
```

## Configuration

| Variable | Utilisation |
| --- | --- |
| `SUPABASE_DB_URL` | URL de connexion PostgreSQL/Supabase. Sans cette variable, le monitor utilise `data/examens_b2_monitor.db`. |
| `KBW_B2_MONITOR_DB` | Chemin facultatif de la base SQLite locale. Sans Supabase, le chemin par défaut est `data/examens_b2_monitor.db`. |
| `TELEGRAM_BOT_TOKEN` | Token du bot Telegram. Nécessaire pour envoyer des alertes. |
| `TELEGRAM_CHAT_ID` | Identifiant du chat destinataire des alertes. |

Pour le déploiement Streamlit, configurez `SUPABASE_DB_URL` dans les secrets de
l’application afin que le tableau de bord et le monitor GitHub utilisent la
même base. Ajoutez également `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` si les
vérifications manuelles depuis le tableau de bord doivent envoyer des alertes.

## Exécutions automatiques

Le workflow `.github/workflows/monitor.yml` lance le monitor toutes les
15 minutes (aux minutes UTC `3`, `18`, `33` et `48`) et peut aussi être lancé
manuellement depuis GitHub Actions. Il installe les dépendances Python et
Chromium avant d’exécuter `python -m monitor`.

Configurez ces secrets dans les paramètres GitHub Actions du dépôt :

- `SUPABASE_DB_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Un scheduler externe tel que cron-job.org peut aussi déclencher le workflow via
l’API GitHub. Si le scheduler externe et le déclencheur `schedule` de GitHub
sont tous deux actifs, ils peuvent provoquer des vérifications rapprochées ou
des exécutions en doublon.

## Déploiement Streamlit

Déployez `app.py` sur Streamlit Community Cloud et configurez
`SUPABASE_DB_URL` dans les secrets de l’application pour partager les données
avec le workflow GitHub Actions. Les dépendances Python sont listées dans
`requirements.txt` et les paquets système nécessaires à Chromium dans
`packages.txt`.

## Sécurité

Ne commitez jamais `.env`, les tokens Telegram, l’URL Supabase contenant son
mot de passe, ni un token d’accès GitHub. Stockez ces valeurs dans les secrets
du service qui exécute le code. Si un token est exposé, révoquez-le et
remplacez-le.
