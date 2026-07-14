#!/bin/bash
set -e

echo "=== GelaFit - Instalação no Raspberry Pi ==="

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
    echo "      .env criado. O e-mail da unidade será configurado no primeiro acesso ao painel web."
else
    echo "[2/4] Arquivo .env já existe, pulando."
fi

# Instalar serviço systemd
echo "[3/4] Instalando serviço systemd..."

WORKING_DIR="$(pwd)"
CURRENT_USER="$(whoami)"

sudo tee /etc/systemd/system/mrit-server.service > /dev/null << EOF
[Unit]
Description=GelaFit Local Server
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

# Instalar hostapd e dnsmasq para hotspot
sudo apt-get install -y hostapd dnsmasq iptables-persistent -qq
sudo systemctl stop hostapd dnsmasq 2>/dev/null || true
sudo systemctl disable hostapd dnsmasq 2>/dev/null || true

# Permitir comandos de rede sem senha (nmcli, ip, hostapd, dnsmasq, pkill)
echo "${CURRENT_USER} ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/mrit-server > /dev/null
sudo chmod 440 /etc/sudoers.d/mrit-server

# Redirecionar porta 80 → 8000 (acesso sem :8000 na URL)
sudo iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000 2>/dev/null || \
    sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000
sudo iptables -t nat -C OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 8000 2>/dev/null || \
    sudo iptables -t nat -A OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 8000
sudo netfilter-persistent save -q 2>/dev/null || true

echo "      Serviço instalado e iniciado."

# Verificar se subiu
echo "[4/4] Verificando servidor..."
sleep 3
if curl -s http://localhost:8000/health > /dev/null; then
    echo "      Servidor respondendo em http://$(hostname).local"
else
    echo "      Servidor ainda iniciando. Verifique com: sudo journalctl -u mrit-server -f"
fi

echo ""
echo "=== Instalação concluída! ==="
echo ""
echo "Próximo passo: acesse o painel web e configure o e-mail da unidade."
echo "  http://gelafit-pi.local"
echo ""
echo "Comandos úteis:"
echo "  sudo systemctl status mrit-server      — status do serviço"
echo "  sudo journalctl -u mrit-server -f      — logs em tempo real"
echo "  sudo systemctl restart mrit-server     — reiniciar o serviço"
echo "  curl http://localhost/health            — testar se está rodando"
