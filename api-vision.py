#!/usr/bin/env python3
"""
Scannor Edge Vision Gateway
Pasarela industrial desacoplada para gestión de hardware, cámaras locales,
sincronización offline con Django y delegación de inferencia IA.
"""

import os
import sys
import glob
import time
import json
import uuid
import socket
import secrets
import platform
import threading
import subprocess
import collections
import queue
import sqlite3
import asyncio
import cv2
import numpy as np
import requests
from datetime import datetime, timezone
from typing import Dict, Optional, Any, Tuple, List
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, WebSocket, HTTPException, status, Security, Query
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

try:
    from microservices.shared.bbox_stabilizer import BBoxStabilizer
    from microservices.shared.multi_camera_coordinator import TunnelPassageCoordinator
except ImportError:
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.bbox_stabilizer import BBoxStabilizer
    from microservices.shared.multi_camera_coordinator import TunnelPassageCoordinator

try:
    if hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_FATAL)
except Exception:
    pass

try:
    import torch
    DEFAULT_DEVICE = "0" if torch.cuda.is_available() else "cpu"
except Exception:
    DEFAULT_DEVICE = "cpu"

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))

try:
    from microservices.shared.edge_credentials import (
        asegurar_credenciales_edge,
        obtener_mac_address,
        obtener_ip_local
    )
except ImportError:
    from edge_credentials import (
        asegurar_credenciales_edge,
        obtener_mac_address,
        obtener_ip_local
    )

creds = asegurar_credenciales_edge(service_name="Edge Vision Gateway", exit_on_failure=True)

DEVICE_ID = creds["device_id"]
DEVICE_API_KEY = creds["device_key"]
ADMIN_API_KEY = os.getenv("SCANNOR_API_KEY") or secrets.token_hex(32)
STREAM_TOKEN = os.getenv("SCANNOR_STREAM_TOKEN") or secrets.token_hex(32)
DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://127.0.0.1:8000/api/guardar-falla/")
INSPECTION_SERVICE_URL = os.getenv("INSPECTION_SERVICE_URL", "")

PORT = int(os.getenv("PORT", os.getenv("PORT_API_VISION", "8001")))
HOST = os.getenv("HOST", "0.0.0.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
DEFAULT_CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))
EDGE_PUBLIC_URL = os.getenv("EDGE_PUBLIC_URL", f"http://{obtener_ip_local()}:{PORT}").rstrip('/')
DB_PATH = os.path.join(BASE_DIR, os.getenv("SCANNOR_DB_PATH", os.getenv("SCANNOR_OFFLINE_DB", "scannor_offline.db")))
CONFIG_FILE = os.path.join(BASE_DIR, os.getenv("CAMERA_CONFIG_FILE", "config_camaras.json"))
CAPTURES_DIR = os.path.join(BASE_DIR, os.getenv("SCANNOR_CAPTURES_DIR", "capturas_falla"))
os.makedirs(CAPTURES_DIR, exist_ok=True)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verificar_api_key(api_key: str = Security(api_key_header)):
    if not secrets.compare_digest(api_key, ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="Admin Key inválida")

async def verificar_stream_token(token: str = Query(None)):
    if not secrets.compare_digest(token or "", STREAM_TOKEN):
        raise HTTPException(status_code=403, detail="Stream Token inválido")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute('''CREATE TABLE IF NOT EXISTS inspecciones 
                     (uid TEXT PRIMARY KEY, payload TEXT, sync INTEGER DEFAULT 0, sync_retries INTEGER DEFAULT 0)''')
        conn.commit()

init_db()

db_queue: queue.Queue = queue.Queue(maxsize=500)
img_queue: queue.Queue = queue.Queue(maxsize=500)
failures = collections.deque(maxlen=100)
total_eventos_procesados = 0
state_lock = threading.RLock()

sync_stats = {
    "ultimo_exito": time.time(),
    "errores_auth": 0,
    "errores_permanentes": 0,
    "imagenes_descartadas": 0,
    "eventos_descartados_cola": 0
}

