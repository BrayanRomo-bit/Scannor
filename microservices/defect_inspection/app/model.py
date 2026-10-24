import os
import numpy as np
from ultralytics import YOLO
from typing import List
from app.schemas import DefectItem

class DefectDetectionModel:
    def __init__(self, model_path: str, device: str = "cpu", conf_threshold: float = 0.5, iou_threshold: float = 0.45):
        self.model_path = model_path
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
        dummy_frame = np.zeros((640, 640, 3), dtype=np.uint8)
        self.model.predict(dummy_frame, device=self.device, verbose=False)

    def predict(self, image: np.ndarray) -> List[DefectItem]:
        results = self.model.predict(
            source=image,
            conf=self.conf_threshold,
            iou=self.iou_threshold,
            device=self.device,
            verbose=False
        )

        defects: List[DefectItem] = []
        if not results or len(results) == 0:
            return defects

        result = results[0]
        if result.boxes is None or len(result.boxes) == 0:
            return defects

        for box in result.boxes:
            cls_id = int(box.cls[0].item())
            class_name = self.model.names.get(cls_id, str(cls_id))
            conf = float(box.conf[0].item())
            xyxy = [float(coord) for coord in box.xyxy[0].tolist()]

            defects.append(DefectItem(
                class_name=class_name,
                confidence=round(conf, 4),
                bbox=[round(c, 2) for c in xyxy]
            ))

        return defects
