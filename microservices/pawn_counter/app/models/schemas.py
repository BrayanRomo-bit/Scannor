from pydantic import BaseModel, Field
from typing import List, Dict, Optional

class DetectionItem(BaseModel):
    track_id: Optional[int] = Field(None, description="Identificador persistente de tracking")
    class_name: str
    confidence: float
    bbox: List[float] = Field(description="Bounding box [x1, y1, x2, y2]")
    has_mask: bool = Field(False, description="Indica si la detección incluye máscara de segmentación")

class DirectionCounts(BaseModel):
    in_count: int = Field(0, description="Piezas que cruzaron en dirección IN")
    out_count: int = Field(0, description="Piezas que cruzaron en dirección OUT")
    total: int = Field(0, description="Total en ambas direcciones")

class PieceProgress(BaseModel):
    class_name: str
    target_count: int = Field(0, description="Cantidad límite deseada en el lote")
    current_count: int = Field(0, description="Cantidad actual detectada")
    remaining: int = Field(0, description="Piezas faltantes para completar el límite")
    status: str = Field("PENDIENTE", description="PENDIENTE | COMPLETADO | EXCEDIDO")

class BatchStatus(BaseModel):
    status: str = Field("EN_PROCESO", description="EN_PROCESO | COMPLETADO | ERROR")
    has_errors: bool = Field(False, description="Indica si se violó una regla de lote")
    error_messages: List[str] = Field(default_factory=list, description="Lista de alertas y errores detectados")
    progress: Dict[str, PieceProgress] = Field(default_factory=dict, description="Progreso por clase configurada")

class BatchLimitsConfig(BaseModel):
    limits: Dict[str, int] = Field(
        ...,
        description="Límites simultáneos por pieza, ej: {'peon_blanco': 5, 'torre_negra': 6}",
        example={"peon_blanco": 5, "torre_negra": 6}
    )
    strict_mode: bool = Field(
        True,
        description="Si es True, disparar error si pasa una pieza no configurada en el lote"
    )
    reject_unconfigured_pieces: Optional[bool] = Field(
        None,
        description="Alias de strict_mode enviado por el frontend"
    )

class LineZoneMetrics(BaseModel):
    in_count: int = Field(0, description="Total global de entradas")
    out_count: int = Field(0, description="Total global de salidas")
    total: int = Field(0, description="Total global acumulado (in + out)")
    counts_by_class: Dict[str, DirectionCounts] = Field(default_factory=dict, description="Desglose por clase")
    batch: BatchStatus = Field(default_factory=BatchStatus, description="Estado y límites del lote activo")

class FrameProcessResponse(BaseModel):
    metrics: LineZoneMetrics
    active_tracks: int
    detections: List[DetectionItem]
    newly_counted_ids: List[int]
    inference_time_ms: float
    annotated_image_base64: Optional[str] = None

class VideoProcessResponse(BaseModel):
    total_frames: int
    duration_seconds: float
    fps: float
    metrics: LineZoneMetrics
    processing_time_ms: float

class Base64FrameRequest(BaseModel):
    image_base64: str
    return_annotated: bool = False

class ResetResponse(BaseModel):
    status: str
    message: str
    metrics: LineZoneMetrics

class LineConfigModel(BaseModel):
    orientation: str = Field("horizontal", description="'horizontal' o 'vertical'")
    position: Optional[float] = Field(0.50, description="Posición relativa de la línea (0.05 a 0.95)")
    sound_enabled: Optional[bool] = Field(True, description="Si es True, emitir pitido sonoro al cruzar")
