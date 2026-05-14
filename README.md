# Redwood Plus (Redwood-One)

Plateforme de **catalogue et diffusion vidéo** pour un usage privé ou restreint : ingestion de fichiers (upload, torrents), traitement **ffmpeg** (transcodage ou envoi direct selon le format), stockage **S3 compatible** (ex. OVH Object Storage), métadonnées **TMDB**, interface **spectateur** et **administration** séparées.

## Fonctionnalités principales

- **Pipeline vidéo** : analyse `ffprobe`, décision transcodage / upload direct, encodage matériel optionnel (AMD VAAPI, NVIDIA, Intel QSV, CPU).
- **Stockage S3** : clés du type `films/{id}/…` ; synchronisation catalogue → base via l’admin (`POST /api/admin/catalog/sync-s3`).
- **Catalogue** : films et **séries** (épisodes regroupés par `series_key`) ; lecture via `/watch/film.html` et `/watch/serie.html`.
- **Authentification** : JWT dans cookies httpOnly ; rôles **admin** et **viewer**.
- **Spectateurs** : inscription avec **code d’invitation** (ou ouverture publique en dev via `REGISTRATION_OPEN=true`), recherche, derniers ajouts, navigation par genres, suggestion **« Choisir pour moi »** selon les genres favoris ; page **Paramètres** (`/watch/settings.html`) pour le profil, les préférences et les **codes d’invitation générés par le membre** (quota **d’un code par mois calendaire UTC** ; voir aussi `core/member_invites.py`).
- **Tickets support** : page spectateur **`/watch/support.html`** (ouverture de ticket, catégories, fil de discussion) ; console admin **Tickets support** (liste filtrée, réponse publique, changement de statut, clôture). Le panneau détail peut être refermé avec la **croix** sans modifier le statut du ticket.
- **Dons crypto** : configuration et suivi des adresses (Bitcoin, Tron, Polygon, etc.), objectif de campagne et rafraîchissement des soldes côté admin ; progression pour spectateur connecté via `GET /api/donations/progress`.
- **Transcodage cloud (Vast.ai)** : optionnel — cible **Vast** sur un titre, recherche d’offres GPU et suivi depuis l’admin (file Celery, **ID d’instance Vast** dans les détails repliables) ; variables `VAST_*` dans `docker/env.example`.
- **Admin** : upload, torrents, file d’attente Celery, utilisateurs, **codes d’invitation** (admin + suivi des codes membres), **réinitialisation du quota mensuel d’invitation** d’un utilisateur, sync S3, **bibliothèque** (films + épisodes de série regroupés) avec **pagination par série** (jusqu’à 10 blocs « show » par page, tous les épisodes de ces séries chargés ensemble) ; confirmations et erreurs affichées dans des **modales** (plus de `alert` / `confirm` navigateur).

## Stack technique

| Composant | Rôle |
|-----------|------|
| **FastAPI** (`api/main.py`) | API REST, `init_db()` au démarrage |
| **PostgreSQL** | Films, utilisateurs, invitations, jetons de rafraîchissement, tickets support, paramètres de dons |
| **Redis** | Broker Celery |
| **Celery** (`worker`, `beat`) | Tâches asynchrones (pipeline, torrents, relance auto torrent, dons) |
| **Flower** | Monitoring des workers (port **5555** en Docker) |
| **nginx** | Fichiers statiques du `frontend/`, reverse proxy `/api/` vers l’API |

## Prérequis

- **Docker** et **Docker Compose** pour le déploiement recommandé.
- Pour le développement local sans Docker : Python **3.11+**, PostgreSQL, Redis, ffmpeg.

## Démarrage rapide (Docker)

