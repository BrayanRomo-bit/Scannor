from pydantic import BaseModel, Field
from typing import List, Union

class ConteoActual(BaseModel):
    aprobados: int = Field(0, description="Bloques en buen estado (bloque_ok)")
    rechazados: int = Field(0, description="Bloques defectuosos (fisura, rotura)")
    total: int = Field(0, description="Total acumulado = aprobados + rechazados")

class FrameCountResponse(BaseModel):
    aprobados: int
    rechazados: int
    total: int
    activos: int
    ids_activos: List[Union[int, str]]
    nuevos_contados: List[Union[int, str]]
    tiempo_inferencia_ms: float

class VideoCountResponse(BaseModel):
    aprobados: int
    rechazados: int
    total: int
    frames_procesados: int
    duracion_segundos: float
    fps_promedio: float

class Base64FrameRequest(BaseModel):
    image_base64: str

class ResetResponse(BaseModel):
    status: str
    aprobados: int
    rechazados: int
    total: int
