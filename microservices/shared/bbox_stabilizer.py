import time
import math
import threading
from typing import List, Dict, Any, Optional, Union, Tuple

def compute_box_iou(box_a: List[float], box_b: List[float]) -> float:
    """Calcula la Intersección sobre Unión (IoU) entre dos rectángulos [x1, y1, x2, y2]."""
    x_a = max(box_a[0], box_b[0])
    y_a = max(box_a[1], box_b[1])
    x_b = min(box_a[2], box_b[2])
    y_b = min(box_a[3], box_b[3])

    inter_w = max(0.0, x_b - x_a)
    inter_h = max(0.0, y_b - y_a)
    inter_area = inter_w * inter_h

    area_a = max(0.0, box_a[2] - box_a[0]) * max(0.0, box_a[3] - box_a[1])
    area_b = max(0.0, box_b[2] - box_b[0]) * max(0.0, box_b[3] - box_b[1])
    denom = area_a + area_b - inter_area
    return (inter_area / denom) if denom > 0 else 0.0

def compute_box_centroid(box: List[float]) -> Tuple[float, float]:
    """Retorna el centroide (cx, cy) de una caja delimitadora."""
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    return cx, cy

class TrackedBox:
    """
    Representa un objeto rastreado con cinemática, amortiguamiento EMA y tolerancia a pérdidas.
    """
    def __init__(self, track_id: int, bbox: List[float], class_name: str, confidence: float, timestamp: float):
        self.track_id = track_id
        self.bbox = [float(c) for c in bbox]
        self.class_name = class_name
        self.confidence = float(confidence)
        self.last_seen = timestamp
        self.first_seen = timestamp
        self.hit_count = 1
        self.miss_count = 0
        self.vx = 0.0
        self.vy = 0.0

    def update(self, new_bbox: List[float], new_conf: float, default_alpha: float, timestamp: float):
        """
        Actualiza las coordenadas mediante Suavizado Exponencial Adaptativo (Adaptive EMA)
        con filtro deadband para congelar micro-temblores y estabilidad garantizada.
        """
        dt = max(0.001, timestamp - self.last_seen)
        old_cx, old_cy = compute_box_centroid(self.bbox)
        new_cx, new_cy = compute_box_centroid(new_bbox)

        dx = new_cx - old_cx
        dy = new_cy - old_cy
        disp = math.sqrt(dx * dx + dy * dy)

        inst_vx = dx / dt
        inst_vy = dy / dt
        inst_vx = max(-1500.0, min(1500.0, inst_vx))
        inst_vy = max(-1500.0, min(1500.0, inst_vy))
        self.vx = 0.65 * inst_vx + 0.35 * self.vx
        self.vy = 0.65 * inst_vy + 0.35 * self.vy

        if disp > 20.0:
            alpha = min(0.90, default_alpha + 0.20)
        else:
            alpha = default_alpha

        if disp < 1.2:
            target_bbox = list(self.bbox)
        else:
            target_bbox = new_bbox

        self.bbox = [
            alpha * float(nb) + (1.0 - alpha) * ob
            for nb, ob in zip(target_bbox, self.bbox)
        ]
        self.confidence = 0.70 * float(new_conf) + 0.30 * self.confidence
        self.last_seen = timestamp
        self.hit_count += 1
        self.miss_count = 0

    def get_extrapolated_bbox(self, current_time: float) -> List[float]:
        """
        Extrapola la posición de la caja hacia adelante si han transcurrido fotogramas
        sin detección nueva únicamente si hay una velocidad real y sostenida (> 25 px/s).
        Para objetos estacionarios o con micro-ruido, mantiene la posición fija sin desvíos.
        """
        dt = current_time - self.last_seen
        if dt <= 0.01:
            return list(self.bbox)
        
        speed = math.sqrt(self.vx * self.vx + self.vy * self.vy)
        if speed < 25.0:
            return list(self.bbox)

        dt_eff = min(0.30, dt)
        shift_x = self.vx * dt_eff
        shift_y = self.vy * dt_eff
        return [
            self.bbox[0] + shift_x,
            self.bbox[1] + shift_y,
            self.bbox[2] + shift_x,
            self.bbox[3] + shift_y
        ]

    def to_dict(self, current_time: Optional[float] = None) -> Dict[str, Any]:
        if current_time and current_time > self.last_seen:
            box = self.get_extrapolated_bbox(current_time)
            time_gap = current_time - self.last_seen
            decay = max(0.60, 1.0 - time_gap * 0.25)
            conf = self.confidence * decay
        else:
            box = self.bbox
            conf = self.confidence

        return {
            "track_id": self.track_id,
            "bbox": [int(round(c)) for c in box],
            "class_name": self.class_name,
            "confidence": round(conf, 4),
            "hit_count": self.hit_count,
            "miss_count": self.miss_count
        }