def img_writer_worker():
    while True:
        item = img_queue.get()
        if item is None:
            img_queue.task_done()
            break
        try:
            fname, img_limpia, img_vis, bbox_txt = item
            if img_limpia is not None:
                cv2.imwrite(os.path.join(CAPTURES_DIR, fname), img_limpia)
            if img_vis is not None:
                cv2.imwrite(os.path.join(CAPTURES_DIR, fname.replace('.jpg', '_vis.jpg')), img_vis)
            if bbox_txt:
                with open(os.path.join(CAPTURES_DIR, fname.replace('.jpg', '.txt')), 'w', encoding='utf-8') as f:
                    f.write(bbox_txt.strip() + '\n')
        except Exception as e:
            print(f"[-] Error escribiendo evidencia en disco: {e}")
        finally:
            img_queue.task_done()

def db_writer_worker():
    while True:
        evento = db_queue.get()
        if evento is None:
            db_queue.task_done()
            break
        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO inspecciones (uid, payload, sync, sync_retries) VALUES (?, ?, 0, 0)",
                    (evento["uid"], json.dumps(evento))
                )
                conn.commit()
        except Exception as e:
            print(f"[-] Error guardando en SQLite: {e}")
        finally:
            db_queue.task_done()

def background_sync_worker():
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT uid, payload, sync_retries FROM inspecciones WHERE sync = 0 ORDER BY rowid ASC LIMIT 15")
                filas = cur.fetchall()

                for uid, payload_str, _ in filas:
                    try:
                        res = requests.post(
                            DJANGO_API_URL, json=json.loads(payload_str),
                            headers={"X-Device-Auth": DEVICE_API_KEY}, timeout=3.0
                        )
                        res.raise_for_status()
                        conn.execute("UPDATE inspecciones SET sync = 1 WHERE uid = ?", (uid,))
                        conn.commit()
                        sync_stats["ultimo_exito"] = time.time()
                    except requests.exceptions.HTTPError as he:
                        code = he.response.status_code if he.response else 0
                        if code in (400, 404, 422):
                            conn.execute("UPDATE inspecciones SET sync = 2 WHERE uid = ?", (uid,))
                            conn.commit()
                            sync_stats["errores_permanentes"] += 1
                        elif code in (401, 403):
                            sync_stats["errores_auth"] += 1
                    except requests.exceptions.RequestException:
                        break
        except Exception as e:
            pass
        time.sleep(1.0)

def registrar_evento(lote: str, estado: str, falla: str, confianza: float, img_limpia: Optional[np.ndarray], img_vis: Optional[np.ndarray], bbox_txt: str = ""):
    global total_eventos_procesados
    t_now = time.time()
    ts_iso = datetime.now(timezone.utc).isoformat()
    fname = f"Evidencia_{lote}_{falla.replace(' ', '_')}_{int(t_now)}.jpg"
    fname_vis = fname.replace('.jpg', '_vis.jpg')

    if estado.lower() == "rechazado":
        threading.Thread(target=_sonar_alarma, daemon=True).start()

    try:
        img_queue.put_nowait((fname, img_limpia, img_vis or img_limpia, bbox_txt))
    except queue.Full:
        sync_stats["imagenes_descartadas"] += 1

    uid = f"{DEVICE_ID}_{lote}_{int(t_now)}_{uuid.uuid4().hex[:6]}"
    evento = {
        "uid": uid,
        "dispositivo_id": DEVICE_ID,
        "lote": lote,
        "timestamp": ts_iso,
        "falla": falla,
        "confianza": confianza,
        "estado": estado,
        "imagen": f"{EDGE_PUBLIC_URL}/capturas/{fname_vis}"
    }

    try:
        db_queue.put_nowait(evento)
    except queue.Full:
        sync_stats["eventos_descartados_cola"] += 1

    with state_lock:
        failures.append(evento)
        total_eventos_procesados += 1

def _sonar_alarma():
    try:
        if platform.system() == "Windows":
            import winsound
            winsound.Beep(1000, 400)
        else:
            subprocess.run(["aplay", "-q", "alarma.wav"], stderr=subprocess.DEVNULL)
    except Exception:
        pass

