"""Film a confirmed purchase opening using the camera on the local network."""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import quote

import requests


REMOTE_PAIRING_SITES = {"miguelbadra@gelafit.com.br", "guaianases@gelafit.com.br"}


class PickupCamera:
    def __init__(self, rest_url, headers, site, state_dir, log):
        self.rest_url = rest_url
        self.headers = headers
        self.site = site
        self.state_dir = Path(state_dir)
        self.log = log
        self.token = os.environ.get("GELAFIT_CAMERA_TOKEN", "")
        self.config = None
        self.busy = threading.Lock()

    def start(self):
        if self.token or self.site().lower() in REMOTE_PAIRING_SITES:
            threading.Thread(target=self.refresh_loop, daemon=True).start()

    def pairing_token(self):
        path = self.state_dir / ".pairing-token"
        if path.is_file():
            token = path.read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[a-f0-9]{64}", token):
                raise ValueError("Invalid local pairing credential")
            return token
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        with NamedTemporaryFile(dir=self.state_dir, mode="w", encoding="ascii", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(token + "\n")
        try:
            os.link(temp, path)
        except FileExistsError:
            return self.pairing_token()
        finally:
            temp.unlink(missing_ok=True)
        return token

    def present_for_pairing(self, site):
        response = requests.post(
            f"{self.rest_url()}/rpc/camera_piloto_apresentar", headers=self.headers(),
            json={"p_site": site, "p_hash": hashlib.sha256(self.token.encode("ascii")).hexdigest()},
            timeout=8,
        )
        response.raise_for_status()

    def rpc(self, name, **data):
        response = requests.post(
            f"{self.rest_url()}/rpc/{name}", headers=self.headers(),
            json={"p_site": self.site(), "p_token": self.token, **data}, timeout=8,
        )
        response.raise_for_status()
        return response

    def refresh_loop(self):
        while True:
            try:
                site = self.site()
                automatic = not os.environ.get("GELAFIT_CAMERA_TOKEN")
                if automatic:
                    if site.lower() not in REMOTE_PAIRING_SITES:
                        self.config = None
                        time.sleep(30)
                        continue
                    if not self.token:
                        self.token = self.pairing_token()
                try:
                    config = self.rpc("camera_piloto_config").json()
                except requests.HTTPError as error:
                    if not automatic or error.response is None or error.response.status_code not in (401, 403):
                        raise
                    self.present_for_pairing(site)
                    time.sleep(15)
                    continue
                self.config = (site, time.monotonic(), config)
                self.purge_expired()
                if self.config[2] and self.config[2]["enabled"]:
                    self.check_test(self.config[2])
            except (requests.RequestException, ValueError, RuntimeError, OSError):
                self.log("[CAMERA] Sincronização ou limpeza indisponível.")
            try:
                self.send_pending()
            except (requests.RequestException, ValueError, RuntimeError, OSError):
                self.log("[CAMERA] Envio pendente. Nova tentativa no próximo ciclo.")
            time.sleep(30)

    def check_test(self, config):
        if self.busy.locked():
            return
        try:
            purchase_id = self.rpc("camera_piloto_receber_teste").json()
        except (requests.RequestException, RuntimeError, ValueError):
            return
        if not purchase_id or not self.busy.acquire(False):
            return
        handed_off = False
        try:
            test_config = {**config, "duration": min(8, int(config["duration"]))}
            threading.Thread(target=self.capture, args=(test_config, purchase_id), daemon=True).start()
            handed_off = True
        except (RuntimeError, ValueError):
            self.log("[CAMERA] Não foi possível iniciar a gravação de teste.")
        finally:
            if not handed_off:
                self.busy.release()

    def send_pending(self):
        site = self.site()
        if not self.state_dir.exists():
            return
        endpoint = self.rest_url()[:-len("/rest/v1")] + "/functions/v1/camera-piloto-clipes"
        for metadata in sorted(self.state_dir.glob("*.json")):
            sent = metadata.with_suffix(".sent")
            clip = metadata.with_suffix(".mp4")
            if sent.exists() or not clip.is_file():
                continue
            info = json.loads(metadata.read_text(encoding="utf-8"))
            if info["site_id"] != site or "captured_at" not in info:
                continue
            if clip.stat().st_size > 25 * 1024 * 1024:
                self.log("[CAMERA] Clipe acima de 25 MiB mantido apenas no Raspberry.")
                sent.touch()
                continue
            headers = {**self.headers(), "Content-Type": "video/mp4",
                       "x-camera-site": quote(site, safe=""), "x-camera-token": self.token,
                       "x-pickup-id": info["purchase_id"], "x-captured-at": info["captured_at"]}
            with clip.open("rb") as video:
                response = requests.post(endpoint, headers=headers, data=video, timeout=35)
            response.raise_for_status()
            if response.json().get("ok") is not True:
                raise ValueError("Envio não confirmado")
            sent.touch()

    def purge_expired(self):
        cutoff = time.time() - 72 * 3600
        for item in self.state_dir.iterdir() if self.state_dir.exists() else ():
            if item.is_file() and item.suffix in {".mp4", ".json", ".sent"} and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)

    def on_open(self, device, purchase_id):
        snapshot = self.config
        if not snapshot or snapshot[0] != self.site() or time.monotonic() - snapshot[1] > 600:
            return
        config = snapshot[2]
        if not config or not config["enabled"] or config["device"] != device or not self.busy.acquire(False):
            return
        try:
            threading.Thread(target=self.capture, args=(config, purchase_id), daemon=True).start()
        except Exception:
            self.busy.release()
            raise

    def capture(self, config, purchase_id):
        captured_at = datetime.now(timezone.utc).isoformat()
        status = None
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256(purchase_id.encode()).hexdigest()
            target = self.state_dir / f"{digest}.mp4"
            if target.exists():
                return
            status = "camera_error"
            address = ipaddress.IPv4Address(config["ip"])
            duration = int(config["duration"])
            port = int(config["port"])
            password = quote(config["key"], safe="")
            username = quote(config["username"], safe="")
            url = f"rtsp://{username}:{password}@{address}:{port}/cam/realmonitor?channel=1&subtype=0"
            if shutil.disk_usage(self.state_dir).free < 256 * 1024 * 1024:
                status = "disk_full"
                return
            with TemporaryDirectory(dir=self.state_dir) as tmp:
                clip = Path(tmp) / "clip.mp4"
                index = Path(tmp) / "clip.json"
                with index.open("w", encoding="utf-8") as handle:
                    json.dump({"purchase_id": purchase_id, "file": target.name,
                               "site_id": config["site"], "device": config["device"],
                               "captured_at": captured_at}, handle)
                # Let FFmpeg repair duplicate DTS from the camera when remuxing.
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-loglevel", "error",
                     "-abort_on", "empty_output", "-rtsp_transport", "tcp", "-timeout", "5000000",
                     "-i", url, "-map", "0:v:0", "-an", "-c:v", "copy",
                     "-t", str(duration), "-movflags", "+faststart", str(clip)],
                    capture_output=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=duration + 12,
                )
                if result.returncode == 0 and clip.is_file() and clip.stat().st_size:
                    os.replace(index, target.with_suffix(".json"))
                    os.replace(clip, target)
                    status = "saved"
        except (ValueError, OSError, subprocess.SubprocessError):
            self.log("[CAMERA] Falha ao gravar a retirada.")
        finally:
            self.busy.release()
            if status and purchase_id.startswith("teste:"):
                self.log(f"[CAMERA] Resultado da gravação de teste: {status}.")
            elif status:
                self.log(f"[CAMERA] Resultado da gravação: {status}.")
                try:
                    self.rpc("camera_piloto_resultado", p_purchase=purchase_id, p_status=status,
                             p_version=config["revision"])
                except (requests.RequestException, ValueError, RuntimeError):
                    self.log("[CAMERA] Não foi possível sincronizar o resultado.")


def schedule_purchase_clip(camera, action, device, purchase_id, log):
    if action != "on" or not isinstance(purchase_id, str):
        return
    if not re.fullmatch(r"(?:mercadopago|cliente_pagamentos):[0-9A-Za-z_-]{1,80}", purchase_id):
        return
    try:
        camera.on_open(device, purchase_id)
    except Exception:
        log("[CAMERA] Não foi possível iniciar a gravação.")