class BBoxStabilizer:
    """
    Estabilizador Temporal Avanzado de Cajas Delimitadoras (Anti-Flicker & Anti-Jitter).
    
    Características Industriales:
      1. Histéresis Dual de Confianza:
         - high_threshold (ej. 0.45): requerido para crear un nuevo track.
         - low_threshold (ej. 0.25): umbral de retención. Permite sostener un objeto detectado
           aunque su confianza oscile brevemente, eliminando el parpadeo estroboscópico.
      2. Suavizado Exponencial Adaptativo (Adaptive EMA): amortigua temblores en bordes.
      3. Inercia Visual Cinemática: extrapola con velocidad [vx, vy] solo en movimiento real.
      4. Filtro de Ruido y Visualización Inmediata (min_hits_to_show = 1 por defecto).
      5. Buffer de Persistencia Temporal (Miss Tolerance extendido contra caídas de cuadros).
      6. Seguridad multihilo garantizada (threading.Lock).
    """
    def __init__(
        self,
        alpha: float = 0.55,
        max_missing_time: float = 1.0,
        max_missing_frames: int = 10,
        iou_threshold: float = 0.20,
        max_centroid_dist: float = 110.0,
        min_hits_to_show: int = 1,
        high_threshold: float = 0.45,
        low_threshold: float = 0.25
    ):
        self.alpha = alpha
        self.max_missing_time = max_missing_time
        self.max_missing_frames = max_missing_frames
        self.iou_threshold = iou_threshold
        self.max_centroid_dist = max_centroid_dist
        self.min_hits_to_show = min_hits_to_show
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        
        self.tracks: Dict[int, TrackedBox] = {}
        self.next_track_id = 1
        self.lock = threading.Lock()

    def _extract_box_data(self, item: Any) -> Optional[Tuple[List[float], str, float]]:
        """Extrae (bbox, class_name, confidence) ya sea de dict o de objeto Pydantic/dataclass."""
        if isinstance(item, dict):
            bbox = item.get("bbox")
            cname = str(item.get("class_name", "objeto"))
            conf = float(item.get("confidence", item.get("conf", 0.0)))
        else:
            bbox = getattr(item, "bbox", None)
            cname = str(getattr(item, "class_name", "objeto"))
            conf = float(getattr(item, "confidence", getattr(item, "conf", 0.0)))

        if not bbox or len(bbox) != 4:
            return None
        return [float(c) for c in bbox], cname, conf

    def update(self, detections: List[Any], timestamp: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Alimenta nuevas detecciones al estabilizador, actualiza el seguimiento temporal
        y retorna la lista de cajas activas suavizadas y filtradas contra parpadeo.
        """
        t_now = timestamp if timestamp is not None else time.time()

        with self.lock:
            parsed_detections = []
            for det in detections:
                extracted = self._extract_box_data(det)
                if extracted:
                    d_box, d_class, d_conf = extracted
                    if d_conf >= self.low_threshold:
                        parsed_detections.append(extracted)

            matched_track_ids = set()
            unmatched_detections = []

            for d_box, d_class, d_conf in parsed_detections:
                best_track_id = None
                best_score = 0.0
                d_cx, d_cy = compute_box_centroid(d_box)

                for tid, track in self.tracks.items():
                    if tid in matched_track_ids:
                        continue
                    if track.class_name.lower() != d_class.lower():
                        continue

                    track_box = track.get_extrapolated_bbox(t_now)
                    iou = compute_box_iou(d_box, track_box)
                    t_cx, t_cy = compute_box_centroid(track_box)
                    dist = math.sqrt((d_cx - t_cx)**2 + (d_cy - t_cy)**2)

                    if iou >= self.iou_threshold or dist <= self.max_centroid_dist:
                        score = iou + max(0.0, 1.0 - (dist / self.max_centroid_dist))
                        if score > best_score:
                            best_score = score
                            best_track_id = tid

                if best_track_id is not None:
                    self.tracks[best_track_id].update(d_box, d_conf, self.alpha, t_now)
                    matched_track_ids.add(best_track_id)
                else:
                    unmatched_detections.append((d_box, d_class, d_conf))

            for d_box, d_class, d_conf in unmatched_detections:
                if d_conf >= self.high_threshold:
                    new_id = self.next_track_id
                    self.next_track_id += 1
                    self.tracks[new_id] = TrackedBox(new_id, d_box, d_class, d_conf, t_now)

            for tid, track in list(self.tracks.items()):
                if tid not in matched_track_ids and track.last_seen != t_now:
                    track.miss_count += 1

            active_boxes = []
            for tid in list(self.tracks.keys()):
                track = self.tracks[tid]
                time_since_seen = t_now - track.last_seen
                if time_since_seen > self.max_missing_time or track.miss_count > self.max_missing_frames:
                    del self.tracks[tid]
                else:
                    if track.hit_count >= self.min_hits_to_show or track.confidence >= 0.65:
                        active_boxes.append(track.to_dict(t_now))

            return active_boxes

    def get_active_boxes(self, timestamp: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Retorna las cajas estabilizadas actualmente vigentes con extrapolación inercial
        sin modificar contadores ni eliminar tracks durante lecturas de streaming rápido (30+ FPS).
        """
        t_now = timestamp if timestamp is not None else time.time()
        with self.lock:
            active = []
            for tid, track in self.tracks.items():
                time_since_seen = t_now - track.last_seen
                if time_since_seen <= self.max_missing_time and track.miss_count <= self.max_missing_frames:
                    if track.hit_count >= self.min_hits_to_show or track.confidence >= 0.65:
                        active.append(track.to_dict(t_now))
            return active

    def clear(self):
        """Reinicia el estado interno del estabilizador."""
        with self.lock:
            self.tracks.clear()
            self.next_track_id = 1
