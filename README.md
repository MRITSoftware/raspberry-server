# GelaFit Pi Server — Manual de Instalação no Raspberry Pi

## O que você vai precisar

- Raspberry Pi 3 ou 4
- Cartão MicroSD (16 GB ou mais, Classe 10)
- Cabo de alimentação USB-C
- Acesso à internet (apenas durante a instalação)

---

## Passo 1 — Gravar o sistema operacional

1. Baixe o **Raspberry Pi Imager**: https://www.raspberrypi.com/software/
2. Clique em **Escolher dispositivo** → selecione seu modelo de Pi
3. Clique em **Escolher SO** → `Raspberry Pi OS (other)` → `Raspberry Pi OS Lite (64-bit)`
4. Clique em **Escolher armazenamento** → selecione o cartão SD
5. Clique no ícone de engrenagem (**Configurações avançadas**) e preencha:

| Campo | Valor |
|-------|-------|
| Nome do host | `gelafit-pi` |
| Usuário | `mrit` |
| Senha | (escolha uma senha, anote) |
| WiFi (opcional) | SSID e senha da rede local |
| Fuso horário | America/Sao_Paulo |
| Habilitar SSH | ✅ Sim |

6. Clique em **Salvar** → **Gravar**

---

## Passo 2 — Ligar e conectar via SSH

Insira o cartão no Pi, conecte a alimentação e aguarde ~60 segundos.

```bash
ssh mrit@gelafit-pi.local
```

> Se não funcionar com `.local`, descubra o IP no roteador e use `ssh mrit@192.168.x.x`

---

## Passo 3 — Clonar o repositório

```bash
git clone https://github.com/MRITSoftware/raspberry-server.git
cd raspberry-server
```

---

## Passo 4 — Executar o instalador

```bash
bash install.sh
```

O instalador vai:
- Criar o ambiente Python e instalar as dependências
- Configurar o serviço para iniciar automaticamente
- Redirecionar a porta 80 → 8000 (acesso sem `:8000` na URL)
- Iniciar o servidor

> O e-mail da unidade **não** é mais solicitado durante a instalação. Ele será configurado no primeiro acesso ao painel web.

---

## Passo 5 — Acessar o painel web

Abra no navegador (qualquer dispositivo na mesma rede):

```
http://gelafit-pi.local
```

**Senha:** `GELAFIT#REDE`

---

## Passo 6 — Configurar o e-mail da unidade (primeiro acesso)

No primeiro acesso após o login, o painel exibirá automaticamente a tela de configuração:

1. Informe o **e-mail da unidade** (ex: `itaquera@gelafit.com.br`)
2. Clique em **Verificar**
   - Se o e-mail **não existir** no banco → a unidade será criada automaticamente
   - Se o e-mail **já existir** → uma confirmação é exibida (continuando, o registro atual será sobrescrito por este Pi)
3. Confirme e o painel carrega normalmente

> Em acessos futuros o e-mail já estará salvo e esta tela não aparece.

---

## Passo 7 — Configurar o WiFi (se necessário)

Se o Pi não estiver conectado a uma rede WiFi:

1. Conecte ao hotspot **GelaFit-Setup** (senha: `rede#gelafit@123`)
2. Acesse `http://192.168.4.1`
3. Vá em **WiFi** → **Buscar Redes** → conecte à rede desejada

### Resgate se o WiFi falhar

A versao `1.0-PI-wifi-rescue` tem protecoes para evitar formatar o cartao SD:

- Antes de trocar ou salvar rede, o painel pede confirmacao e permite mostrar a senha digitada.
- Se a troca de WiFi falhar, o Pi tenta restaurar automaticamente o hotspot `GelaFit-Setup` / `rede#gelafit@123`.
- Se a rede reserva falhar ou ficar sem internet, o Pi tambem volta para o hotspot.

Para uma unidade que ja ficou presa antes dessa versao, tente primeiro:

1. Reiniciar o Pi e aguardar ate 4 minutos pelo hotspot `GelaFit-Setup`.
2. Se nao voltar, ligar cabo de rede no roteador, descobrir o IP no roteador e acessar `http://IP_DO_PI`.
3. Se conseguir SSH: `ssh mrit@IP_DO_PI` e depois `sudo systemctl restart mrit-server`.

---

## Passo 8 — Sincronizar o dispositivo Tuya

1. No painel web, vá em **Dispositivos**
2. Clique em **Buscar Dispositivos Tuya** (aguarde ~20 segundos)
3. Clique em **Sincronizar com Banco** no dispositivo encontrado

---

## Atualizações via painel web

O painel possui um botão de atualização embutido na seção **Sistema**:

1. Clique em **Verificar Atualizações**
2. Se houver nova versão disponível, clique em **Atualizar Agora**
3. O sistema aplicará o `git pull` e reiniciará o serviço automaticamente

---

## Atualizar via SSH (Pi já instalado)

Caso prefira atualizar manualmente:

```bash
ssh mrit@gelafit-pi.local
cd raspberry-server
git pull
sudo systemctl restart mrit-server
```

---

## Resumo dos serviços instalados

| Serviço | Função | Porta |
|---------|--------|-------|
| `mrit-server` | Controla dispositivos Tuya, heartbeat, comandos remotos, painel web | 8000 (80 via redirecionamento) |

---

## Comandos úteis via SSH

```bash
# Ver logs do servidor em tempo real
sudo journalctl -u mrit-server -f

# Últimas 100 linhas do log
sudo journalctl -u mrit-server -n100

# Reiniciar o serviço manualmente
sudo systemctl restart mrit-server

# Status do serviço
sudo systemctl status mrit-server

# Testar se o servidor está respondendo
curl http://localhost/health
```

---

## Comandos remotos via banco (Supabase)

```sql
-- Ligar a placa
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'on');

-- Desligar a placa
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'off');

-- Reiniciar o serviço
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'restart');

-- Reiniciar o Raspberry inteiro
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'reboot');

-- Forcar heartbeat/status agora
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'heartbeat');

-- Buscar logs remotamente
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'logs');

-- Atualizar o código do servidor (git pull + reinicia serviço)
INSERT INTO remote_commands (site_id, action)
VALUES ('email@gelafit.com.br', 'update');

-- Ver os últimos 10 resultados
SELECT id, action, status, result, error_message, executed_at
FROM remote_commands
WHERE site_id = 'email@gelafit.com.br'
ORDER BY created_at DESC
LIMIT 10;

-- Ver eventos da unidade, incluindo server_online apos boot/restart/reboot
SELECT id, site_id, event_type, message, data, versao, created_at
FROM pi_system_events
WHERE site_id = 'email@gelafit.com.br'
ORDER BY created_at DESC
LIMIT 20;
```

> Para habilitar todos os comandos e eventos, execute no Supabase:
> `docs/remote_commands_add_restart_logs.sql` e `docs/pi_system_events_setup.sql`.

---

## Informações do sistema

| Item | Valor |
|------|-------|
| Usuário do Pi | `mrit` |
| Senha do painel web | `GELAFIT#REDE` |
| Hotspot de configuração | `GelaFit-Setup` / senha `rede#gelafit@123` |
| Endereço local | `http://gelafit-pi.local` |
| Endereço via hotspot | `http://192.168.4.1` |
| Repositório | https://github.com/MRITSoftware/raspberry-server |
