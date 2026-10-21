"""Recuperacao do Wi-Fi sem interromper clientes associados ao hotspot."""

import subprocess
import time


class WifiRecovery:
    INTERVAL_SECONDS = 5 * 60

    def __init__(self, nmcli, check_internet, stop_hotspot, start_hotspot, log):
        self.nmcli = nmcli
        self.check_internet = check_internet
        self.stop_hotspot = stop_hotspot
        self.start_hotspot = start_hotspot
        self.log = log
        self.last_attempt = time.monotonic()

    def hotspot_started(self):
        self.last_attempt = time.monotonic()

    def _clients_present(self):
        # A tabela de associacao inclui clientes mesmo sem DHCP ou trafego.
        # Erros devem preservar o acesso local, nunca autorizar a desconexao.
        try:
            result = subprocess.run(
                ["sudo", "-n", "iw", "dev", "wlan0", "station", "dump"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError("iw nao conseguiu consultar os clientes")
            return bool(result.stdout.strip())
        except Exception as exc:
            self.log(f"[WIFI] Recuperacao adiada: consulta de clientes falhou: {exc}")
            return True

    def _saved_profiles(self):
        result = self.nmcli("-t", "-f", "UUID,TYPE", "connection", "show", timeout=10)
        if result.returncode != 0:
            raise RuntimeError("Nao foi possivel listar as redes salvas")
        profiles = []
        for line in result.stdout.splitlines():
            uuid, _, kind = line.partition(":")
            if kind not in ("802-11-wireless", "wifi"):
                continue
            details = self.nmcli(
                "-g", "connection.interface-name,802-11-wireless.mode",
                "connection", "show", "uuid", uuid, timeout=10,
            )
            if details.returncode != 0:
                continue
            fields = details.stdout.splitlines()
            if (len(fields) >= 2
                    and fields[0] in ("", "--", "wlan0")
                    and fields[1] in ("", "--", "infrastructure")):
                profiles.append(uuid)
        return profiles

    def try_reconnect(self, backup):
        """Retorna True somente se recuperou internet. Chamador serializa operacoes Wi-Fi."""
        if time.monotonic() - self.last_attempt < self.INTERVAL_SECONDS:
            return False
        if self._clients_present():
            self.log("[WIFI] Recuperacao adiada: hotspot em uso ou consulta indisponivel")
            return False

        try:
            profiles = self._saved_profiles()
        except Exception as exc:
            self.last_attempt = time.monotonic()
            self.log(f"[WIFI] Recuperacao adiada: {exc}")
            return False
        if not profiles and not backup.get("ssid"):
            self.last_attempt = time.monotonic()
            self.log("[WIFI] Sem redes salvas para recuperar; mantendo hotspot")
            return False

        # A listagem de perfis pode demorar; conferir novamente antes de parar o AP.
        if self._clients_present():
            return False

        self.log("[WIFI] Hotspot sem clientes; tentando recuperar internet")
        restored = False
        try:
            self.stop_hotspot()
            for uuid in profiles:
                try:
                    result = self.nmcli(
                        "connection", "up", "uuid", uuid, "ifname", "wlan0",
                        sudo=True, timeout=40,
                    )
                except subprocess.TimeoutExpired:
                    continue
                if result.returncode == 0 and self.check_internet():
                    restored = True
                    break
            if not restored and backup.get("ssid"):
                args = ["device", "wifi", "connect", backup["ssid"], "ifname", "wlan0"]
                if backup.get("password"):
                    args += ["password", backup["password"]]
                result = self.nmcli(*args, sudo=True, timeout=40)
                restored = result.returncode == 0 and self.check_internet()
            if restored:
                self.log("[WIFI] Internet recuperada; hotspot desligado")
        except Exception as exc:
            self.log(f"[WIFI] Falha na recuperacao automatica: {type(exc).__name__}")
        finally:
            if not restored:
                self.start_hotspot("internet ainda indisponivel apos recuperacao automatica")
            self.last_attempt = time.monotonic()
        return restored
