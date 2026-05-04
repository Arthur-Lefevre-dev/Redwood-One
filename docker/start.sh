#!/bin/bash
# ─────────────────────────────────────────────
# Redwood Plus — script de démarrage v7
# ─────────────────────────────────────────────
set -e

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLD='\033[1m'
NC='\033[0m'

echo -e "${RED}"
echo "  ██████╗ ███████╗██████╗ ██╗    ██╗ ██████╗  ██████╗ ██████╗ "
echo "  ██╔══██╗██╔════╝██╔══██╗██║    ██║██╔═══██╗██╔═══██╗██╔══██╗"
echo "  ██████╔╝█████╗  ██║  ██║██║ █╗ ██║██║   ██║██║   ██║██║  ██║"
echo "  ██╔══██╗██╔══╝  ██║  ██║██║███╗██║██║   ██║██║   ██║██║  ██║"
echo "  ██║  ██║███████╗██████╔╝╚███╔███╔╝╚██████╔╝╚██████╔╝██████╔╝"
echo "  ╚═╝  ╚═╝╚══════╝╚═════╝  ╚══╝╚══╝  ╚═════╝  ╚═════╝ ╚═════╝ Plus"
echo -e "${NC}"

# ── 1. Vérifications ──────────────────────────────────────

echo -e "${YLW}[1/6] Vérification de l'environnement...${NC}"

if [ ! -f ".env" ]; then
    echo -e "${RED}✗ Fichier .env introuvable.${NC}"
    echo "  → Copie .env.example en .env et remplis les valeurs."
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo -e "${RED}✗ Docker n'est pas installé.${NC}"
    exit 1
fi

if ! docker compose version &>/dev/null; then
    echo -e "${RED}✗ Docker Compose v2 requis (docker compose, pas docker-compose).${NC}"
    exit 1
fi

# Vérifier que les valeurs par défaut ont été remplacées
source .env
DEFAULTS=("CHANGE_MOI_postgres" "CHANGE_MOI_redis" "CHANGE_MOI_cle_secrete" "CHANGE_MOI_mdp_admin")
for d in "${DEFAULTS[@]}"; do
    if grep -q "$d" .env; then
        echo -e "${RED}✗ Valeurs par défaut encore présentes dans .env (chercher : $d).${NC}"
        exit 1
    fi
done

echo -e "${GRN}  ✓ Environnement OK${NC}"

# ── 2. Détection GPU ─────────────────────────────────────

echo -e "${YLW}[2/6] Détection GPU...${NC}"

GPU_INFO="Aucun GPU détecté — transcodage CPU (lent)"
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    GPU_INFO="NVIDIA ${GPU_NAME} — encodeur h264_nvenc / hevc_nvenc"
elif lspci 2>/dev/null | grep -qi "amd\|radeon"; then
    GPU_INFO="AMD détecté — encodeur h264_amf / hevc_amf"
elif ls /dev/dri/renderD* &>/dev/null; then
    GPU_INFO="Intel Quick Sync détecté — encodeur h264_qsv / hevc_qsv"
fi

echo -e "${GRN}  ✓ $GPU_INFO${NC}"
echo "  (gpu_detect.py effectuera la détection définitive au démarrage)"

# ── 3. Build ──────────────────────────────────────────────

echo -e "${YLW}[3/6] Build des images Docker...${NC}"
docker compose build --parallel
echo -e "${GRN}  ✓ Images construites${NC}"

# ── 4. Démarrage des services ─────────────────────────────

echo -e "${YLW}[4/6] Démarrage des services...${NC}"
docker compose up -d postgres redis
echo "  → Attente PostgreSQL et Redis..."

# Attendre que les healthchecks passent (max 60s)
WAIT=0
until docker compose ps postgres | grep -q "healthy" && \
      docker compose ps redis    | grep -q "healthy"; do
    sleep 2; WAIT=$((WAIT+2))
    if [ $WAIT -ge 60 ]; then
        echo -e "${RED}✗ Timeout : PostgreSQL ou Redis ne démarre pas.${NC}"
        docker compose logs postgres redis
        exit 1
    fi
done

docker compose up -d api worker beat flower nginx
echo -e "${GRN}  ✓ Tous les services démarrés${NC}"

# ── 5. Schéma base de données ───────────────────────────────

echo -e "${YLW}[5/6] Initialisation du schéma (API lifespan / SQLAlchemy)...${NC}"
sleep 3
docker compose exec api python -c "from db.session import init_db; init_db()"
echo -e "${GRN}  ✓ Tables prêtes${NC}"

# ── 6. Seed admin ─────────────────────────────────────────

echo -e "${YLW}[6/6] Création du compte admin...${NC}"
docker compose exec api python scripts/seed_admin.py
echo -e "${GRN}  ✓ Compte admin prêt${NC}"

# ── Résumé ────────────────────────────────────────────────

echo ""
echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLD}  Redwood Plus est démarré !${NC}"
echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Site (visionnage)  →  http://localhost"
echo "  Interface admin    →  http://localhost/admin/"
echo "  Flower (Celery)    →  http://localhost:5555"
echo ""
echo -e "  GPU utilisé        →  ${BLD}${GPU_INFO}${NC}"
echo "  Login admin        →  $ADMIN_USERNAME"
echo ""
echo -e "  Arrêter            →  ${YLW}./stop.sh${NC}"
echo -e "  Logs               →  ${YLW}docker compose logs -f${NC}"
echo -e "  Logs worker        →  ${YLW}docker compose logs -f worker${NC}"
echo ""