class AsyncCameraReader:
    def __init__(self, src: int):
        self.src = int(src)
        self.lock = threading.Lock()
        self.running = True
        self.frame: Optional[np.ndarray] = None
        self.connected = False
        self.backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_V4L2
        self.cap: Optional[cv2.VideoCapture] = None

        self._conectar()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _conectar(self) -> bool:
        """Intenta abrir o reabrir el dispositivo de captura de video."""
        try:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None

            if platform.system() == "Linux":
                try:
                    import subprocess
                    subprocess.run(["v4l2-ctl", "-d", f"/dev/video{self.src}", "--set-ctrl=exposure_dynamic_framerate=0"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

            self.cap = cv2.VideoCapture(self.src, self.backend)
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                self.cap.set(cv2.CAP_PROP_FPS, 30)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ret, test_f = self.cap.read()
                if ret and test_f is not None:
                    with self.lock:
                        self.frame = test_f
                        self.connected = True
                    return True

            with self.lock:
                self.connected = False
            return False
        except Exception:
            with self.lock:
                self.connected = False
            return False

    def _loop(self):
        fallos_consecutivos = 0
        while self.running:
            if not self.connected or self.cap is None or not self.cap.isOpened():
                if self._conectar():
                    fallos_consecutivos = 0
                else:
                    time.sleep(2.0)
                    continue

            try:
                ret, frame = self.cap.read()
                if ret and frame is not None:
                    fallos_consecutivos = 0
                    with self.lock:
                        self.frame = frame
                        self.connected = True
                else:
                    fallos_consecutivos += 1
                    if fallos_consecutivos >= 10:
                        with self.lock:
                            self.connected = False
                        if self.cap:
                            try:
                                self.cap.release()
                            except Exception:
                                pass
                            self.cap = None
            except Exception:
                fallos_consecutivos += 1
                if fallos_consecutivos >= 10:
                    with self.lock:
                        self.connected = False

            time.sleep(0.002)

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        with self.lock:
            if self.frame is not None and self.connected:
                return True, self.frame.copy()
            return False, None

    def release(self):
        self.running = False
        with self.lock:
            self.connected = False
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

def escanear_puertos_disponibles(puertos_en_uso: Optional[List[int]] = None) -> List[int]:
    """
    Escaneo multiplataforma de hardware (Linux y Windows).
    En Linux valida capacidades V4L2_CAP_VIDEO_CAPTURE para evitar
    nodos secundarios de metadatos o salidas de loopback.
    """
    puertos = set()
    sistema = platform.system()

    if sistema == "Linux":
        import glob
        import fcntl
        import struct

        VIDIOC_QUERYCAP = 0x80685600
        V4L2_CAP_VIDEO_CAPTURE = 0x00000001
        V4L2_CAP_DEVICE_CAPS = 0x80000000

        puertos_validos = set()
        for dev_path in sorted(glob.glob("/dev/video*")):
            try:
                idx = int(os.path.basename(dev_path).replace("video", ""))
                fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
                buf = bytearray(104)
                fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
                os.close(fd)
                _, _, _, _, caps, dev_caps = struct.unpack("16s32s32sIII12x", buf[:104])
                actual_caps = dev_caps if (caps & V4L2_CAP_DEVICE_CAPS) else caps
                if actual_caps & V4L2_CAP_VIDEO_CAPTURE:
                    puertos_validos.add(idx)
            except Exception:
                try:
                    idx = int(os.path.basename(dev_path).replace("video", ""))
                    idx_file = f"/sys/class/video4linux/video{idx}/index"
                    if os.path.exists(idx_file):
                        with open(idx_file, "r") as f:
                            if f.read().strip() == "0":
                                puertos_validos.add(idx)
                except Exception:
                    pass

        if puertos_en_uso:
            for p in puertos_en_uso:
                if p in puertos_validos:
                    puertos.add(p)
        puertos.update(puertos_validos)

    elif sistema == "Windows":
        for i in range(10):
            cap = None
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        puertos.add(i)
            except Exception:
                pass
            finally:
                if cap is not None:
                    cap.release()
        if puertos_en_uso:
            puertos.update(puertos_en_uso)
    else:
        for i in range(10):
            cap = None
            try:
                cap = cv2.VideoCapture(i)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        puertos.add(i)
            except Exception:
                pass
            finally:
                if cap is not None:
                    cap.release()
        if puertos_en_uso:
            puertos.update(puertos_en_uso)

    return sorted(list(puertos))

LOCAL_INSPECTION_SERVICE = None
try:
    sys.path.insert(0, os.path.join(BASE_DIR, "microservices", "defect_inspection"))
    from app.model import DefectDetectionModel
    from app.service import DefectInspectionService
    from app.config import settings as inspect_settings

    candidates = [
        os.getenv("MODEL_PATH"),
        os.path.join(BASE_DIR, "microservices", "shared", "models", "best_block.pt"),
        os.path.join(BASE_DIR, "microservices", "shared", "models", "best_abolladura.pt"),
        os.path.join(BASE_DIR, "microservices", "pawn_counter", "weights", "best_peones.pt"),
        os.path.join(BASE_DIR, "best_block.pt"),
        os.path.join(BASE_DIR, "best_abolladura.pt"),
        os.path.join(BASE_DIR, "best.pt"),
    ]
    model_candidate = None
    for cand in candidates:
        if cand and os.path.exists(cand):
            model_candidate = cand
            break

    if os.path.exists(model_candidate):
        defect_model = DefectDetectionModel(model_path=model_candidate, device=DEFAULT_DEVICE, conf_threshold=DEFAULT_CONF_THRESHOLD)
        LOCAL_INSPECTION_SERVICE = DefectInspectionService(model=defect_model)
        print(f"[+] Motor de Inspección local cargado exitosamente ({os.path.basename(model_candidate)}) en dispositivo [{DEFAULT_DEVICE}].")
except Exception as e:
    print(f"[*] Inferencia in-process omitida ({e}). Se utilizará delegación HTTP.")

def ejecutar_inspeccion(frame: np.ndarray) -> Dict[str, Any]:
    """
    Ejecuta inspección modular:
    1. Si INSPECTION_SERVICE_URL está configurado, delega por HTTP al contenedor Docker.
    2. Si no, ejecuta el servicio modular in-process sin código espagueti.
    """
    if INSPECTION_SERVICE_URL:
        try:
            _, buffer = cv2.imencode(".jpg", frame)
            files = {"file": ("frame.jpg", buffer.tobytes(), "image/jpeg")}
            res = requests.post(INSPECTION_SERVICE_URL, files=files, timeout=3.0)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            print(f"[-] Error delegando a microservicio HTTP: {e}")

    if LOCAL_INSPECTION_SERVICE is not None:
        _, buffer = cv2.imencode(".jpg", frame)
        resp = LOCAL_INSPECTION_SERVICE.process_image(buffer.tobytes())
        return resp.model_dump()

    return {
        "status": "APROBADO",
        "is_approved": True,
        "qr": {"detected": True, "content": "DEMO_BATCH"},
        "defectos": [],
        "total_defectos": 0,
        "tiempo_inferencia_ms": 0.0
    }

motor_activo = False
frames_individuales: Dict[str, Optional[bytes]] = {"frontal": None, "lat_der": None, "lat_izq": None}
estado_camaras = {"frontal": False, "lat_der": False, "lat_izq": False}

try:
    with open(CONFIG_FILE, 'r') as f:
        config_camaras = json.load(f)
except Exception:
    config_camaras = {"frontal": 1, "lat_der": None, "lat_izq": None}

def dibujar_defectos(frame: np.ndarray, defectos: List[Dict[str, Any]], cam_nombre: str = "") -> np.ndarray:
    if not defectos:
        return frame
    vis = frame.copy()
    h, w = vis.shape[:2]
    for d in defectos:
        box = d.get("bbox")
        if not box or len(box) != 4:
            continue
        x1, y1, x2, y2 = [int(round(v)) for v in box]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)
        if x2 <= x1 or y2 <= y1:
            continue

        cname = d.get("class_name", "defecto")
        conf = float(d.get("confidence", 0.0))
        tid = d.get("track_id")
        color = (0, 40, 240)
        cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
        lbl = f"#{tid} {cname} {conf*100:.0f}%" if tid is not None else f"{cname} {conf*100:.0f}%"
        (tw, th), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(vis, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(vis, lbl, (x1 + 3, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    cv2.rectangle(vis, (0, 0), (w, 28), (20, 24, 30), -1)
    tag = f"ScannorQA · [{cam_nombre.upper()}] Defectos: {len(defectos)}"
    cv2.putText(vis, tag, (10, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return vis

stabilizers: Dict[str, BBoxStabilizer] = {
    "frontal": BBoxStabilizer(alpha=0.45, max_missing_time=1.2, max_missing_frames=15, high_threshold=0.45, low_threshold=0.25, min_hits_to_show=1),
    "lat_der": BBoxStabilizer(alpha=0.45, max_missing_time=1.2, max_missing_frames=15, high_threshold=0.45, low_threshold=0.25, min_hits_to_show=1),
    "lat_izq": BBoxStabilizer(alpha=0.45, max_missing_time=1.2, max_missing_frames=15, high_threshold=0.45, low_threshold=0.25, min_hits_to_show=1)
}

tunnel_coordinator = TunnelPassageCoordinator(cooldown_segundos=1.8, tiempo_cierre_paso=0.45)

def procesar_video_continuo():
    global motor_activo, frames_individuales, config_camaras
    cams: Dict[str, Optional[AsyncCameraReader]] = {"frontal": None, "lat_der": None, "lat_izq": None}
    ultimo_analisis = 0.0

    while True:
        with state_lock:
            activo = motor_activo
            cfg_actual = dict(config_camaras)

        if activo:
            for k in ["frontal", "lat_der", "lat_izq"]:
                port = cfg_actual.get(k)
                reader = cams.get(k)
                if port is not None:
                    try:
                        port_int = int(port)
                    except (ValueError, TypeError):
                        port_int = None

                    if port_int is not None:
                        if reader is None or reader.src != port_int:
                            if reader is not None:
                                reader.release()
                            cams[k] = AsyncCameraReader(port_int)
                    else:
                        if reader is not None:
                            reader.release()
                            cams[k] = None
                else:
                    if reader is not None:
                        reader.release()
                        cams[k] = None
                estado_camaras[k] = bool(cams[k] and cams[k].connected)

            frames_actuales: Dict[str, np.ndarray] = {}
            t_now_cap = time.time()
            for k in ["frontal", "lat_der", "lat_izq"]:
                if cams[k]:
                    ret, f = cams[k].read()
                    if ret and f is not None:
                        frames_actuales[k] = f
                        cajas_activas = stabilizers[k].get_active_boxes(t_now_cap)
                        if cajas_activas:
                            frame_stream = dibujar_defectos(f, cajas_activas, k)
                        else:
                            frame_stream = f
                        _, buf = cv2.imencode('.jpg', frame_stream, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                        frames_individuales[k] = buf.tobytes()

            t_now = time.time()
            if (t_now - ultimo_analisis) >= 0.20 and len(frames_actuales) > 0:
                ultimo_analisis = t_now

                orden_cams = [c for c in ["frontal", "lat_der", "lat_izq"] if c in frames_actuales]

                for cam_nombre in orden_cams:
                    frame_cam = frames_actuales[cam_nombre]
                    res_cam = ejecutar_inspeccion(frame_cam)

                    qr_c = res_cam.get("qr", {}).get("content")
                    defectos_cam = res_cam.get("defectos", [])
                    is_approved_cam = res_cam.get("is_approved", True)

                    defectos_estabilizados = stabilizers[cam_nombre].update(defectos_cam, t_now)

                    qr_presente = bool(qr_c and qr_c not in ["Sin_QR", "DEMO_BATCH", None, ""])
                    hay_defecto_cam = (not is_approved_cam) or len(defectos_estabilizados) > 0
                    hay_presencia = qr_presente or hay_defecto_cam

                    vis_cam = dibujar_defectos(frame_cam, defectos_estabilizados, cam_nombre)

                    tunnel_coordinator.registrar_observacion(
                        t_now=t_now,
                        hay_presencia=hay_presencia,
                        qr_encontrado=qr_c if qr_presente else None,
                        defectos_cam=defectos_estabilizados if hay_defecto_cam else [],
                        cam_nombre=cam_nombre,
                        frame_limpio=frame_cam,
                        frame_vis=vis_cam
                    )

                evento_consolidado = tunnel_coordinator.evaluar_cierre_sesion(t_now)
                if evento_consolidado is not None:
                    registrar_evento(
                        lote=evento_consolidado["lote"],
                        estado=evento_consolidado["estado"],
                        falla=evento_consolidado["falla"],
                        confianza=evento_consolidado["confianza"],
                        img_limpia=evento_consolidado["img_limpia"],
                        img_vis=evento_consolidado["img_vis"]
                    )

            time.sleep(0.005)
        else:
            tunnel_coordinator.reset()
            for stab in stabilizers.values():
                stab.clear()
            for k, c in cams.items():
                if c is not None:
                    c.release()
                    cams[k] = None
                estado_camaras[k] = False
            time.sleep(0.5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    t_img = threading.Thread(target=img_writer_worker, daemon=True)
    t_db = threading.Thread(target=db_writer_worker, daemon=True)
    t_sync = threading.Thread(target=background_sync_worker, daemon=True)
    t_cam = threading.Thread(target=procesar_video_continuo, daemon=True)

    t_img.start(); t_db.start(); t_sync.start(); t_cam.start()
    yield
    with state_lock:
        global motor_activo
        motor_activo = False

app = FastAPI(title="Scannor Edge Vision Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.mount("/capturas", StaticFiles(directory=CAPTURES_DIR), name="capturas")

class ConfiguracionHardware(BaseModel):
    frontal: Optional[int] = None
    lat_der: Optional[int] = None
    lat_izq: Optional[int] = None

    @model_validator(mode='after')
    def validar_puertos(self):
        p = [x for x in (self.frontal, self.lat_der, self.lat_izq) if x is not None]
        if len(p) != len(set(p)):
            raise ValueError('Conflicto: Múltiples cámaras configuradas en el mismo puerto.')
        return self

@app.get("/api/health", dependencies=[Security(verificar_api_key)])
def health_check():
    with state_lock:
        return {
            "motor_activo": motor_activo,
            "camaras": estado_camaras,
            "segundos_sin_sync": int(time.time() - sync_stats["ultimo_exito"]),
            "errores_auth_sync": sync_stats["errores_auth"],
            "errores_permanentes": sync_stats["errores_permanentes"],
            "total_inspecciones": total_eventos_procesados
        }

@app.post("/api/start", dependencies=[Security(verificar_api_key)])
def start_motor():
    global motor_activo
    with state_lock:
        motor_activo = True
    return {"status": "encendido"}

@app.post("/api/stop", dependencies=[Security(verificar_api_key)])
def stop_motor():
    global motor_activo
    with state_lock:
        motor_activo = False
    return {"status": "apagado"}

@app.delete("/api/clear", dependencies=[Security(verificar_api_key)])
def clear_history():
    with state_lock:
        failures.clear()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM inspecciones WHERE sync = 1")
        conn.commit()
    return {"status": "Memoria limpia. Datos pendientes preservados."}

@app.api_route("/api/borrar-capturas", methods=["DELETE", "POST"], dependencies=[Security(verificar_api_key)])
def borrar_todas_capturas():
    total_borrados = 0
    if os.path.exists(CAPTURES_DIR):
        for f in os.listdir(CAPTURES_DIR):
            fp = os.path.join(CAPTURES_DIR, f)
            try:
                if os.path.isfile(fp):
                    os.unlink(fp)
                    total_borrados += 1
            except Exception:
                pass
    with state_lock:
        failures.clear()
    return {"status": "Capturas eliminadas con éxito", "total_borrados": total_borrados}

async def stream_generator(cam_id: str):
    last_frame_bytes = None
    while True:
        frame_bytes = frames_individuales.get(cam_id)
        if frame_bytes is not None and frame_bytes is not last_frame_bytes:
            last_frame_bytes = frame_bytes
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            await asyncio.sleep(0.001)
        else:
            await asyncio.sleep(0.003)

@app.get("/video_feed", dependencies=[Security(verificar_stream_token)])
@app.get("/video_feed/{cam_id}", dependencies=[Security(verificar_stream_token)])
async def video_feed_cam(cam_id: str = "frontal"):
    if cam_id not in ["frontal", "lat_der", "lat_izq"]:
        raise HTTPException(status_code=404, detail="Cámara no encontrada")
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return StreamingResponse(stream_generator(cam_id), media_type="multipart/x-mixed-replace; boundary=frame", headers=headers)

@app.websocket("/ws/failures")
async def websocket_failures(websocket: WebSocket, token: str = Query(None)):
    if not secrets.compare_digest(token or "", STREAM_TOKEN):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    last_sent = -1
    try:
        while True:
            with state_lock:
                current_total = total_eventos_procesados
                current_data = list(failures)
            if current_total != last_sent:
                await websocket.send_json({"failures": current_data})
                last_sent = current_total
            await asyncio.sleep(1.0)
    except Exception:
        pass

@app.get("/api/estado-camaras", dependencies=[Security(verificar_api_key)])
def estado_camaras_actual():
    with state_lock:
        return {"motor_activo": motor_activo, "config_actual": config_camaras, "conectada": estado_camaras}

@app.get("/api/escanear-hardware", dependencies=[Security(verificar_api_key)])
def escanear_hardware():
    with state_lock:
        cfg = dict(config_camaras)
        en_uso = [int(v) for v in cfg.values() if v is not None and str(v).isdigit()]
    puertos = escanear_puertos_disponibles(puertos_en_uso=en_uso)
    return {"puertos_disponibles": puertos, "config_actual": cfg, "error": None}

def actualizar_modelo_ota():
    print("\n[*] Verificando actualizaciones del Modelo de IA OTA...")
    try:
        base_url = DJANGO_API_URL.split('/api/')[0]
        ota_url = f"{base_url}/api/descargar-modelo/"
        res = requests.get(ota_url, headers={"X-Device-Auth": DEVICE_API_KEY}, stream=True, timeout=15)
        if res.status_code == 200:
            target = os.path.join(BASE_DIR, os.getenv("MODEL_PATH", "best.pt"))
            with open(target, 'wb') as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print(f"[+] ¡Descarga OTA exitosa en {target}!")
            return True
    except Exception as e:
        print(f"[-] Error en descarga OTA: {e}")
    return False

@app.post("/api/actualizar-modelo-ahora", dependencies=[Security(verificar_api_key)])
def trigger_hot_reload():
    global LOCAL_INSPECTION_SERVICE, motor_activo
    estaba_activo = motor_activo
    with state_lock:
        motor_activo = False

    descargado = actualizar_modelo_ota()
    target = os.path.join(BASE_DIR, os.getenv("MODEL_PATH", "best.pt"))
    if not os.path.exists(target) and not descargado:
        raise HTTPException(status_code=500, detail="Fallo en descarga OTA.")

    try:
        if os.path.exists(target):
            from app.model import DefectDetectionModel
            from app.service import DefectInspectionService
            d_model = DefectDetectionModel(model_path=target, device=DEFAULT_DEVICE, conf_threshold=DEFAULT_CONF_THRESHOLD)
            LOCAL_INSPECTION_SERVICE = DefectInspectionService(model=d_model)
        with state_lock:
            if estaba_activo:
                motor_activo = True
        return {"status": "exito", "mensaje": "Modelo recargado exitosamente vía OTA"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/guardar-configuracion", dependencies=[Security(verificar_api_key)])
def guardar_configuracion(nueva_config: ConfiguracionHardware):
    global motor_activo, config_camaras
    with state_lock:
        config_camaras.update({"frontal": nueva_config.frontal, "lat_der": nueva_config.lat_der, "lat_izq": nueva_config.lat_izq})
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(config_camaras, f)
    os.replace(tmp, CONFIG_FILE)
    return {"status": "exito"}

@app.get("/api/descargar-dataset", dependencies=[Security(verificar_api_key)])
def descargar_dataset_edge():
    """Empaqueta y descarga en zip EXCLUSIVAMENTE las imágenes limpias (.jpg sin _vis.jpg) y sus etiquetas .txt."""
    import zipfile
    from io import BytesIO
    from fastapi.responses import Response

    buf = BytesIO()
    if os.path.exists(CAPTURES_DIR):
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(CAPTURES_DIR):
                if f.endswith("_vis.jpg"):
                    continue
                if f.endswith(".jpg") or f.endswith(".txt"):
                    fp = os.path.join(CAPTURES_DIR, f)
                    if os.path.isfile(fp):
                        zf.write(fp, arcname=f)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="Dataset_Edge_Limpio.zip"'}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL)