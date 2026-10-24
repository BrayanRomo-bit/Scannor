import os
import numpy as np
from ultralytics import YOLO
from typing import List, Dict, Any

class ObjectTrackingModel:
    def __init__(self, model_path: str, tracker_config: str = "bytetrack.yaml", device: str = "cpu", conf_threshold: float = 0.5, iou_threshold: float = 0.45):
        self.model_path = model_path
        self.tracker_config = tracker_config
        self.device = device
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold

        resolved_path = model_path
        if not os.path.exists(resolved_path):
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "..", "shared", "models", os.path.basename(model_path)),
                os.path.join(os.path.dirname(__file__), "..", "models", os.path.basename(model_path)),
                os.path.join(os.path.dirname(__file__), "models", os.path.basename(model_path)),
                os.path.join(os.getcwd(), "models", os.path.basename(model_path)),
            ]
            for c in candidates:
                if os.path.exists(c):
                    resolved_path = c
                    break

        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"Pesos del modelo no encontrados en: {model_path}")

        self.model_path = resolved_path
        self.model = YOLO(resolved_path)
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        self.model.predict(dummy, device=self.device, verbose=False)

    def track(self, frame: np.ndarray, persist: bool = True) -> List[Dict[str, Any]]:
        results = self.model.track(
            source=frame,
            persist=persist,
            tracker=self.tracker_config,
            conf=self.conf_threshold,
            iou=self.iou_threshold,
            device=self.device,
            verbose=False
        )

        tracked_items: List[Dict[str, Any]] = []
        if not results or len(results) == 0 or results[0].boxes is None:
            return tracked_items

        boxes = results[0].boxes
        if len(boxes) == 0:
            return tracked_items

        for idx in range(len(boxes)):
            if boxes.id is not None:
                try:
                    track_id = int(boxes.id[idx].item())
                except Exception:
                    track_id = idx + 1
            else:
                track_id = idx + 1

            cls_id = int(boxes.cls[idx].item())
            class_name = self.model.names.get(cls_id, str(cls_id))
            conf = float(boxes.conf[idx].item())
            xyxy = [float(c) for c in boxes.xyxy[idx].tolist()]

            cx = (xyxy[0] + xyxy[2]) / 2.0
            cy = (xyxy[1] + xyxy[3]) / 2.0

            tracked_items.append({
                "track_id": track_id,
                "class_name": class_name,
                "conf": conf,
                "bbox": xyxy,
                "centroid": (cx, cy)
            })

        return tracked_items
