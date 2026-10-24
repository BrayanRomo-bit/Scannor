#!/usr/bin/env python3
"""
Scannor Universal Offline Sync Manager (Store-and-Forward)
Garantiza persistencia local en SQLite (WAL mode) para piezas Aprobadas y Rechazadas
de cualquier modelo o microservicio IA, y resincronización automática con Django
cuando regrese la conectividad a Internet.
"""

import os
import sys
import time
import json
import uuid
import glob
import queue
import socket
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger("ScannorOfflineSync")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [OfflineSync] %(message)s", datefmt="%H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

def find_project_root() -> Path:
    current = Path(__file__).resolve().parent
    for _ in range(4):
        if (current / "manage.py").exists() or (current / "scannor_offline.db").exists():
            return current
        if list(current.glob("*.env")):
            return current
        current = current.parent
    return Path.cwd()

try:
    from microservices.shared.edge_credentials import (
        asegurar_credenciales_edge,
        obtener_mac_address,
        obtener_ip_local as get_local_ip,
        load_edge_environment
    )
except ImportError:
    from edge_credentials import (
        asegurar_credenciales_edge,
        obtener_mac_address,
        obtener_ip_local as get_local_ip,
        load_edge_environment
    )

def load_environment():
    load_edge_environment()

def sonar_alarma_rechazo():
    try:
        import platform
        sistema = platform.system()
        if sistema == "Windows":
            import winsound
            winsound.Beep(1000, 400)
        else:
            import subprocess
            alarma_path = os.path.join(os.path.dirname(__file__), "..", "..", "alarma.wav")
            if os.path.isfile(alarma_path):
                subprocess.run(["aplay", "-q", alarma_path], stderr=subprocess.DEVNULL, timeout=1.0)
            else:
                subprocess.run(["beep"], stderr=subprocess.DEVNULL, timeout=0.5)
    except Exception:
        pass

