#!/usr/bin/env python3
"""
Scannor Edge Credentials & Auto-Discovery Guard
Garantiza que ningún microservicio o script de visión artificial (api-vision.py,
defect_inspection, inventory_counting, pawn_counter) inicie sin credenciales
válidas en su .env (SCANNOR_DEVICE_ID y SCANNOR_DEVICE_KEY).

Si faltan credenciales:
  1. Detecta la Dirección MAC física y la Dirección IP local de la cámara/hardware.
  2. Notifica al servidor central Django (/api/provisionar/) para registrar el intento en el Radar.
  3. Imprime en consola un banner de error visible con la MAC y la IP.
  4. Detiene la ejecución (sys.exit(1)) para evitar ejecuciones sin autorización.
"""

import os
import sys
import uuid
import socket
import logging
from pathlib import Path
from typing import Optional, Dict, Tuple, Any
from dotenv import load_dotenv

logger = logging.getLogger("ScannorEdgeAuth")

def find_project_root() -> Path:
    current = Path(__file__).resolve().parent
    for _ in range(5):
        if (current / "manage.py").exists() or (current / "scannor_offline.db").exists():
            return current
        if (current / "microservices").is_dir():
            return current
        current = current.parent
    return Path.cwd()

def obtener_mac_address() -> str:
    """
    Detecta la dirección MAC de hardware de la máquina/cámara.
    Permite anulación manual mediante variable SCANNOR_MAC_OVERRIDE para pruebas locales
    o múltiples instancias/cuentas en la misma máquina física.
    """
    override = os.getenv("SCANNOR_MAC_OVERRIDE") or os.getenv("SCANNOR_MAC_ADDRESS")
    if override and override.strip():
        return override.strip().upper()

    try:
        net_dir = Path("/sys/class/net")
        if net_dir.is_dir():
            interfaces = sorted(net_dir.iterdir(), key=lambda p: p.name)
            for iface in interfaces:
                name = iface.name
                if name.startswith(("eth", "eno", "enp", "ens", "em")):
                    addr_file = iface / "address"
                    if addr_file.exists():
                        mac = addr_file.read_text().strip().upper()
                        if mac and mac != "00:00:00:00:00:00" and len(mac) == 17:
                            return mac
            for iface in interfaces:
                name = iface.name
                if name.startswith(("wl", "wlan", "wlp")):
                    addr_file = iface / "address"
                    if addr_file.exists():
                        mac = addr_file.read_text().strip().upper()
                        if mac and mac != "00:00:00:00:00:00" and len(mac) == 17:
                            return mac
            for iface in interfaces:
                name = iface.name
                if not name.startswith(("lo", "docker", "veth", "br-", "tailscale", "virbr")):
                    addr_file = iface / "address"
                    if addr_file.exists():
                        mac = addr_file.read_text().strip().upper()
                        if mac and mac != "00:00:00:00:00:00" and len(mac) == 17:
                            return mac
    except Exception:
        pass

    mac_num = uuid.getnode()
    mac_hex = f"{mac_num:012x}"
    return ":".join(mac_hex[i:i+2] for i in range(0, 12, 2)).upper()

def obtener_ip_local() -> str:
    """
    Detecta la dirección IP local de la cámara / nodo en la red LAN.
    """
    try:
        probe_ip = os.getenv("DNS_PROBE_IP", "8.8.8.8")
        probe_port = int(os.getenv("DNS_PROBE_PORT", "80"))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect((probe_ip, probe_port))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    try:
        host = socket.gethostname()
        ip = socket.gethostbyname(host)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    return os.getenv("DEFAULT_EDGE_HOST", "127.0.0.1")

def load_edge_environment() -> Optional[Path]:
    """
    Carga variables de entorno buscando un archivo .env válido.
    Prioridad:
      1. Archivo especificado en SCANNOR_ENV_FILE
      2. .env en directorio de trabajo actual
      3. .env en directorio del servicio
      4. .env en raíz del proyecto
    """
    custom_env = os.getenv("SCANNOR_ENV_FILE")
    if custom_env and Path(custom_env).is_file():
        load_dotenv(custom_env, override=True)
        return Path(custom_env)

    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        load_dotenv(cwd_env, override=False)

    root_dir = find_project_root()
    root_env = root_dir / ".env"
    if root_env.is_file() and root_env != cwd_env:
        load_dotenv(root_env, override=False)

    return cwd_env if cwd_env.is_file() else (root_env if root_env.is_file() else None)

