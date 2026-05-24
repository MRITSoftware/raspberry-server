#!/usr/bin/env python3

import os
import json
import traceback
import threading
import time
import socket
import subprocess
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

from flask import Flask, request, jsonify, render_template
import tinytuya

# =========================
# LOGGING (definido primeiro)
# =========================

def log(msg: str) -> None:
    """Função de log centralizada."""
    print(msg, flush=True)

def mask_local_key(local_key: Optional[str], visible_chars: int = 8) -> str:
    """Mascara local_key mostrando apenas os primeiros caracteres."""
    if not local_key:
        return "None"
    if len(local_key) <= visible_chars:
        return "*" * len(local_key)
    return local_key[:visible_chars] + "*" * (len(local_key) - visible_chars)

# Usar requests para chamadas HTTP diretas ao Supabase
# Isso evita dependências problemáticas como pydantic-core
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    log("[WARN] requests não disponível - funcionalidades de banco desabilitadas")

# Tuya Connector para buscar local_key da API Tuya
try:
    from tuya_connector import TuyaOpenAPI
    TUYA_CONNECTOR_AVAILABLE = True
except ImportError:
    TUYA_CONNECTOR_AVAILABLE = False
    log("[WARN] tuya-connector-python não disponível - busca de local_key desabilitada")


try:
    import websocket
    WEBSOCKET_CLIENT_AVAILABLE = True
except ImportError:
    WEBSOCKET_CLIENT_AVAILABLE = False
    log("[WARN] websocket-client n?o dispon?vel - comandos remotos em tempo real desabilitados")
# =========================
# CONFIG & AUTO-SETUP
# =========================

# Valores padrão do Supabase (configuração interna)
DEFAULT_SUPABASE_URL = "https://kihyhoqbrkwbfudttevo.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtpaHlob3Ficmt3YmZ1ZHR0ZXZvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MTU1NTUwMjcsImV4cCI6MjAzMTEzMTAyN30.XtBTlSiqhsuUIKmhAMEyxofV-dRst7240n912m4O4Us"

# Valores padrão das contas Tuya (configuração interna)
DEFAULT_TUYA_ACCOUNTS = [
    {
        "access_id": "td7tp3cvq3nrc35emwg3",
        "access_key": "bbcdaa3dfe9545fca4326fcfa1cf3e2c",
        "endpoint": "https://openapi.tuyaus.com",
        "uid": "az1715569264750N2mUr"
    },
    {
        "access_id": "wwxsqj37wnfdnp98wu54",
        "access_key": "d7a140221f3b4e8f916601af4fbd6816",
        "endpoint": "https://openapi.tuyaus.com",
        "uid": "az1759235287550HcJRz"
    }
]

# Refresh periódico para manter comunicação com a placa
DEVICE_REFRESH_INTERVAL_SECONDS = 5 * 60  # 5 minutos
DEVICE_REFRESH_RETRY_ON_FAILURE_SECONDS = 60  # 1 minuto quando houver falha
COMMAND_MAX_RETRIES = 3
COMMAND_RETRY_DELAY_SECONDS = 1
COMMAND_PREFLIGHT_TIMEOUT_SECONDS = 8
COMMAND_ACTION_TIMEOUT_SECONDS = 20
REFRESH_FAIL_COUNTS: Dict[str, int] = {}
REFRESH_LAST_STATUS: Dict[str, bool] = {}
APP_VERSION = "1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEVICES_CACHE_PATH = os.path.join(BASE_DIR, "devices_cache.json")
PENDING_HEARTBEAT_LOGS_PATH = os.path.join(BASE_DIR, "pending_heartbeat_logs.json")
REMOTE_COMMAND_TABLE = "remote_commands"
REMOTE_COMMAND_TOPIC = "realtime:remote_commands"
REMOTE_COMMAND_HEARTBEAT_SECONDS = 20
REMOTE_COMMAND_RECONNECT_SECONDS = 10
REMOTE_COMMAND_LISTENER_STARTED = False
REMOTE_COMMAND_LISTENER_LOCK = threading.Lock()
REMOTE_COMMAND_WS_LOCK = threading.Lock()
REMOTE_COMMAND_WS_APP = None
REMOTE_COMMAND_REF_COUNTER = 0

def load_config_from_env() -> Dict[str, Any]:
    """Carrega configurações de variáveis de ambiente."""
    config = {}
    
    # Supabase
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY")
    if supabase_url and supabase_anon_key:
        config["supabase"] = {
            "url": supabase_url,
            "anon_key": supabase_anon_key
        }
    
    # Admin token
    admin_token = os.getenv("ADMIN_TOKEN")
    if admin_token:
        config["admin_token"] = admin_token

    # Site name
    site_name = os.getenv("SITE_NAME")
    if site_name:
        config["site_name"] = site_name

    # Tuya accounts (JSON string)
    tuya_accounts_json = os.getenv("TUYA_ACCOUNTS")
    if tuya_accounts_json:
        try:
            accounts = json.loads(tuya_accounts_json)
            if isinstance(accounts, list):
                config["tuya_accounts"] = accounts
        except json.JSONDecodeError:
            log("[WARN] TUYA_ACCOUNTS inválido, ignorando")
    
    return config

def create_config_if_needed():
    """Cria o config.json com nome do site/tablet."""
    if not os.path.exists(CONFIG_PATH):
        site = os.getenv("SITE_NAME", "RASPBERRY_PI")
        
        cfg = {
            "site_name": site,
            "supabase": {
                "url": DEFAULT_SUPABASE_URL,
                "anon_key": DEFAULT_SUPABASE_ANON_KEY
            },
            "tuya_accounts": DEFAULT_TUYA_ACCOUNTS.copy()
        }
        
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        
        log(f"[OK] config.json criado com site_name = {site} e configuração do Supabase")

def update_site_name(new_name: str):
    """Atualiza o nome do site no config.json"""
    # Carregar config existente
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    
    cfg["site_name"] = new_name
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    
    global SITE_NAME
    SITE_NAME = new_name
    log(f"[OK] site_name atualizado para = {new_name}")

def update_supabase_config(url: str, anon_key: str):
    """Atualiza a configuração do Supabase no config.json"""
    # Carregar config existente
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    
    cfg["supabase"] = {
        "url": url,
        "anon_key": anon_key
    }
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    
    # Atualizar variável global
    global SUPABASE_CONFIG
    SUPABASE_CONFIG = cfg["supabase"]
    log(f"[OK] Configuração do Supabase atualizada")

def update_tuya_accounts(accounts: List[Dict[str, str]]):
    """Atualiza as contas Tuya no config.json"""
    # Carregar config existente
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    
    cfg["tuya_accounts"] = accounts
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    
    # Atualizar variável global
    global TUYA_ACCOUNTS
    TUYA_ACCOUNTS = accounts
    log(f"[OK] Configuração de contas Tuya atualizada: {len(accounts)} conta(s)")

def update_backup_wifi(ssid: str, password: str):
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg["backup_wifi"] = {"ssid": ssid, "password": password}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    global BACKUP_WIFI
    BACKUP_WIFI = {"ssid": ssid, "password": password}
    log(f"[OK] Rede reserva salva: {ssid}")

# cria se não existir
create_config_if_needed()

# carrega o config
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    SITE_NAME: str = cfg.get("site_name", "SITE_DESCONHECIDO")
    SUPABASE_CONFIG = cfg.get("supabase", {})
    TUYA_ACCOUNTS = cfg.get("tuya_accounts", [])
    ADMIN_TOKEN = cfg.get("admin_token", "")
    BACKUP_WIFI: Dict[str, str] = cfg.get("backup_wifi", {"ssid": "", "password": ""})
else:
    SITE_NAME = "SITE_DESCONHECIDO"
    SUPABASE_CONFIG = {}
    TUYA_ACCOUNTS = []
    ADMIN_TOKEN = ""
    BACKUP_WIFI: Dict[str, str] = {"ssid": "", "password": ""}

# Carregar de variáveis de ambiente e preencher config se vazio
env_config = load_config_from_env()

# Se env var existir e config estiver vazio, preencher automaticamente
if not SUPABASE_CONFIG.get("url") and env_config.get("supabase"):
    SUPABASE_CONFIG = env_config["supabase"]
    # Salvar no config.json
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg["supabase"] = SUPABASE_CONFIG
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    log("[INFO] Supabase configurado automaticamente a partir de variáveis de ambiente")

if not TUYA_ACCOUNTS and env_config.get("tuya_accounts"):
    TUYA_ACCOUNTS = env_config["tuya_accounts"]
    # Salvar no config.json
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg["tuya_accounts"] = TUYA_ACCOUNTS
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    log(f"[INFO] Contas Tuya configuradas automaticamente a partir de variáveis de ambiente: {len(TUYA_ACCOUNTS)} conta(s)")

if not ADMIN_TOKEN and env_config.get("admin_token"):
    ADMIN_TOKEN = env_config["admin_token"]
    # Salvar no config.json
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg["admin_token"] = ADMIN_TOKEN
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    log("[INFO] Admin token configurado automaticamente a partir de variáveis de ambiente")

# Env var SITE_NAME tem prioridade sobre config.json
_env_site_name = os.getenv("SITE_NAME")
if _env_site_name and _env_site_name != SITE_NAME:
    SITE_NAME = _env_site_name
    log(f"[INFO] SITE_NAME definido por variável de ambiente: {SITE_NAME}")

# Garantir que SUPABASE_CONFIG tem a estrutura correta
if not isinstance(SUPABASE_CONFIG, dict):
    SUPABASE_CONFIG = {}

# Garantir que TUYA_ACCOUNTS é uma lista
if not isinstance(TUYA_ACCOUNTS, list):
    TUYA_ACCOUNTS = []

# Se ainda não há configuração, usar valores padrão internos
if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
    SUPABASE_CONFIG = {
        "url": DEFAULT_SUPABASE_URL,
        "anon_key": DEFAULT_SUPABASE_ANON_KEY
    }
    
    # Salvar no config.json
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg["supabase"] = SUPABASE_CONFIG
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    
    log("[INFO] Configuração do Supabase inicializada com valores padrão internos")

# Se ainda não há contas Tuya configuradas, usar valores padrão internos
if not TUYA_ACCOUNTS:
    TUYA_ACCOUNTS = DEFAULT_TUYA_ACCOUNTS.copy()
    
    # Salvar no config.json
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg["tuya_accounts"] = TUYA_ACCOUNTS
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    
    log(f"[INFO] Contas Tuya inicializadas com valores padrão internos: {len(TUYA_ACCOUNTS)} conta(s)")

log(f"[INFO] Servidor local iniciado para SITE = {SITE_NAME}")

# =========================
# DATABASE (SUPABASE)
# =========================

HEARTBEAT_LOG_TABLE = "tuya_heartbeat_logs"
PENDING_HEARTBEAT_LOGS_LOCK = threading.Lock()

def get_supabase_headers():
    """Retorna headers para requisições ao Supabase."""
    if not REQUESTS_AVAILABLE:
        raise RuntimeError("requests não está disponível")
    
    url = SUPABASE_CONFIG.get("url")
    anon_key = SUPABASE_CONFIG.get("anon_key")
    
    if not url or not anon_key:
        raise RuntimeError("Configuração do Supabase não encontrada (url ou anon_key faltando)")
    
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def get_supabase_url():
    """Retorna a URL base do Supabase."""
    url = SUPABASE_CONFIG.get("url")
    if not url:
        raise RuntimeError("URL do Supabase não configurada")
    # Garantir que a URL termina com /rest/v1
    # Remover barra final se existir
    url = url.rstrip("/")
    return f"{url}/rest/v1"