class OfflineSyncManager:
    """
    Gestor universal Store-and-Forward con tolerancia a cortes de red/internet.
    - Escritura inmediata en SQLite (WAL) en disco local.
    - Soporte completo para eventos 'Aprobado' y 'Rechazado'.
    - Worker en segundo plano para envío automático a Django.
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(
        self,
        db_path: Optional[str] = None,
        captures_dir: Optional[str] = None,
        django_api_url: Optional[str] = None,
        device_id: Optional[str] = None,
        device_key: Optional[str] = None,
        edge_public_url: Optional[str] = None,
        auto_start: bool = True
    ):
        load_environment()
        self.root_dir = find_project_root()

        self.db_path = db_path or os.getenv("SCANNOR_DB_PATH") or str(self.root_dir / "scannor_offline.db")
        self.captures_dir = captures_dir or os.getenv("SCANNOR_CAPTURES_DIR") or str(self.root_dir / "capturas_falla")
        os.makedirs(self.captures_dir, exist_ok=True)

        self.django_api_url = django_api_url or os.getenv("DJANGO_API_URL", "http://127.0.0.1:8000/api/guardar-falla/")
        self.device_id = device_id or os.getenv("SCANNOR_DEVICE_ID", "")
        self.device_key = device_key or os.getenv("SCANNOR_DEVICE_KEY", "")

        if auto_start and (not self.device_id or not self.device_key):
            creds = asegurar_credenciales_edge(service_name="Universal Offline Sync")
            self.device_id = creds["device_id"]
            self.device_key = creds["device_key"]
        
        port = int(os.getenv("PORT", "8001"))
        ip_local = get_local_ip()
        self.edge_url = edge_public_url or os.getenv("EDGE_PUBLIC_URL", f"http://{ip_local}:{port}").rstrip('/')

        self.db_queue: queue.Queue = queue.Queue(maxsize=1000)
        self.img_queue: queue.Queue = queue.Queue(maxsize=500)
        self.running = False

        self.stats = {
            "total_registrados": 0,
            "sincronizados_ok": 0,
            "fallos_conexion": 0,
            "errores_payload": 0,
            "ultimo_exito": None,
            "ultimo_intento": None,
            "is_online": True
        }

        self._init_db()

        if auto_start:
            self.start()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS inspecciones (
                        uid TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        sync INTEGER DEFAULT 0,
                        sync_retries INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inspecciones_sync ON inspecciones(sync);")
                conn.commit()
            logger.info(f"SQLite WAL inicializado en: {self.db_path}")
        except Exception as e:
            logger.error(f"Error inicializando SQLite en {self.db_path}: {e}")

    def start(self):
        if self.running:
            return
        self.running = True

        self.writer_thread = threading.Thread(target=self._db_writer_worker, daemon=True, name="OfflineDBWriter")
        self.writer_thread.start()

        self.img_thread = threading.Thread(target=self._img_writer_worker, daemon=True, name="OfflineImgWriter")
        self.img_thread.start()

        self.sync_thread = threading.Thread(target=self._sync_worker, daemon=True, name="OfflineDBSync")
        self.sync_thread.start()

        logger.info(f"OfflineSyncManager iniciado [Device: {self.device_id}, Target: {self.django_api_url}]")

    def stop(self):
        self.running = False
        try:
            self.db_queue.put_nowait(None)
            self.img_queue.put_nowait(None)
        except Exception:
            pass

    def _img_writer_worker(self):
        import cv2
        while self.running:
            try:
                item = self.img_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:
                self.img_queue.task_done()
                break

            try:
                if len(item) == 4:
                    filepath, frame, frame_vis, bbox_txt = item
                else:
                    filepath, frame = item
                    frame_vis = None
                    bbox_txt = None

                def _optimize_frame(fr):
                    if fr is None or not hasattr(fr, "shape") or len(fr.shape) < 2:
                        return None
                    h_fr, w_fr = fr.shape[:2]
                    max_dim = max(h_fr, w_fr)
                    if max_dim > 1920:
                        scale = 1920.0 / max_dim
                        new_w, new_h = int(w_fr * scale), int(h_fr * scale)
                        fr = cv2.resize(fr, (new_w, new_h), interpolation=cv2.INTER_AREA)
                    return fr

                if frame is not None:
                    opt_frame = _optimize_frame(frame)
                    if opt_frame is not None:
                        cv2.imwrite(filepath, opt_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])

                if frame_vis is not None:
                    opt_vis = _optimize_frame(frame_vis)
                    if opt_vis is not None:
                        vis_path = filepath.replace('.jpg', '_vis.jpg')
                        cv2.imwrite(vis_path, opt_vis, [int(cv2.IMWRITE_JPEG_QUALITY), 85])

                if bbox_txt:
                    txt_path = filepath.replace('.jpg', '.txt')
                    with open(txt_path, 'w', encoding='utf-8') as f_txt:
                        f_txt.write(str(bbox_txt).strip() + '\n')
            except Exception as e:
                logger.error(f"Error guardando evidencia a disco {filepath}: {e}")
            finally:
                self.img_queue.task_done()

    def _db_writer_worker(self):
        while self.running:
            try:
                evento = self.db_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if evento is None:
                self.db_queue.task_done()
                break

            try:
                with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO inspecciones (uid, payload, sync, sync_retries) VALUES (?, ?, 0, 0)",
                        (evento["uid"], json.dumps(evento))
                    )
                    conn.commit()
                self.stats["total_registrados"] += 1
            except Exception as e:
                logger.error(f"Error insertando inspección en SQLite: {e}")
            finally:
                self.db_queue.task_done()

    def drain_db_queue(self):
        """Vacia la cola en memoria directamente a SQLite de forma atómica."""
        eventos = []
        while not self.db_queue.empty():
            try:
                ev = self.db_queue.get_nowait()
                if ev is not None:
                    eventos.append(ev)
                self.db_queue.task_done()
            except queue.Empty:
                break
        if eventos:
            try:
                with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                    for evento in eventos:
                        conn.execute(
                            "INSERT OR IGNORE INTO inspecciones (uid, payload, sync, sync_retries) VALUES (?, ?, 0, 0)",
                            (evento["uid"], json.dumps(evento))
                        )
                    conn.commit()
                self.stats["total_registrados"] += len(eventos)
            except Exception as e:
                logger.error(f"Error volcando db_queue a SQLite: {e}")

    def registrar_inspeccion(
        self,
        lote: str,
        estado: str,
        falla: str,
        confianza: float = 1.0,
        imagen_frame: Optional[Any] = None,
        imagen_vis: Optional[Any] = None,
        bbox_txt: Optional[str] = None,
        imagen_url: Optional[str] = None,
        extra_meta: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Registra una inspección de cualquier modelo (Aprobado o Rechazado).
        - Genera UID único.
        - Guarda captura visual en capturas_dir si hay fotograma.
        - Emite alarma sonora si es Rechazado.
        - Encola atómicamente para persistencia inmediata en SQLite.
        """
        t_now = time.time()
        ts_iso = datetime.now(timezone.utc).isoformat()
        clean_falla = str(falla).strip().replace(" ", "_")
        clean_lote = str(lote or "LOTE-DEFAULT").strip().replace(" ", "_")

        estado_str = "Aprobado" if "aprob" in str(estado).lower() or str(estado).upper() == "OK" else "Rechazado"

        if estado_str == "Rechazado":
            threading.Thread(target=sonar_alarma_rechazo, daemon=True).start()

        fname = f"Evidencia_{clean_lote}_{clean_falla}_{int(t_now)}_{uuid.uuid4().hex[:4]}.jpg"
        fname_vis = fname.replace('.jpg', '_vis.jpg')
        img_path = os.path.join(self.captures_dir, fname)

        if imagen_frame is not None or imagen_vis is not None:
            try:
                self.img_queue.put_nowait((img_path, imagen_frame, imagen_vis, bbox_txt))
                final_img_url = f"{self.edge_url}/capturas/{fname_vis if imagen_vis is not None else fname}"
            except queue.Full:
                logger.warning("Cola de imágenes llena. Se descartó fotograma temporal.")
                final_img_url = imagen_url or f"{self.edge_url}/capturas/no_frame.jpg"
        elif imagen_url:
            final_img_url = imagen_url
        else:
            final_img_url = f"{self.edge_url}/capturas/{fname_vis if imagen_vis is not None else fname}"

        uid = f"{self.device_id}_{clean_lote}_{int(t_now)}_{uuid.uuid4().hex[:6]}"

        evento = {
            "uid": uid,
            "dispositivo_id": self.device_id,
            "lote": clean_lote,
            "timestamp": ts_iso,
            "falla": clean_falla or ("Pieza_Conforme" if estado_str == "Aprobado" else "Defecto_No_Especificado"),
            "confianza": float(confianza),
            "estado": estado_str,
            "imagen": final_img_url
        }

        if bbox_txt:
            evento["bbox_txt"] = bbox_txt

        if extra_meta:
            evento["meta"] = extra_meta

        try:
            self.db_queue.put_nowait(evento)
        except queue.Full:
            logger.error("Cola de base de datos llena. Guardando directamente de emergencia...")
            try:
                with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO inspecciones (uid, payload, sync, sync_retries) VALUES (?, ?, 0, 0)",
                        (evento["uid"], json.dumps(evento))
                    )
                    conn.commit()
            except Exception as e:
                logger.critical(f"Pérdida crítica de registro SQLite: {e}")

        return uid

    def _sync_worker(self):
        import requests
        while self.running:
            self.flush_sync_now(max_items=25)
            time.sleep(1.5)

    def flush_sync_now(self, max_items: int = 50) -> int:
        """
        Intenta sincronizar los registros pendientes (sync = 0) con Django.
        Retorna la cantidad de registros exitosamente sincronizados en este ciclo.
        """
        import requests
        import base64
        self.drain_db_queue()
        sincronizados_ahora = 0

        try:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT uid, payload, sync_retries FROM inspecciones WHERE sync = 0 ORDER BY rowid ASC LIMIT ?",
                    (max_items,)
                )
                filas = cur.fetchall()

                if not filas:
                    return 0

                headers = {
                    "Content-Type": "application/json",
                    "X-Device-Auth": self.device_key
                }

                for uid, payload_str, retries in filas:
                    self.stats["ultimo_intento"] = time.time()
                    try:
                        payload = json.loads(payload_str)
                        payload_django = {
                            "uid": payload["uid"],
                            "dispositivo_id": payload["dispositivo_id"],
                            "timestamp": payload["timestamp"],
                            "lote": payload["lote"],
                            "falla": payload["falla"],
                            "confianza": payload["confianza"],
                            "estado": payload["estado"],
                            "imagen": payload["imagen"]
                        }

                        fname = os.path.basename(payload["imagen"])
                        fname_limpio = fname.replace('_vis.jpg', '.jpg')
                        fname_vis = fname_limpio.replace('.jpg', '_vis.jpg')

                        candidato_vis = os.path.join(self.captures_dir, fname_vis)
                        if os.path.isfile(candidato_vis):
                            try:
                                with open(candidato_vis, 'rb') as f_bytes:
                                    payload_django["imagen_base64"] = base64.b64encode(f_bytes.read()).decode('utf-8')
                            except Exception:
                                pass

                        candidato_limpio = os.path.join(self.captures_dir, fname_limpio)
                        if os.path.isfile(candidato_limpio):
                            try:
                                with open(candidato_limpio, 'rb') as f_clean:
                                    payload_django["imagen_limpia_base64"] = base64.b64encode(f_clean.read()).decode('utf-8')
                            except Exception:
                                pass
                        elif "imagen_base64" in payload_django and not fname.endswith('_vis.jpg'):
                            payload_django["imagen_limpia_base64"] = payload_django["imagen_base64"]

                        txt_path = os.path.join(self.captures_dir, fname_limpio.replace('.jpg', '.txt'))
                        if os.path.isfile(txt_path):
                            try:
                                with open(txt_path, 'r', encoding='utf-8') as f_txt:
                                    payload_django["bbox_txt"] = f_txt.read()
                            except Exception:
                                pass
                        elif payload.get("bbox_txt"):
                            payload_django["bbox_txt"] = payload["bbox_txt"]

                        res = requests.post(
                            self.django_api_url,
                            json=payload_django,
                            headers=headers,
                            timeout=3.5
                        )

                        if res.status_code in (200, 201):
                            conn.execute("UPDATE inspecciones SET sync = 1 WHERE uid = ?", (uid,))
                            conn.commit()
                            sincronizados_ahora += 1
                            self.stats["sincronizados_ok"] += 1
                            self.stats["ultimo_exito"] = time.time()
                            self.stats["is_online"] = True
                        elif res.status_code in (400, 404, 422):
                            conn.execute("UPDATE inspecciones SET sync = 2, sync_retries = sync_retries + 1 WHERE uid = ?", (uid,))
                            conn.commit()
                            self.stats["errores_payload"] += 1
                            logger.warning(f"Error permanente ({res.status_code}) en {uid}: {res.text}")
                        else:
                            conn.execute("UPDATE inspecciones SET sync_retries = sync_retries + 1 WHERE uid = ?", (uid,))
                            conn.commit()
                            break

                    except requests.exceptions.RequestException as re:
                        self.stats["fallos_conexion"] += 1
                        self.stats["is_online"] = False
                        break

        except Exception as e:
            logger.error(f"Error en sweep de sincronización: {e}")

        return sincronizados_ahora

    def get_sync_status(self) -> Dict[str, Any]:
        """
        Retorna la telemetría del buffer offline y estado de sincronización.
        """
        self.drain_db_queue()
        pendientes = 0
        sincronizados = 0
        descartados = 0
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT sync, count(*) FROM inspecciones GROUP BY sync")
                for s_val, cnt in cur.fetchall():
                    if s_val == 0:
                        pendientes = cnt
                    elif s_val == 1:
                        sincronizados = cnt
                    elif s_val == 2:
                        descartados = cnt
        except Exception:
            pass

        return {
            "is_online": self.stats["is_online"],
            "pendientes_offline": pendientes,
            "sincronizados_central": sincronizados,
            "errores_payload": descartados,
            "total_registrados_sesion": self.stats["total_registrados"],
            "ultimo_exito": self.stats["ultimo_exito"],
            "db_path": self.db_path,
            "target_url": self.django_api_url,
            "device_id": self.device_id
        }

_global_manager: Optional[OfflineSyncManager] = None

def get_offline_sync_manager(**kwargs) -> OfflineSyncManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = OfflineSyncManager(**kwargs)
    return _global_manager
