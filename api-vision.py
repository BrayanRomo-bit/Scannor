from fastapi import FastAPI, WebSocket, HTTPException, status, Security, Query
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from ultralytics import YOLO
from pyzbar.pyzbar import decode 
from pydantic import BaseModel, model_validator
from datetime import datetime, timezone
from uuid import uuid4
import collections
import sqlite3
import cv2
import json
import numpy as np
import time
import os
import sys
import glob
import asyncio
import requests
import threading 
import subprocess 
import re
import queue
import secrets
import platform
import uuid
from dotenv import load_dotenv

# ==========================================
# 0. BUSCADOR INTELIGENTE DE CREDENCIALES (.ENV)
# ==========================================
if getattr(sys, 'frozen', False):
    directorio_base = os.path.dirname(sys.executable)
else:
    directorio_base = os.path.dirname(os.path.abspath(__file__))

archivos_env = glob.glob(os.path.join(directorio_base, '*.env'))
if archivos_env:
    print(f"[*] Archivo de credenciales detectado: {os.path.basename(archivos_env[0])}")
    load_dotenv(archivos_env[0])
else:
    print("[!] No se detectó archivo .env local. Se intentará Aprovisionamiento Zero-Touch.")

# ==========================================
# 1. APROVISIONAMIENTO AUTOMÁTICO (ZERO-TOUCH)
# ==========================================
DJANGO_PROVISION_URL = "http://127.0.0.1:8000/api/provisionar/"

def obtener_mac_address():
    """Obtiene la dirección MAC física de la tarjeta de red (Multiplataforma)"""
    mac_num = uuid.getnode()
    mac_hex = '{:012x}'.format(mac_num)
    mac_str = ':'.join(mac_hex[i:i+2] for i in range(0, 12, 2)).upper()
    return mac_str

def auto_provisionar():
    mac = obtener_mac_address()
    print("\n" + "="*50)
    print(f"[*] INICIANDO SCANNOR EDGE VISION")
    print(f"[*] MAC Address detectada: {mac}")
    print("[*] Solicitando credenciales al servidor central...")

    try:
        respuesta = requests.post(DJANGO_PROVISION_URL, json={'mac_address': mac}, timeout=10)

        if respuesta.status_code == 200:
            datos = respuesta.json()
            # Inyectamos las credenciales directamente en la memoria RAM (Entorno Virtual)
            for llave, valor in datos['env_data'].items():
                os.environ[llave] = str(valor)
            print("[+] ¡Aprovisionamiento Exitoso! Credenciales cargadas en memoria.")
            print("="*50 + "\n")
            return True
        else:
            print(f"[-] Acceso Denegado. El servidor respondió: {respuesta.text}")
            print(f"[-] Asegúrate de registrar la MAC {mac} en el panel de Django.")
            print("="*50 + "\n")
            return False

    except requests.exceptions.ConnectionError:
        print("[-] Error crítico: No se pudo conectar con el servidor Django.")
        print("="*50 + "\n")
        return False

# Si no existen las llaves en el entorno, intentamos descargarlas
if not os.getenv("SCANNOR_DEVICE_ID"):
    if not auto_provisionar():
        print("Deteniendo sistema por falta de credenciales...")
        exit(1)

# ==========================================
# 2. CONFIGURACIÓN Y CONSTANTES
# ==========================================
DEVICE_ID = os.getenv("SCANNOR_DEVICE_ID")
DEVICE_API_KEY = os.getenv("SCANNOR_DEVICE_KEY")
ADMIN_API_KEY = os.getenv("SCANNOR_API_KEY")
STREAM_TOKEN = os.getenv("SCANNOR_STREAM_TOKEN")
DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://127.0.0.1:8000/api/guardar-falla/")

if not all([DEVICE_ID, DEVICE_API_KEY, ADMIN_API_KEY, STREAM_TOKEN]):
    raise RuntimeError("CRÍTICO: Faltan llaves de seguridad en el entorno (DEVICE_ID, DEVICE_KEY, ADMIN o STREAM).")