def notificar_radar_descubrimiento(mac: str, ip: str) -> Tuple[bool, Dict[str, Any], str]:
    """
    Envía solicitud de auto-aprovisionamiento / descubrimiento a Django (/api/provisionar/).
    Retorna (autorizado: bool, env_data: dict, status_msg: str).
    """
    try:
        import requests
    except ImportError:
        return False, {}, "Biblioteca 'requests' no disponible para notificación automática."

    candidatas = []
    
    prov_env = os.getenv("CENTRAL_PROVISION_URL")
    if prov_env:
        candidatas.append(prov_env)

    base_env = os.getenv("DJANGO_BASE_URL")
    if base_env:
        candidatas.append(f"{base_env.rstrip('/')}/api/provisionar/")

    django_api = os.getenv("DJANGO_API_URL")
    if django_api:
        base = django_api.split("/api/")[0]
        candidatas.append(f"{base}/api/provisionar/")

    candidatas.extend([
        "http://127.0.0.1:8000/api/provisionar/",
        "http://localhost:8000/api/provisionar/",
    ])

    payload = {
        "mac_address": mac,
        "ip_address": ip,
        "device_id": os.getenv("SCANNOR_DEVICE_ID", "").strip()
    }

    headers = {"Content-Type": "application/json"}

    vistas = set()
    urls_unicas = []
    for u in candidatas:
        if u not in vistas:
            vistas.add(u)
            urls_unicas.append(u)

    for url in urls_unicas:
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=3.0)
            if res.status_code == 200:
                data = res.json()
                env_data = data.get("env_data", {})
                return True, env_data, f"Dispositivo autorizado en servidor central ({url})."
            elif res.status_code == 404:
                return False, {}, f"Equipo registrado en el Radar de Dispositivos (Modo Descubrimiento) en {url}."
        except Exception:
            continue

    return False, {}, "No se pudo contactar el servidor central de ScannorQA (servidor apagado o sin red)."

def asegurar_credenciales_edge(
    service_name: str = "Servicio de Visión IA",
    exit_on_failure: bool = True
) -> Dict[str, str]:
    """
    Verifica que el entorno cuente con credenciales válidas en .env (SCANNOR_DEVICE_ID y SCANNOR_DEVICE_KEY).
    Si faltan:
      - Obtiene la MAC y la IP.
      - Notifica al Radar en Django.
      - Imprime el banner crítico con MAC e IP.
      - Si exit_on_failure=True, detiene el proceso con sys.exit(1).
      - Si exit_on_failure=False, lanza RuntimeError.
    Si existen:
      - Imprime la confirmación y retorna las credenciales.
    """
    load_edge_environment()

    device_id = os.getenv("SCANNOR_DEVICE_ID", "").strip()
    device_key = os.getenv("SCANNOR_DEVICE_KEY", "").strip()
    mac = obtener_mac_address()
    ip = obtener_ip_local()

    if not device_id or not device_key or device_id in ("cambiar_por_id_de_dispositivo", "none"):
        autorizado, env_data, radar_msg = notificar_radar_descubrimiento(mac, ip)

        if autorizado and env_data:
            for k, v in env_data.items():
                os.environ[k] = str(v)
            print("=" * 80)
            print(f"[+] ¡APROVISIONAMIENTO EXITOSO! ({service_name})")
            print(f"    Dispositivo ID : {env_data.get('SCANNOR_DEVICE_ID')}")
            print(f"    Dirección MAC  : {mac}")
            print(f"    Dirección IP   : {ip}")
            print("=" * 80)
            return {
                "device_id": str(env_data.get("SCANNOR_DEVICE_ID")),
                "device_key": str(env_data.get("SCANNOR_DEVICE_KEY")),
                "mac_address": mac,
                "ip_local": ip
            }

        panel_base = os.getenv("DJANGO_BASE_URL", "http://localhost:8000").rstrip("/")
        banner = f"""
================================================================================
[!] ERROR CRÍTICO: CREDENCIALES .ENV NO CONFIGURADAS ({service_name.upper()})
================================================================================
El servicio de IA no puede inicializarse porque no cuenta con credenciales
válidas de dispositivo en el archivo .env (SCANNOR_DEVICE_ID / SCANNOR_DEVICE_KEY).

Información de hardware detectada para registro de la cámara/equipo:
  -> DIRECCIÓN MAC : {mac}
  -> DIRECCIÓN IP  : {ip}

Estado de Radar:
  -> {radar_msg}

PASOS PARA REGISTRAR Y ACTIVAR ESTE DISPOSITIVO:
  1. Ingresa al panel Staff de ScannorQA:
     {panel_base}/staff/dispositivos/
  2. En el 'Radar de Dispositivos (Modo Descubrimiento)', ubica la MAC '{mac}'.
  3. Haz clic en 'Autorizar y Registrar'.
     (La Dirección MAC y la Dirección IP '{ip}' se autocompletarán).
  4. Asigna un nombre, la empresa y el modelo IA, luego haz clic en 'Generar Credenciales'.
  5. Descarga el archivo .env generado (botón ⬇️ .env) y colócalo como '.env'
     en la raíz de este servicio.
  6. Vuelve a iniciar este servicio de IA.
================================================================================
"""
        print(banner, file=sys.stderr)

        if exit_on_failure:
            sys.exit(1)
        else:
            raise RuntimeError(f"Faltan credenciales .env para {service_name}. MAC: {mac}, IP: {ip}")

    print("=" * 80)
    print(f"[+] SCANNOR EDGE - CREDENCIALES VERIFICADAS ({service_name})")
    print(f"    Dispositivo ID : {device_id}")
    print(f"    Dirección MAC  : {mac}")
    print(f"    Dirección IP   : {ip}")
    print("=" * 80)

    return {
        "device_id": device_id,
        "device_key": device_key,
        "mac_address": mac,
        "ip_local": ip
    }
