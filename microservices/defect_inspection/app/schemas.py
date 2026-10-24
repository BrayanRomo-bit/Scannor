from pydantic import BaseModel, Field
from typing import List, Optional

class DefectItem(BaseModel):
    class_name: str
    confidence: float
    bbox: List[float] = Field(description="Bounding box [x1, y1, x2, y2]")

class QRCodeData(BaseModel):
    detected: bool
    content: Optional[str] = None

class InspectionResponse(BaseModel):
    status: str = Field(description="'APROBADO' o 'RECHAZADO'")
    is_approved: bool
    qr: QRCodeData
    defectos: List[DefectItem]
    total_defectos: int
    motivo_rechazo: Optional[str] = None
    tiempo_inferencia_ms: float

class Base64InspectionRequest(BaseModel):
    image_base64: str