def get_devices_from_db(tuya_device_ids: List[str]) -> Dict[str, Dict]:
    """
    Busca devices da tabela tuya_devices pelos tuya_device_id.
    Retorna um dict onde a chave é tuya_device_id e o valor é um dict com os dados.
    Com retry automático para lidar com latência de internet.
    """
    if not REQUESTS_AVAILABLE or not SUPABASE_CONFIG.get("url"):
        return {}
    
    if not tuya_device_ids:
        return {}
    
    # Retry para lidar com internet lenta
    max_retries = 2
    timeout_seconds = 30  # Timeout aumentado para internet lenta
    
    for attempt in range(1, max_retries + 1):
        try:
            base_url = get_supabase_url()
            headers = get_supabase_headers()
            
            # Usar requests com params para URL encoding correto
            # Supabase PostgREST usa formato: tuya_device_id=in.(id1,id2,id3)
            # Construir a query string corretamente com URL encoding
            ids_list = ",".join(tuya_device_ids)
            params = {
                "tuya_device_id": f"in.({ids_list})",
                "select": "*"
            }
            
            url = f"{base_url}/tuya_devices"
            
            if attempt > 1:
                log(f"[DB] Tentativa {attempt}/{max_retries} para buscar devices (timeout: {timeout_seconds}s)")
                time.sleep(1)  # Delay entre tentativas
            
            response = requests.get(url, headers=headers, params=params, timeout=timeout_seconds)
            response.raise_for_status()
            
            data = response.json()
            result = {}
            for row in data:
                tuya_id = row.get('tuya_device_id')
                if tuya_id:
                    result[tuya_id] = {
                        'id': str(row.get('id', '')),
                        'site_id': row.get('site_id'),
                        'tuya_device_id': tuya_id,
                        'name': row.get('name'),
                        'local_key': row.get('local_key'),
                        'lan_ip': row.get('lan_ip'),
                        'protocol_version': row.get('protocol_version')
                    }
            
            log(f"[DB] Encontrados {len(result)} devices no banco")
            return result
            
        except requests.exceptions.Timeout as e:
            log(f"[DB] Timeout ao buscar devices (tentativa {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                log(f"[DB] Tentando novamente...")
                continue
            else:
                log(f"[DB] Todas as tentativas falharam por timeout. Internet pode estar muito lenta.")
                return {}
        except requests.exceptions.HTTPError as e:
            log(f"[DB] Erro HTTP ao buscar devices: {e}")
            log(f"[DB] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
            traceback.print_exc()
            return {}
        except Exception as e:
            log(f"[DB] Erro ao buscar devices: {e}")
            traceback.print_exc()
            return {}
    
    return {}

def get_device_site_id_from_db(tuya_device_id: str) -> Optional[str]:
    """Busca o site_id de um device no banco."""
    if not REQUESTS_AVAILABLE:
        return None
    
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        return None
    
    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        url = f"{base_url}/tuya_devices?tuya_device_id=eq.{tuya_device_id}&select=site_id&limit=1"
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        if data and len(data) > 0:
            return data[0].get("site_id")
        return None
    except Exception as e:
        log(f"[DB] Não foi possível buscar site_id para {tuya_device_id}: {e}")
        return None

def get_device_by_site_id_from_db(site_id: str) -> Optional[Dict[str, Any]]:
    """Busca um device pelo site_id para permitir atualização da mesma linha quando o código da placa mudar."""
    if not REQUESTS_AVAILABLE:
        return None
    
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        return None
    
    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        url = f"{base_url}/tuya_devices?site_id=eq.{site_id}&select=id,tuya_device_id,site_id,name,local_key,lan_ip,protocol_version&limit=1"
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        if data and len(data) > 0:
            return data[0]
        return None
    except Exception as e:
        log(f"[DB] Não foi possível buscar device por site_id={site_id}: {e}")
        return None

def update_device_by_id_in_db(
    device_row_id: str,
    tuya_device_id: Optional[str] = None,
    site_id: Optional[str] = None,
    name: Optional[str] = None,
    local_key: Optional[str] = None,
    lan_ip: Optional[str] = None,
    protocol_version: Optional[str] = None
) -> bool:
    """Atualiza um device na tabela tuya_devices usando o id da linha."""
    if not REQUESTS_AVAILABLE:
        log("[DB] requests não está disponível")
        return False
    
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        log(f"[DB] Configuração do Supabase não encontrada. URL: {SUPABASE_CONFIG.get('url')}, Key: {'presente' if SUPABASE_CONFIG.get('anon_key') else 'ausente'}")
        return False
    
    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        
        update_data: Dict[str, Any] = {}
        if tuya_device_id is not None:
            update_data["tuya_device_id"] = tuya_device_id
        if site_id is not None:
            update_data["site_id"] = site_id
        if name is not None:
            update_data["name"] = name
        if local_key is not None:
            update_data["local_key"] = local_key
        if lan_ip is not None:
            update_data["lan_ip"] = lan_ip
        if protocol_version is not None:
            update_data["protocol_version"] = protocol_version
        update_data["versao"] = APP_VERSION
        
        if not update_data:
            log(f"[DB] Nenhum dado para atualizar por id={device_row_id}")
            return False
        
        url = f"{base_url}/tuya_devices?id=eq.{device_row_id}"
        response = requests.patch(url, json=update_data, headers=headers, timeout=30)
        response.raise_for_status()
        
        # Com return=representation, lista vazia indica que a linha não foi encontrada.
        data = response.json()
        if data and len(data) > 0:
            log(f"[DB] Device id={device_row_id} atualizado com sucesso")
            return True
        
        log(f"[DB] Nenhum device encontrado com id={device_row_id}")
        return False
    except Exception as e:
        log(f"[DB] Erro ao atualizar device por id={device_row_id}: {e}")
        traceback.print_exc()
        return False

def load_pending_heartbeat_logs() -> List[Dict[str, Any]]:
    """Carrega a fila local de logs pendentes."""
    if not os.path.exists(PENDING_HEARTBEAT_LOGS_PATH):
        return []
    try:
        with open(PENDING_HEARTBEAT_LOGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []
    except Exception as e:
        log(f"[HEARTBEAT_LOG] Erro ao carregar fila local: {e}")
        return []

def save_pending_heartbeat_logs(rows: List[Dict[str, Any]]) -> None:
    """Salva a fila local de logs pendentes."""
    with open(PENDING_HEARTBEAT_LOGS_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

def enqueue_pending_heartbeat_log(payload: Dict[str, Any]) -> bool:
    """Adiciona um evento na fila local quando não for possível enviar ao banco."""
    try:
        with PENDING_HEARTBEAT_LOGS_LOCK:
            pending = load_pending_heartbeat_logs()
            pending.append(payload)
            save_pending_heartbeat_logs(pending)
        log(
            f"[HEARTBEAT_LOG] Evento enfileirado localmente "
            f"(site_id={payload.get('site_id')}, device={payload.get('tuya_device_id')}, event_time={payload.get('event_time')})"
        )
        return True
    except Exception as e:
        log(f"[HEARTBEAT_LOG] Falha ao enfileirar evento local: {e}")
        return False

def send_heartbeat_log_payload(payload: Dict[str, Any]) -> bool:
    """Envia payload de log para o Supabase mantendo o event_time original."""
    if not REQUESTS_AVAILABLE:
        return False
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        return False
    
    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        url = f"{base_url}/{HEARTBEAT_LOG_TABLE}"
        
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return True
    except Exception as e:
        log(f"[HEARTBEAT_LOG] Falha ao enviar payload para {HEARTBEAT_LOG_TABLE}: {e}")
        return False

def flush_pending_heartbeat_logs(max_items: int = 200) -> int:
    """Tenta enviar eventos pendentes salvos localmente."""
    if max_items <= 0:
        return 0
    if not REQUESTS_AVAILABLE:
        return 0
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        return 0
    
    with PENDING_HEARTBEAT_LOGS_LOCK:
        pending = load_pending_heartbeat_logs()
        if not pending:
            return 0
        
        sent_count = 0
        remaining: List[Dict[str, Any]] = []
        
        for idx, payload in enumerate(pending):
            if sent_count >= max_items:
                remaining.extend(pending[idx:])
                break
            
            if send_heartbeat_log_payload(payload):
                sent_count += 1
            else:
                # Mantém ordem e tenta novamente no próximo ciclo.
                remaining.extend(pending[idx:])
                break
        
        save_pending_heartbeat_logs(remaining)
    
    if sent_count > 0:
        log(f"[HEARTBEAT_LOG] Reenvio concluído: {sent_count} evento(s) pendente(s) enviado(s)")
    return sent_count

def insert_heartbeat_ok_log(
    site_id: str,
    status: str = "ok",
    tuya_device_id: Optional[str] = None,
    event_time_iso: Optional[str] = None
) -> bool:
    """
    Insere evento em tabela dedicada para monitoramento.
    Se não conseguir enviar, salva localmente mantendo event_time original.
    """
    if event_time_iso:
        timestamp_iso = event_time_iso
    else:
        now_utc = datetime.now(timezone.utc)
        timestamp_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"
    
    payload: Dict[str, Any] = {
        "site_id": site_id,
        "status": status,
        "event_time": timestamp_iso
    }
    if tuya_device_id:
        payload["tuya_device_id"] = tuya_device_id
    
    if send_heartbeat_log_payload(payload):
        log(f"[HEARTBEAT_LOG] Evento gravado para site_id={site_id}, status={status}, event_time={timestamp_iso}")
        # Tenta reenvio de pendências junto com um envio bem-sucedido.
        flush_pending_heartbeat_logs(max_items=50)
        return True
    
    # O log em tabela auxiliar não deve quebrar o endpoint principal.
    return enqueue_pending_heartbeat_log(payload)

def create_device_in_db(
    tuya_device_id: str,
    site_id: str,
    name: Optional[str] = None,
    local_key: Optional[str] = None,
    lan_ip: Optional[str] = None,
    protocol_version: Optional[str] = None
) -> bool:
    """
    Cria um novo device na tabela tuya_devices.
    """
    if not REQUESTS_AVAILABLE:
        log("[DB] requests não está disponível")
        return False
    
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        log(f"[DB] Configuração do Supabase não encontrada. URL: {SUPABASE_CONFIG.get('url')}, Key: {'presente' if SUPABASE_CONFIG.get('anon_key') else 'ausente'}")
        return False
    
    try:
        # Regra de negócio: site_id não deve gerar duplicidade.
        # Se já existir uma linha para o mesmo site_id, substituímos/atualizamos
        # a linha atual em vez de criar um novo registro.
        existing_by_site = get_device_by_site_id_from_db(site_id)
        if existing_by_site:
            row_id = existing_by_site.get("id")
            old_tuya_id = existing_by_site.get("tuya_device_id")
            log(
                f"[DB] site_id={site_id} já existe (tuya antigo={old_tuya_id}); "
                f"atualizando linha existente em vez de criar nova"
            )
            return update_device_by_id_in_db(
                device_row_id=str(row_id),
                tuya_device_id=tuya_device_id,
                site_id=site_id,
                name=name,
                local_key=local_key,
                lan_ip=lan_ip,
                protocol_version=protocol_version
            )

        base_url = get_supabase_url()
        headers = get_supabase_headers()
        
        # Construir dict com dados do novo device
        device_data = {
            'tuya_device_id': tuya_device_id,
            'site_id': site_id,
            'versao': APP_VERSION
        }
        
        if name is not None:
            device_data['name'] = name
        
        if local_key is not None:
            device_data['local_key'] = local_key
        
        if lan_ip is not None:
            device_data['lan_ip'] = lan_ip
        
        if protocol_version is not None:
            device_data['protocol_version'] = protocol_version
        
        # Criar usando Supabase REST API
        url = f"{base_url}/tuya_devices"
        
        # Retry para lidar com internet lenta
        max_retries = 2
        timeout_seconds = 30  # Timeout aumentado para internet lenta
        
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    log(f"[DB] Tentativa {attempt}/{max_retries} para criar device {tuya_device_id}")
                    time.sleep(1)  # Delay entre tentativas
                
                log(f"[DB] Tentando criar device {tuya_device_id} (tentativa {attempt}/{max_retries})")
                log(f"[DB] URL: {url}")
                log(f"[DB] Dados: {device_data}")
                
                response = requests.post(url, json=device_data, headers=headers, timeout=timeout_seconds)
                
                log(f"[DB] Status code: {response.status_code}")
                log(f"[DB] Response: {response.text[:200]}")  # Primeiros 200 caracteres
                
                response.raise_for_status()
                
                data = response.json()
                if data and len(data) > 0:
                    log(f"[DB] Device {tuya_device_id} criado com sucesso: {data}")
                    return True
                else:
                    log(f"[DB] Resposta vazia ao criar device {tuya_device_id}")
                    return False
                    
            except requests.exceptions.Timeout as e:
                log(f"[DB] Timeout ao criar device {tuya_device_id} (tentativa {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    log(f"[DB] Tentando novamente...")
                    continue
                else:
                    log(f"[DB] Todas as tentativas falharam por timeout. Internet pode estar muito lenta.")
                    return False
            except requests.exceptions.HTTPError as e:
                log(f"[DB] Erro HTTP ao criar device {tuya_device_id}: {e}")
                log(f"[DB] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
                traceback.print_exc()
                return False
        
        return False
        
    except Exception as e:
        log(f"[DB] Erro ao criar device {tuya_device_id}: {e}")
        traceback.print_exc()
        return False

def tuya_status_with_timeout(device: Any, timeout_seconds: int = 20) -> Optional[Dict]:
    """Executa status() com timeout para evitar travamentos."""
    result = [None]
    exception = [None]
    
    def status_thread():
        try:
            result[0] = device.status()
        except Exception as e:
            exception[0] = e
    
    thread = threading.Thread(target=status_thread, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    
    if thread.is_alive():
        log(f"[TUYA] Timeout após {timeout_seconds} segundos em status()")
        return None
    
    if exception[0]:
        log(f"[TUYA] Exceção durante status(): {exception[0]}")
        return None
    
    return result[0]

def normalize_tuya_power_value(value: Any) -> Optional[bool]:
    """Converte valores comuns do status Tuya em ligado/desligado."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "on", "1"):
            return True
        if normalized in ("false", "off", "0"):
            return False
    return None

def extract_tuya_power_state(status_payload: Any) -> Optional[bool]:
    """Tenta extrair do payload de status se a placa está ligada."""
    if not isinstance(status_payload, dict):
        return None

    dps = status_payload.get("dps")
    if isinstance(dps, dict):
        for key in ("1", 1, "switch", "switch_1", "20"):
            if key in dps:
                parsed = normalize_tuya_power_value(dps.get(key))
                if parsed is not None:
                    return parsed

    for key in ("switch", "switch_1", "power", "is_on", "on"):
        if key in status_payload:
            parsed = normalize_tuya_power_value(status_payload.get(key))
            if parsed is not None:
                return parsed

    return None

def tuya_command_with_timeout(device: Any, action: str, timeout_seconds: int = 20) -> Optional[Dict]:
    """Executa turn_on() ou turn_off() com timeout para evitar travamentos."""
    result = [None]
    exception = [None]
    
    def command_thread():
        try:
            if action == "on":
                result[0] = device.turn_on()
            elif action == "off":
                result[0] = device.turn_off()
            else:
                exception[0] = ValueError(f"Ação inválida: {action}")
        except Exception as e:
            exception[0] = e
    
    thread = threading.Thread(target=command_thread, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    
    if thread.is_alive():
        log(f"[TUYA] Timeout após {timeout_seconds} segundos em {action}()")
        return None
    
    if exception[0]:
        log(f"[TUYA] Exceção durante {action}(): {exception[0]}")
        raise exception[0]
    
    return result[0]

def update_device_heartbeat(
    tuya_device_id: str,
    battery_level: Optional[int] = None,
    internet_speed_mbps: Optional[float] = None
) -> bool:
    """
    Atualiza o campo servidor_online de um device (heartbeat/ping).
    Primeiro tenta fazer ping na placa física, depois atualiza o banco.
    Aceita métricas opcionais: battery_level (0-100) e internet_speed_mbps.
    A velocidade da internet é salva no campo wifi_speed (integer) do banco.
    """
    if not REQUESTS_AVAILABLE:
        log("[DB] requests não está disponível")
        return False
    
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        log(f"[DB] Configuração do Supabase não encontrada. URL: {SUPABASE_CONFIG.get('url')}, Key: {'presente' if SUPABASE_CONFIG.get('anon_key') else 'ausente'}")
        return False
    
    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        
        # Buscar dados do dispositivo no banco (precisa de IP e local_key para ping)
        # Retry para lidar com internet lenta
        max_retries = 2
        timeout_seconds = 30  # Timeout aumentado para internet lenta
        response_get = None
        
        for attempt in range(1, max_retries + 1):
            try:
                url_get = f"{base_url}/tuya_devices?tuya_device_id=eq.{tuya_device_id}&select=id,lan_ip,local_key,protocol_version"
                
                if attempt > 1:
                    log(f"[HEARTBEAT] Tentativa {attempt}/{max_retries} para buscar device (timeout: {timeout_seconds}s)")
                    time.sleep(1)  # Delay entre tentativas
                
                response_get = requests.get(url_get, headers=headers, timeout=timeout_seconds)
                response_get.raise_for_status()
                break  # Sucesso, sair do loop
                
            except requests.exceptions.Timeout as e:
                log(f"[HEARTBEAT] Timeout ao buscar device (tentativa {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    log(f"[HEARTBEAT] Tentando novamente...")
                    continue
                else:
                    log(f"[HEARTBEAT] Todas as tentativas falharam por timeout. Internet pode estar muito lenta.")
                    return False
            except requests.exceptions.HTTPError as e:
                log(f"[HEARTBEAT] Erro HTTP ao buscar device: {e}")
                return False
        
        if response_get is None:
            return False
        
        devices = response_get.json()
        if not devices or len(devices) == 0:
            log(f"[HEARTBEAT] Device {tuya_device_id} não encontrado no banco")
            return False
        
        device_data = devices[0]
        lan_ip = device_data.get("lan_ip")
        local_key = device_data.get("local_key")
        protocol_version = device_data.get("protocol_version")
        
        # Tentar fazer ping na placa física (consultar status sem alterar estado)
        device_online = False
        if lan_ip and local_key:
            try:
                # Usar versão do protocolo do banco ou padrão 3.3
                version = float(protocol_version) if protocol_version else 3.3
                
                log(f"[HEARTBEAT] Fazendo ping na placa {tuya_device_id} @ {lan_ip} (versão {version})...")
                
                # Criar dispositivo Tuya e consultar status (ping sem alterar estado)
                d = tinytuya.OutletDevice(tuya_device_id, lan_ip, local_key)
                d.set_version(version)
                
                # Consultar status do dispositivo com timeout (não altera o estado, apenas verifica conexão)
                status = tuya_status_with_timeout(d, timeout_seconds=20)
                
                if status:
                    device_online = True
                    log(f"[HEARTBEAT] Placa respondeu ao ping: {status}")
                else:
                    log(f"[HEARTBEAT] Placa não respondeu ao ping (timeout ou erro)")
                    # Limpar cache se houver timeout
                    with DEVICE_CACHE_LOCK:
                        if tuya_device_id in DEVICE_CACHE:
                            log(f"[HEARTBEAT] Limpando cache de IP para {tuya_device_id} devido a timeout")
                            del DEVICE_CACHE[tuya_device_id]
                    
            except Exception as e:
                log(f"[HEARTBEAT] Erro ao fazer ping na placa {tuya_device_id}: {e}")
                # Limpar cache se houver erro
                with DEVICE_CACHE_LOCK:
                    if tuya_device_id in DEVICE_CACHE:
                        log(f"[HEARTBEAT] Limpando cache de IP para {tuya_device_id} devido a erro")
                        del DEVICE_CACHE[tuya_device_id]
                # Não atualizar servidor_online quando a placa não responde ao ping
        else:
            log(f"[HEARTBEAT] IP ou local_key não disponíveis para ping (IP: {lan_ip}, Key: {'presente' if local_key else 'ausente'})")
            # Continuar mesmo sem ping - atualizar banco para indicar que servidor está online
        
        # Preparar dados para atualização
        # Regra:
        # - servidor_online só é atualizado se a placa respondeu OK OU se não há lan_ip/local_key
        # - métricas (bateria/velocidade) são sempre atualizadas quando enviadas, mesmo se status falhar
        now_utc = datetime.now(timezone.utc)
        timestamp_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"
        
        url = f"{base_url}/tuya_devices?tuya_device_id=eq.{tuya_device_id}"
        
        update_data: Dict[str, Any] = {}
        update_data["versao"] = APP_VERSION
        
        # Atualizar servidor_online apenas se considerarmos o device online
        if (not lan_ip or not local_key) or device_online:
            update_data["servidor_online"] = timestamp_iso
        
        # Adicionar métricas opcionais se fornecidas
        if battery_level is not None:
            # Validar bateria (0-100)
            if 0 <= battery_level <= 100:
                update_data["battery_level"] = battery_level
                log(f"[HEARTBEAT] Bateria: {battery_level}%")
            else:
                log(f"[HEARTBEAT] Bateria inválida (fora do range 0-100): {battery_level}")
        
        if internet_speed_mbps is not None:
            # Validar velocidade (deve ser positiva)
            # Usar campo wifi_speed que já existe na tabela (integer)
            if internet_speed_mbps >= 0:
                # Converter Mbps para inteiro (arredondar)
                wifi_speed_int = int(round(internet_speed_mbps))
                update_data["wifi_speed"] = wifi_speed_int
                log(f"[HEARTBEAT] Velocidade da internet: {internet_speed_mbps:.2f} Mbps (salvando como wifi_speed={wifi_speed_int})")
            else:
                log(f"[HEARTBEAT] Velocidade da internet inválida (negativa): {internet_speed_mbps}")
        
        metrics_info = []
        if battery_level is not None and 0 <= battery_level <= 100:
            metrics_info.append(f"bateria={battery_level}%")
        if internet_speed_mbps is not None and internet_speed_mbps >= 0:
            wifi_speed_int = int(round(internet_speed_mbps))
            metrics_info.append(f"velocidade={wifi_speed_int} Mbps")
        
        metrics_str = f", {', '.join(metrics_info)}" if metrics_info else ""
        
        # Se não houver nada para atualizar, sair sem erro
        if not update_data:
            log(f"[HEARTBEAT] Nenhum campo para atualizar para device {tuya_device_id} (placa_online={device_online})")
            return False
        log(f"[HEARTBEAT] Atualizando servidor_online para device {tuya_device_id} (timestamp: {timestamp_iso}, placa online: {device_online}{metrics_str})")
        
        # Usar PATCH com Prefer: return=minimal para não retornar dados
        # Retry para lidar com internet lenta
        max_retries = 2
        timeout_seconds = 30  # Timeout aumentado para internet lenta
        
        for attempt in range(1, max_retries + 1):
            try:
                headers_with_prefer = {**headers, "Prefer": "return=minimal,resolution=merge-duplicates"}
                
                if attempt > 1:
                    log(f"[HEARTBEAT] Tentativa {attempt}/{max_retries} para atualizar heartbeat (timeout: {timeout_seconds}s)")
                    time.sleep(1)  # Delay entre tentativas
                
                # Log detalhado do que está sendo enviado
                log(f"[HEARTBEAT] Enviando PATCH para {url}")
                log(f"[HEARTBEAT] Dados a atualizar: {update_data}")
                
                response = requests.patch(url, json=update_data, headers=headers_with_prefer, timeout=timeout_seconds)
                
                # Log da resposta antes de verificar status
                log(f"[HEARTBEAT] Status code: {response.status_code}")
                log(f"[HEARTBEAT] Response headers: {dict(response.headers)}")
                
                # Tentar ler resposta mesmo se houver erro
                try:
                    response_text = response.text
                    log(f"[HEARTBEAT] Response body: {response_text[:500]}")  # Primeiros 500 caracteres
                except:
                    pass
                
                response.raise_for_status()
                
                # Verificar se o update realmente aconteceu (status 204 ou 200)
                if response.status_code in (200, 204):
                    log(f"[HEARTBEAT] servidor_online atualizado com sucesso para device {tuya_device_id} (status: {response.status_code}, placa online: {device_online})")
                    return True
                else:
                    log(f"[HEARTBEAT] Resposta inesperada do Supabase: {response.status_code}")
                    return False
                    
            except requests.exceptions.Timeout as e:
                log(f"[HEARTBEAT] Timeout ao atualizar heartbeat (tentativa {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    log(f"[HEARTBEAT] Tentando novamente...")
                    continue
                else:
                    log(f"[HEARTBEAT] Todas as tentativas falharam por timeout. Internet pode estar muito lenta.")
                    return False
            except requests.exceptions.HTTPError as e:
                # Se o device não existir, não é erro crítico para heartbeat
                if e.response.status_code == 404 or (hasattr(e, 'response') and e.response.status_code == 406):
                    log(f"[HEARTBEAT] Device {tuya_device_id} não encontrado no banco")
                else:
                    log(f"[HEARTBEAT] Erro HTTP ao atualizar heartbeat para device {tuya_device_id}: {e}")
                    if hasattr(e, 'response'):
                        log(f"[HEARTBEAT] Status code: {e.response.status_code}")
                        log(f"[HEARTBEAT] Response headers: {dict(e.response.headers)}")
                        try:
                            error_text = e.response.text
                            log(f"[HEARTBEAT] Response body: {error_text[:500]}")
                        except:
                            pass
                    else:
                        log(f"[HEARTBEAT] Response: N/A")
                return False
        
        return False
        
    except requests.exceptions.HTTPError as e:
        # Se o device não existir, não é erro crítico para heartbeat
        if e.response.status_code == 404 or (hasattr(e, 'response') and e.response.status_code == 406):
            log(f"[HEARTBEAT] Device {tuya_device_id} não encontrado no banco")
        else:
            log(f"[HEARTBEAT] Erro HTTP ao atualizar heartbeat para device {tuya_device_id}: {e}")
            log(f"[HEARTBEAT] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
        return False
    except Exception as e:
        log(f"[HEARTBEAT] Erro ao atualizar heartbeat para device {tuya_device_id}: {e}")
        traceback.print_exc()
        return False

def update_device_in_db(
    tuya_device_id: str,
    site_id: Optional[str] = None,
    name: Optional[str] = None,
    local_key: Optional[str] = None,
    lan_ip: Optional[str] = None,
    protocol_version: Optional[str] = None
) -> bool:
    """
    Atualiza um device na tabela tuya_devices.
    Apenas atualiza os campos que foram fornecidos (não None).
    """
    if not REQUESTS_AVAILABLE:
        log("[DB] requests não está disponível")
        return False
    
    if not SUPABASE_CONFIG.get("url") or not SUPABASE_CONFIG.get("anon_key"):
        log(f"[DB] Configuração do Supabase não encontrada. URL: {SUPABASE_CONFIG.get('url')}, Key: {'presente' if SUPABASE_CONFIG.get('anon_key') else 'ausente'}")
        return False
    
    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        
        # Construir dict com apenas os campos que foram fornecidos
        update_data = {}
        
        if site_id is not None:
            update_data['site_id'] = site_id
        
        if name is not None:
            update_data['name'] = name
        
        if local_key is not None:
            update_data['local_key'] = local_key
        
        if lan_ip is not None:
            update_data['lan_ip'] = lan_ip
        
        if protocol_version is not None:
            update_data['protocol_version'] = protocol_version
        update_data['versao'] = APP_VERSION
        
        # updated_at será atualizado automaticamente pelo banco (default now())
        
        if not update_data:
            log(f"[DB] Nenhum dado para atualizar para device {tuya_device_id}")
            return False
        
        # Atualizar usando Supabase REST API
        # Supabase usa formato: /rest/v1/tuya_devices?tuya_device_id=eq.{id}
        url = f"{base_url}/tuya_devices?tuya_device_id=eq.{tuya_device_id}"
        
        # Retry para lidar com internet lenta
        max_retries = 2
        timeout_seconds = 30  # Timeout aumentado para internet lenta
        
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    log(f"[DB] Tentativa {attempt}/{max_retries} para atualizar device {tuya_device_id}")
                    time.sleep(1)  # Delay entre tentativas
                
                log(f"[DB] Tentando atualizar device {tuya_device_id} (tentativa {attempt}/{max_retries})")
                log(f"[DB] URL: {url}")
                log(f"[DB] Dados: {update_data}")
                
                response = requests.patch(url, json=update_data, headers=headers, timeout=timeout_seconds)
                
                log(f"[DB] Status code: {response.status_code}")
                log(f"[DB] Response: {response.text[:200]}")  # Primeiros 200 caracteres
                
                response.raise_for_status()
                
                data = response.json()
                if data and len(data) > 0:
                    log(f"[DB] Device {tuya_device_id} atualizado com sucesso: {data}")
                    return True
                else:
                    log(f"[DB] Nenhum device encontrado com tuya_device_id = {tuya_device_id}")
                    return False
                    
            except requests.exceptions.Timeout as e:
                log(f"[DB] Timeout ao atualizar device {tuya_device_id} (tentativa {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    log(f"[DB] Tentando novamente...")
                    continue
                else:
                    log(f"[DB] Todas as tentativas falharam por timeout. Internet pode estar muito lenta.")
                    return False
            except requests.exceptions.HTTPError as e:
                log(f"[DB] Erro HTTP ao atualizar device {tuya_device_id}: {e}")
                log(f"[DB] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
                traceback.print_exc()
                return False
        
        return False
        
    except Exception as e:
        log(f"[DB] Erro ao atualizar device {tuya_device_id}: {e}")
        traceback.print_exc()
        return False

# =========================
# CACHE PERSISTENTE DE DISPOSITIVOS
# =========================

def load_devices_cache() -> Dict[str, Dict[str, Any]]:
    """
    Carrega cache persistente de dispositivos do arquivo JSON.
    Retorna dict onde chave é tuya_device_id e valor é dict com dados do dispositivo.
    Filtra campos internos (que começam com "_").
    """
    if not os.path.exists(DEVICES_CACHE_PATH):
        return {}
    
    try:
        with open(DEVICES_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        
        if not isinstance(cache, dict):
            return {}
        
        # Filtrar campos internos (que começam com "_")
        device_cache = {k: v for k, v in cache.items() if not k.startswith("_")}
        
        log(f"[CACHE] Cache carregado: {len(device_cache)} dispositivo(s)")
        return device_cache
    except Exception as e:
        log(f"[CACHE] Erro ao carregar cache: {e}")
        return {}

def save_device_to_cache(
    tuya_device_id: str,
    local_key: Optional[str] = None,
    lan_ip: Optional[str] = None,
    version: Optional[float] = None,
    device_name: Optional[str] = None
) -> None:
    """
    Salva ou atualiza um dispositivo no cache persistente.
    Preserva campos internos (que começam com "_").
    """
    try:
        # Carregar cache completo (incluindo campos internos)
        if os.path.exists(DEVICES_CACHE_PATH):
            with open(DEVICES_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        else:
            cache = {}
        
        if not isinstance(cache, dict):
            cache = {}
        
        # Criar ou atualizar entrada do dispositivo
        if tuya_device_id not in cache:
            cache[tuya_device_id] = {}
        
        device_data = cache[tuya_device_id]
        
        # Atualizar apenas campos fornecidos (não sobrescrever com None)
        if local_key is not None:
            device_data["local_key"] = local_key
        if lan_ip is not None:
            device_data["lan_ip"] = lan_ip
        if version is not None:
            device_data["version"] = float(version)
        if device_name is not None:
            device_data["device_name"] = device_name
        
        # Salvar cache atualizado (preservando campos internos)
        with open(DEVICES_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)
        
        log(f"[CACHE] Device {tuya_device_id} salvo no cache persistente")
    except Exception as e:
        log(f"[CACHE] Erro ao salvar device no cache: {e}")
        traceback.print_exc()

def get_device_from_cache(tuya_device_id: str) -> Optional[Dict[str, Any]]:
    """
    Busca um dispositivo no cache persistente.
    Retorna dict com dados do dispositivo ou None se não encontrado.
    """
    cache = load_devices_cache()
    return cache.get(tuya_device_id)

def get_last_active_device() -> Optional[str]:
    """
    Retorna o ID do último dispositivo ativo (último usado com sucesso).
    """
    try:
        if not os.path.exists(DEVICES_CACHE_PATH):
            return None
        
        with open(DEVICES_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        
        if not isinstance(cache, dict):
            return None
        
        # Buscar campo _last_active_device_id
        last_active = cache.get("_last_active_device_id")
        if last_active and last_active in cache and not last_active.startswith("_"):
            return last_active
        
        # Se não tiver último ativo, mas tiver apenas um dispositivo, usar ele
        device_ids = [k for k in cache.keys() if not k.startswith("_")]
        if len(device_ids) == 1:
            return device_ids[0]
        
        return None
    except Exception as e:
        log(f"[CACHE] Erro ao buscar último dispositivo ativo: {e}")
        return None

def set_last_active_device(tuya_device_id: str) -> None:
    """
    Salva o ID do último dispositivo ativo no cache.
    """
    try:
        # Carregar cache completo (incluindo campos internos)
        if os.path.exists(DEVICES_CACHE_PATH):
            with open(DEVICES_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        else:
            cache = {}
        
        if not isinstance(cache, dict):
            cache = {}
        
        cache["_last_active_device_id"] = tuya_device_id
        
        with open(DEVICES_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)
        
        log(f"[CACHE] Último dispositivo ativo salvo: {tuya_device_id}")
    except Exception as e:
        log(f"[CACHE] Erro ao salvar último dispositivo ativo: {e}")

# Carregar cache na inicialização
DEVICES_CACHE = load_devices_cache()
log(f"[CACHE] Cache inicializado com {len(DEVICES_CACHE)} dispositivo(s)")

# =========================
# DISCOVERY / CACHE DE IP
# =========================

DEVICE_CACHE: Dict[str, str] = {}
DEVICE_CACHE_LOCK = threading.Lock()

def scan_and_print_devices() -> None:
    """Faz um scan na rede e imprime todos os dispositivos Tuya encontrados."""
    log("[SCAN] Iniciando scan de dispositivos Tuya na rede...")
    
    try:
        # Usar timeout para evitar travamentos
        devices = scan_with_timeout(30)  # 30 segundos de timeout
        
        if devices is None:
            log("[SCAN] Timeout ou erro ao escanear dispositivos")
            return
        
        if not isinstance(devices, dict):
            log(f"[SCAN] Resultado inesperado de deviceScan(): {type(devices)}")
            return
        
        if not devices:
            log("[SCAN] Nenhum dispositivo Tuya encontrado.")
            return
        
        log(f"[SCAN] {len(devices)} dispositivo(s) encontrado(s):")
        for ip, dev in devices.items():
            gwid = dev.get("gwId")
            ver = dev.get("version") or dev.get("ver")
            log(f"[SCAN] gwId={gwid}  ip={ip}  ver={ver}")
    
    except Exception as e:
        log(f"[SCAN] Erro ao escanear dispositivos Tuya: {e}")
        traceback.print_exc()

def scan_devices() -> Dict[str, Any]:
    """Faz um scan na rede e retorna todos os dispositivos Tuya encontrados em formato dict."""
    log("[SCAN] Iniciando scan de dispositivos Tuya na rede...")
    discovered_devices = {}
    
    try:
        # Usar timeout para evitar travamentos
        devices = scan_with_timeout(30)  # 30 segundos de timeout
        
        if devices is None:
            log("[SCAN] Timeout ou erro ao escanear dispositivos")
            return {}
        
        if not isinstance(devices, dict):
            log(f"[SCAN] Resultado inesperado de deviceScan(): {type(devices)}")
            return {}
        
        if not devices:
            log("[SCAN] Nenhum dispositivo Tuya encontrado.")
            return {}
        
        log(f"[SCAN] {len(devices)} dispositivo(s) encontrado(s):")
        for ip, dev in devices.items():
            gwid = dev.get("gwId")
            ver = dev.get("version") or dev.get("ver")
            log(f"[SCAN] gwId={gwid}  ip={ip}  ver={ver}")
            
            if gwid:
                discovered_devices[gwid] = {
                    "id": gwid,
                    "ip": ip,
                    "version": ver
                }
    
    except Exception as e:
        log(f"[SCAN] Erro ao escanear dispositivos Tuya: {e}")
        traceback.print_exc()
    
    return discovered_devices

def scan_with_timeout(timeout_seconds: int = 30) -> Optional[Dict]:
    """Executa deviceScan com timeout para evitar travamentos."""
    result = [None]
    exception = [None]
    
    def scan_thread():
        try:
            result[0] = tinytuya.deviceScan()
        except Exception as e:
            exception[0] = e
    
    thread = threading.Thread(target=scan_thread, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    
    if thread.is_alive():
        log(f"[SCAN] Timeout após {timeout_seconds} segundos")
        return None
    
    if exception[0]:
        log(f"[SCAN] Exceção durante scan: {exception[0]}")
        return None
    
    return result[0]

def discover_tuya_ip(tuya_device_id: str) -> Optional[str]:
    """
    Tenta descobrir o IP LAN de um dispositivo Tuya pelo gwId (device_id),
    usando tinytuya.deviceScan() e guarda em cache.
    """
    # se já descobrimos antes, usa o cache (com lock)
    with DEVICE_CACHE_LOCK:
        if tuya_device_id in DEVICE_CACHE:
            ip_cached = DEVICE_CACHE[tuya_device_id]
            log(f"[DISCOVER] Usando IP em cache para {tuya_device_id}: {ip_cached}")
            return ip_cached
    
    log(f"[DISCOVER] Varrendo a rede para encontrar o device_id = {tuya_device_id} ...")
    
    try:
        # Usar timeout para evitar travamentos
        devices = scan_with_timeout(30)  # 30 segundos de timeout
        
        if devices is None:
            log(f"[DISCOVER] Timeout ou erro ao escanear dispositivos")
            return None
        
        if not isinstance(devices, dict):
            log(f"[DISCOVER] Resultado inesperado de deviceScan(): {type(devices)}")
            return None
        
        log(f"[DISCOVER] deviceScan encontrou {len(devices)} dispositivo(s).")
        
        for ip, dev in devices.items():
            gwid = dev.get("gwId")
            dev_ip = dev.get("ip", ip)
            log(f"[DISCOVER] Achado gwId={gwid} ip={dev_ip}")
            if gwid == tuya_device_id:
                log(f"[DISCOVER] Encontrado! device_id={gwid} ip={dev_ip}")
                with DEVICE_CACHE_LOCK:
                    DEVICE_CACHE[tuya_device_id] = dev_ip
                return dev_ip
        
        log(f"[DISCOVER] Nenhum dispositivo encontrado com device_id = {tuya_device_id}")
        return None
    
    except Exception as e:
        log(f"[DISCOVER] Erro ao escanear dispositivos Tuya: {e}")
        traceback.print_exc()
        return None

# =========================
# TUYA
# =========================

def recadastrar_device(
    tuya_device_id: str,
    site_id: str,
    local_key: Optional[str]
) -> Optional[str]:
    """
    Faz um recadastro leve do device:
    - escaneia a LAN para achar IP/protocolo,
    - atualiza/cria no Supabase usando o mesmo site_id,
    - atualiza o cache local.
    Retorna o lan_ip encontrado (se houver).
    """
    try:
        log(f"[REC] Tentando recadastrar device {tuya_device_id} (site_id={site_id})")
        lan_devices = scan_devices()
        if not lan_devices or tuya_device_id not in lan_devices:
            log(f"[REC] Device {tuya_device_id} não encontrado na LAN")
            return None
        
        lan_info = lan_devices[tuya_device_id]
        lan_ip = lan_info.get("ip")
        protocol_version = lan_info.get("version")
        if protocol_version:
            protocol_version = str(protocol_version)
        
        cache_version = normalize_version(protocol_version)
        save_device_to_cache(
            tuya_device_id=tuya_device_id,
            local_key=local_key,
            lan_ip=lan_ip,
            version=cache_version
        )
        
        db_devices = get_devices_from_db([tuya_device_id])
        if tuya_device_id in db_devices:
            update_device_in_db(
                tuya_device_id=tuya_device_id,
                site_id=site_id,
                local_key=local_key,
                lan_ip=lan_ip,
                protocol_version=protocol_version
            )
        else:
            create_device_in_db(
                tuya_device_id=tuya_device_id,
                site_id=site_id,
                name=site_id,
                local_key=local_key,
                lan_ip=lan_ip,
                protocol_version=protocol_version
            )
        
        return lan_ip
    except Exception as e:
        log(f"[REC] Erro ao recadastrar {tuya_device_id}: {e}")
        return None

def has_internet_connection(timeout_seconds: float = 2.0) -> bool:
    """Verifica conectividade externa básica (sem depender de DNS HTTP)."""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=timeout_seconds).close()
        return True
    except Exception:
        return False

def refresh_devices_once() -> bool:
    """Faz um refresh (status) nos dispositivos cacheados para manter a conexão ativa."""
    try:
        internet_ok = has_internet_connection()
        if internet_ok:
            flush_pending_heartbeat_logs(max_items=200)
        
        cache = load_devices_cache()
        device_ids = [k for k in cache.keys() if not k.startswith("_")]
        if not device_ids:
            log("[REFRESH] Nenhum dispositivo no cache para refresh")
            return False
        
        log(f"[REFRESH] Iniciando refresh de {len(device_ids)} dispositivo(s)")
        has_failures = False
        for device_id in device_ids:
            device_data = cache.get(device_id) or {}
            lan_ip = device_data.get("lan_ip")
            local_key = device_data.get("local_key")
            version = normalize_version(device_data.get("version") or device_data.get("protocol_version")) or 3.3
            
            if not lan_ip or not local_key:
                log(f"[REFRESH] Pulando {device_id} (IP/local_key ausentes)")
                continue
            
            try:
                success = False
                d = tinytuya.OutletDevice(device_id, lan_ip, local_key)
                d.set_version(version)
                status = tuya_status_with_timeout(d, timeout_seconds=8)
                if status:
                    log(f"[REFRESH] OK {device_id} @ {lan_ip}")
                    success = True
                else:
                    log(f"[REFRESH] Falha {device_id} @ {lan_ip} (timeout/erro). Tentando redescobrir IP...")
                    discovered_ip = discover_tuya_ip(device_id)
                    if discovered_ip and discovered_ip != lan_ip:
                        lan_ip = discovered_ip
                        log(f"[REFRESH] IP redescoberto: {lan_ip}. Tentando status novamente...")
                        d = tinytuya.OutletDevice(device_id, lan_ip, local_key)
                        d.set_version(version)
                        if tuya_status_with_timeout(d, timeout_seconds=8):
                            log(f"[REFRESH] OK após redescoberta {device_id} @ {lan_ip}")
                            success = True
                    if not success:
                        if internet_ok:
                            log(f"[REFRESH] Tentando recadastro do device {device_id}...")
                            rec_lan_ip = recadastrar_device(device_id, SITE_NAME, local_key)
                            if rec_lan_ip:
                                lan_ip = rec_lan_ip
                                log(f"[REFRESH] Recadastro encontrou IP {lan_ip}. Tentando status novamente...")
                                d = tinytuya.OutletDevice(device_id, lan_ip, local_key)
                                d.set_version(version)
                                if tuya_status_with_timeout(d, timeout_seconds=8):
                                    log(f"[REFRESH] OK após recadastro {device_id} @ {lan_ip}")
                                    success = True
                            else:
                                log(f"[REFRESH] Recadastro não encontrou o device {device_id}")
                        else:
                            log("[REFRESH] Sem internet - recadastro adiado para próximo ciclo")
                
                if success:
                    REFRESH_FAIL_COUNTS[device_id] = 0
                    previous_status = REFRESH_LAST_STATUS.get(device_id)
                    if previous_status is False:
                        if internet_ok:
                            # Dispara atualização no banco apenas na transição offline -> online
                            heartbeat_ok = update_device_heartbeat(device_id)
                            if heartbeat_ok:
                                REFRESH_LAST_STATUS[device_id] = True
                            else:
                                # Mantém como offline para tentar sincronizar novamente no próximo ciclo
                                REFRESH_LAST_STATUS[device_id] = False
                                has_failures = True
                        else:
                            # Sem internet: manter pendente para sincronizar quando voltar
                            REFRESH_LAST_STATUS[device_id] = False
                            has_failures = True
                    else:
                        REFRESH_LAST_STATUS[device_id] = True
                else:
                    REFRESH_FAIL_COUNTS[device_id] = REFRESH_FAIL_COUNTS.get(device_id, 0) + 1
                    REFRESH_LAST_STATUS[device_id] = False
                    has_failures = True
            except Exception as e:
                log(f"[REFRESH] Erro ao refrescar {device_id}: {e}")
                REFRESH_LAST_STATUS[device_id] = False
                has_failures = True
        return has_failures
    except Exception as e:
        log(f"[REFRESH] Erro geral no refresh: {e}")
        return True

def start_device_refresh_loop(interval_seconds: int = DEVICE_REFRESH_INTERVAL_SECONDS) -> None:
    """Inicia loop em background para refresh periódico."""
    def refresh_loop():
        while True:
            try:
                has_failures = refresh_devices_once()
            except Exception as e:
                log(f"[REFRESH] Erro no loop: {e}")
                has_failures = True
            sleep_seconds = (
                DEVICE_REFRESH_RETRY_ON_FAILURE_SECONDS
                if has_failures
                else interval_seconds
            )
            time.sleep(sleep_seconds)
    
    thread = threading.Thread(target=refresh_loop, daemon=True)
    thread.start()
    log(
        f"[REFRESH] Loop iniciado (intervalo normal: {interval_seconds}s, "
        f"retry em falha: {DEVICE_REFRESH_RETRY_ON_FAILURE_SECONDS}s)"
    )

def send_tuya_command(
    action: str,
    tuya_device_id: str,
    local_key: str,
    lan_ip: Optional[str],
    version: Optional[float] = None
) -> None:
    
    if not tuya_device_id:
        raise RuntimeError("Campo tuya_device_id é obrigatório")
    if not local_key:
        raise RuntimeError("Campo local_key é obrigatório")
    
    # Se não veio IP ou veio "auto", tenta descobrir
    if not lan_ip or str(lan_ip).lower() == "auto":
        log(f"[INFO] Nenhum lan_ip informado (ou 'auto'). Tentando descobrir IP do device {tuya_device_id}...")
        lan_ip = discover_tuya_ip(tuya_device_id)
        if not lan_ip:
            raise RuntimeError("Não foi possível descobrir o IP LAN do dispositivo Tuya.")
        # armazenar cache para próxima vez
        with DEVICE_CACHE_LOCK:
            DEVICE_CACHE[tuya_device_id] = lan_ip
    
    # Garante que venha só IP, nada de 'http://'
    lan_ip = str(lan_ip).strip()
    if lan_ip.startswith("http://") or lan_ip.startswith("https://"):
        raise RuntimeError("lan_ip deve ser apenas o IP (ex: 192.168.0.50), sem http:// e sem porta.")
    
    # Normalizar version: usar version or 3.3 e garantir float
    if version is None:
        version = 3.3
    else:
        try:
            version = float(version)
        except (ValueError, TypeError):
            version = 3.3
    
    log(f"[INFO] [{SITE_NAME}] Enviando '{action}' → {tuya_device_id} @ {lan_ip} (versão {version})")
    log(f"[INFO] local_key: {mask_local_key(local_key)}")
    
    def create_device(ip: str) -> Any:
        d = tinytuya.OutletDevice(tuya_device_id, ip, local_key)
        d.set_version(version)
        return d
    
    # Preflight: verificar se a placa responde antes do comando
    preflight_device = create_device(lan_ip)
    preflight_status = tuya_status_with_timeout(preflight_device, timeout_seconds=COMMAND_PREFLIGHT_TIMEOUT_SECONDS)
    if not preflight_status:
        log(f"[INFO] Preflight falhou para {tuya_device_id} @ {lan_ip}. Tentando redescobrir IP...")
        discovered_ip = discover_tuya_ip(tuya_device_id)
        if discovered_ip:
            lan_ip = discovered_ip
            log(f"[INFO] IP redescoberto: {lan_ip}")
            # atualizar cache após redescoberta
            with DEVICE_CACHE_LOCK:
                DEVICE_CACHE[tuya_device_id] = lan_ip
    
    last_error: Optional[Exception] = None
    for attempt in range(1, COMMAND_MAX_RETRIES + 1):
        try:
            if attempt > 1:
                log(f"[INFO] Tentativa {attempt}/{COMMAND_MAX_RETRIES} para enviar comando '{action}'")
                time.sleep(COMMAND_RETRY_DELAY_SECONDS)
            
            d = create_device(lan_ip)
            
            # Usar função com timeout aumentado para 20s
            resp = tuya_command_with_timeout(d, action, timeout_seconds=COMMAND_ACTION_TIMEOUT_SECONDS)
            
            if resp is None:
                # Timeout - tentar novamente se ainda houver tentativas
                if attempt < COMMAND_MAX_RETRIES:
                    log(f"[INFO] Timeout na tentativa {attempt}, tentando novamente...")
                    last_error = RuntimeError("Timeout ao enviar comando para dispositivo")
                    continue
                else:
                    # Última tentativa falhou - limpar cache apenas se todas falharam
                    log(f"[INFO] Todas as {COMMAND_MAX_RETRIES} tentativas falharam por timeout")
                    with DEVICE_CACHE_LOCK:
                        if tuya_device_id in DEVICE_CACHE:
                            log(f"[INFO] Limpando cache de IP para {tuya_device_id} devido a timeout após {COMMAND_MAX_RETRIES} tentativas")
                            del DEVICE_CACHE[tuya_device_id]
                    raise RuntimeError(f"Timeout ao enviar comando para dispositivo após {COMMAND_MAX_RETRIES} tentativas")
            
            post_status = tuya_status_with_timeout(d, timeout_seconds=COMMAND_PREFLIGHT_TIMEOUT_SECONDS)
            desired_state = action == "on"
            actual_state = extract_tuya_power_state(post_status)
            if actual_state is None:
                raise RuntimeError("Não foi possível confirmar o estado final da placa após o comando")
            if actual_state != desired_state:
                raise RuntimeError(
                    f"Comando enviado, mas a placa continuou {'ligada' if actual_state else 'desligada'}"
                )

            # Sucesso com confirmação do estado final.
            log(f"[DEBUG] Resposta do dispositivo: {resp}")
            log(f"[DEBUG] Status confirmado após comando: {post_status}")
            if attempt > 1:
                log(f"[INFO] Comando enviado com sucesso na tentativa {attempt}")
            return {
                "command_response": resp,
                "confirmed_status": post_status,
            }
        
        except Exception as e:
            last_error = e
            log(f"[INFO] Erro na tentativa {attempt}/{COMMAND_MAX_RETRIES}: {e}")
            
            # Se não for a última tentativa, continuar
            if attempt < COMMAND_MAX_RETRIES:
                log(f"[INFO] Tentando novamente...")
                # Tentar redescobrir IP antes da próxima tentativa
                discovered_ip = discover_tuya_ip(tuya_device_id)
                if discovered_ip:
                    lan_ip = discovered_ip
                    log(f"[INFO] IP redescoberto antes da próxima tentativa: {lan_ip}")
                continue
            else:
                # Última tentativa falhou - limpar cache apenas se todas falharam
                log(f"[INFO] Todas as {COMMAND_MAX_RETRIES} tentativas falharam")
                with DEVICE_CACHE_LOCK:
                    if tuya_device_id in DEVICE_CACHE:
                        log(f"[INFO] Limpando cache de IP para {tuya_device_id} devido a erro após {COMMAND_MAX_RETRIES} tentativas")
                        del DEVICE_CACHE[tuya_device_id]
                raise RuntimeError(f"Erro ao enviar comando para dispositivo após {COMMAND_MAX_RETRIES} tentativas: {e}")
    
    # Se chegou aqui, todas as tentativas falharam
    if last_error:
        raise last_error
    raise RuntimeError("Erro desconhecido ao enviar comando para dispositivo")

# =========================
# API HTTP
# =========================

app = Flask(__name__)

def validate_admin_token() -> bool:
    """Valida o token de admin do header X-ADMIN-TOKEN."""
    admin_token_header = request.headers.get("X-ADMIN-TOKEN", "")
    
    # Obter token do config ou env
    expected_token = ADMIN_TOKEN or os.getenv("ADMIN_TOKEN", "")
    
    if not expected_token:
        log("[AUTH] Admin token não configurado")
        return False
    
    if admin_token_header != expected_token:
        log(f"[AUTH] Token inválido (recebido: {mask_local_key(admin_token_header, 4)})")
        return False
    
    return True

@app.route("/", methods=["GET"])
def dashboard():
    return render_template("index.html")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "site": SITE_NAME}), 200

@app.route("/api/status", methods=["GET"])
def api_status():
    cache = load_devices_cache()
    device_ids = [k for k in cache.keys() if not k.startswith("_")]
    return jsonify({
        "site": SITE_NAME,
        "version": APP_VERSION,
        "supabase_configured": bool(SUPABASE_CONFIG.get("url") and SUPABASE_CONFIG.get("anon_key")),
        "realtime_connected": REMOTE_COMMAND_WS_APP is not None,
        "devices_cached": len(device_ids),
    }), 200

@app.route("/api/devices/cached", methods=["GET"])
def api_devices_cached():
    cache = load_devices_cache()
    devices = []
    for device_id, data in cache.items():
        if device_id.startswith("_"):
            continue
        local_key = data.get("local_key", "")
        masked = (local_key[:6] + "****") if len(local_key) > 6 else ("****" if local_key else "—")
        devices.append({
            "id": device_id,
            "lan_ip": data.get("lan_ip"),
            "version": data.get("version"),
            "local_key_masked": masked,
        })
    return jsonify({"devices": devices}), 200

# =========================
# WI-FI (nmcli)
# =========================

def _nmcli(*args, sudo=False, timeout=30) -> subprocess.CompletedProcess:
    cmd = (["sudo", "nmcli"] if sudo else ["nmcli"]) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _parse_nmcli_terse(output: str, num_fields: int) -> List[List[str]]:
    """Divide linhas do nmcli -t respeitando ':' escapados como '\\:'."""
    rows = []
    for line in output.strip().splitlines():
        parts: List[str] = []
        cur = ""
        i = 0
        while i < len(line):
            if line[i] == "\\" and i + 1 < len(line) and line[i + 1] == ":":
                cur += ":"
                i += 2
            elif line[i] == ":":
                parts.append(cur)
                cur = ""
                i += 1
            else:
                cur += line[i]
                i += 1
        parts.append(cur)
        if len(parts) >= num_fields:
            rows.append(parts[:num_fields])
    return rows

@app.route("/api/wifi/status", methods=["GET"])
def api_wifi_status():
    try:
        r = _nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL,DEVICE", "device", "wifi")
        current = None
        for row in _parse_nmcli_terse(r.stdout, 4):
            if row[0].lower() == "yes" and row[1]:
                current = {"ssid": row[1], "signal": int(row[2]) if row[2].isdigit() else 0, "device": row[3]}
                break
        return jsonify({"current": current}), 200
    except FileNotFoundError:
        return jsonify({"error": "nmcli não encontrado — NetworkManager não está instalado"}), 501
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _do_wifi_scan() -> List[Dict]:
    r = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes", sudo=True, timeout=35)
    networks: List[Dict] = []
    seen: set = set()
    for row in _parse_nmcli_terse(r.stdout, 3):
        ssid, signal, security = row[0], row[1], row[2]
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        networks.append({
            "ssid": ssid,
            "signal": int(signal) if signal.isdigit() else 0,
            "security": security or "Aberta",
        })
    networks.sort(key=lambda x: x["signal"], reverse=True)
    return networks

@app.route("/api/wifi/scan", methods=["GET"])
def api_wifi_scan():
    try:
        if _hotspot_running():
            try:
                with open(WIFI_SCAN_CACHE_PATH) as f:
                    networks = json.load(f)
                return jsonify({"networks": networks, "cached": True}), 200
            except Exception:
                return jsonify({"networks": [], "cached": True, "error": "Hotspot ativo — escaneie antes de ligar o hotspot"}), 200
        networks = _do_wifi_scan()
        try:
            with open(WIFI_SCAN_CACHE_PATH, "w") as f:
                json.dump(networks, f)
        except Exception:
            pass
        return jsonify({"networks": networks}), 200
    except FileNotFoundError:
        return jsonify({"error": "nmcli não encontrado"}), 501
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "").strip()

    if not ssid:
        return jsonify({"ok": False, "error": "SSID obrigatório"}), 400

    was_hotspot = _hotspot_running()

    if was_hotspot:
        # Em modo hotspot: dispara em background para responder ANTES de derrubar o AP.
        # O cliente recebe a resposta enquanto o hotspot ainda está no ar, depois o AP cai.
        def _do_connect_bg():
            time.sleep(0.8)  # garante que a resposta HTTP foi entregue ao cliente
            _stop_hotspot_internal()
            time.sleep(1)
            try:
                args = ["device", "wifi", "connect", ssid]
                if password:
                    args += ["password", password]
                r = _nmcli(*args, sudo=True, timeout=45)
                if r.returncode == 0:
                    log(f"[WIFI] Conectado a {ssid} (via hotspot)")
                else:
                    log(f"[WIFI] Falha ao conectar a {ssid}: {(r.stderr or r.stdout).strip()}")
                    log("[WIFI] Restaurando hotspot")
                    _start_hotspot_internal()
            except Exception as ex:
                log(f"[WIFI] Erro ao conectar (bg): {ex}")
                _start_hotspot_internal()

        threading.Thread(target=_do_connect_bg, daemon=True).start()
        return jsonify({
            "ok": True,
            "async": True,
            "ssid": ssid,
            "message": f"Tentando conectar a '{ssid}'..."
        }), 200

    # Sem hotspot: conexão síncrona normal
    try:
        args = ["device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        r = _nmcli(*args, sudo=True, timeout=40)
        if r.returncode == 0:
            log(f"[WIFI] Conectado a {ssid}")
            return jsonify({"ok": True, "message": f"Conectado a '{ssid}'"}), 200
        err = (r.stderr or r.stdout).strip()
        log(f"[WIFI] Falha ao conectar a {ssid}: {err}")
        return jsonify({"ok": False, "error": err}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout — verifique a senha e tente novamente"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/wifi/backup", methods=["GET"])
def api_wifi_backup_get():
    ssid = BACKUP_WIFI.get("ssid", "")
    return jsonify({"ssid": ssid, "configured": bool(ssid)}), 200

@app.route("/api/wifi/backup", methods=["POST"])
def api_wifi_backup_post():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    if not ssid:
        return jsonify({"ok": False, "error": "SSID obrigatório"}), 400
    update_backup_wifi(ssid, password)
    return jsonify({"ok": True, "ssid": ssid}), 200

# ── Monitor de conectividade ──────────────────────────────────────────────────
_CONNECTIVITY_FAIL_COUNT = 0

def _check_connectivity() -> bool:
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "3", "8.8.8.8"], capture_output=True, timeout=6)
        return r.returncode == 0
    except Exception:
        return False

def _connectivity_watch_loop():
    global _CONNECTIVITY_FAIL_COUNT
    # Checagem no boot: aguarda NM conectar e sobe hotspot se não tiver internet
    time.sleep(45)
    if not _check_connectivity() and not _hotspot_running():
        log("[WIFI] Sem internet no boot — iniciando hotspot automático (MRIT-Setup / mrit1234)")
        _start_hotspot_internal("MRIT-Setup", "mrit1234")

    while True:
        time.sleep(60)
        if _hotspot_running():
            _CONNECTIVITY_FAIL_COUNT = 0
            continue
        if _check_connectivity():
            _CONNECTIVITY_FAIL_COUNT = 0
        else:
            _CONNECTIVITY_FAIL_COUNT += 1
            log(f"[WIFI] Sem conectividade ({_CONNECTIVITY_FAIL_COUNT}/3)")
            if _CONNECTIVITY_FAIL_COUNT >= 3:
                backup = BACKUP_WIFI
                if backup.get("ssid"):
                    log(f"[WIFI] Tentando rede reserva: {backup['ssid']}")
                    try:
                        args = ["device", "wifi", "connect", backup["ssid"]]
                        if backup.get("password"):
                            args += ["password", backup["password"]]
                        r = _nmcli(*args, sudo=True, timeout=40)
                        if r.returncode == 0:
                            log(f"[WIFI] Conectado à rede reserva: {backup['ssid']}")
                            _CONNECTIVITY_FAIL_COUNT = 0
                        else:
                            log(f"[WIFI] Falha na reserva: {(r.stderr or r.stdout).strip()}")
                    except Exception as e:
                        log(f"[WIFI] Erro ao tentar reserva: {e}")
                else:
                    log("[WIFI] Sem reserva — subindo hotspot para reconfiguração")
                    _start_hotspot_internal("MRIT-Setup", "mrit1234")
                    _CONNECTIVITY_FAIL_COUNT = 0

HOTSPOT_CONF_PATH   = "/tmp/mrit-hostapd.conf"
HOTSPOT_PID_PATH    = "/tmp/mrit-hostapd.pid"
DNSMASQ_CONF_PATH   = "/tmp/mrit-dnsmasq.conf"
DNSMASQ_PID_PATH    = "/tmp/mrit-dnsmasq.pid"
WIFI_SCAN_CACHE_PATH = "/tmp/mrit-wifi-scan.json"

def _hotspot_running() -> bool:
    r = subprocess.run(["pgrep", "-F", HOTSPOT_PID_PATH], capture_output=True)
    return r.returncode == 0

def _start_hotspot_internal(ssid: str = "MRIT-Setup", password: str = "mrit1234"):
    """Inicia o hotspot via hostapd. Retorna (ok, error_msg)."""
    try:
        try:
            networks = _do_wifi_scan()
            with open(WIFI_SCAN_CACHE_PATH, "w") as f:
                json.dump(networks, f)
        except Exception:
            pass

        subprocess.run(["sudo", "pkill", "-F", HOTSPOT_PID_PATH], capture_output=True)
        subprocess.run(["sudo", "pkill", "-F", DNSMASQ_PID_PATH], capture_output=True)
        time.sleep(1)

        with open(HOTSPOT_CONF_PATH, "w") as f:
            f.write(f"interface=wlan0\ndriver=nl80211\nssid={ssid}\n"
                    f"hw_mode=g\nchannel=6\nwmm_enabled=0\nmacaddr_acl=0\n"
                    f"auth_algs=1\nwpa=2\nwpa_passphrase={password}\n"
                    f"wpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\n")

        with open(DNSMASQ_CONF_PATH, "w") as f:
            f.write("interface=wlan0\nbind-interfaces\nexcept-interface=lo\n"
                    "dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h\n")

        subprocess.run(["sudo", "nmcli", "device", "disconnect", "wlan0"], capture_output=True, timeout=10)
        subprocess.run(["sudo", "nmcli", "device", "set", "wlan0", "managed", "no"], capture_output=True, timeout=10)
        time.sleep(1)

        subprocess.run(["sudo", "ip", "addr", "flush", "dev", "wlan0"], capture_output=True, timeout=5)
        subprocess.run(["sudo", "ip", "addr", "add", "192.168.4.1/24", "dev", "wlan0"], capture_output=True, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", "wlan0", "up"], capture_output=True, timeout=5)

        r = subprocess.run(
            ["sudo", "hostapd", "-B", "-P", HOTSPOT_PID_PATH, HOTSPOT_CONF_PATH],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            subprocess.run(["sudo", "nmcli", "device", "set", "wlan0", "managed", "yes"], capture_output=True)
            return False, (r.stderr or r.stdout).strip()

        subprocess.run(
            ["sudo", "dnsmasq", f"--conf-file={DNSMASQ_CONF_PATH}", f"--pid-file={DNSMASQ_PID_PATH}"],
            capture_output=True, timeout=10
        )
        log(f"[WIFI] Hotspot iniciado: SSID={ssid}")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Timeout ao iniciar hotspot"
    except Exception as e:
        return False, str(e)

def _stop_hotspot_internal():
    """Para o hotspot e devolve wlan0 ao NetworkManager."""
    subprocess.run(["sudo", "pkill", "-F", HOTSPOT_PID_PATH], capture_output=True, timeout=10)
    subprocess.run(["sudo", "pkill", "-F", DNSMASQ_PID_PATH], capture_output=True, timeout=10)
    time.sleep(1)
    subprocess.run(["sudo", "ip", "addr", "flush", "dev", "wlan0"], capture_output=True, timeout=5)
    subprocess.run(["sudo", "nmcli", "device", "set", "wlan0", "managed", "yes"], capture_output=True, timeout=10)

@app.route("/api/wifi/hotspot/status", methods=["GET"])
def api_hotspot_status():
    return jsonify({"active": _hotspot_running()}), 200

@app.route("/api/wifi/hotspot/start", methods=["POST"])
def api_hotspot_start():
    data     = request.get_json(silent=True) or {}
    ssid     = data.get("ssid",     "MRIT-Setup").strip() or "MRIT-Setup"
    password = data.get("password", "mrit1234").strip()
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Senha precisa ter pelo menos 8 caracteres"}), 400
    ok, err = _start_hotspot_internal(ssid, password)
    if ok:
        return jsonify({"ok": True, "ssid": ssid, "password": password}), 200
    return jsonify({"ok": False, "error": err}), 500

@app.route("/api/wifi/hotspot/stop", methods=["POST"])
def api_hotspot_stop():
    try:
        _stop_hotspot_internal()
        log("[WIFI] Hotspot desligado")
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/config/tuya", methods=["POST"])
def api_config_tuya():
    """
    Configura as contas Tuya para buscar local_key.
    Requer header X-ADMIN-TOKEN.
    Body:
    {
        "accounts": [
            {
                "access_id": "td7tp3cvq3nrc35emwg3",
                "access_key": "bbcdaa3dfe9545fca4326fcfa1cf3e2c",
                "endpoint": "https://openapi.tuyaus.com",
                "uid": "az1715569264750N2mUr"
            },
            ...
        ]
    }
    """
    # Validar token de admin
    if not validate_admin_token():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"ok": False, "error": "JSON inválido"}), 400
        
        accounts = data.get("accounts", [])
        
        if not accounts:
            return jsonify({"ok": False, "error": "Nenhuma conta fornecida"}), 400
        
        # Validar contas
        for account in accounts:
            required_fields = ["access_id", "access_key", "endpoint", "uid"]
            for field in required_fields:
                if field not in account:
                    return jsonify({"ok": False, "error": f"Campo obrigatório ausente: {field}"}), 400
        
        update_tuya_accounts(accounts)
        
        return jsonify({
            "ok": True,
            "message": f"{len(accounts)} conta(s) Tuya configurada(s)"
        }), 200
        
    except Exception as e:
        err = str(e)
        log(f"[ERRO] API /config/tuya: {err}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": err}), 500

@app.route("/config/supabase", methods=["POST"])
def api_config_supabase():
    """
    Configura as credenciais do Supabase.
    Requer header X-ADMIN-TOKEN.
    
    Body:
    {
        "url": "https://kihyhoqbrkwbfudttevo.supabase.co",
        "anon_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    }
    """
    # Validar token de admin
    if not validate_admin_token():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"ok": False, "error": "JSON inválido"}), 400
        
        url = data.get("url")
        anon_key = data.get("anon_key")
        
        if not url or not anon_key:
            return jsonify({
                "ok": False,
                "error": "url e anon_key são obrigatórios"
            }), 400
        
        update_supabase_config(url, anon_key)
        
        return jsonify({
            "ok": True,
            "message": "Configuração do Supabase atualizada com sucesso"
        }), 200
        
    except Exception as e:
        err = str(e)
        log(f"[ERRO] API /config/supabase: {err}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": err}), 500

def normalize_version(version: Any) -> Optional[float]:
    """
    Normaliza version para float quando possível.
    Aceita None/"" como None.
    Se inválido, retorna None.
    """
    if version is None or version == "":
        return None
    
    try:
        return float(version)
    except (ValueError, TypeError):
        return None

def current_timestamp_iso() -> str:
    """Retorna timestamp UTC no formato ISO com milissegundos."""
    now_utc = datetime.now(timezone.utc)
    return now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"

def execute_tuya_command_request(data: Dict[str, Any], command_received_iso: Optional[str] = None) -> Dict[str, Any]:
    """Executa a mesma lógica de /tuya/command para uso via HTTP e comandos remotos."""
    if command_received_iso is None:
        command_received_iso = current_timestamp_iso()

    action = data.get("action")
    tuya_device_id = data.get("tuya_device_id")
    local_key = data.get("local_key")
    lan_ip = data.get("lan_ip")  # pode vir None, vazio ou "auto"
    version = data.get("version")  # pode vir None, vazio ou um número

    if action not in ("on", "off"):
        raise ValueError("action deve ser 'on' ou 'off'")

    if not tuya_device_id:
        last_active = get_last_active_device()
        if last_active:
            tuya_device_id = last_active
            log(f"[COMMAND] Usando último dispositivo ativo: {tuya_device_id}")
        else:
            cache = load_devices_cache()
            device_ids = [k for k in cache.keys() if not k.startswith("_")]
            if len(device_ids) == 1:
                tuya_device_id = device_ids[0]
                log(f"[COMMAND] Usando único dispositivo no cache: {tuya_device_id}")
            else:
                raise ValueError("tuya_device_id é obrigatório quando há múltiplos dispositivos no cache")

    if not tuya_device_id:
        raise ValueError("tuya_device_id é obrigatório")

    version = normalize_version(version)
    if version is not None and version <= 0:
        raise ValueError("version deve ser um número positivo")

    device_name = data.get("device_name")
    used_fallback = False
    fallback_source = None

    if not local_key or not lan_ip or lan_ip == "auto" or version is None:
        log("[COMMAND] Dados incompletos no JSON - buscando do cache persistente/banco")
        log(f"[COMMAND] local_key presente: {bool(local_key)}, lan_ip: {lan_ip}, version: {version}")

        cached_device = get_device_from_cache(tuya_device_id)
        if cached_device:
            log("[COMMAND] Device encontrado no cache persistente")
            used_fallback = True
            fallback_source = "cache_local"

            if not local_key:
                local_key = cached_device.get('local_key')
                if local_key:
                    log(f"[COMMAND] local_key obtida do cache local: {mask_local_key(local_key)}")

            if not lan_ip or lan_ip == "auto":
                lan_ip = cached_device.get('lan_ip')
                if lan_ip:
                    log(f"[COMMAND] lan_ip obtido do cache local: {lan_ip}")

            if version is None:
                cached_version = cached_device.get('version')
                if cached_version:
                    version = normalize_version(cached_version)
                    if version:
                        log(f"[COMMAND] version obtida do cache local: {version}")

        if not local_key or not lan_ip or lan_ip == "auto" or version is None:
            db_devices = get_devices_from_db([tuya_device_id])
            if tuya_device_id in db_devices:
                db_device = db_devices[tuya_device_id]
                log("[COMMAND] Device encontrado no banco, usando dados do banco")
                used_fallback = True
                fallback_source = "banco"

                if not local_key:
                    local_key = db_device.get('local_key')
                    if local_key:
                        log(f"[COMMAND] local_key obtida do banco: {mask_local_key(local_key)}")

                if not lan_ip or lan_ip == "auto":
                    lan_ip = db_device.get('lan_ip')
                    if lan_ip:
                        log(f"[COMMAND] lan_ip obtido do banco: {lan_ip}")

                if version is None:
                    protocol_version = db_device.get('protocol_version')
                    if protocol_version:
                        version = normalize_version(protocol_version)
                        if version:
                            log(f"[COMMAND] version obtida do banco: {version}")
            else:
                log(f"[COMMAND] Device {tuya_device_id} não encontrado no banco")

    if not local_key:
        raise ValueError("local_key é obrigatório e não foi encontrado no cache ou banco")

    version_float = float(version) if version is not None else None

    command_result = send_tuya_command(
        action=action,
        tuya_device_id=tuya_device_id,
        local_key=local_key,
        lan_ip=lan_ip,
        version=version_float
    )

    site_id = get_device_site_id_from_db(tuya_device_id) or SITE_NAME
    insert_heartbeat_ok_log(
        site_id=site_id,
        status="ok",
        tuya_device_id=tuya_device_id,
        event_time_iso=command_received_iso
    )

    if local_key and lan_ip and version_float:
        save_device_to_cache(
            tuya_device_id=tuya_device_id,
            local_key=local_key,
            lan_ip=lan_ip,
            version=version_float,
            device_name=device_name
        )
        set_last_active_device(tuya_device_id)
        log("[COMMAND] Dados salvos no cache persistente para próximas chamadas")

    response_data = {
        "ok": True,
        "device": {
            "id": tuya_device_id,
            "ip": str(lan_ip) if lan_ip else "",
            "version": str(version_float) if version_float else ""
        },
        "command_result": command_result.get("command_response") if isinstance(command_result, dict) else None,
        "device_status": command_result.get("confirmed_status") if isinstance(command_result, dict) else None,
    }

    if used_fallback:
        log(f"[COMMAND] Retornando dados do dispositivo (usado fallback: {fallback_source})")
    else:
        log("[COMMAND] Retornando dados do dispositivo (dados do JSON)")

    return response_data

def resolve_device_context_for_test(record: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve dados do device para um teste remoto sem alterar o estado da placa."""
    requested_device_id = record.get("tuya_device_id")
    resolved_device_id = requested_device_id
    resolution_source = "request"

    if not resolved_device_id:
        last_active = get_last_active_device()
        if last_active:
            resolved_device_id = last_active
            resolution_source = "last_active"
        else:
            cache = load_devices_cache()
            device_ids = [k for k in cache.keys() if not k.startswith("_")]
            if len(device_ids) == 1:
                resolved_device_id = device_ids[0]
                resolution_source = "cache_single"

    if not resolved_device_id:
        raise ValueError("tuya_device_id é obrigatório para teste quando não há dispositivo único/ativo")

    cache_device = get_device_from_cache(resolved_device_id) or {}
    db_device = get_devices_from_db([resolved_device_id]).get(resolved_device_id, {})

    local_key = (
        record.get("local_key")
        or cache_device.get("local_key")
        or db_device.get("local_key")
    )
    lan_ip = (
        record.get("lan_ip")
        or cache_device.get("lan_ip")
        or db_device.get("lan_ip")
    )
    version = normalize_version(
        record.get("version")
        or cache_device.get("version")
        or db_device.get("protocol_version")
    )

    return {
        "tuya_device_id": resolved_device_id,
        "resolution_source": resolution_source,
        "local_key": local_key,
        "lan_ip": lan_ip,
        "version": version,
        "cache_device": cache_device,
        "db_device": db_device,
    }

def run_remote_system_test(record: Dict[str, Any]) -> Dict[str, Any]:
    """Executa um teste remoto de saúde do gateway e da placa, sem alterar estado."""
    context = resolve_device_context_for_test(record)
    tuya_device_id = context["tuya_device_id"]
    lan_ip = context["lan_ip"]
    local_key = context["local_key"]
    version = context["version"] or 3.3
    cache_device = context["cache_device"]
    db_device = context["db_device"]

    device_ping_ok = False
    device_status_payload = None
    ping_error = None

    if lan_ip and local_key:
        try:
            device = tinytuya.OutletDevice(tuya_device_id, lan_ip, local_key)
            device.set_version(version)
            status = tuya_status_with_timeout(device, timeout_seconds=COMMAND_PREFLIGHT_TIMEOUT_SECONDS)
            if status:
                device_ping_ok = True
                device_status_payload = status
            else:
                ping_error = "Placa não respondeu ao status()"
        except Exception as e:
            ping_error = str(e)
    else:
        ping_error = "Dados insuficientes para ping (lan_ip/local_key ausentes)"

    checks = {
        "server_running": True,
        "supabase_configured": bool(SUPABASE_CONFIG.get("url") and SUPABASE_CONFIG.get("anon_key")),
        "device_found_in_db": bool(db_device),
        "device_found_in_cache": bool(cache_device),
        "device_has_lan_ip": bool(lan_ip),
        "device_has_local_key": bool(local_key),
        "device_ping_ok": device_ping_ok,
    }

    all_ok = all([
        checks["server_running"],
        checks["supabase_configured"],
        checks["device_found_in_db"] or checks["device_found_in_cache"],
        checks["device_has_lan_ip"],
        checks["device_has_local_key"],
        checks["device_ping_ok"],
    ])

    result = {
        "ok": all_ok,
        "action": "test",
        "site": SITE_NAME,
        "app_version": APP_VERSION,
        "device": {
            "id": tuya_device_id,
            "lan_ip": lan_ip or "",
            "version": str(version) if version else "",
            "resolution_source": context["resolution_source"],
        },
        "checks": checks,
    }

    if device_status_payload is not None:
        result["device_status"] = device_status_payload

    if ping_error:
        result["ping_error"] = ping_error

    return result

def execute_remote_command_action(record: Dict[str, Any]) -> Dict[str, Any]:
    """Executa a ação do comando remoto."""
    action = record.get("action")
    if action in ("on", "off"):
        payload = {
            "action": action,
            "tuya_device_id": record.get("tuya_device_id"),
            "local_key": record.get("local_key"),
            "lan_ip": record.get("lan_ip"),
            "version": record.get("version"),
            "device_name": record.get("device_name"),
        }
        return execute_tuya_command_request(payload)

    if action == "test":
        return run_remote_system_test(record)

    raise ValueError(f"Ação remota inválida: {action}")

def get_supabase_realtime_url() -> str:
    """Monta a URL do websocket Realtime a partir da URL base do Supabase."""
    supabase_url = SUPABASE_CONFIG.get("url")
    anon_key = SUPABASE_CONFIG.get("anon_key")
    if not supabase_url or not anon_key:
        raise RuntimeError("Configuração do Supabase não encontrada para Realtime")

    parsed = urlparse(supabase_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("URL do Supabase inválida para Realtime")

    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/realtime/v1/websocket?apikey={anon_key}&vsn=1.0.0"

def next_remote_command_ref() -> str:
    """Gera refs sequenciais para mensagens do protocolo Phoenix."""
    global REMOTE_COMMAND_REF_COUNTER
    with REMOTE_COMMAND_WS_LOCK:
        REMOTE_COMMAND_REF_COUNTER += 1
        return str(REMOTE_COMMAND_REF_COUNTER)

def claim_remote_command(command_id: Any) -> bool:
    """Marca um comando pendente como processing para evitar execução duplicada."""
    if not REQUESTS_AVAILABLE:
        return False

    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        claim_url = f"{base_url}/{REMOTE_COMMAND_TABLE}?id=eq.{command_id}&status=eq.pending"
        payload = {
            "status": "processing",
            "claimed_at": current_timestamp_iso(),
            "processor_site_id": SITE_NAME,
        }
        response = requests.patch(
            claim_url,
            json=payload,
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        claimed = bool(data)
        if claimed:
            log(f"[REMOTE] Comando {command_id} reivindicado para processamento")
        else:
            log(f"[REMOTE] Comando {command_id} já não estava pendente")
        return claimed
    except Exception as e:
        log(f"[REMOTE] Erro ao reivindicar comando {command_id}: {e}")
        traceback.print_exc()
        return False

def update_remote_command_status(
    command_id: Any,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None
) -> bool:
    """Atualiza o status final do comando remoto no banco."""
    if not REQUESTS_AVAILABLE:
        return False

    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        update_url = f"{base_url}/{REMOTE_COMMAND_TABLE}?id=eq.{command_id}"
        payload: Dict[str, Any] = {
            "status": status,
            "updated_at": current_timestamp_iso(),
        }

        if status in ("done", "error"):
            payload["executed_at"] = current_timestamp_iso()

        if result is not None:
            payload["result"] = result

        if error_message is not None:
            payload["error_message"] = error_message[:500]

        response = requests.patch(update_url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        log(f"[REMOTE] Comando {command_id} marcado como {status}")
        return True
    except Exception as e:
        log(f"[REMOTE] Erro ao atualizar status do comando {command_id}: {e}")
        traceback.print_exc()
        return False

def delete_remote_command(command_id: Any) -> bool:
    """Remove um comando remoto do banco."""
    if not REQUESTS_AVAILABLE:
        return False

    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        delete_url = f"{base_url}/{REMOTE_COMMAND_TABLE}?id=eq.{command_id}"
        response = requests.delete(delete_url, headers=headers, timeout=15)
        response.raise_for_status()
        log(f"[REMOTE] Comando {command_id} removido da fila")
        return True
    except Exception as e:
        log(f"[REMOTE] Erro ao remover comando {command_id}: {e}")
        traceback.print_exc()
        return False

def find_existing_saved_test_command(site_id: str, exclude_command_id: Any) -> Optional[Dict[str, Any]]:
    """Busca um comando de teste já salvo para o mesmo site, excluindo o atual."""
    if not REQUESTS_AVAILABLE:
        return None

    try:
        base_url = get_supabase_url()
        headers = get_supabase_headers()
        find_url = (
            f"{base_url}/{REMOTE_COMMAND_TABLE}"
            f"?site_id=eq.{site_id}"
            f"&action=eq.test"
            f"&id=neq.{exclude_command_id}"
            f"&select=id"
            f"&order=updated_at.desc.nullslast,created_at.desc"
            f"&limit=1"
        )
        response = requests.get(find_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data:
            return data[0]
        return None
    except Exception as e:
        log(f"[REMOTE] Erro ao buscar teste salvo para site_id={site_id}: {e}")
        traceback.print_exc()
        return None

def save_single_test_command_result(
    command_id: Any,
    site_id: str,
    result: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None
) -> bool:
    """
    Mantém apenas um registro de teste por site.
    Se já existir um teste anterior para o site, sobrescreve o antigo e remove o novo.
    """
    target_command_id = command_id
    existing_saved = find_existing_saved_test_command(site_id, command_id)
    if existing_saved and existing_saved.get("id") is not None:
        target_command_id = existing_saved["id"]

    status = "done" if error_message is None else "error"
    ok = update_remote_command_status(
        target_command_id,
        status=status,
        result=result,
        error_message=error_message
    )
    if not ok:
        return False

    if target_command_id != command_id:
        delete_remote_command(command_id)

    return True

def process_remote_command_record(record: Dict[str, Any]) -> None:
    """Processa um comando remoto recebido via Realtime."""
    command_id = record.get("id")
    action = record.get("action")
    status = record.get("status")
    command_site_id = record.get("site_id")

    if command_site_id and command_site_id != SITE_NAME:
        log(f"[REMOTE] Ignorando comando {command_id}: site_id diferente ({command_site_id})")
        return

    if status not in (None, "pending"):
        log(f"[REMOTE] Ignorando comando {command_id}: status atual = {status}")
        return

    if not command_id:
        log("[REMOTE] Ignorando comando sem id")
        return

    if not claim_remote_command(command_id):
        return

    try:
        response_data = execute_remote_command_action(record)
        if action in ("on", "off"):
            update_remote_command_status(command_id, "done", result=response_data)
            log(f"[REMOTE] Comando {command_id} mantido na tabela para diagnósticos")
        elif action == "test":
            save_single_test_command_result(
                command_id=command_id,
                site_id=command_site_id or SITE_NAME,
                result=response_data
            )
        else:
            update_remote_command_status(command_id, "done", result=response_data)
    except Exception as e:
        err = str(e)
        log(f"[REMOTE] Falha ao executar comando remoto {command_id}: {err}")
        if action == "test":
            save_single_test_command_result(
                command_id=command_id,
                site_id=command_site_id or SITE_NAME,
                error_message=err
            )
        else:
            update_remote_command_status(command_id, "error", error_message=err)

def handle_remote_realtime_message(message: str) -> None:
    """Processa mensagens recebidas do websocket Realtime do Supabase."""
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        log(f"[REMOTE] Mensagem inválida recebida do Realtime: {message[:200]}")
        return

    event = payload.get("event")
    topic = payload.get("topic")

    if event in ("phx_reply", "system", "heartbeat"):
        return

    if topic != REMOTE_COMMAND_TOPIC or event != "postgres_changes":
        return

    data = payload.get("payload", {}).get("data", {})
    record = data.get("record") or {}
    if not isinstance(record, dict):
        return

    threading.Thread(
        target=process_remote_command_record,
        args=(record,),
        daemon=True
    ).start()

def send_remote_realtime_heartbeat(ws_app: Any) -> None:
    """Envia heartbeats do protocolo Phoenix enquanto o websocket estiver aberto."""
    while True:
        time.sleep(REMOTE_COMMAND_HEARTBEAT_SECONDS)
        with REMOTE_COMMAND_WS_LOCK:
            active_ws = REMOTE_COMMAND_WS_APP
        if active_ws is not ws_app:
            return
        try:
            ws_app.send(json.dumps({
                "topic": "phoenix",
                "event": "heartbeat",
                "payload": {},
                "ref": next_remote_command_ref()
            }))
        except Exception as e:
            log(f"[REMOTE] Erro ao enviar heartbeat Realtime: {e}")
            return

def remote_command_listener_loop() -> None:
    """Mantém uma conexão Realtime com o Supabase para ouvir comandos remotos."""
    if not WEBSOCKET_CLIENT_AVAILABLE:
        log("[REMOTE] Listener em tempo real não iniciado: websocket-client indisponível")
        return

    while True:
        try:
            ws_url = get_supabase_realtime_url()
            join_payload = {
                "config": {
                    "broadcast": {"self": False},
                    "presence": {"key": SITE_NAME},
                    "postgres_changes": [{
                        "event": "INSERT",
                        "schema": "public",
                        "table": REMOTE_COMMAND_TABLE,
                        "filter": f"site_id=eq.{SITE_NAME}"
                    }]
                }
            }

            def on_open(ws_app: Any) -> None:
                with REMOTE_COMMAND_WS_LOCK:
                    global REMOTE_COMMAND_WS_APP
                    REMOTE_COMMAND_WS_APP = ws_app
                log(f"[REMOTE] Conectado ao Realtime do Supabase para site_id={SITE_NAME}")
                ws_app.send(json.dumps({
                    "topic": REMOTE_COMMAND_TOPIC,
                    "event": "phx_join",
                    "payload": join_payload,
                    "ref": next_remote_command_ref()
                }))
                threading.Thread(
                    target=send_remote_realtime_heartbeat,
                    args=(ws_app,),
                    daemon=True
                ).start()

            def on_message(_: Any, message: str) -> None:
                handle_remote_realtime_message(message)

            def on_error(_: Any, error: Any) -> None:
                log(f"[REMOTE] Erro no websocket Realtime: {error}")

            def on_close(_: Any, status_code: Any, close_msg: Any) -> None:
                with REMOTE_COMMAND_WS_LOCK:
                    global REMOTE_COMMAND_WS_APP
                    REMOTE_COMMAND_WS_APP = None
                log(f"[REMOTE] Websocket Realtime encerrado (code={status_code}, msg={close_msg})")

            ws_app = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws_app.run_forever(ping_interval=0)
        except Exception as e:
            log(f"[REMOTE] Listener Realtime caiu: {e}")
            traceback.print_exc()

        time.sleep(REMOTE_COMMAND_RECONNECT_SECONDS)

def start_remote_command_listener() -> None:
    """Inicia o listener Realtime uma única vez."""
    global REMOTE_COMMAND_LISTENER_STARTED
    with REMOTE_COMMAND_LISTENER_LOCK:
        if REMOTE_COMMAND_LISTENER_STARTED:
            return

        threading.Thread(
            target=remote_command_listener_loop,
            daemon=True
        ).start()
        REMOTE_COMMAND_LISTENER_STARTED = True
        log("[REMOTE] Listener de comandos remotos iniciado")

@app.route("/tuya/command", methods=["POST"])
def api_tuya_command():
    try:
        data: Dict[str, Any] = request.get_json(silent=True)
        if data is None:
            return jsonify({"ok": False, "error": "JSON inválido"}), 400
        response_data = execute_tuya_command_request(data)
        return jsonify(response_data), 200
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    
    except RuntimeError as e:
        err = str(e)
        log(f"[ERRO] API /tuya/command (runtime): {err}")
        return jsonify({"ok": False, "error": err, "retriable": True}), 503
    except Exception as e:
        err = str(e)
        log(f"[ERRO] API /tuya/command: {err}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": err}), 500

@app.route("/tuya/devices", methods=["GET"])
def api_tuya_devices():
    """Retorna lista de dispositivos escaneados na rede"""
    try:
        devices = scan_devices()
        device_list = []
        for gwid, dev_info in devices.items():
            device_list.append({
                "id": gwid,
                "ip": dev_info.get("ip", ""),
                "version": dev_info.get("version", "")
            })
        return jsonify({"ok": True, "devices": device_list}), 200
    except Exception as e:
        err = str(e)
        log(f"[ERRO] API /tuya/devices: {err}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": err}), 500

@app.route("/tuya/heartbeat", methods=["POST"])
def api_tuya_heartbeat():
    """
    Atualiza o campo servidor_online de um dispositivo (heartbeat/ping).
    Atualiza com o timestamp atual para indicar que o servidor está online.
    Também aceita métricas opcionais: battery_level e internet_speed_mbps.
    A velocidade da internet é salva no campo wifi_speed (integer) do banco.
    
    Body:
    {
        "tuya_device_id": "bf1234567890abcdef",
        "battery_level": 85,  # opcional: porcentagem de bateria (0-100)
        "internet_speed_mbps": 25.5  # opcional: velocidade da internet em Mbps (salvo como wifi_speed)
    }
    """
    try:
        data: Dict[str, Any] = request.get_json(silent=True)
        if data is None:
            return jsonify({"ok": False, "error": "JSON inválido"}), 400
        
        tuya_device_id = data.get("tuya_device_id")
        battery_level = data.get("battery_level")  # opcional
        internet_speed_mbps = data.get("internet_speed_mbps")  # opcional
        
        if not tuya_device_id:
            return jsonify({
                "ok": False,
                "error": "tuya_device_id é obrigatório"
            }), 400
        
        # Log das métricas recebidas
        metrics_log = []
        if battery_level is not None:
            metrics_log.append(f"bateria={battery_level}%")
        if internet_speed_mbps is not None:
            metrics_log.append(f"velocidade={internet_speed_mbps} Mbps")
        
        if metrics_log:
            log(f"[HEARTBEAT] Métricas recebidas: {', '.join(metrics_log)}")
        
        # Atualizar heartbeat no banco (com métricas opcionais)
        success = update_device_heartbeat(tuya_device_id, battery_level, internet_speed_mbps)
        
        if success:
            response_msg = f"Heartbeat atualizado com sucesso para device {tuya_device_id}"
            if metrics_log:
                response_msg += f" ({', '.join(metrics_log)})"
            
            return jsonify({
                "ok": True,
                "message": response_msg
            }), 200
        else:
            return jsonify({
                "ok": False,
                "error": "Heartbeat não atualizado (placa sem resposta ou erro)"
            }), 200
    
    except Exception as e:
        err = str(e)
        log(f"[ERRO] API /tuya/heartbeat: {err}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": err}), 500

def fetch_local_key_from_tuya_api(tuya_device_id: str) -> Optional[str]:
    """
    Busca a local_key de um dispositivo usando a API Tuya.
    Tenta todas as contas configuradas até encontrar.
    Retorna a local_key se encontrada, None caso contrário.
    """
    if not TUYA_CONNECTOR_AVAILABLE:
        log("[TUYA_API] tuya-connector-python não está disponível")
        return None
    
    if not TUYA_ACCOUNTS:
        log("[TUYA_API] Nenhuma conta Tuya configurada")
        return None
    
    for account in TUYA_ACCOUNTS:
        try:
            access_id = account.get("access_id")
            access_key = account.get("access_key")
            endpoint = account.get("endpoint")
            uid = account.get("uid")
            
            if not all([access_id, access_key, endpoint, uid]):
                log(f"[TUYA_API] Conta incompleta, pulando...")
                continue
            
            log(f"[TUYA_API] Tentando buscar local_key para {tuya_device_id} na conta {mask_local_key(access_id, 8)}...")
            
            api = TuyaOpenAPI(endpoint, access_id, access_key)
            api.connect()
            
            # Buscar local_key via /v2.0/cloud/thing/{dev_id}
            detail_v2 = api.get(f"/v2.0/cloud/thing/{tuya_device_id}", {})
            
            if detail_v2 and detail_v2.get("success"):
                result = detail_v2.get("result", {}) or {}
                local_key = result.get("local_key")
                
                if local_key:
                    log(f"[TUYA_API] local_key encontrada para {tuya_device_id}: {mask_local_key(local_key)}")
                    return local_key
                else:
                    log(f"[TUYA_API] local_key não encontrada na resposta para {tuya_device_id}")
            else:
                log(f"[TUYA_API] Erro ao buscar /v2.0/cloud/thing/{tuya_device_id}: {detail_v2}")
        
        except Exception as e:
            log(f"[TUYA_API] Erro ao buscar local_key na conta {mask_local_key(account.get('access_id', 'unknown'), 8)}: {e}")
            traceback.print_exc()
            continue
    
    log(f"[TUYA_API] local_key não encontrada para {tuya_device_id} em nenhuma conta")
    return None

@app.route("/tuya/sync", methods=["POST"])
def api_sync_devices():
    """
    Sincroniza devices encontrados na rede LAN com a tabela tuya_devices.
    Para cada device encontrado na rede, se existir na tabela com mesmo tuya_device_id,
    atualiza: lan_ip, protocol_version (sempre que disponíveis do scan).
    Opcionalmente pode receber site_id, name e local_key no body para atualizar também.
    
    Body opcional:
    {
        "site_id": "Nome da Unidade",
        "devices": {
            "tuya_device_id_1": {
                "name": "Nome do Device",
                "local_key": "local_key_da_placa"
            },
            ...
        }
    }
    """
    try:
        log("[SYNC] Iniciando sincronização de devices...")
        
        # Ler dados opcionais do body
        body_data = request.get_json(silent=True) or {}
        site_id_from_body = body_data.get("site_id") or SITE_NAME
        devices_data = body_data.get("devices", {})
        
        # 1) Fazer scan LAN para pegar devices na rede
        lan_devices = scan_devices()
        
        if not lan_devices:
            log("[SYNC] Nenhum device encontrado na rede")
            return jsonify({
                "ok": True,
                "message": "Nenhum device encontrado na rede",
                "updated": 0
            }), 200
        
        log(f"[SYNC] Encontrados {len(lan_devices)} devices na rede")
        
        # 2) Buscar devices no banco que correspondem aos encontrados na rede
        tuya_ids = list(lan_devices.keys())
        db_devices = get_devices_from_db(tuya_ids)
        
        log(f"[SYNC] Encontrados {len(db_devices)} devices no banco")
        
        # 3) Para cada device encontrado na rede, atualizar ou criar
        updated_count = 0
        created_count = 0
        updated_devices = []
        created_devices = []
        
        for tuya_id, lan_info in lan_devices.items():
            lan_ip = lan_info.get("ip")
            protocol_version = lan_info.get("version")
            
            # Converter version para string se necessário
            if protocol_version:
                protocol_version = str(protocol_version)
            
            # Buscar dados opcionais do body (name e local_key)
            device_extra_data = devices_data.get(tuya_id, {})
            name_from_body = device_extra_data.get("name") or site_id_from_body  # Usar site_id se name não fornecido
            local_key_from_body = device_extra_data.get("local_key")
            
            # Se não temos local_key do body, tentar buscar da API Tuya
            if not local_key_from_body:
                log(f"[SYNC] Tentando buscar local_key da API Tuya para {tuya_id}...")
                local_key_from_api = fetch_local_key_from_tuya_api(tuya_id)
                if local_key_from_api:
                    local_key_from_body = local_key_from_api
                    log(f"[SYNC] local_key obtida da API Tuya para {tuya_id}: {mask_local_key(local_key_from_api)}")
                else:
                    log(f"[SYNC] Não foi possível obter local_key da API Tuya para {tuya_id}")
            
            # Verificar se device existe no banco
            if tuya_id in db_devices:
                # Device existe: ATUALIZAR
                db_info = db_devices[tuya_id]
                
                # Preparar dados para atualização
                update_needed = False
                update_data = {}
                
                # Sempre atualizar lan_ip e protocol_version se disponíveis do scan
                if lan_ip and lan_ip != db_info.get('lan_ip'):
                    update_data['lan_ip'] = lan_ip
                    update_needed = True
                
                if protocol_version and protocol_version != db_info.get('protocol_version'):
                    update_data['protocol_version'] = protocol_version
                    update_needed = True
                
                # Atualizar site_id se fornecido no body
                if site_id_from_body and site_id_from_body != db_info.get('site_id'):
                    update_data['site_id'] = site_id_from_body
                    update_needed = True
                
                # Sempre atualizar name com site_id se fornecido
                if site_id_from_body:
                    if name_from_body != db_info.get('name'):
                        update_data['name'] = name_from_body
                        update_needed = True
                
                # Atualizar local_key se fornecido no body
                if local_key_from_body and local_key_from_body != db_info.get('local_key'):
                    update_data['local_key'] = local_key_from_body
                    update_needed = True
                
                if update_needed:
                    success = update_device_in_db(
                        tuya_device_id=tuya_id,
                        site_id=update_data.get('site_id'),
                        name=update_data.get('name'),
                        local_key=update_data.get('local_key'),
                        lan_ip=update_data.get('lan_ip'),
                        protocol_version=update_data.get('protocol_version')
                    )
                    
                    if success:
                        updated_count += 1
                        updated_devices.append({
                            "tuya_device_id": tuya_id,
                            "action": "updated",
                            "updated_fields": list(update_data.keys())
                        })
                        log(f"[SYNC] Device {tuya_id} atualizado: {list(update_data.keys())}")
                else:
                    log(f"[SYNC] Device {tuya_id} já está atualizado")
            else:
                # Device não encontrado por tuya_device_id.
                # Tentar reaproveitar a linha do mesmo site_id (código da placa pode ter mudado).
                reused_by_site = False
                if site_id_from_body:
                    existing_by_site = get_device_by_site_id_from_db(site_id_from_body)
                    if existing_by_site:
                        old_tuya_id = existing_by_site.get("tuya_device_id")
                        row_id = existing_by_site.get("id")
                        log(f"[SYNC] Device {tuya_id} não encontrado por código; atualizando linha existente do site_id={site_id_from_body} (tuya antigo={old_tuya_id})")
                        
                        success = update_device_by_id_in_db(
                            device_row_id=str(row_id),
                            tuya_device_id=tuya_id,
                            site_id=site_id_from_body,
                            name=name_from_body or site_id_from_body,
                            local_key=local_key_from_body,
                            lan_ip=lan_ip,
                            protocol_version=protocol_version
                        )
                        
                        if success:
                            updated_count += 1
                            updated_devices.append({
                                "tuya_device_id": tuya_id,
                                "action": "updated_by_site_id",
                                "old_tuya_device_id": old_tuya_id,
                                "site_id": site_id_from_body
                            })
                            reused_by_site = True
                            log(f"[SYNC] Linha do site_id={site_id_from_body} atualizada com novo tuya_device_id={tuya_id}")
                
                if not reused_by_site:
                    # Sem correspondência por site_id: criar novo registro normalmente.
                    log(f"[SYNC] Device {tuya_id} não encontrado no banco, criando novo registro...")
                    
                    success = create_device_in_db(
                        tuya_device_id=tuya_id,
                        site_id=site_id_from_body,
                        name=name_from_body or site_id_from_body,  # Garantir que name seja preenchido
                        local_key=local_key_from_body,
                        lan_ip=lan_ip,
                        protocol_version=protocol_version
                    )
                    
                    if success:
                        created_count += 1
                        created_devices.append({
                            "tuya_device_id": tuya_id,
                            "action": "created"
                        })
                        log(f"[SYNC] Device {tuya_id} criado com sucesso")
        
        total_processed = updated_count + created_count
        log(f"[SYNC] Sincronização concluída: {updated_count} atualizados, {created_count} criados")
        
        return jsonify({
            "ok": True,
            "message": f"{updated_count} device(s) atualizado(s), {created_count} device(s) criado(s)",
            "updated": updated_count,
            "created": created_count,
            "total": total_processed,
            "devices": updated_devices + created_devices
        }), 200
        
    except Exception as e:
        err = str(e)
        log(f"[ERRO] API /tuya/sync: {err}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": err}), 500

def start_server(host="0.0.0.0", port=8000):
    """Inicia o servidor Flask"""
    log(f"[START] Servidor Tuya local rodando em http://{host}:{port} (SITE={SITE_NAME})")
    # Faz o scan inicial
    scan_and_print_devices()
    # Iniciar refresh periódico
    start_device_refresh_loop()
    # Escutar comandos remotos em tempo real sem polling REST contínuo
    start_remote_command_listener()
    # Monitor de conectividade com fallback para rede reserva
    threading.Thread(target=_connectivity_watch_loop, daemon=True).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    start_server()
