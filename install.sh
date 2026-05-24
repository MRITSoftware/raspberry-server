#!/bin/bash
set -e

echo "=== MRIT Server - Instalação no Raspberry Pi ==="

# Verificar Python 3
if ! command -v python3 &> /dev/null; then
    echo "[ERRO] Python 3 não encontrado. Instale com: sudo apt install python3 python3-pip"
    exit 1
fi

# Instalar dependências Python
echo "[1/4] Instalando dependências Python..."
pip3 install -r requirements.txt

# Criar .env a partir do exemplo se não existir
if [ ! -f .env ]; then
    echo "[2/4] Criando .env a partir do exemplo..."
    cp .env.example .env
    echo "      ATENÇÃO: edite o arquivo .env e defina SITE_NAME e ADMIN_TOKEN antes de continuar."
    echo "      Use: nano .env"
else
    echo "[2/4] Arquivo .env já existe, pulando."
fi

# Criar config.json a partir do exemplo se não existir
if [ ! -f config.json ]; then
    echo "[3/4] Criando config.json a partir do exemplo..."
    cp config.example.json config.json
else
    echo "[3/4] Arquivo config.json já existe, pulando."
fi

# Iniciar com PM2
echo "[4/4] Iniciando servidor com PM2..."
pm2 start ecosystem.config.js
pm2 save

echo ""
echo "=== Instalação concluída! ==="
echo "Comandos úteis:"
echo "  pm2 logs mrit-server     — ver logs em tempo real"
echo "  pm2 status               — status do processo"
echo "  pm2 restart mrit-server  — reiniciar após mudanças no .env"
echo "  curl http://localhost:8000/health  — testar se está rodando"
