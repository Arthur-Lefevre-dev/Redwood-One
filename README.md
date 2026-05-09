# Redwood Plus (Redwood-One)

Plateforme de **catalogue et diffusion vidéo** pour un usage privé ou restreint : ingestion de fichiers (upload, torrents), traitement **ffmpeg** (transcodage ou envoi direct selon le format), stockage **S3 compatible** (ex. OVH Object Storage), métadonnées **TMDB**, interface **spectateur** et **administration** séparées.

## Fonctionnalités principales

- **Pipeline vidéo** : analyse `ffprobe`, décision transcodage / upload direct, encodage matériel optionnel (AMD VAAPI, NVIDIA, Intel QSV, CPU).
- **Stockage S3** : clés du type `films/{id}/…` ; synchronisation catalogue → base via l’admin (`POST /api/admin/catalog/sync-s3`).
- **Catalogue** : films et **séries** (épisodes regroupés par `series_key`) ; lecture via `/watch/film.html` et `/watch/serie.html`.
- **Authentification** : JWT dans cookies httpOnly ; rôles **admin** et **viewer**.
- **Spectateurs** : inscription avec **code d’invitation** (ou ouverture publique en dev via `REGISTRATION_OPEN=true`), recherche, derniers ajouts, navigation par genres, suggestion **« Choisir pour moi »** selon les genres favoris ; page **Paramètres** (`/watch/settings.html`) pour le profil, les préférences et les **codes d’invitation générés par le membre** (quota **d’un code par mois calendaire UTC** ; voir aussi `core/member_invites.py`).
- **Admin** : upload, torrents, file d’attente Celery, utilisateurs, **codes d’invitation** (admin + suivi des codes membres), **réinitialisation du quota mensuel d’invitation** d’un utilisateur, sync S3 ; confirmations et erreurs affichées dans des **modales** (plus de `alert` / `confirm` navigateur).

## Stack technique

| Composant | Rôle |
|-----------|------|
| **FastAPI** (`api/main.py`) | API REST, `init_db()` au démarrage |
| **PostgreSQL** | Films, utilisateurs, invitations, jetons de rafraîchissement |
| **Redis** | Broker Celery |
| **Celery** (`worker`, `beat`) | Tâches asynchrones (pipeline, torrents) |
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
   - Catalogue spectateur : `/watch/` — paramètres / invitations membre : `/watch/settings.html`
   - API santé : `GET /api/health`
   - Flower : **http://localhost:5555** (auth basique selon `FLOWER_USER` / `FLOWER_PASSWORD`)

### GPU (transcodage)

Le worker peut utiliser un GPU selon l’environnement. Voir les commentaires dans [docker/docker-compose.yml](docker/docker-compose.yml) (`/dev/dri` pour AMD, réservations NVIDIA, etc.) et la variable `REDWOOD_GPU_VENDOR` dans `env.example`.

## Développement local (sans Docker)

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS

pip install -r requirements.txt
```

Configurer `.env` à la racine (même variables que dans `docker/env.example`, avec `DATABASE_URL` / `REDIS_URL` pointant vers vos services locaux).

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
api/           # FastAPI — routes auth, films, séries, admin
core/          # Pipeline vidéo, S3, TMDB, détection GPU, sync catalogue, invitations membres
db/            # Modèles SQLAlchemy, session
worker/        # Tâches Celery
frontend/      # HTML/JS/CSS — login, register, admin, watch (dont settings)
docker/        # Dockerfiles, compose, env.example
nginx/         # Configuration reverse proxy
scripts/       # seed_admin.py, scripts SQL optionnels (migrations manuelles)
```

### Schéma PostgreSQL (mises à jour)

`init_db()` crée ou complète les tables au démarrage de l’API. Pour appliquer à la main des changements documentés (ou en secours), des scripts SQL sont fournis sous `scripts/` — par exemple `add_user_last_invite_at_postgres.sql`, `add_invitation_created_by_postgres.sql`, `add_series_columns_postgres.sql`, `add_trailers_manual_postgres.sql` (voir les en-têtes de chaque fichier).

## API (résumé)

| Préfixe | Usage |
|---------|--------|
| `/api/auth/*` | Login, register, refresh, logout ; profil `GET`/`PATCH /me`, préférences `PATCH /me/preferences` ; invitations membre `POST /me/invite` (alias `POST /member-invite`) et état quota dans les réponses `/me` |
| `/api/films/*` | Liste, featured, latest, genres, surprise-me, détail, URL de lecture |
| `/api/series/*` | Liste des séries, détail par `series_key` (saisons / épisodes) |
| `/api/admin/*` | Films, upload, torrents, file d’attente, sync S3, invitations, utilisateurs, `POST .../users/{id}/reset-invite-monthly-quota` (protégé admin) |

Documentation interactive Swagger : `http://localhost:8000/docs` si vous exposez le port de l’API (ex. `docker compose port api 8000` ou profil de debug). Avec la config nginx fournie, seul le préfixe `/api/` est proxifié vers FastAPI : les routes `/docs` et `/openapi.json` ne sont pas servies sur le port 80 tant qu’elles ne sont pas ajoutées à nginx.

## Sécurité et production

- Ne jamais commiter `docker/.env` ni un `.env` contenant des secrets.
- Remplacer `SECRET_KEY`, mots de passe forts, HTTPS (certificats dans `nginx/certs/` si vous activez le bloc TLS).
- `REGISTRATION_OPEN=false` en production pour exiger un code d’invitation valide à l’inscription.

## Licence et support

Projet interne / privé — adaptez cette section selon votre politique de dépôt.
