import os
import cv2
import time
import json
import asyncio
import threading
import platform
import numpy as np
from typing import Dict, Optional, Tuple, List
from fastapi.responses import StreamingResponse

CONFIG_FILE_NAME = "config_camaras.json"

class AsyncCameraReader:
    def __init__(self, src: int):
        self.src = int(src)
        self.lock = threading.Lock()
        self.running = True
        self.frame: Optional[np.ndarray] = None
        self.connected = False
        self.frame_id = 0
        self.backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_V4L2
        self.cap: Optional[cv2.VideoCapture] = None

        self._conectar()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _conectar(self) -> bool:
        try:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None

            if platform.system() == "Linux":
                dev_node = f"/dev/video{self.src}"
                if not os.path.exists(dev_node):
                    with self.lock:
                        self.connected = False
                    return False
                try:
                    import subprocess
                    subprocess.run(["v4l2-ctl", "-d", dev_node, "--set-ctrl=exposure_dynamic_framerate=0"],
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
                        self.frame_id += 1
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
                        self.frame_id += 1
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

    def read_with_id(self) -> Tuple[bool, Optional[np.ndarray], int]:
        with self.lock:
            if self.frame is not None and self.connected:
                return True, self.frame.copy(), self.frame_id
            return False, None, 0

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

class CameraStreamManager:
    def __init__(self, config_path: Optional[str] = None):
        root_cfg = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), CONFIG_FILE_NAME))
        if config_path:
            self.config_path = config_path
        elif os.path.exists(root_cfg):
            self.config_path = root_cfg
        else:
            candidates = [
                root_cfg,
                os.path.join(os.getcwd(), CONFIG_FILE_NAME),
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CONFIG_FILE_NAME),
            ]
            self.config_path = root_cfg
            for cand in candidates:
                if os.path.exists(cand):
                    self.config_path = os.path.abspath(cand)
                    break
        self.root_cfg_path = root_cfg
        self.lock = threading.RLock()
        self.proc_lock = threading.Lock()
        self.motor_activo = True
        self.running = True
        self.frame_processor = None
        self.config_camaras: Dict[str, Optional[int]] = {"frontal": 0, "lat_der": None, "lat_izq": None}
        self.readers: Dict[str, Optional[AsyncCameraReader]] = {"frontal": None, "lat_der": None, "lat_izq": None}

        self.cached_jpegs: Dict[str, bytes] = {}
        self.frame_versions: Dict[str, int] = {"frontal": 0, "lat_der": 0, "lat_izq": 0}
        self.last_reader_frame_ids: Dict[str, int] = {"frontal": 0, "lat_der": 0, "lat_izq": 0}

        self._cargar_configuracion()

        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    def start(self):
        """Inicia los lectores de cámara sin interferir con reloaders de Uvicorn."""
        with self.lock:
            self.motor_activo = True
            self.running = True
            self._cargar_configuracion()
            self._sincronizar_readers()
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()

    def stop(self):
        """Detiene y libera todas las cámaras físicas."""
        with self.lock:
            self.motor_activo = False
            for k, reader in self.readers.items():
                if reader is not None:
                    reader.release()
                    self.readers[k] = None
        with self.proc_lock:
            self.cached_jpegs.clear()

    def set_frame_processor(self, processor_fn):
        """Asigna una función que recibe (frame: np.ndarray) y retorna (annotated_frame: np.ndarray)."""
        self.frame_processor = processor_fn
        if processor_fn is not None:
            try:
                import inspect
                sig = inspect.signature(processor_fn)
                self._processor_takes_cam_id = len(sig.parameters) >= 2
            except Exception:
                self._processor_takes_cam_id = False
        else:
            self._processor_takes_cam_id = False

    def _worker_loop(self):
        """Hilo en segundo plano dedicado a captura e inferencia IA desacoplada sin bloquear el stream web."""
        while self.running:
            if not self.motor_activo:
                time.sleep(0.04)
                continue

            active_readers = []
            with self.lock:
                for k, r in self.readers.items():
                    if r is not None and r.connected:
                        active_readers.append((k, r))

            if not active_readers:
                time.sleep(0.02)
                continue

            for cam_id, reader in active_readers:
                ret, frame, fid = reader.read_with_id()
                if not ret or frame is None:
                    continue

                if fid == self.last_reader_frame_ids.get(cam_id, -1):
                    continue
                self.last_reader_frame_ids[cam_id] = fid

                proc_frame = frame
                if self.frame_processor is not None:
                    try:
                        if getattr(self, '_processor_takes_cam_id', False):
                            res = self.frame_processor(frame, cam_id)
                        else:
                            res = self.frame_processor(frame)
                        if res is not None:
                            proc_frame = res
                    except Exception:
                        proc_frame = frame

                success, buf = cv2.imencode('.jpg', proc_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if success:
                    jpeg_bytes = buf.tobytes()
                    with self.proc_lock:
                        self.cached_jpegs[cam_id] = jpeg_bytes
                        self.frame_versions[cam_id] = self.frame_versions.get(cam_id, 0) + 1

            time.sleep(0.002)

    def _cargar_configuracion(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k in ["frontal", "lat_der", "lat_izq"]:
                        if k in data:
                            val = data[k]
                            self.config_camaras[k] = int(val) if val is not None and str(val).isdigit() else None
            except Exception:
                pass

    def _sincronizar_readers(self):
        with self.lock:
            for k in ["frontal", "lat_der", "lat_izq"]:
                port = self.config_camaras.get(k)
                current_reader = self.readers.get(k)
                if port is not None:
                    if current_reader is None or current_reader.src != port:
                        if current_reader is not None:
                            current_reader.release()
                        try:
                            self.readers[k] = AsyncCameraReader(port)
                        except Exception:
                            self.readers[k] = None
                else:
                    if current_reader is not None:
                        current_reader.release()
                        self.readers[k] = None

    def guardar_configuracion(self, nueva_config: dict) -> dict:
        with self.lock:
            for k in ["frontal", "lat_der", "lat_izq"]:
                if k in nueva_config:
                    val = nueva_config[k]
                    if val is None or val == "none" or val == "":
                        self.config_camaras[k] = None
                    else:
                        try:
                            self.config_camaras[k] = int(val)
                        except (ValueError, TypeError):
                            self.config_camaras[k] = None

            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config_camaras, f, indent=2)
            except Exception as e:
                print(f"[!] Error guardando config_camaras.json: {e}")

        self._sincronizar_readers()
        return {"status": "exito", "config_actual": self.config_camaras}

    def get_estado_camaras(self) -> dict:
        with self.lock:
            if self.motor_activo:
                self._sincronizar_readers()
            conectada = {}
            for k in ["frontal", "lat_der", "lat_izq"]:
                r = self.readers.get(k)
                conectada[k] = bool(r and r.connected)

            return {
                "motor_activo": self.motor_activo,
                "config_actual": dict(self.config_camaras),
                "conectada": conectada
            }

    def escanear_hardware(self) -> dict:
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

            with self.lock:
                en_uso = [int(v) for v in self.config_camaras.values() if v is not None and str(v).isdigit()]
            for p in en_uso:
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
            with self.lock:
                en_uso = [int(v) for v in self.config_camaras.values() if v is not None and str(v).isdigit()]
            puertos.update(en_uso)
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
            with self.lock:
                en_uso = [int(v) for v in self.config_camaras.values() if v is not None and str(v).isdigit()]
            puertos.update(en_uso)

        return {
            "puertos_disponibles": sorted(list(puertos)),
            "config_actual": dict(self.config_camaras),
            "error": None
        }

    def generar_frame_standby(self, cam_id: str) -> np.ndarray:
        """Genera un fotograma digital informativo si no hay cámara física conectada."""
        w, h = 640, 480
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:] = (18, 22, 28)

        for x in range(0, w, 40):
            cv2.line(img, (x, 0), (x, h), (26, 32, 40), 1)
        for y in range(0, h, 40):
            cv2.line(img, (0, y), (w, y), (26, 32, 40), 1)

        cx, cy = w // 2, h // 2
        cv2.circle(img, (cx, cy), 35, (0, 165, 255), 1)
        cv2.line(img, (cx - 50, cy), (cx + 50, cy), (0, 165, 255), 1)
        cv2.line(img, (cx, cy - 50), (cx, cy + 50), (0, 165, 255), 1)

        port = self.config_camaras.get(cam_id)
        port_txt = f"/dev/video{port}" if port is not None else "SIN PUERTO ASIGNADO"
        cv2.putText(img, f"CANAL: {cam_id.upper()}", (25, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(img, f"PUERTO: {port_txt}", (25, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 160, 180), 1)

        estado_txt = "MOTOR ACTIVO · ESPERANDO STREAM" if self.motor_activo else "MOTOR EN REPOSO"
        color_estado = (0, 220, 100) if self.motor_activo else (100, 120, 140)
        cv2.putText(img, estado_txt, (25, h - 35), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color_estado, 1)

        t_str = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, t_str, (w - 210, h - 35), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (140, 160, 180), 1)

        return img

    async def stream_generator(self, cam_id: str):
        last_version = -1
        standby_bytes = None
        while True:
            reader = self.readers.get(cam_id)
            if not self.motor_activo or reader is None or not reader.connected:
                if standby_bytes is None:
                    standby = self.generar_frame_standby(cam_id)
                    success, buf = cv2.imencode('.jpg', standby, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
                    if success:
                        standby_bytes = buf.tobytes()
                if standby_bytes:
                    hdr = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(standby_bytes)}\r\n\r\n".encode("ascii")
                    yield (hdr + standby_bytes + b"\r\n")
                await asyncio.sleep(0.2)
                continue
            else:
                standby_bytes = None

            jpeg_bytes = None
            cur_ver = -1
            with self.proc_lock:
                cur_ver = self.frame_versions.get(cam_id, 0)
                jpeg_bytes = self.cached_jpegs.get(cam_id)

            if jpeg_bytes is not None and cur_ver != last_version:
                last_version = cur_ver
                hdr = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg_bytes)}\r\n\r\n".encode("ascii")
                yield (hdr + jpeg_bytes + b"\r\n")
                await asyncio.sleep(0.001)
            else:
                await asyncio.sleep(0.002)

    def get_stream_response(self, cam_id: str) -> StreamingResponse:
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        return StreamingResponse(
            self.stream_generator(cam_id),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers=headers
        )

_stream_manager: Optional[CameraStreamManager] = None

def get_camera_stream_manager() -> CameraStreamManager:
    global _stream_manager
    if _stream_manager is None:
        _stream_manager = CameraStreamManager()
    return _stream_manager
