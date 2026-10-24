import time
import base64
import cv2
import numpy as np
from typing import Optional, Tuple
try:
    from pyzbar.pyzbar import decode as pyzbar_decode
except ImportError:
    pyzbar_decode = None

from app.model import DefectDetectionModel
from app.schemas import InspectionResponse, QRCodeData, DefectItem
from app.config import settings

try:
    from microservices.shared.offline_sync import get_offline_sync_manager
    from microservices.shared.bbox_stabilizer import BBoxStabilizer
except ImportError:
    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.offline_sync import get_offline_sync_manager
    from microservices.shared.bbox_stabilizer import BBoxStabilizer

class DefectInspectionService:
    def __init__(self, model: DefectDetectionModel):
        self.model = model
        self.qr_detector_cv2 = cv2.QRCodeDetector()
        self.sync_manager = get_offline_sync_manager()
        self.stabilizer = BBoxStabilizer(alpha=0.70, max_missing_time=0.45, max_missing_frames=6)

    def decode_qr(self, image: np.ndarray) -> QRCodeData:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        try:
            barcodes = pyzbar_decode(gray)
            for barcode in barcodes:
                data = barcode.data.decode("utf-8").strip()
                if data:
                    return QRCodeData(detected=True, content=data)
        except Exception:
            pass

        try:
            retval, decoded_info, _, _ = self.qr_detector_cv2.detectAndDecodeMulti(gray)
            if retval and decoded_info:
                for text in decoded_info:
                    if text and text.strip():
                        return QRCodeData(detected=True, content=text.strip())
        except Exception:
            pass

        return QRCodeData(detected=False, content=None)

    def process_image(self, image_bytes: bytes) -> InspectionResponse:
        t0 = time.perf_counter()

        np_arr = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("No se pudo decodificar la imagen enviada.")

        qr_data = self.decode_qr(image)

        all_detections = self.model.predict(image)

        defect_classes_set = set(settings.defect_classes)
        defects = [
            d for d in all_detections
            if (not defect_classes_set) or (d.class_name.lower() in defect_classes_set)
        ]

        reasons = []
        if settings.qr_required and (not qr_data.detected or not qr_data.content):
            reasons.append("Código QR ausente o no legible")
        if len(defects) > 0:
            defect_summary = ", ".join([f"{d.class_name} ({d.confidence*100:.1f}%)" for d in defects])
            reasons.append(f"Defectos detectados: {defect_summary}")

        is_approved = (len(reasons) == 0)
        status = "APROBADO" if is_approved else "RECHAZADO"
        motivo = " | ".join(reasons) if reasons else None

        try:
            lote_id = qr_data.content or "LOTE-DEFAULT"
            falla_nombre = "Pieza_Conforme" if is_approved else (defects[0].class_name if defects else "Falta_QR")
            conf_val = defects[0].confidence if defects else 1.0
            self.sync_manager.registrar_inspeccion(
                lote=lote_id,
                estado=status,
                falla=falla_nombre,
                confianza=conf_val,
                imagen_frame=image,
                extra_meta={"motivo": motivo, "model_type": "defect_inspection"}
            )
        except Exception as ex_sync:
            print(f"[-] Error registrando inspección offline en defectos: {ex_sync}")

        t1 = time.perf_counter()
        tiempo_ms = round((t1 - t0) * 1000, 2)

        return InspectionResponse(
            status=status,
            is_approved=is_approved,
            qr=qr_data,
            defectos=defects,
            total_defectos=len(defects),
            motivo_rechazo=motivo,
            tiempo_inferencia_ms=tiempo_ms
        )

    def process_base64(self, b64_str: str) -> InspectionResponse:
        if "," in b64_str:
            b64_str = b64_str.split(",")[1]
        raw_bytes = base64.b64decode(b64_str)
        return self.process_image(raw_bytes)

    def process_camera_frame(self, frame: np.ndarray) -> np.ndarray:
        """Inferencia y dibujo en tiempo real de defectos detectados para el stream de video con estabilización anti-parpadeo."""
        annotated_frame = frame.copy()
        h, w = frame.shape[:2]
        all_detections = self.model.predict(frame)

        defect_classes_set = set(settings.defect_classes)
        raw_defects = [
            d for d in all_detections
            if (not defect_classes_set) or (d.class_name.lower() in defect_classes_set)
        ]

        stabilized_defects = self.stabilizer.update(raw_defects)

        for d in stabilized_defects:
            box = d["bbox"]
            x1 = max(0, min(w - 1, int(box[0])))
            y1 = max(0, min(h - 1, int(box[1])))
            x2 = max(0, min(w - 1, int(box[2])))
            y2 = max(0, min(h - 1, int(box[3])))
            if x2 <= x1 or y2 <= y1:
                continue

            color = (0, 40, 240)
            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
            tid = d.get("track_id")
            cname = d.get("class_name", "defecto")
            conf = float(d.get("confidence", 0.0))
            lbl = f"#{tid} {cname} {conf*100:.0f}%" if tid is not None else f"{cname} {conf*100:.0f}%"
            (tw, th), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(annotated_frame, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
            cv2.putText(annotated_frame, lbl, (x1 + 3, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        cv2.rectangle(annotated_frame, (0, 0), (w, 30), (20, 24, 30), -1)
        hud = f"ScannorQA · Inspección Defectos | Defectos en vista: {len(stabilized_defects)}"
        cv2.putText(annotated_frame, hud, (15, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

        return annotated_frame
