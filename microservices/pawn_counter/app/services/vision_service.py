import os
import time
import base64
import threading
import cv2
import numpy as np
from typing import List, Dict, Tuple, Optional, Set, Any
from ultralytics import YOLO
import supervision as sv

from app.core.config import settings
from app.models.schemas import (
    DetectionItem,
    DirectionCounts,
    PieceProgress,
    BatchStatus,
    LineZoneMetrics,
    FrameProcessResponse,
    VideoProcessResponse
)

try:
    from microservices.shared.offline_sync import get_offline_sync_manager
    from microservices.shared.sound_effects import emitir_pitido
except ImportError:
    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.offline_sync import get_offline_sync_manager
    from microservices.shared.sound_effects import emitir_pitido

def normalize_class_name(name: str) -> str:
    """Estandariza nombres de clase para coincidir con selecciones del frontend."""
    return name.strip().lower().replace(" ", "_").replace("-", "_")

class PawnVisionService:
    """
    Servicio de Visión Artificial para Detección, Seguimiento y Conteo de Piezas de Ajedrez.
    Incluye:
      - YOLOv8 (detección / segmentación)
      - ByteTrack con persistencia de track_id
      - Supervision LineZone para cruce geométrico
      - Limitador de piezas con alertas automáticas (exceso de piezas o piezas no deseadas)
      - Prevención estricta de doble conteo
    """
    def __init__(self, model: Optional[Any] = None):
        self.lock = threading.RLock()
        self.model_lock = threading.Lock()

        import torch
        if torch.cuda.is_available() and not os.getenv("FORCE_CPU"):
            self.device = "0"
            print("[+] Hardware Acceleration: GPU NVIDIA detectada (CUDA:0). Máxima fluidez activada.")
        else:
            self.device = settings.DEVICE
            print(f"[*] Hardware Acceleration: Usando dispositivo {self.device}.")
        
        if model is not None:
            self.model = model
            print("[+] Modelo inyectado en memoria para PawnVisionService.")
        else:
            if not os.path.exists(settings.MODEL_PATH):
                raise FileNotFoundError(
                    f"Pesos de modelo no encontrados en: {settings.MODEL_PATH}. "
                    f"Por favor verifica la variable MODEL_PATH en el archivo .env"
                )

            print(f"[*] Cargando modelo YOLOv8 ({settings.MODEL_TYPE}) desde: {settings.MODEL_PATH}")
            self.model = YOLO(settings.MODEL_PATH)
            
            dummy = np.zeros((384, 384, 3), dtype=np.uint8)
            self.model.predict(dummy, device=self.device, verbose=False)
            print(f"[+] Modelo cargado y optimizado en memoria sobre dispositivo '{self.device}'.")

        self.box_annotator = sv.BoxAnnotator(thickness=2)
        self.mask_annotator = sv.MaskAnnotator()
        self.label_annotator = sv.LabelAnnotator(text_scale=0.5, text_thickness=1)
        self.line_zone_annotator = sv.LineZoneAnnotator(thickness=2, text_thickness=1, text_scale=0.6)

        self.sync_manager = get_offline_sync_manager()

        self.line_config_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config_linea_ajedrez.json"
        )
        self.line_orientation: str = "horizontal"
        self.line_position: float = 0.50
        self.sound_enabled: bool = True
        self.last_crossing_event: Optional[Dict[str, Any]] = None
        self._load_line_config()
        self._update_line_zone_annotator()

        self.line_zone: Optional[sv.LineZone] = None
        self.last_dimensions: Tuple[int, int] = (0, 0)

        self.counted_in_ids: Set[int] = set()
        self.counted_out_ids: Set[int] = set()
        
        self.counts_by_class: Dict[str, Dict[str, int]] = {}

        self.batch_limits: Dict[str, int] = {}
        self.strict_mode: bool = True
        self.error_messages: List[str] = []

        self.tracker_config_path = settings.TRACKER_CONFIG_PATH
        self.confidence_threshold = settings.CONFIDENCE_THRESHOLD
        self.iou_threshold = settings.IOU_THRESHOLD
        self.device = settings.DEVICE
        self.target_classes = getattr(settings, "TARGET_CLASSES", None)

        self.cam_stabilizers: Dict[str, Any] = {}
        self.cam_frame_counters: Dict[str, int] = {}
        self.cam_last_detections: Dict[str, Any] = {}

        self.max_aspect_ratio = getattr(settings, "MAX_ASPECT_RATIO", 0.76)
        self.min_aspect_ratio = getattr(settings, "MIN_ASPECT_RATIO", 0.15)
        self.min_rel_area = getattr(settings, "MIN_REL_AREA", 0.001)
        self.max_rel_area = getattr(settings, "MAX_REL_AREA", 0.15)
        self.max_white_saturation = getattr(settings, "MAX_WHITE_SATURATION", 115.0)
        self.max_black_value = getattr(settings, "MAX_BLACK_VALUE", 130.0)
        try:
            from microservices.shared.bbox_stabilizer import BBoxStabilizer
            self.stabilizer = BBoxStabilizer(alpha=0.55, max_missing_time=0.55, max_missing_frames=8, high_threshold=0.45, low_threshold=0.28)
            self.cam_stabilizers["frontal"] = self.stabilizer
        except Exception:
            self.stabilizer = None

    def _get_cam_stabilizer(self, cam_id: str) -> Optional[Any]:
        if cam_id not in self.cam_stabilizers:
            try:
                from microservices.shared.bbox_stabilizer import BBoxStabilizer
                self.cam_stabilizers[cam_id] = BBoxStabilizer(
                    alpha=0.55,
                    max_missing_time=0.55,
                    max_missing_frames=8,
                    high_threshold=0.45,
                    low_threshold=0.28
                )
            except Exception:
                self.cam_stabilizers[cam_id] = None
        return self.cam_stabilizers[cam_id]

    def set_batch_limits(self, limits: Dict[str, int], strict_mode: bool = True) -> BatchStatus:
        """Establece los límites deseados para cada tipo de pieza en el lote."""
        with self.lock:
            self.batch_limits = {normalize_class_name(k): int(v) for k, v in limits.items() if int(v) > 0}
            self.strict_mode = strict_mode
            self.error_messages.clear()
            return self._calculate_batch_status()

    def get_batch_limits(self) -> Dict[str, int]:
        with self.lock:
            return dict(self.batch_limits)

    def _load_line_config(self):
        """Carga la orientación, posición y sonido de la línea virtual desde disco si existe."""
        try:
            if os.path.exists(self.line_config_file):
                import json
                with open(self.line_config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    ori = str(cfg.get("orientation", "horizontal")).strip().lower()
                    self.line_orientation = ori if ori in ("horizontal", "vertical") else "horizontal"
                    pos = float(cfg.get("position", 0.50))
                    self.line_position = max(0.05, min(0.95, pos))
                    self.sound_enabled = bool(cfg.get("sound_enabled", True))
        except Exception as e:
            print(f"[!] Advertencia leyendo config_linea_ajedrez.json: {e}")

    def _save_line_config(self):
        """Persiste la orientación, posición y sonido en disco."""
        try:
            import json
            cfg = {
                "orientation": self.line_orientation,
                "position": self.line_position,
                "sound_enabled": self.sound_enabled
            }
            with open(self.line_config_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"[!] Error guardando config_linea_ajedrez.json: {e}")

    def get_line_config(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "orientation": self.line_orientation,
                "position": round(self.line_position, 2),
                "sound_enabled": self.sound_enabled
            }

    def set_line_config(
        self,
        orientation: Optional[str] = None,
        position: Optional[float] = None,
        sound_enabled: Optional[bool] = None
    ) -> Dict[str, Any]:
        with self.lock:
            cambio_geometria = False
            if orientation is not None:
                nueva_ori = orientation.strip().lower()
                if nueva_ori in ("horizontal", "vertical") and nueva_ori != self.line_orientation:
                    self.line_orientation = nueva_ori
                    cambio_geometria = True
            if position is not None:
                pos = max(0.05, min(0.95, float(position)))
                if pos != self.line_position:
                    self.line_position = pos
                    cambio_geometria = True
            if sound_enabled is not None:
                self.sound_enabled = bool(sound_enabled)

            if cambio_geometria:
                self.line_zone = None
                self.last_dimensions = (0, 0)
                self._update_line_zone_annotator()

            self._save_line_config()
            return self.get_line_config()

    def _update_line_zone_annotator(self):
        """Configura el rotulado visual de la línea con sentido de paso unidireccional."""
        if self.line_orientation == "vertical":
            self.line_zone_annotator = sv.LineZoneAnnotator(
                thickness=2,
                text_thickness=1,
                text_scale=0.55,
                custom_in_text="PASO (IZQ -> DER)",
                display_out_count=False
            )
        else:
            self.line_zone_annotator = sv.LineZoneAnnotator(
                thickness=2,
                text_thickness=1,
                text_scale=0.55,
                custom_in_text="PASO (ARR -> ABA)",
                display_out_count=False
            )

    def _get_or_create_line_zone(self, width: int, height: int) -> sv.LineZone:
        """Inicializa o actualiza la línea virtual según dimensiones y orientación (horizontal vs vertical)."""
        if self.line_zone is None or self.last_dimensions != (width, height):
            pos = self.line_position
            if self.line_orientation == "vertical":
                x_val = int(width * pos)
                start = sv.Point(x_val, int(height * 0.05))
                end = sv.Point(x_val, int(height * 0.95))
            else:
                y_val = int(height * pos)
                start = sv.Point(int(width * 0.05), y_val)
                end = sv.Point(int(width * 0.95), y_val)

            self.line_zone = sv.LineZone(start=start, end=end)
            self._update_line_zone_annotator()
            self.last_dimensions = (width, height)
        return self.line_zone

    def reset_state(self) -> LineZoneMetrics:
        """Reinicia acumuladores, alertas y registros de IDs para un nuevo lote."""
        with self.lock:
            self.counted_in_ids.clear()
            self.counted_out_ids.clear()
            self.counts_by_class.clear()
            self.error_messages.clear()
            self.line_zone = None
            self.last_dimensions = (0, 0)
            self.last_crossing_event = None
            return self.get_metrics()

    def _calculate_batch_status(self) -> BatchStatus:
        """Evalúa las reglas del lote: completado, exceso de piezas o piezas no permitidas."""
        progress: Dict[str, PieceProgress] = {}
        all_completed = True if self.batch_limits else False
        has_limit_errors = False

        for cls_name, target in self.batch_limits.items():
            dir_data = self.counts_by_class.get(cls_name, {"in": 0, "out": 0})
            curr = dir_data.get("in", 0) if self.line_orientation == "vertical" else (dir_data.get("in", 0) + dir_data.get("out", 0))
            rem = max(0, target - curr)

            if curr == target:
                p_status = "COMPLETADO"
            elif curr > target:
                p_status = "EXCEDIDO"
                has_limit_errors = True
                all_completed = False
            else:
                p_status = "PENDIENTE"
                all_completed = False

            progress[cls_name] = PieceProgress(
                class_name=cls_name,
                target_count=target,
                current_count=curr,
                remaining=rem,
                status=p_status
            )

        if len(self.error_messages) > 0 or has_limit_errors:
            general_status = "ERROR"
            has_errors = True
        elif all_completed:
            general_status = "COMPLETADO"
            has_errors = False
        else:
            general_status = "EN_PROCESO"
            has_errors = False

        return BatchStatus(
            status=general_status,
            has_errors=has_errors,
            error_messages=list(self.error_messages),
            progress=progress
        )

    def get_metrics(self) -> LineZoneMetrics:
        """Retorna el estado actual de conteo consolidado con límites."""
        with self.lock:
            in_total = len(self.counted_in_ids)
            out_total = len(self.counted_out_ids)
            
            by_class: Dict[str, DirectionCounts] = {}
            for cls_name, directions in self.counts_by_class.items():
                c_in = directions.get("in", 0)
                c_out = directions.get("out", 0)
                c_tot = c_in if self.line_orientation == "vertical" else (c_in + c_out)
                by_class[cls_name] = DirectionCounts(
                    in_count=c_in,
                    out_count=c_out,
                    total=c_tot
                )

            batch_status = self._calculate_batch_status()
            total_global = in_total if self.line_orientation == "vertical" else (in_total + out_total)

            return LineZoneMetrics(
                in_count=in_total,
                out_count=out_total,
                total=total_global,
                counts_by_class=by_class,
                batch=batch_status
            )

    def _filter_hallucinations(self, frame: np.ndarray, detections: sv.Detections) -> sv.Detections:
        """
        Filtro físico y cromático anti-alucinaciones para piezas de ajedrez.
        Rechaza detecciones espurias (bolsas de frituras/papas, ropa, paquetes, fondos o ruido)
        evaluando:
        1. Relación de aspecto (Aspect Ratio = ancho / alto): las piezas de ajedrez son esbeltas y verticales.
        2. Escala y área relativa al fotograma.
        3. Límites cromáticos en HSV: piezas blancas no pueden tener alta saturación (ej. bolsas amarillas/doradas),
           y piezas negras no pueden tener brillo excesivo.
        """
        if len(detections) == 0:
            return detections

        h_img, w_img = frame.shape[:2]
        valid_indices = []

        for idx in range(len(detections)):
            cid = int(detections.class_id[idx]) if detections.class_id is not None else 0
            cname = normalize_class_name(self.model.names.get(cid, str(cid))).lower()
            box = detections.xyxy[idx]
            x1, y1 = max(0, int(round(box[0]))), max(0, int(round(box[1])))
            x2, y2 = min(w_img, int(round(box[2]))), min(h_img, int(round(box[3])))
            bw = x2 - x1
            bh = y2 - y1

            if bw <= 10 or bh <= 15:
                continue

            ar = bw / float(bh)
            if ar > self.max_aspect_ratio or ar < self.min_aspect_ratio:
                continue

            rel_area = (bw * bh) / float(w_img * h_img)
            if rel_area > self.max_rel_area or rel_area < self.min_rel_area:
                continue

            crop = frame[y1:y2, x1:x2]
            if crop.size > 0:
                hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
                sat = float(np.mean(hsv[:, :, 1]))
                val = float(np.mean(hsv[:, :, 2]))

                if "blanco" in cname and sat > self.max_white_saturation:
                    continue
                elif "negro" in cname and val > self.max_black_value:
                    continue

            valid_indices.append(idx)

        if len(valid_indices) == len(detections):
            return detections
        elif len(valid_indices) == 0:
            return sv.Detections.empty()
        else:
            mask = np.zeros(len(detections), dtype=bool)
            mask[valid_indices] = True
            return detections[mask]

    def process_frame(self, frame: np.ndarray, return_annotated: bool = False) -> FrameProcessResponse:
        """
        Ejecuta Tracking de piezas de ajedrez, valida el cruce de la línea virtual
        y aplica la regla del limitador de lote sin duplicar cuentas.
        """
        t0 = time.perf_counter()
        h, w = frame.shape[:2]

        with self.lock:
            line_zone = self._get_or_create_line_zone(w, h)

            results = self.model.track(
                source=frame,
                persist=True,
                tracker=settings.TRACKER_CONFIG_PATH,
                conf=settings.CONFIDENCE_THRESHOLD,
                iou=settings.IOU_THRESHOLD,
                device=settings.DEVICE,
                verbose=False
            )[0]

            detections = sv.Detections.from_ultralytics(results)
            detections = self._filter_hallucinations(frame, detections)

            newly_counted_ids: List[int] = []
            detection_items: List[DetectionItem] = []

            annotated_frame = frame.copy()
            annotated_b64 = None
            has_segmentation = False
            bbox_txt_content = ""

            if len(detections) > 0 and detections.tracker_id is not None:
                crossed_in, crossed_out = line_zone.trigger(detections=detections)

                if self.mask_annotator and detections.mask is not None and len(detections.mask) > 0:
                    annotated_frame = self.mask_annotator.annotate(scene=annotated_frame, detections=detections)
                annotated_frame = self.box_annotator.annotate(scene=annotated_frame, detections=detections)

                labels = []
                for idx_l in range(len(detections)):
                    tid = detections.tracker_id[idx_l] if detections.tracker_id is not None else "N/A"
                    cid = detections.class_id[idx_l] if detections.class_id is not None else 0
                    cname = normalize_class_name(self.model.names.get(cid, str(cid)))
                    labels.append(f"#{tid} {cname}")

                annotated_frame = self.label_annotator.annotate(scene=annotated_frame, detections=detections, labels=labels)
                annotated_frame = self.line_zone_annotator.annotate(frame=annotated_frame, line_counter=line_zone)

                has_segmentation = (
                    results.masks is not None 
                    and hasattr(results.masks, 'xyn') 
                    and len(results.masks.xyn) > 0
                )
                yolo_txt_lines = []
                for idx_txt in range(len(detections)):
                    cid = int(detections.class_id[idx_txt]) if detections.class_id is not None else 0
                    if has_segmentation and len(results.masks.xyn) > idx_txt:
                        poly = results.masks.xyn[idx_txt]
                        if len(poly) >= 3:
                            pts_str = " ".join(f"{float(pt[0]):.6f} {float(pt[1]):.6f}" for pt in poly)
                            yolo_txt_lines.append(f"{cid} {pts_str}")
                        else:
                            box = detections.xyxy[idx_txt]
                            x_c = ((box[0] + box[2]) / 2.0) / w
                            y_c = ((box[1] + box[3]) / 2.0) / h
                            bw = (box[2] - box[0]) / w
                            bh = (box[3] - box[1]) / h
                            yolo_txt_lines.append(f"{cid} {x_c:.6f} {y_c:.6f} {bw:.6f} {bh:.6f}")
                    else:
                        box = detections.xyxy[idx_txt]
                        x_c = ((box[0] + box[2]) / 2.0) / w
                        y_c = ((box[1] + box[3]) / 2.0) / h
                        bw = (box[2] - box[0]) / w
                        bh = (box[3] - box[1]) / h
                        yolo_txt_lines.append(f"{cid} {x_c:.6f} {y_c:.6f} {bw:.6f} {bh:.6f}")

                bbox_txt_content = "\n".join(yolo_txt_lines)

                for idx in range(len(detections)):
                    track_id = int(detections.tracker_id[idx])
                    cls_id = int(detections.class_id[idx]) if detections.class_id is not None else 0
                    raw_class = self.model.names.get(cls_id, f"clase_{cls_id}")
                    class_name = normalize_class_name(raw_class)
                    conf = float(detections.confidence[idx]) if detections.confidence is not None else 0.0
                    box = [round(float(c), 2) for c in detections.xyxy[idx].tolist()]
                    has_mask = (detections.mask is not None and len(detections.mask) > idx)

                    detection_items.append(DetectionItem(
                        track_id=track_id,
                        class_name=class_name,
                        confidence=round(conf, 4),
                        bbox=box,
                        has_mask=has_mask
                    ))

                    just_counted = False
                    
                    if self.line_orientation == "vertical":
                        if crossed_in[idx] and (track_id not in self.counted_in_ids):
                            self.counted_in_ids.add(track_id)
                            just_counted = True
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["in"] += 1

                        if crossed_out[idx] and (track_id not in self.counted_out_ids):
                            self.counted_out_ids.add(track_id)
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["out"] += 1
                    else:
                        if crossed_in[idx] and (track_id not in self.counted_in_ids):
                            self.counted_in_ids.add(track_id)
                            just_counted = True
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["in"] += 1

                        if crossed_out[idx] and (track_id not in self.counted_out_ids):
                            self.counted_out_ids.add(track_id)
                            just_counted = True
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["out"] += 1

                    if just_counted:
                        newly_counted_ids.append(track_id)
                        
                        item_estado = "Aprobado"
                        item_falla = class_name

                        if self.batch_limits:
                            curr_total = self.counts_by_class[class_name]["in"] if self.line_orientation == "vertical" else (self.counts_by_class[class_name]["in"] + self.counts_by_class[class_name]["out"])

                            if class_name not in self.batch_limits and self.strict_mode:
                                err = f"¡PIEZA NO PERMITIDA!: Se detectó '{class_name}' (ID: #{track_id}) que no pertenece al lote configurado."
                                if err not in self.error_messages:
                                    self.error_messages.append(err)
                                item_estado = "Rechazado"
                                item_falla = f"pieza_no_permitida_{class_name}"

                            elif class_name in self.batch_limits:
                                target = self.batch_limits[class_name]
                                if curr_total > target:
                                    err = f"¡LÍMITE EXCEDIDO!: Se configuraron máximo {target} de '{class_name}', pero han pasado {curr_total} (ID: #{track_id})."
                                    if err not in self.error_messages:
                                        self.error_messages.append(err)
                                item_estado = "Rechazado"
                                item_falla = f"limite_excedido_{class_name}"

                        try:
                            lote_nombre = getattr(self, 'batch_id', None) or "LOTE-AJEDREZ-01"
                            self.sync_manager.registrar_inspeccion(
                                lote=lote_nombre,
                                estado=item_estado,
                                falla=item_falla,
                                confianza=conf,
                                imagen_frame=frame.copy(),
                                imagen_vis=annotated_frame.copy(),
                                bbox_txt=bbox_txt_content,
                                extra_meta={
                                    "track_id": track_id,
                                    "class_name": class_name,
                                    "batch_id": lote_nombre,
                                    "is_segmentation": has_segmentation
                                }
                            )
                        except Exception as ex_sync:
                            print(f"[-] Error guardando inspección offline en ajedrez: {ex_sync}")

                        if self.sound_enabled:
                            try:
                                emitir_pitido("rechazado" if item_estado == "Rechazado" else "cruce")
                            except Exception as ex_beep:
                                print(f"[-] Error emitiendo pitido: {ex_beep}")

                        self.last_crossing_event = {
                            "timestamp": time.time(),
                            "track_id": track_id,
                            "class_name": class_name,
                            "estado": item_estado,
                            "falla": item_falla,
                            "curr_total": self.counts_by_class[class_name]["in"] + self.counts_by_class[class_name]["out"],
                            "orientation": self.line_orientation
                        }

            if return_annotated:
                _, buffer = cv2.imencode(".jpg", annotated_frame)
                annotated_b64 = base64.b64encode(buffer).decode("utf-8")

        t1 = time.perf_counter()
        elapsed_ms = round((t1 - t0) * 1000, 2)

        metrics = self.get_metrics()
        active_count = len(detections.tracker_id) if (len(detections) > 0 and detections.tracker_id is not None) else 0

        return FrameProcessResponse(
            metrics=metrics,
            active_tracks=active_count,
            detections=detection_items,
            newly_counted_ids=newly_counted_ids,
            inference_time_ms=elapsed_ms,
            annotated_image_base64=annotated_b64
        )

    def process_video_file(self, video_path: str) -> VideoProcessResponse:
        """Procesa una secuencia de video completa y consolida las métricas."""
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"No se pudo abrir el archivo de video: {video_path}")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = total_frames / fps if fps > 0 else 0.0

        t0 = time.perf_counter()
        processed_frames = 0

        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            self.process_frame(frame, return_annotated=False)
            processed_frames += 1

        cap.release()
        elapsed_sec = time.perf_counter() - t0
        avg_fps = round(processed_frames / elapsed_sec, 2) if elapsed_sec > 0 else 0.0

        metrics = self.get_metrics()

        return VideoProcessResponse(
            total_frames=processed_frames,
            duration_seconds=round(duration, 2),
            fps=avg_fps,
            metrics=metrics,
            processing_time_ms=round(elapsed_sec * 1000, 2)
        )

    def process_camera_frame(self, frame: np.ndarray, cam_id: Optional[str] = None) -> np.ndarray:
        """Inferencia y anotación en tiempo real para el streaming MJPEG con cadencia optimizada, aislamiento multi-cámara y anti-parpadeo."""
        cam_channel = (cam_id or "frontal").lower()
        is_counting_cam = (cam_channel == "frontal")

        h, w = frame.shape[:2]
        line_zone = self._get_or_create_line_zone(w, h)

        now = time.time()
        dt = now - getattr(self, '_last_cam_time', now)
        self._last_cam_time = now
        if dt > 0:
            cur_fps = 1.0 / dt
            self._cam_fps = 0.90 * getattr(self, '_cam_fps', cur_fps) + 0.10 * cur_fps

        cadence = getattr(settings, "INFERENCE_CADENCE", 1)
        c_count = self.cam_frame_counters.get(cam_channel, 0) + 1
        self.cam_frame_counters[cam_channel] = c_count
        should_infer = (c_count % cadence == 0) or (cam_channel not in self.cam_last_detections)

        img_sz = getattr(settings, "IMG_SIZE", 384)

        if should_infer:
            with self.model_lock:
                if is_counting_cam:
                    results = self.model.track(
                        source=frame,
                        persist=True,
                        tracker=self.tracker_config_path,
                        conf=self.confidence_threshold,
                        iou=self.iou_threshold,
                        device=self.device,
                        imgsz=img_sz,
                        verbose=False
                    )[0]
                else:
                    results = self.model.predict(
                        source=frame,
                        conf=self.confidence_threshold,
                        iou=self.iou_threshold,
                        device=self.device,
                        imgsz=img_sz,
                        verbose=False
                    )[0]

            detections = sv.Detections.from_ultralytics(results)
            if self.target_classes and len(detections) > 0:
                mask = np.array([
                    normalize_class_name(self.model.names.get(int(cid), str(cid))) in self.target_classes
                    for cid in detections.class_id
                ])
                detections = detections[mask]

            detections = self._filter_hallucinations(frame, detections)
            self.cam_last_detections[cam_channel] = detections
        else:
            detections = self.cam_last_detections.get(cam_channel, sv.Detections.empty())

        annotated_frame = frame.copy()

        if is_counting_cam and len(detections) > 0 and detections.tracker_id is not None:
            with self.lock:
                crossed_in, crossed_out = line_zone.trigger(detections=detections)
                for idx in range(len(detections)):
                    track_id = int(detections.tracker_id[idx])
                    cls_id = int(detections.class_id[idx]) if detections.class_id is not None else 0
                    raw_class = self.model.names.get(cls_id, f"clase_{cls_id}")
                    class_name = normalize_class_name(raw_class)
                    conf = float(detections.confidence[idx]) if detections.confidence is not None else 1.0

                    is_new_crossing = False
                    if self.line_orientation == "vertical":
                        if crossed_in[idx] and (track_id not in self.counted_in_ids):
                            self.counted_in_ids.add(track_id)
                            is_new_crossing = True
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["in"] += 1

                        if crossed_out[idx] and (track_id not in self.counted_out_ids):
                            self.counted_out_ids.add(track_id)
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["out"] += 1
                    else:
                        if crossed_in[idx] and (track_id not in self.counted_in_ids):
                            self.counted_in_ids.add(track_id)
                            is_new_crossing = True
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["in"] += 1

                        if crossed_out[idx] and (track_id not in self.counted_out_ids):
                            self.counted_out_ids.add(track_id)
                            is_new_crossing = True
                            if class_name not in self.counts_by_class:
                                self.counts_by_class[class_name] = {"in": 0, "out": 0}
                            self.counts_by_class[class_name]["out"] += 1

                    if is_new_crossing:
                        curr_total = self.counts_by_class[class_name]["in"] if self.line_orientation == "vertical" else (self.counts_by_class[class_name]["in"] + self.counts_by_class[class_name]["out"])
                        item_estado = "Aprobado"
                        item_falla = f"{class_name}_ok"

                        if self.batch_limits:
                            if class_name not in self.batch_limits or self.batch_limits.get(class_name, 0) <= 0:
                                item_estado = "Rechazado"
                                item_falla = f"pieza_no_permitida_{class_name}"
                                err = f"¡PIEZA NO PERMITIDA!: Se detectó '{class_name}' (ID: #{track_id}) que no pertenece a las piezas autorizadas."
                                if err not in self.error_messages:
                                    self.error_messages.append(err)

                            else:
                                target = self.batch_limits[class_name]
                                if curr_total > target:
                                    item_estado = "Rechazado"
                                    item_falla = f"limite_excedido_{class_name}"
                                    err = f"¡LÍMITE EXCEDIDO!: '{class_name}' superó el máximo de {target} (Pasaron: {curr_total}, ID: #{track_id})."
                                    if err not in self.error_messages:
                                        self.error_messages.append(err)
                                else:
                                    item_estado = "Aprobado"
                                    item_falla = f"{class_name}_ok"

                        try:
                            lote_nombre = getattr(self, 'batch_id', None) or "LOTE-AJEDREZ"
                            vis_evidencia = frame.copy()
                            if detections.xyxy is not None and len(detections.xyxy) > idx:
                                b = detections.xyxy[idx]
                                bx1 = max(0, min(w - 1, int(round(b[0]))))
                                by1 = max(0, min(h - 1, int(round(b[1]))))
                                bx2 = max(0, min(w - 1, int(round(b[2]))))
                                by2 = max(0, min(h - 1, int(round(b[3]))))
                                box_col = (0, 0, 255) if item_estado == "Rechazado" else (0, 220, 100)
                                cv2.rectangle(vis_evidencia, (bx1, by1), (bx2, by2), box_col, 3)
                                estado_lbl = f"#{track_id} {item_falla.upper()}"
                                (lw, lh), _ = cv2.getTextSize(estado_lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
                                cv2.rectangle(vis_evidencia, (bx1, max(0, by1 - lh - 8)), (bx1 + lw + 8, by1), box_col, -1)
                                cv2.putText(vis_evidencia, estado_lbl, (bx1 + 4, max(14, by1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

                            self.sync_manager.registrar_inspeccion(
                                lote=lote_nombre,
                                estado=item_estado,
                                falla=item_falla,
                                confianza=conf,
                                imagen_frame=frame.copy(),
                                imagen_vis=vis_evidencia,
                                extra_meta={
                                    "track_id": track_id,
                                    "class_name": class_name,
                                    "batch_id": lote_nombre,
                                    "curr_total": curr_total,
                                    "batch_limits": dict(self.batch_limits),
                                    "cam_channel": cam_channel,
                                    "direccion": "IZQ_A_DER" if self.line_orientation == "vertical" else "ARR_A_ABA"
                                }
                            )
                        except Exception as ex_sync:
                            print(f"[-] Error registrando inspección en Resultados: {ex_sync}")

                        if self.sound_enabled:
                            sonido_tipo = "rechazado" if item_estado == "Rechazado" else "cruce"
                            try:
                                emitir_pitido(sonido_tipo)
                            except Exception as ex_beep:
                                print(f"[-] Error emitiendo pitido en cámara: {ex_beep}")

                        self.last_crossing_event = {
                            "timestamp": time.time(),
                            "track_id": track_id,
                            "class_name": class_name,
                            "estado": item_estado,
                            "falla": item_falla,
                            "curr_total": curr_total,
                            "orientation": self.line_orientation,
                            "direccion": "IZQ -> DER" if self.line_orientation == "vertical" else "ARR -> ABA"
                        }

        if len(detections) > 0:
            if self.mask_annotator and detections.mask is not None and len(detections.mask) > 0:
                try:
                    annotated_frame = self.mask_annotator.annotate(scene=annotated_frame, detections=detections)
                except Exception:
                    pass

            for idx in range(len(detections)):
                b = detections.xyxy[idx]
                x1 = max(0, min(w - 1, int(round(b[0]))))
                y1 = max(0, min(h - 1, int(round(b[1]))))
                x2 = max(0, min(w - 1, int(round(b[2]))))
                y2 = max(0, min(h - 1, int(round(b[3]))))
                if x2 <= x1 or y2 <= y1:
                    continue

                cid = int(detections.class_id[idx]) if detections.class_id is not None else 0
                cname = normalize_class_name(self.model.names.get(cid, str(cid)))
                tid = int(detections.tracker_id[idx]) if (detections.tracker_id is not None and len(detections.tracker_id) > idx and detections.tracker_id[idx] is not None) else idx + 1

                if self.batch_limits:
                    if cname not in self.batch_limits or self.batch_limits.get(cname, 0) <= 0:
                        color = (0, 0, 255)
                        tag_estado = " [NO PERMITIDA]"
                    else:
                        c_tot = self.counts_by_class.get(cname, {}).get("in", 0) if self.line_orientation == "vertical" else (self.counts_by_class.get(cname, {}).get("in", 0) + self.counts_by_class.get(cname, {}).get("out", 0))
                        if c_tot > self.batch_limits[cname]:
                            color = (0, 0, 255)
                            tag_estado = " [EXCEDIDO]"
                        else:
                            color = (0, 220, 100)
                            tag_estado = " [OK]"
                else:
                    color = (0, 220, 100) if "blanco" in cname else (50, 150, 255)
                    tag_estado = ""

                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
                lbl = f"#{tid} {cname}{tag_estado}"
                (tw, th), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.50, 1)
                cv2.rectangle(annotated_frame, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
                cv2.putText(annotated_frame, lbl, (x1 + 3, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (0, 0, 0) if color == (0, 220, 100) else (255, 255, 255), 1)

        if is_counting_cam:
            annotated_frame = self.line_zone_annotator.annotate(frame=annotated_frame, line_counter=line_zone)

        fps_val = getattr(self, '_cam_fps', 30.0)
        in_cnt = len(self.counted_in_ids)
        cam_badge = f"CAM: {cam_channel.upper()}" + (" [CONTEO]" if is_counting_cam else " [VISTA AUXILIAR]")
        if is_counting_cam:
            ori_badge = "LÍNEA: VERTICAL (IZQ -> DER)" if self.line_orientation == "vertical" else "LÍNEA: HORIZONTAL (ARR -> ABA)"
        else:
            ori_badge = ""
        hud_txt = f"{cam_badge} {(' | ' + ori_badge) if ori_badge else ''} | FPS: {fps_val:.1f} | PASARON: {in_cnt}"
        (htw, hth), _ = cv2.getTextSize(hud_txt, cv2.FONT_HERSHEY_SIMPLEX, 0.48, 1)
        cv2.rectangle(annotated_frame, (10, 8), (20 + htw, 16 + hth), (15, 23, 42), -1)
        cv2.rectangle(annotated_frame, (10, 8), (20 + htw, 16 + hth), (56, 189, 248), 1)
        cv2.putText(annotated_frame, hud_txt, (15, 12 + hth), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (56, 189, 248), 1)

        return annotated_frame

VisionService = PawnVisionService
