#!/bin/bash
set -e

echo "=== MRIT Server - Instalação no Raspberry Pi ==="

# Verificar Python 3
if ! command -v python3 &> /dev/null; then
    echo "[ERRO] Python 3 não encontrado. Instale com: sudo apt install python3 python3-full"
    exit 1
fi

# Criar virtual environment e instalar dependências
echo "[1/4] Criando virtual environment e instalando dependências Python..."
python3 -m venv venv
venv/bin/pip install --quiet -r requirements.txt
echo "      Dependências instaladas."

# Criar .env a partir do exemplo se não existir
if [ ! -f .env ]; then
    echo "[2/4] Criando .env..."
    cp .env.example .env
    echo ""
    echo "      *** ATENÇÃO ***"
    echo "      Edite o arquivo .env e defina o SITE_NAME (email da unidade)."
    echo "      Comando: nano .env"
    echo ""
    read -p "      Pressione Enter após salvar o .env para continuar..."
else
    echo "[2/4] Arquivo .env já existe, pulando."
fi

# Instalar serviço systemd
echo "[3/4] Instalando serviço systemd..."

WORKING_DIR="$(pwd)"
CURRENT_USER="$(whoami)"

sudo tee /etc/systemd/system/mrit-server.service > /dev/null << EOF
[Unit]
Description=MRIT Tuya Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${WORKING_DIR}
EnvironmentFile=${WORKING_DIR}/.env
ExecStart=${WORKING_DIR}/venv/bin/python3 ${WORKING_DIR}/tuya_server.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mrit-server
sudo systemctl start mrit-server

echo "      Serviço instalado e iniciado."

# Verificar se subiu
echo "[4/4] Verificando servidor..."
sleep 3
if curl -s http://localhost:8000/health > /dev/null; then
    echo "      Servidor respondendo em http://localhost:8000/health"
else
    echo "      Servidor ainda iniciando. Verifique com: sudo journalctl -u mrit-server -f"
fi

echo ""
echo "=== Instalação concluída! ==="
echo ""
echo "Comandos úteis:"
echo "  sudo systemctl status mrit-server      — status do serviço"
echo "  sudo journalctl -u mrit-server -f      — logs em tempo real"
echo "  sudo systemctl restart mrit-server     — reiniciar após mudanças no .env"
echo "  curl http://localhost:8000/health       — testar se está rodando"
