import time
import base64
import cv2
import numpy as np
from typing import List, Set, Dict, Tuple, Optional, Any
from app.model import ObjectTrackingModel
from app.schemas import FrameCountResponse, VideoCountResponse, ConteoActual
from app.config import settings

try:
    from microservices.shared.offline_sync import get_offline_sync_manager
except ImportError:
    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.offline_sync import get_offline_sync_manager

def compute_box_iou(boxA: List[float], boxB: List[float]) -> float:
    """Calcula la Intersección sobre Unión (IoU) entre dos rectángulos [x1, y1, x2, y2]."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interW = max(0.0, xB - xA)
    interH = max(0.0, yB - yA)
    interArea = interW * interH
    boxAArea = max(0.0, boxA[2] - boxA[0]) * max(0.0, boxA[3] - boxA[1])
    boxBArea = max(0.0, boxB[2] - boxB[0]) * max(0.0, boxB[3] - boxB[1])
    denom = boxAArea + boxBArea - interArea
    return (interArea / denom) if denom > 0 else 0.0

class InventoryCountingService:
    def __init__(self, model: ObjectTrackingModel):
        self.model = model
        self.aprobados: int = 0
        self.rechazados: int = 0
        self.total: int = 0
        self.active_pieces: Dict[str, Dict[str, Any]] = {}
        self.piece_seq: int = 0
        self.sync_manager = get_offline_sync_manager()

    def reset(self) -> ConteoActual:
        self.aprobados = 0
        self.rechazados = 0
        self.total = 0
        self.active_pieces.clear()
        self.piece_seq = 0
        return ConteoActual(aprobados=0, rechazados=0, total=0)

    def get_counts(self) -> ConteoActual:
        return ConteoActual(
            aprobados=self.aprobados,
            rechazados=self.rechazados,
            total=self.total
        )

    def process_frame(self, frame: np.ndarray) -> FrameCountResponse:
        resp, _ = self.process_frame_with_vis(frame)
        return resp

    def process_camera_frame(self, frame: np.ndarray, cam_id: str = "frontal") -> np.ndarray:
        """Hook invocado en tiempo real por CameraStreamManager para el streaming MJPEG de /video_feed."""
        if cam_id != "frontal":
            return frame
        _, annotated = self.process_frame_with_vis(frame, is_camera_stream=True)
        return annotated

    def process_frame_with_vis(self, frame: np.ndarray, is_camera_stream: bool = False) -> Tuple[FrameCountResponse, np.ndarray]:
        t0 = time.perf_counter()
        h, w = frame.shape[:2]
        line_y = int(h * settings.line_position_y)
        annotated_frame = frame.copy()
        t_now = time.time()

        raw_items = self.model.track(frame, persist=True)

        block_dets = [d for d in raw_items if "bloque_ok" in d["class_name"].lower() and d["conf"] >= 0.40]
        defect_dets = [d for d in raw_items if any(k in d["class_name"].lower() for k in ["fisura", "rotura"]) and d["conf"] >= 0.48]

        frame_pieces = []
        if block_dets:
            for b_det in block_dets:
                b_box = b_det["bbox"]
                associated_defects = []
                for d_det in defect_dets:
                    d_box = d_det["bbox"]
                    inter_x1 = max(b_box[0], d_box[0])
                    inter_y1 = max(b_box[1], d_box[1])
                    inter_x2 = min(b_box[2], d_box[2])
                    inter_y2 = min(b_box[3], d_box[3])
                    inter_w = max(0.0, inter_x2 - inter_x1)
                    inter_h = max(0.0, inter_y2 - inter_y1)
                    defect_area = (d_box[2] - d_box[0]) * (d_box[3] - d_box[1])
                    if defect_area > 0 and ((inter_w * inter_h) / defect_area) > 0.3:
                        associated_defects.append(d_det)
                
                frame_pieces.append({
                    "bbox": b_box,
                    "centroid": b_det["centroid"],
                    "ok_conf": b_det["conf"],
                    "defects": associated_defects
                })
        elif defect_dets:
            for d_det in defect_dets:
                frame_pieces.append({
                    "bbox": d_det["bbox"],
                    "centroid": d_det["centroid"],
                    "ok_conf": None,
                    "defects": [d_det]
                })

        newly_counted = []

        matched_current_pids = set()
        for fp in frame_pieces:
            f_box = fp["bbox"]
            f_cx, f_cy = fp["centroid"]

            matched_pid = None
            best_iou = 0.0

            for pid, p in self.active_pieces.items():
                iou = compute_box_iou(f_box, p["bbox"])
                p_cx, p_cy = p["centroid"]
                c_dist = ((f_cx - p_cx)**2 + (f_cy - p_cy)**2)**0.5

                if iou > 0.20 or c_dist < 90:
                    if iou > best_iou or matched_pid is None:
                        best_iou = iou
                        matched_pid = pid

            if matched_pid is None:
                self.piece_seq += 1
                matched_pid = self.piece_seq
                self.active_pieces[matched_pid] = {
                    "id": matched_pid,
                    "bbox": f_box,
                    "centroid": (f_cx, f_cy),
                    "prev_cy": f_cy,
                    "t_first_seen": t_now,
                    "t_last_seen": t_now,
                    "frames_seen": 1,
                    "inspected": False,
                    "estado": None,
                    "falla": None,
                    "confianza": 0.0,
                    "defects_seen": list(fp["defects"]),
                    "ok_confs": [fp["ok_conf"]] if fp["ok_conf"] is not None else []
                }
            else:
                p = self.active_pieces[matched_pid]
                p["prev_cy"] = p["centroid"][1]
                
                alpha = 0.70
                p["bbox"] = [
                    alpha * float(nb) + (1.0 - alpha) * float(ob)
                    for nb, ob in zip(f_box, p["bbox"])
                ]
                p["centroid"] = (
                    alpha * float(f_cx) + (1.0 - alpha) * float(p["centroid"][0]),
                    alpha * float(f_cy) + (1.0 - alpha) * float(p["centroid"][1])
                )
                p["t_last_seen"] = t_now
                p["frames_seen"] += 1
                if fp["ok_conf"] is not None:
                    p["ok_confs"].append(fp["ok_conf"])
                if fp["defects"]:
                    p["defects_seen"] = list(fp["defects"])

            matched_current_pids.add(matched_pid)

        if is_camera_stream:
            expired_ids = [
                pid for pid, p in self.active_pieces.items()
                if pid not in matched_current_pids and (t_now - p["t_last_seen"]) > 2.5
            ]
            for pid in expired_ids:
                del self.active_pieces[pid]

        for matched_pid in matched_current_pids:
            piece = self.active_pieces[matched_pid]
            if not piece["inspected"]:
                cruzo_linea = (piece["prev_cy"] < line_y <= piece["centroid"][1]) or (piece["prev_cy"] > line_y >= piece["centroid"][1])

                estable_en_camara = piece["frames_seen"] >= 5 or not is_camera_stream

                if cruzo_linea or estable_en_camara:
                    if piece["defects_seen"]:
                        best_defect = max(piece["defects_seen"], key=lambda x: x["conf"])
                        estado_final = "Rechazado"
                        falla_final = best_defect["class_name"]
                        conf_final = best_defect["conf"]
                        self.rechazados += 1
                    else:
                        estado_final = "Aprobado"
                        falla_final = "bloque_ok"
                        conf_final = (sum(piece["ok_confs"]) / len(piece["ok_confs"])) if piece["ok_confs"] else 0.85
                        self.aprobados += 1

                    self.total = self.aprobados + self.rechazados
                    piece["inspected"] = True
                    piece["estado"] = estado_final
                    piece["falla"] = falla_final
                    piece["confianza"] = conf_final
                    newly_counted.append(matched_pid)

                    p_box = piece["bbox"]
                    p_cx, p_cy = piece["centroid"]
                    bw = (p_box[2] - p_box[0]) / float(w)
                    bh = (p_box[3] - p_box[1]) / float(h)
                    norm_cx = p_cx / float(w)
                    norm_cy = p_cy / float(h)
                    cls_id = 1 if estado_final == "Aprobado" else 0
                    bbox_txt = f"{cls_id} {norm_cx:.6f} {norm_cy:.6f} {bw:.6f} {bh:.6f}"

                    try:
                        self.sync_manager.registrar_inspeccion(
                            lote="LOTE-BLOQUES-01",
                            estado=estado_final,
                            falla=falla_final,
                            confianza=conf_final,
                            imagen_frame=frame,
                            imagen_vis=annotated_frame,
                            bbox_txt=bbox_txt,
                            extra_meta={
                                "piece_id": matched_pid,
                                "class_name": falla_final,
                                "model_type": "inventory_counting"
                            }
                        )
                    except Exception as e:
                        print(f"[-] Error registrando inspección en inventario: {e}")

        cv2.line(annotated_frame, (0, line_y), (w, line_y), (255, 0, 200), 2)
        cv2.putText(annotated_frame, f"LÍNEA DE CONTEO Y = {int(settings.line_position_y*100)}%", 
                    (15, max(line_y - 8, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 200), 1)

        piezas_visibles = [
            pid for pid, p in self.active_pieces.items()
            if (t_now - p["t_last_seen"]) <= 0.45
        ]

        for pid in piezas_visibles:
            piece = self.active_pieces[pid]
            p_box = [int(round(c)) for c in piece["bbox"]]
            x1, y1 = max(0, p_box[0]), max(0, p_box[1])
            x2, y2 = min(w - 1, p_box[2]), min(h - 1, p_box[3])
            if x2 <= x1 or y2 <= y1:
                continue
            
            if piece["inspected"]:
                is_ok = (piece["estado"] == "Aprobado")
                color = (0, 220, 80) if is_ok else (0, 50, 240)
                label_txt = f"#{pid} [{piece['estado'].upper()}] {piece['confianza']*100:.0f}%"
            else:
                color = (0, 215, 255)
                label_txt = f"#{pid} Evaluando ({piece['frames_seen']}/5)..."

            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
            (tw, th), _ = cv2.getTextSize(label_txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(annotated_frame, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
            cv2.putText(annotated_frame, label_txt, (x1 + 3, max(12, y1 - 4)), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            if piece["inspected"] and piece["estado"] == "Rechazado":
                for df in piece.get("defects_seen", []):
                    d_box = [int(round(c)) for c in df["bbox"]]
                    dx1, dy1 = max(0, d_box[0]), max(0, d_box[1])
                    dx2, dy2 = min(w - 1, d_box[2]), min(h - 1, d_box[3])
                    if dx2 > dx1 and dy2 > dy1:
                        cv2.rectangle(annotated_frame, (dx1, dy1), (dx2, dy2), (0, 30, 255), 2)
                        d_lbl = f"{df['class_name']} {df['conf']*100:.0f}%"
                        cv2.putText(annotated_frame, d_lbl, (dx1, max(12, dy1 - 3)), 
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 30, 255), 1)

        cv2.rectangle(annotated_frame, (0, 0), (w, 32), (18, 24, 32), -1)
        hud_txt = f"ScannorQA · Conteo Bloques | Aprobados: {self.aprobados} | Rechazados: {self.rechazados} | Total: {self.total} | En vista: {len(piezas_visibles)}"
        cv2.putText(annotated_frame, hud_txt, (15, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

        t1 = time.perf_counter()
        ms = round((t1 - t0) * 1000, 2)

        resp = FrameCountResponse(
            aprobados=self.aprobados,
            rechazados=self.rechazados,
            total=self.total,
            activos=len(matched_current_pids),
            ids_activos=list(matched_current_pids),
            nuevos_contados=newly_counted,
            tiempo_inferencia_ms=ms
        )
        return resp, annotated_frame

    def process_frame_bytes(self, image_bytes: bytes) -> FrameCountResponse:
        np_arr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("No se pudo decodificar el fotograma.")
        return self.process_frame(frame)

    def process_video_file(self, video_path: str) -> VideoCountResponse:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"No se pudo abrir el archivo de video: {video_path}")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = total_frames / fps if fps > 0 else 0.0

        t0 = time.perf_counter()
        processed_count = 0

        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            self.process_frame(frame)
            processed_count += 1

        cap.release()
        elapsed = time.perf_counter() - t0
        avg_fps = round(processed_count / elapsed, 2) if elapsed > 0 else 0.0

        return VideoCountResponse(
            aprobados=self.aprobados,
            rechazados=self.rechazados,
            total=self.total,
            frames_procesados=processed_count,
            duracion_segundos=round(duration, 2),
            fps_promedio=avg_fps
        )