EDGE_PUBLIC_URL = os.getenv("EDGE_PUBLIC_URL", "http://127.0.0.1:8001").rstrip('/')
DB_PATH = os.path.join(directorio_base, os.getenv("SCANNOR_DB_PATH", "scannor_offline.db"))
ARCHIVO_CONFIG = os.path.join(directorio_base, 'config_camaras.json')

# --- UMBRALES DE CONSENSO TEMPORAL Y COOLDOWN ---
CONF_THRESHOLD = float(os.getenv("QA_CONF_THRESHOLD", "0.15")) 
VENTANA_FRAMES = int(os.getenv("QA_VENTANA_FRAMES", "5"))
MIN_DETECCIONES_CONSISTENTES = int(os.getenv("QA_MIN_DETECCIONES", "3"))
CONF_MINIMA_EVIDENCIA = float(os.getenv("QA_CONF_MINIMA_EVIDENCIA", "0.20")) 

if MIN_DETECCIONES_CONSISTENTES > VENTANA_FRAMES:
    raise RuntimeError("CRÍTICO: QA_MIN_DETECCIONES no puede ser mayor que QA_VENTANA_FRAMES.")

TIEMPO_APROBACION = float(os.getenv("QA_TIEMPO_APROBACION", "3.0"))
TIEMPO_VIDA_MAX = TIEMPO_APROBACION + 1.5 
TIEMPO_GC = float(os.getenv("QA_TIEMPO_GC", "2.0")) 
MAX_FAILURES_BUFFER = int(os.getenv("QA_MAX_BUFFER", "100"))

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)
state_lock = threading.Lock()

async def verificar_api_key(api_key: str = Security(api_key_header)):
    if not secrets.compare_digest(api_key, ADMIN_API_KEY): 
        raise HTTPException(status_code=403, detail="Admin Key inválida")

async def verificar_stream_token(token: str = Query(None)):
    if not secrets.compare_digest(token or "", STREAM_TOKEN): 
        raise HTTPException(status_code=403, detail="Stream Token inválido")

