#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  Redwood Plus — Setup serveur de production
#  OS      : Debian 12 (Bookworm)
#  GPU     : AMD (VAAPI via /dev/dri)
#  HTTPS   : Non (IP uniquement)
#
#  Usage   : sudo bash setup-server.sh
#  Repo custom : sudo GITHUB_REPO=https://github.com/toi/redwood.git \
#                     bash setup-server.sh
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; BLD='\033[1m'; NC='\033[0m'

OK()   { echo -e "${GRN}  [✓] $*${NC}"; }
STEP() { echo -e "\n${YLW}${BLD}━━━ $* ${NC}"; }
INFO() { echo -e "${BLU}  [i] $*${NC}"; }
FAIL() { echo -e "${RED}  [✗] $*${NC}"; exit 1; }
WARN() { echo -e "${YLW}  [!] $*${NC}"; }

echo -e "${RED}"
cat << 'EOF'
  ██████╗ ███████╗██████╗ ██╗    ██╗ ██████╗  ██████╗ ██████╗
  ██╔══██╗██╔════╝██╔══██╗██║    ██║██╔═══██╗██╔═══██╗██╔══██╗
  ██████╔╝█████╗  ██║  ██║██║ █╗ ██║██║   ██║██║   ██║██║  ██║
  ██╔══██╗██╔══╝  ██║  ██║██║███╗██║██║   ██║██║   ██║██║  ██║
  ██║  ██║███████╗██████╔╝╚███╔███╔╝╚██████╔╝╚██████╔╝██████╔╝
  ╚═╝  ╚═╝╚══════╝╚═════╝  ╚══╝╚══╝  ╚═════╝  ╚═════╝ ╚═════╝ Plus
EOF
echo -e "${NC}"
echo -e "  Setup serveur de production — Debian 12 / AMD GPU / HTTP\n"

# ── Variables ───────────────────────────────────────────────────────
GITHUB_REPO="${GITHUB_REPO:-}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
PROJ_DIR="/opt/redwood"
SERVER_IP=$(hostname -I | awk '{print $1}')

# ── Vérifications root + OS ─────────────────────────────────────────
[ "$EUID" -ne 0 ]              && FAIL "Lancer avec : sudo bash setup-server.sh"
[ ! -f /etc/debian_version ]   && FAIL "Debian 12 requis."
[ "$(cut -d. -f1 /etc/debian_version)" -lt 12 ] && FAIL "Debian 12 minimum requis."

INFO "IP serveur  : $SERVER_IP"
INFO "Dossier     : $PROJ_DIR"
INFO "Début       : $(date)"


# ════════════════════════════════════════════════════════════════════
# 1 — Mise à jour système
# ════════════════════════════════════════════════════════════════════
STEP "1/9 — Mise à jour du système"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git unzip ca-certificates gnupg \
    lsb-release apt-transport-https \
    htop iotop ncdu tree \
    ufw fail2ban \
    pciutils vainfo mesa-va-drivers mesa-vdpau-drivers \
    libva2 libva-drm2 libdrm-amdgpu1 \
    ffmpeg

OK "Système à jour"


# ════════════════════════════════════════════════════════════════════
# 2 — Sécurité (UFW + Fail2ban)
# ════════════════════════════════════════════════════════════════════
STEP "2/9 — Pare-feu UFW + Fail2ban"

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow http
ufw allow 5555/tcp
ufw --force enable
OK "UFW : SSH(22), HTTP(80), Flower(5555)"

cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5
[sshd]
enabled = true
EOF
systemctl enable fail2ban && systemctl restart fail2ban
OK "Fail2ban actif"


# ════════════════════════════════════════════════════════════════════
# 3 — Drivers AMD GPU (VAAPI)
# ════════════════════════════════════════════════════════════════════
STEP "3/9 — Drivers AMD GPU (VAAPI)"

