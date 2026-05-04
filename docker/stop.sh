#!/bin/bash
# ─────────────────────────────────────────────
# Redwood Plus — arrêt
# ─────────────────────────────────────────────
YLW='\033[1;33m'
GRN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YLW}Arrêt de Redwood Plus...${NC}"
docker compose down
echo -e "${GRN}✓ Tous les services sont arrêtés.${NC}"
echo ""
echo "  Les données (PostgreSQL, Redis) sont conservées dans les volumes."
echo -e "  Tout supprimer (données comprises) : ${RED}docker compose down -v${NC}"
