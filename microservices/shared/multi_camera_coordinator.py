import time
import threading
from typing import List, Dict, Any, Optional
import numpy as np

class TunnelPassageCoordinator:
    """
    Coordinador de Paso por Túnel y Deduplicador Multi-Cámara.
    
    1. Agrupa las observaciones de todas las cámaras activas (frontal, lat_der, lat_izq)
       durante el tránsito de una pieza física a través de la estación.
    2. Evita registros duplicados: un paso físico = un único registro consolidado en BD/Django.
    3. Suprime la emisión de eventos espurios cuando la banda está vacía (cero piezas).
    4. Aplica una ventana de cooldown tras confirmar el evento para no re-disparar mientras
       la pieza abandona el túnel.
    """
    def __init__(self, cooldown_segundos: float = 1.8, tiempo_cierre_paso: float = 0.45):
        self.cooldown_segundos = cooldown_segundos
        self.tiempo_cierre_paso = tiempo_cierre_paso
        self.lock = threading.Lock()
        
        self.sesion_activa = False
        self.t_inicio = 0.0
        self.t_ultimo_avistamiento = 0.0
        self.t_ultimo_compromiso = 0.0
        self.ultimo_lote_comprometido = None
        
        self.qr_detectado = "Sin_QR"
        self.defectos_detectados: List[Dict[str, Any]] = []
        self.mejor_defecto: Optional[Dict[str, Any]] = None
        self.camara_con_falla: Optional[str] = None
        self.img_limpia_evidencia: Optional[np.ndarray] = None
        self.img_vis_evidencia: Optional[np.ndarray] = None
        self.conteo_frames_pieza = 0

    def registrar_observacion(
        self,
        t_now: float,
        hay_presencia: bool,
        qr_encontrado: Optional[str],
        defectos_cam: List[Dict[str, Any]],
        cam_nombre: str,
        frame_limpio: np.ndarray,
        frame_vis: np.ndarray
    ):
        with self.lock:
            if (t_now - self.t_ultimo_compromiso) < self.cooldown_segundos:
                if qr_encontrado and qr_encontrado != "Sin_QR" and qr_encontrado == self.ultimo_lote_comprometido:
                    return

            if hay_presencia:
                if not self.sesion_activa:
                    self.sesion_activa = True
                    self.t_inicio = t_now
                    self.t_ultimo_avistamiento = t_now
                    self.qr_detectado = qr_encontrado or "Sin_QR"
                    self.defectos_detectados.clear()
                    self.mejor_defecto = None
                    self.camara_con_falla = None
                    self.img_limpia_evidencia = frame_limpio.copy() if frame_limpio is not None else None
                    self.img_vis_evidencia = frame_vis.copy() if frame_vis is not None else None
                    self.conteo_frames_pieza = 1
                else:
                    self.t_ultimo_avistamiento = t_now
                    self.conteo_frames_pieza += 1
                    if qr_encontrado and qr_encontrado != "Sin_QR" and self.qr_detectado == "Sin_QR":
                        self.qr_detectado = qr_encontrado

                if defectos_cam:
                    self.defectos_detectados.extend(defectos_cam)
                    top_defect = max(defectos_cam, key=lambda d: float(d.get("confidence", 0.0)))
                    if self.mejor_defecto is None or float(top_defect.get("confidence", 0.0)) > float(self.mejor_defecto.get("confidence", 0.0)):
                        self.mejor_defecto = top_defect
                        self.camara_con_falla = cam_nombre
                        self.img_limpia_evidencia = frame_limpio.copy() if frame_limpio is not None else None
                        self.img_vis_evidencia = frame_vis.copy() if frame_vis is not None else None

    def evaluar_cierre_sesion(self, t_now: float) -> Optional[Dict[str, Any]]:
        with self.lock:
            if not self.sesion_activa:
                return None

            tiempo_desde_ultimo = t_now - self.t_ultimo_avistamiento
            duracion_total = t_now - self.t_inicio

            debe_cerrar = (
                (tiempo_desde_ultimo >= self.tiempo_cierre_paso and self.conteo_frames_pieza >= 2) or
                (duracion_total >= 1.4 and self.conteo_frames_pieza >= 3)
            )

            if not debe_cerrar:
                return None

            hay_defecto = (self.mejor_defecto is not None) or (len(self.defectos_detectados) > 0)
            if hay_defecto:
                estado = "Rechazado"
                clase_def = self.mejor_defecto["class_name"] if self.mejor_defecto else "Rechazado_Inspeccion"
                conf = float(self.mejor_defecto["confidence"]) if self.mejor_defecto else 1.0
                falla = f"{clase_def}_{self.camara_con_falla}" if self.camara_con_falla else clase_def
            else:
                estado = "Aprobado"
                falla = "Pieza_Conforme"
                conf = 1.0

            evento = {
                "lote": self.qr_detectado,
                "estado": estado,
                "falla": falla,
                "confianza": conf,
                "img_limpia": self.img_limpia_evidencia,
                "img_vis": self.img_vis_evidencia
            }

            self.sesion_activa = False
            self.t_ultimo_compromiso = t_now
            self.ultimo_lote_comprometido = self.qr_detectado

            return evento

    def reset(self):
        with self.lock:
            self.sesion_activa = False
            self.conteo_frames_pieza = 0
            self.defectos_detectados.clear()
            self.mejor_defecto = None
            self.camara_con_falla = None
            self.img_limpia_evidencia = None
            self.img_vis_evidencia = None
