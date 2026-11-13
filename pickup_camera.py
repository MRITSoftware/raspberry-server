"""Film a confirmed purchase opening using the camera on the local network."""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from tempfile import TemporaryDirectory
from urllib.parse import quote

import requests


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
        if self.token:
            threading.Thread(target=self.refresh_loop, daemon=True).start()

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
                self.config = (site, time.monotonic(), self.rpc("camera_piloto_config").json())
                self.purge_expired()
            except (requests.RequestException, ValueError, RuntimeError, OSError):
                self.log("[CAMERA] Sincronização ou limpeza indisponível.")
            try:
                self.send_pending()
            except (requests.RequestException, ValueError, RuntimeError, OSError):
                self.log("[CAMERA] Envio pendente. Nova tentativa no próximo ciclo.")
            time.sleep(30)

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
            if status:
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