# ==========================================
# 3. COLAS ASÍNCRONAS Y SQLITE
# ==========================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute('''CREATE TABLE IF NOT EXISTS inspecciones 
                     (uid TEXT PRIMARY KEY, payload TEXT, sync INTEGER DEFAULT 0, sync_retries INTEGER DEFAULT 0)''')
init_db()

db_queue = queue.Queue(maxsize=500) 
img_queue = queue.Queue(maxsize=500)
total_eventos_procesados = 0  

estado_sincronizacion = {
    "ultimo_exito": time.time(), 
    "errores_permanentes": 0, 
    "imagenes_descartadas": 0,
    "eventos_descartados_cola": 0,
    "eventos_perdidos_local": 0
}

def img_writer_worker():
    while True:
        item = img_queue.get()
        if item is None: 
            img_queue.task_done(); break
        
        fname, img_data, bbox_txt = item
        try: 
            ruta_img = os.path.join(directorio_base, 'capturas_falla', fname)
            cv2.imwrite(ruta_img, img_data)
            
            # Generador Automático de Etiquetas YOLO (.txt)
            if bbox_txt:
                ruta_txt = os.path.join(directorio_base, 'capturas_falla', fname.replace('.jpg', '.txt'))
                with open(ruta_txt, 'w') as f:
                    f.write(bbox_txt + '\n')
                    
        except Exception as e: 
            print(f"CRÍTICO: Error escribiendo evidencia a disco: {e}")
        finally: 
            img_queue.task_done()

def db_writer_worker():
    while True:
        evento = db_queue.get()
        if evento is None: 
            db_queue.task_done(); break
        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                conn.execute("INSERT OR IGNORE INTO inspecciones (uid, payload, sync, sync_retries) VALUES (?, ?, 0, 0)",
                             (evento["uid"], json.dumps(evento)))
        except Exception as e:
            if evento.get("insert_retry_count", 0) < 3:
                evento["insert_retry_count"] = evento.get("insert_retry_count", 0) + 1
                time.sleep(0.5) 
                db_queue.put(evento)
            else:
                print(f"CRÍTICO: Evento perdido tras 3 reintentos I/O local: {evento.get('uid', 'Desconocido')}")
                estado_sincronizacion["eventos_perdidos_local"] += 1
        finally:
            db_queue.task_done()

def persistir_evento(registro):
    global total_eventos_procesados
    registro["uid"] = f"{DEVICE_ID}_{registro['lote']}_{int(time.time())}_{uuid4().hex[:6]}"
    registro["dispositivo_id"] = DEVICE_ID
    try: db_queue.put_nowait(registro)
    except queue.Full: 
        print("CRÍTICO: DB Queue llena. Evento descartado.")
        estado_sincronizacion["eventos_descartados_cola"] += 1

    with state_lock:
        failures.append(registro)
        total_eventos_procesados += 1

def background_sync_worker():
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                c = conn.cursor()
                c.execute("SELECT uid, payload, sync_retries FROM inspecciones WHERE sync = 0 ORDER BY rowid ASC LIMIT 15")
                filas = c.fetchall()

                for uid, payload_str, retries in filas:
                    if retries >= 10:
                        conn.execute("UPDATE inspecciones SET sync = 2 WHERE uid = ?", (uid,))
                        estado_sincronizacion["errores_permanentes"] += 1
                        print(f"CRÍTICO: Evento {uid} abandonado. Máximo de retries superado.")
                        continue

                    try:
                        res = requests.post(DJANGO_API_URL, json=json.loads(payload_str), headers={"X-Device-Auth": DEVICE_API_KEY}, timeout=3.0)
                        res.raise_for_status() 
                        conn.execute("UPDATE inspecciones SET sync = 1 WHERE uid = ?", (uid,))
                        estado_sincronizacion["ultimo_exito"] = time.time()
                    except requests.exceptions.HTTPError as he:
                        status_code = he.response.status_code
                        if 400 <= status_code < 500 and status_code not in (408, 429):
                            conn.execute("UPDATE inspecciones SET sync = 2 WHERE uid = ?", (uid,))
                            estado_sincronizacion["errores_permanentes"] += 1
                            print(f"Poison Pill {status_code} detectada y aislada para UID: {uid}")
                        else:
                            conn.execute("UPDATE inspecciones SET sync_retries = sync_retries + 1 WHERE uid = ?", (uid,))
                    except requests.exceptions.RequestException as e:
                        conn.execute("UPDATE inspecciones SET sync_retries = sync_retries + 1 WHERE uid = ?", (uid,))
                        break 
        except Exception:
            pass 
        time.sleep(5)

# ==========================================
# 4. CICLO DE VIDA FASTAPI
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    h_img = threading.Thread(target=img_writer_worker, daemon=True)
    h_db = threading.Thread(target=db_writer_worker, daemon=True)
    h_sync = threading.Thread(target=background_sync_worker, daemon=True)
    h_vision = threading.Thread(target=procesar_video_continuo, daemon=True)

    h_img.start(); h_db.start(); h_sync.start(); h_vision.start()
    yield

    global motor_activo
    with state_lock: motor_activo = False
    print("Drenando colas de I/O en disco...")
    try:
        img_queue.put(None, timeout=2.0)
        db_queue.put(None, timeout=2.0)
        h_img.join(timeout=3.0)
        h_db.join(timeout=3.0)
    except queue.Full:
        pass

app = FastAPI(title="Motor de Visión ScannorQA Edge", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv("ALLOWED_ORIGIN", "http://127.0.0.1:8000")], allow_credentials=True, allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])

ruta_capturas = os.path.join(directorio_base, 'capturas_falla')
if not os.path.exists(ruta_capturas): os.makedirs(ruta_capturas)
app.mount("/capturas", StaticFiles(directory=ruta_capturas), name="capturas")

try:
    with open(ARCHIVO_CONFIG, 'r') as f: config_camaras = json.load(f)
except:
    config_camaras = {"frontal": 1, "lat_der": None, "lat_izq": None}

class ConfiguracionHardware(BaseModel):
    frontal: int | None; lat_der: int | None; lat_izq: int | None
    @model_validator(mode='after')
    def validar_puertos(self):
        p = [x for x in (self.frontal, self.lat_der, self.lat_izq) if x is not None]
        if len(p) != len(set(p)): raise ValueError('Conflicto: Múltiples cámaras en el mismo puerto.')
        return self

estado_camaras = {"frontal": False, "lat_der": False, "lat_izq": False}

# ==========================================
# --- BUSCADOR DEL MODELO YOLO (CON DESCARGA OTA) ---
# ==========================================
ruta_modelo = os.path.join(directorio_base, os.getenv("MODEL_PATH", 'best.pt'))

def actualizar_modelo_ota():
    print("\n[*] Verificando actualizaciones del Modelo de IA en la nube...")
    try:
        base_url = DJANGO_API_URL.split('/api/')[0]
        ota_url = f"{base_url}/api/descargar-modelo/"
        
        respuesta = requests.get(ota_url, headers={"X-Device-Auth": DEVICE_API_KEY}, stream=True, timeout=15)
        
        if respuesta.status_code == 200:
            print("[*] Descargando modelo asignado... (Esto puede tomar unos minutos)")
            with open(ruta_modelo, 'wb') as f:
                for chunk in respuesta.iter_content(chunk_size=8192):
                    if chunk: f.write(chunk)
            print("[+] ¡Descarga OTA exitosa! Modelo guardado y listo.")
        elif respuesta.status_code == 404:
            print("[-] El servidor indica que esta cámara aún no tiene un modelo asignado.")
        else:
            print(f"[-] Error de conexión OTA: HTTP {respuesta.status_code}")
    except Exception as e:
        print(f"[-] No se pudo conectar al servidor para OTA: {e}")

# Ejecutamos la búsqueda OTA antes de arrancar el motor
actualizar_modelo_ota()

if not os.path.exists(ruta_modelo):
    print(f"CRÍTICO: No se encontró el modelo local ni se pudo descargar.")

try:
    model = YOLO(ruta_modelo)
except Exception as e:
    print(f"Error al cargar YOLO. Detalle: {e}")
    model = None

failures = collections.deque(maxlen=MAX_FAILURES_BUFFER)
frame_global_codificado = None
motor_activo = False  

class LectorCamaraAsincrono:
    def __init__(self, src):
        self.src = src
        self.lock = threading.Lock()
        self.conectar()
        self.hilo = threading.Thread(target=self.actualizar, daemon=True)
        self.hilo.start()

    def conectar(self):
        self.cap = cv2.VideoCapture(self.src)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            ret, frame = self.cap.read()
            with self.lock:
                self.ret = ret
                self.frame = frame
                self.corriendo = True
        else:
            with self.lock:
                self.corriendo = False
                self.ret = False
                self.frame = None

    def actualizar(self):
        fallos_consecutivos = 0
        while True:
            with self.lock: corriendo = self.corriendo
            if not corriendo:
                time.sleep(5)
                self.conectar()
                continue

            ret, frame = self.cap.read()
            with self.lock:
                self.ret = ret
                self.frame = frame

            if not ret:
                fallos_consecutivos += 1
                if fallos_consecutivos > 30:
                    self.cap.release()
                    with self.lock: self.corriendo = False 
                    fallos_consecutivos = 0
            else:
                fallos_consecutivos = 0

            time.sleep(0.03) 

    def leer(self):
        with self.lock:
            return self.ret, self.frame.copy() if self.frame is not None else None

    def detener(self):
        with self.lock: self.corriendo = False
        if hasattr(self, 'cap') and self.cap.isOpened(): self.cap.release()

def sanitizar_texto(texto):
    s = re.sub(r'[^a-zA-Z0-9_\-]', '', texto)[:50]
    return s if s else None

def reportar_inspeccion(pieza, lote, estado, falla, confianza, t_actual):
    pieza["estado"] = estado
    ts_iso = datetime.now(timezone.utc).isoformat()
    fname = f"Evidencia_{lote}_{falla.replace(' ','_')}_{int(t_actual)}.jpg"

    if estado == "Rechazado":
        try:
            if platform.system() == "Windows":
                # En Windows el wav no viene nativo, hace beep
                import winsound
                threading.Thread(target=lambda: winsound.Beep(1000, 500), daemon=True).start()
            else:
                threading.Thread(target=lambda: subprocess.run(["aplay", "-q", "alarma.wav"], stderr=subprocess.DEVNULL), daemon=True).start()
        except:
            pass

    try: 
        img_queue.put_nowait((fname, pieza["evidencia"], pieza.get("bbox_txt", "")))
    except queue.Full: 
        estado_sincronizacion["imagenes_descartadas"] += 1

    persistir_evento({
        "lote": lote, "timestamp": ts_iso,
        "falla": falla, "confianza": confianza,
        "estado": estado, "imagen": f"{EDGE_PUBLIC_URL}/capturas/{fname}"
    })

# ==========================================
# 5. MOTOR DE VISIÓN (HILO CENTRAL)
# ==========================================
def procesar_video_continuo():
    global frame_global_codificado, motor_activo

    cams = {"frontal": None, "lat_der": None, "lat_izq": None}
    camaras_inicializadas = False

    id_en_transito = None 
    tiempo_ultimo_qr = 0.0
    ultimo_reporte = 0.0  
    piezas_en_escena = {}
    vista_panoramica = np.zeros((300, 400, 3), dtype=np.uint8)
    ultimo_analisis = 0

    while True:
        try:
            with state_lock: activo_local = motor_activo

            if activo_local and model is not None:
                if not camaras_inicializadas:
                    for k in cams.keys():
                        if config_camaras.get(k) is not None:
                            cams[k] = LectorCamaraAsincrono(config_camaras[k])
                    camaras_inicializadas = True
                    ultimo_analisis = time.time()

                t_actual = time.time()
                if (t_actual - ultimo_analisis) >= 0.1: 

                    with state_lock:
                        for k in cams: estado_camaras[k] = cams[k].corriendo if cams[k] else False

                    frames_a_concatenar = []
                    s1, frame1 = cams["frontal"].leer() if cams["frontal"] else (False, None)
                    s2, frame2 = cams["lat_der"].leer() if cams["lat_der"] else (False, None)
                    s3, frame3 = cams["lat_izq"].leer() if cams["lat_izq"] else (False, None)

                    # 1. TRACKING SIN FRAGMENTACIÓN
                    qr_texto = None
                    if s1 and frame1 is not None:
                        vis_frame1 = frame1.copy()
                        codigos_qr = decode(cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY))
                        if codigos_qr:
                            qr_raw = sanitizar_texto(codigos_qr[0].data.decode('utf-8'))
                            if qr_raw:
                                qr_texto = qr_raw
                                pts = codigos_qr[0].rect
                                cv2.rectangle(vis_frame1, (pts[0], pts[1]), (pts[0] + pts[2], pts[1] + pts[3]), (0, 255, 0), 2)
                                cv2.putText(vis_frame1, qr_texto, (pts[0], pts[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                                tiempo_ultimo_qr = t_actual
                    else:
                        vis_frame1 = None

                    if qr_texto:
                        instancia_activa_id = None
                        for tid, data_pieza in piezas_en_escena.items():
                            if data_pieza["lote"] == qr_texto and data_pieza["estado"] == "evaluando":
                                instancia_activa_id = tid
                                break

                        if not instancia_activa_id:
                            instancia_activa_id = f"{qr_texto}_{uuid4().hex[:6]}"
                            piezas_en_escena[instancia_activa_id] = {
                                "lote": qr_texto, "t_ingreso": t_actual, "t_visto": t_actual, 
                                "historial": collections.deque(maxlen=VENTANA_FRAMES),
                                "estado": "evaluando", "falla": "Desconocida", 
                                "confianza": 0.0, "evidencia": vista_panoramica.copy()
                            }
                        id_en_transito = instancia_activa_id
                    else:
                        if id_en_transito is None or (t_actual - tiempo_ultimo_qr) > 1.5:
                            if (t_actual - ultimo_reporte) > TIEMPO_GC:
                                if id_en_transito is None or not id_en_transito.startswith("Sintetico_"):
                                    id_en_transito = f"Sintetico_{int(t_actual)}_{uuid4().hex[:4]}"
                                    piezas_en_escena[id_en_transito] = {
                                        "lote": "Lote_Desconocido", "t_ingreso": t_actual, "t_visto": t_actual, 
                                        "historial": collections.deque(maxlen=VENTANA_FRAMES),
                                        "estado": "evaluando", "falla": "Desconocida", 
                                        "confianza": 0.0, "evidencia": vista_panoramica.copy()
                                    }

                    track_id_actual = id_en_transito

                    # 2. INFERENCIA YOLO 
                    mejor_clase_ciclo, mejor_conf_ciclo, mejor_box_ciclo = None, 0.0, None

                    for nombre_cam, s, f_eval, f_vis, w in [
                        ("frontal", s1, frame1, vis_frame1, 1.0), 
                        ("lat_der", s2, frame2, frame2, 0.8), 
                        ("lat_izq", s3, frame3, frame3, 0.8)
                    ]:
                        if config_camaras.get(nombre_cam) is not None:
                            if s and f_eval is not None:
                                res = model.predict(f_eval, conf=CONF_THRESHOLD, verbose=False)
                                frame_plot = f_vis.copy() if f_vis is not None else f_eval.copy()

                                if len(res[0].boxes) > 0:
                                    res_plot = res[0].plot(img=frame_plot)
                                    idx_max = int(res[0].boxes.conf.argmax())
                                    c = float(res[0].boxes.conf[idx_max])
                                    
                                    if c >= CONF_MINIMA_EVIDENCIA and c > mejor_conf_ciclo:
                                        mejor_conf_ciclo = c
                                        cls_id = int(res[0].boxes.cls[idx_max])
                                        mejor_clase_ciclo = model.names[cls_id]
                                        
                                        box = res[0].boxes.xywhn[idx_max]
                                        mejor_box_ciclo = f"{cls_id} {box[0]:.6f} {box[1]:.6f} {box[2]:.6f} {box[3]:.6f}"
                                else:
                                    res_plot = frame_plot

                                frames_a_concatenar.append(cv2.resize(res_plot, (400, 300)))
                            else:
                                ph = np.zeros((300, 400, 3), dtype=np.uint8)
                                cv2.putText(ph, f"SIN SENAL: {nombre_cam}", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)
                                frames_a_concatenar.append(ph)

                    if frames_a_concatenar: vista_panoramica = cv2.hconcat(frames_a_concatenar)

                    # 3. ACTUALIZACIÓN MÁQUINA DE ESTADOS
                    if track_id_actual and track_id_actual in piezas_en_escena:
                        pieza = piezas_en_escena[track_id_actual]
                        pieza["t_visto"] = t_actual 
                        
                        if pieza["estado"] == "evaluando":
                            pieza["historial"].append((mejor_clase_ciclo, mejor_conf_ciclo) if mejor_clase_ciclo else None)

                            if mejor_conf_ciclo > pieza["confianza"]:
                                pieza["confianza"] = mejor_conf_ciclo
                                pieza["falla"] = mejor_clase_ciclo
                                pieza["evidencia"] = vista_panoramica.copy()
                                if mejor_box_ciclo:
                                    pieza["bbox_txt"] = mejor_box_ciclo

                            # 4. RUTAS DE ACCIÓN DE FALLAS
                            detecciones = [d for d in pieza["historial"] if d is not None]
                            if len(detecciones) >= MIN_DETECCIONES_CONSISTENTES:
                                clase_dominante, apariciones = collections.Counter(cl for cl, _ in detecciones).most_common(1)[0]
                                if apariciones >= MIN_DETECCIONES_CONSISTENTES:
                                    confs = [c for cl, c in detecciones if cl == clase_dominante]
                                    reportar_inspeccion(pieza, pieza["lote"], "Rechazado", clase_dominante, sum(confs) / len(confs), t_actual)
                                    pieza["estado"] = "rechazado"

                    # 5. LIMPIEZA Y GARBAGE COLLECTOR
                    lotes_a_borrar = []
                    for tid, info in piezas_en_escena.items():
                        tiempo_en_escena = t_actual - info["t_ingreso"]
                        tiempo_sin_ver = t_actual - info["t_visto"]

                        # Criterio Aprobado: Sin fallas, tiempo cumplido y CON QR DETECTADO
                        if info["estado"] == "evaluando" and tiempo_en_escena > TIEMPO_APROBACION:
                            if info["lote"] != "Lote_Desconocido":
                                reportar_inspeccion(info, info["lote"], "Aprobado", "NINGUNA", 1.0, t_actual)
                                info["estado"] = "aprobado"
                            else:
                                reportar_inspeccion(info, info["lote"], "Requiere_Revision", "Falta_QR", 1.0, t_actual)
                                info["estado"] = "revision"
                                
                            ultimo_reporte = t_actual
                            if tid == id_en_transito:
                                id_en_transito = None

                        if tiempo_en_escena > TIEMPO_VIDA_MAX:
                            lotes_a_borrar.append(tid)
                        elif tiempo_sin_ver > TIEMPO_GC:
                            if info["estado"] == "evaluando":
                                falla_reporte = info["falla"] if info["falla"] != "Desconocida" else ("Falta_QR" if info["lote"] == "Lote_Desconocido" else "Perdida_Visual")
                                reportar_inspeccion(info, info["lote"], "Requiere_Revision", falla_reporte, info["confianza"], t_actual)
                            lotes_a_borrar.append(tid)

                    for tid in lotes_a_borrar:
                        del piezas_en_escena[tid]
                        if tid == id_en_transito: 
                            id_en_transito = None 
                            ultimo_reporte = t_actual 

                    ultimo_analisis = t_actual

                if vista_panoramica is not None:
                    ret, buffer = cv2.imencode('.jpg', vista_panoramica)
                    frame_global_codificado = buffer.tobytes()
                time.sleep(0.01) 

            else:
                if camaras_inicializadas:
                    for v in cams.values():
                        if v: v.detener()
                    camaras_inicializadas = False
                    with state_lock:
                        for k in estado_camaras: estado_camaras[k] = False
                    frame_global_codificado = None; id_en_transito = None; piezas_en_escena.clear()
                time.sleep(0.5)
        except Exception as e:
            print(f"CRÍTICO: Excepción Hilo Visión: {e}")
            time.sleep(1)

@app.get("/api/health", dependencies=[Security(verificar_api_key)])
def health_check():
    return {
        "motor_activo": motor_activo, 
        "camaras": estado_camaras,
        "segundos_sin_sync": int(time.time() - estado_sincronizacion["ultimo_exito"]),
        "cola_disco_db": db_queue.qsize(),
        "cola_disco_img": img_queue.qsize(),
        "errores_permanentes": estado_sincronizacion["errores_permanentes"],
        "imagenes_descartadas": estado_sincronizacion["imagenes_descartadas"],
        "eventos_descartados_cola": estado_sincronizacion["eventos_descartados_cola"],
        "eventos_perdidos_local": estado_sincronizacion["eventos_perdidos_local"]
    }

@app.post("/api/start", dependencies=[Security(verificar_api_key)])
def start_motor():
    global motor_activo
    if model is None:
        raise HTTPException(status_code=500, detail="El modelo de IA (best.pt) no se encontró y no pudo ser descargado vía OTA.")
    with state_lock: motor_activo = True
    return {"status": "encendido"}

@app.post("/api/stop", dependencies=[Security(verificar_api_key)])
def stop_motor():
    global motor_activo
    with state_lock: motor_activo = False
    return {"status": "apagado"}

@app.delete("/api/clear", dependencies=[Security(verificar_api_key)])
def clear_history():
    with state_lock: failures.clear()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM inspecciones WHERE sync = 1")
    return {"status": "Memoria limpia. Datos pendientes preservados."}

async def servidor_streaming_web():
    global frame_global_codificado 
    while True:
        if frame_global_codificado is not None:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_global_codificado + b'\r\n')
        await asyncio.sleep(0.03)

@app.get("/video_feed", dependencies=[Security(verificar_stream_token)])
async def video_feed():
    return StreamingResponse(servidor_streaming_web(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/failures")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(None)):
    import secrets
    if not secrets.compare_digest(token or "", STREAM_TOKEN):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION); return

    await websocket.accept()
    last_sent_count = -1 
    try:
        while True:
            with state_lock:
                current_total = total_eventos_procesados
                current_data = list(failures)

            if current_total != last_sent_count:
                await websocket.send_json({"failures": current_data})
                last_sent_count = current_total
            await asyncio.sleep(1)  
    except Exception: pass 

@app.get("/api/estado-camaras", dependencies=[Security(verificar_api_key)])
def estado_camaras_actual():
    with state_lock: return {"motor_activo": motor_activo, "config_actual": config_camaras, "conectada": estado_camaras}

@app.get("/api/escanear-hardware", dependencies=[Security(verificar_api_key)])
def escanear_hardware():
    with state_lock:
        if motor_activo: return {"puertos_disponibles": [], "config_actual": config_camaras, "error": "Detén el motor"}
        config = config_camaras.copy()

    puertos_activos = []
    for i in range(10):
        cap = cv2.VideoCapture(i)
        if cap.isOpened() and cap.read()[0]: puertos_activos.append(i)
        cap.release()  
    return {"puertos_disponibles": puertos_activos, "config_actual": config, "error": None}

@app.post("/api/guardar-configuracion", dependencies=[Security(verificar_api_key)])
def guardar_configuracion(nueva_config: ConfiguracionHardware):
    global motor_activo, config_camaras
    with state_lock:
        config_camaras.update({"frontal": nueva_config.frontal, "lat_der": nueva_config.lat_der, "lat_izq": nueva_config.lat_izq})

    temp_file = ARCHIVO_CONFIG + '.tmp'
    with open(temp_file, 'w') as f: json.dump(config_camaras, f)
    os.replace(temp_file, ARCHIVO_CONFIG)

    reiniciado = False
    with state_lock:
        if motor_activo: motor_activo = False; reiniciado = True
    return {"status": "exito", "mensaje": "Reinicia motor para aplicar" if reiniciado else "Config guardada."}

if __name__ == "__main__":
    import uvicorn
    import multiprocessing
    # Vital para que PyInstaller pueda abrir sub-procesos en Windows sin crashear
    multiprocessing.freeze_support()
    
    # Pasamos 'app' de forma nativa en vez del string 'api-vision:app'
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")