if lspci | grep -qi "amd\|radeon"; then
    INFO "GPU AMD détecté : $(lspci | grep -i 'amd\|radeon' | head -1)"
    groupadd -f video 2>/dev/null || true
    groupadd -f render 2>/dev/null || true
    chmod 660 /dev/dri/* 2>/dev/null || true
    chgrp video  /dev/dri/card*   2>/dev/null || true
    chgrp render /dev/dri/render* 2>/dev/null || true
    cat > /etc/udev/rules.d/99-dri-permissions.rules << 'EOF'
SUBSYSTEM=="drm", GROUP="video", MODE="0660"
SUBSYSTEM=="drm", KERNEL=="renderD*", GROUP="render", MODE="0660"
EOF
    udevadm control --reload-rules 2>/dev/null || true
    OK "GPU AMD prêt — encodeurs : h264_vaapi / hevc_vaapi"
else
    WARN "Aucun GPU AMD détecté — le transcodage utilisera le CPU"
fi


# ════════════════════════════════════════════════════════════════════
# 4 — Docker + Docker Compose v2
# ════════════════════════════════════════════════════════════════════
STEP "4/9 — Installation Docker"

if command -v docker &>/dev/null; then
    WARN "Docker déjà présent : $(docker --version)"
else
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable docker && systemctl start docker
    OK "Docker $(docker --version | cut -d' ' -f3 | tr -d ',') installé"
fi
docker compose version &>/dev/null || FAIL "Docker Compose v2 manquant."
OK "Docker Compose $(docker compose version --short) prêt"


# ════════════════════════════════════════════════════════════════════
# 5 — Préparation dossier projet
# ════════════════════════════════════════════════════════════════════
STEP "5/9 — Préparation du dossier projet"
mkdir -p "$PROJ_DIR"
OK "Dossier $PROJ_DIR prêt"


# ════════════════════════════════════════════════════════════════════
# 6 — Clone / pull depuis GitHub
# ════════════════════════════════════════════════════════════════════
STEP "6/9 — Récupération du code depuis GitHub"

if [ -z "$GITHUB_REPO" ]; then
    echo ""
    read -rp "  URL du repo GitHub : " GITHUB_REPO
    echo ""
fi
[ -z "$GITHUB_REPO" ] && FAIL "Aucun repo GitHub fourni."

if [ -d "$PROJ_DIR/.git" ]; then
    # Repo déjà présent → simple pull
    WARN "Repo déjà cloné — git pull"
    cd "$PROJ_DIR" && git pull origin "$GITHUB_BRANCH"
    OK "Code mis à jour"
else
    # Repo privé : proposer un Personal Access Token
    echo ""
    read -rp "  Repo privé ? Coller un Personal Access Token (Entrée si public) : " GH_TOKEN
    if [ -n "$GH_TOKEN" ]; then
        GITHUB_REPO=$(echo "$GITHUB_REPO" | sed "s|https://|https://${GH_TOKEN}@|")
        INFO "Token GitHub injecté"
    fi

    git clone --branch "$GITHUB_BRANCH" --depth 1 "$GITHUB_REPO" "$PROJ_DIR"
    OK "Repo cloné dans $PROJ_DIR (branche : $GITHUB_BRANCH)"
fi

cd "$PROJ_DIR"


# ════════════════════════════════════════════════════════════════════
# 7 — Configuration .env
# ════════════════════════════════════════════════════════════════════
STEP "7/9 — Configuration .env"

if [ -f "$PROJ_DIR/.env" ]; then
    WARN ".env déjà présent — non écrasé."
else
    if [ -f "$PROJ_DIR/.env.example" ]; then
        cp "$PROJ_DIR/.env.example" "$PROJ_DIR/.env"
        sed -i "s|VOTRE_IP|$SERVER_IP|g" "$PROJ_DIR/.env"
        OK ".env créé depuis .env.example"
    else
        WARN ".env.example introuvable dans le repo."
        touch "$PROJ_DIR/.env"
    fi

    echo ""
    echo -e "  ${YLW}${BLD}⚠  Remplis le fichier .env avant de continuer.${NC}"
    echo ""
    echo "  Ouvre-le avec :  nano $PROJ_DIR/.env"
    echo ""
    echo "  Valeurs obligatoires :"
    echo "    POSTGRES_PASSWORD   REDIS_PASSWORD   SECRET_KEY"
    echo "    ADMIN_USERNAME      ADMIN_PASSWORD   ADMIN_EMAIL"
    echo "    TMDB_API_KEY"
    echo "    S3_ACCESS_KEY       S3_SECRET_KEY    S3_BUCKET_NAME"
    echo "    FLOWER_PASSWORD"
    echo ""
    read -rp "  Appuie sur [Entrée] quand le .env est rempli..." _
fi

# Vérification valeurs par défaut non remplacées
if grep -q "CHANGE_MOI" "$PROJ_DIR/.env" 2>/dev/null; then
    echo ""
    FAIL ".env contient encore des 'CHANGE_MOI'. Modifie-les et relance."
fi
OK ".env validé"


# ════════════════════════════════════════════════════════════════════
# 8 — Build + démarrage Docker
# ════════════════════════════════════════════════════════════════════
STEP "8/9 — Build et démarrage de la stack"

echo "  → Build des images..."
docker compose build --parallel
OK "Images construites"

echo "  → Démarrage PostgreSQL + Redis..."
docker compose up -d postgres redis

WAIT=0
until docker inspect --format='{{.State.Health.Status}}' redwood_postgres 2>/dev/null | grep -q "healthy" && \
      docker inspect --format='{{.State.Health.Status}}' redwood_redis    2>/dev/null | grep -q "healthy"; do
    sleep 3; WAIT=$((WAIT+3))
    [ $WAIT -ge 90 ] && docker compose logs postgres redis && FAIL "Timeout — PostgreSQL ou Redis ne démarre pas."
    echo -e "  ... attente ($WAIT s)"
done
OK "PostgreSQL + Redis prêts"

echo "  → Démarrage de tous les services..."
docker compose up -d
OK "Stack complète démarrée"

echo "  → Migrations Alembic..."
sleep 5
docker compose exec api python -m alembic upgrade head
OK "Migrations appliquées"

echo "  → Création du compte admin..."
docker compose exec api python scripts/seed_admin.py
OK "Compte admin créé"


# ════════════════════════════════════════════════════════════════════
# 9 — Service systemd (auto-start au boot)
# ════════════════════════════════════════════════════════════════════
STEP "9/9 — Démarrage automatique (systemd)"

cat > /etc/systemd/system/redwood.service << SYSTEMD
[Unit]
Description=Redwood Plus
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJ_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300
Restart=on-failure

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable redwood.service
OK "Service 'redwood' activé au démarrage"

# Script de mise à jour rapide
cat > "$PROJ_DIR/deploy.sh" << 'DEPLOY'
#!/bin/bash
# Redwood Plus — mise à jour depuis GitHub
set -e
cd /opt/redwood
echo "Pull GitHub..."
git pull
echo "Build..."
docker compose build --parallel
docker compose up -d
sleep 5
docker compose exec api python -m alembic upgrade head
echo "✓ Mise à jour terminée"
docker compose ps
DEPLOY
chmod +x "$PROJ_DIR/deploy.sh"


# ════════════════════════════════════════════════════════════════════
# Résumé final
# ════════════════════════════════════════════════════════════════════
ADMIN_USER=$(grep "^ADMIN_USERNAME=" "$PROJ_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "admin")

echo ""
echo -e "${RED}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLD}   Redwood Plus est en production !${NC}"
echo -e "${RED}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "   Site       →  ${GRN}http://$SERVER_IP${NC}"
echo -e "   Admin      →  ${GRN}http://$SERVER_IP/admin/${NC}"
echo -e "   Flower     →  ${GRN}http://$SERVER_IP:5555${NC}"
echo ""
echo    "   Login      →  $ADMIN_USER"
echo    "   Code       →  $PROJ_DIR"
echo    "   Repo       →  $GITHUB_REPO"
echo ""
echo -e "   ${BLD}Commandes utiles :${NC}"
echo    "     bash /opt/redwood/deploy.sh          # mettre à jour"
echo    "     docker compose logs -f worker        # logs transcodage"
echo    "     systemctl status redwood             # statut service"
echo ""
echo -e "   ${YLW}Vérification GPU AMD :${NC}"
echo    "     docker compose exec worker vainfo --display drm --device /dev/dri/renderD128"
echo    "     docker compose exec worker ffmpeg -hwaccels"
echo ""