1. **Variables d’environnement**

   ```bash
   cp docker/env.example docker/.env
   ```

   Éditer `docker/.env` : mots de passe Postgres/Redis, `SECRET_KEY`, `TMDB_API_KEY`, identifiants S3, `ALLOWED_ORIGINS`, etc. Détails dans [docker/env.example](docker/env.example).

   **Docker Compose** : dans les conteneurs, Redis et Postgres sont joignables par les **noms de services** (`REDIS_HOST=redis`, hôte `postgres` dans `DATABASE_URL`). Ne pas utiliser `localhost` pour le broker depuis `worker` / `beat`. Les mots de passe avec des caractères réservés dans une URL (ex. `#`) doivent être **encodés en pourcent** dans `DATABASE_URL` (voir commentaires dans `docker/env.example`).

2. **Lancer la stack** (depuis le dossier `docker/`)

   ```bash
   cd docker
   docker compose up -d --build
   ```

3. **Créer le compte admin** (une fois les conteneurs prêts)

   ```bash
   docker compose exec api python scripts/seed_admin.py
   ```

4. **Accès**

   - Site : **http://localhost** (nginx)
   - Connexion spectateur : `/login.html` — inscription : `/register.html`
   - Connexion admin uniquement : `/login-admin.html` — console : `/admin/`
   - Catalogue spectateur : `/watch/` — paramètres / invitations membre : `/watch/settings.html` — support : `/watch/support.html`
   - API santé : `GET /api/health`
   - Flower : **http://localhost:5555** (auth basique selon `FLOWER_USER` / `FLOWER_PASSWORD`)

### HTTPS en production (VPS)

- Fichier dédié : [nginx/nginx.production.conf](nginx/nginx.production.conf) (HTTP : challenge ACME + redirection HTTPS, TLS sur `:443`).
- Dans [docker/docker-compose.yml](docker/docker-compose.yml), remplacer le volume Nginx `nginx.conf` par **`nginx.production.conf`** (même cible `/etc/nginx/nginx.conf`) — voir le commentaire au-dessus de la ligne dans le compose.
- Certificats : répertoire `nginx/certs/` monté dans le conteneur (`fullchain.pem`, `privkey.pem`).

### Page maintenance et pile arrêtée

- **API indisponible** : nginx sert [frontend/maintenance.html](frontend/maintenance.html) sur les erreurs **502/503/504** du proxy `/api/` (fichier servi avec le volume `frontend/`).
- **Stack principale arrêtée mais Docker actif** : [docker/docker-compose.maintenance.yml](docker/docker-compose.maintenance.yml) — un seul service nginx sur le port **80** (libérer le port avant : `docker compose down` sur la stack principale).

### Nettoyage `/tmp/redwood` après incident

Script : [scripts/cleanup_redwood_tmp.sh](scripts/cleanup_redwood_tmp.sh) — répertoires aria2 `torrents/job_*`, optionnellement `torrent_blobs/*.torrent` et le staging `uploads/` (`--yes` / `--dry-run`, voir l’en-tête du script).

```bash
cd docker
# Worker arrêté : conteneur ponctuel (même volume tmp_data)
docker compose run --rm worker bash /app/scripts/cleanup_redwood_tmp.sh --dry-run --all
docker compose run --rm worker bash /app/scripts/cleanup_redwood_tmp.sh --yes --all
```

### Relances automatiques des torrents

Si `TORRENT_AUTO_RETRY_ENABLED=true` (défaut, voir `docker/env.example`), **Celery Beat** relance périodiquement les téléchargements torrent en erreur récupérables (limite `TORRENT_AUTO_RETRY_MAX`). Règles : `core/torrent_auto_retry.py`.

### GPU (transcodage)

Le worker peut utiliser un **GPU local** selon l’environnement. Voir les commentaires dans [docker/docker-compose.yml](docker/docker-compose.yml) (`/dev/dri` pour AMD, réservations NVIDIA, etc.) et la variable `REDWOOD_GPU_VENDOR` dans `env.example`. Pour un transcodage **délégué sur machine louée**, configurer plutôt **Vast.ai** (`VAST_API_KEY`, `VAST_MAX_DPH_PER_HOUR`, etc. dans `docker/env.example`).

## Développement local (sans Docker)

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS

pip install -r requirements.txt
```

Configurer `.env` à la racine (mêmes variables que dans `docker/env.example`) : `DATABASE_URL` vers PostgreSQL local, et pour Celery **`REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD`** (l’application construit l’URL Redis ; une ligne `REDIS_URL` seule dans `.env` n’est pas utilisée par le code Redwood).

```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Les pages HTML du dossier `frontend/` sont pensées pour être servies par nginx en production ; en local, ouvrez les fichiers ou utilisez un serveur statique sur le même origin que l’API pour les cookies.

## Tests

```bash
pytest
```

## Structure du dépôt (aperçu)

```
api/           # FastAPI — auth, films, séries, admin, support_tickets, donations, announcement
core/          # Pipeline vidéo, S3, TMDB, GPU, sync catalogue, invitations membres, dons, libellés admin séries
db/            # Modèles SQLAlchemy, session
worker/        # Tâches Celery
frontend/      # HTML/JS/CSS — login, register, admin, watch (catalogue, settings, support)
docker/        # Dockerfiles, compose, env.example
nginx/         # nginx.conf (HTTP local), nginx.production.conf (HTTPS), maintenance-standalone pour compose maintenance
scripts/       # seed_admin.py, cleanup_redwood_tmp.sh, scripts SQL optionnels (migrations manuelles)
```

### Schéma PostgreSQL (mises à jour)

`init_db()` crée ou complète les tables au démarrage de l’API. Pour appliquer à la main des changements documentés (ou en secours), des scripts SQL sont fournis sous `scripts/` — par exemple `add_user_last_invite_at_postgres.sql`, `add_invitation_created_by_postgres.sql`, `add_series_columns_postgres.sql`, `add_trailers_manual_postgres.sql` (voir les en-têtes de chaque fichier).

## API (résumé)

| Préfixe | Usage |
|---------|--------|
| `/api/auth/*` | Login, register, refresh, logout ; profil `GET`/`PATCH /me`, préférences `PATCH /me/preferences` ; invitations membre `POST /me/invite` (alias `POST /member-invite`) et état quota dans les réponses `/me` |
| `/api/films/*` | Liste, featured, latest, genres, surprise-me, détail, URL de lecture |
| `/api/series/*` | Liste des séries, détail par `series_key` (saisons / épisodes) |
| `/api/support-tickets/*` | Spectateur connecté : création de ticket, liste, détail, réponses |
| `/api/donations/progress` | Spectateur connecté : objectif, avancement, fenêtre de campagne, adresses de dépôt (lecture seule) |
| `/api/public/watch-ads` | JSON sans auth : **A-ADS** optionnel — `aads` (page film), `aads_auth` (connexion / inscription) ; variables `WATCH_ADS_AADS_*` et `WATCH_ADS_AADS_AUTH_*` |
| `/api/admin/*` | Ressources admin : catalogue / films, upload, torrents, file Celery, sync S3, utilisateurs, invitations, estimation coûts **Vast**, dons crypto, **tickets support** ; `GET /api/admin/library-meta` (totaux bibliothèque). Pour les épisodes : `GET /api/admin/films?content_kind=series_episode&paginate_by=series_show` pagine par **bloc série** (défaut `episode` = dix lignes épisode). |

Documentation interactive Swagger : `http://localhost:8000/docs` si vous exposez le port de l’API (ex. `docker compose port api 8000` ou profil de debug). Avec la config nginx fournie, seul le préfixe `/api/` est proxifié vers FastAPI : les routes `/docs` et `/openapi.json` ne sont pas servies sur le port 80 tant qu’elles ne sont pas ajoutées à nginx.

## Sécurité et production

- Ne jamais commiter `docker/.env` ni un `.env` contenant des secrets.
- Remplacer `SECRET_KEY`, mots de passe forts, HTTPS (certificats dans `nginx/certs/` si vous activez le bloc TLS).
- `REGISTRATION_OPEN=false` en production pour exiger un code d’invitation valide à l’inscription.

## Licence et support

Projet interne / privé — adaptez cette section selon votre politique de dépôt.
