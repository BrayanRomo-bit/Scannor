import os
import shutil
import tempfile
import base64
import cv2
import numpy as np
from fastapi import APIRouter, UploadFile, File, Query, HTTPException, Depends
from fastapi.responses import HTMLResponse

from app.models.schemas import (
    FrameProcessResponse,
    VideoProcessResponse,
    LineZoneMetrics,
    Base64FrameRequest,
    ResetResponse,
    BatchLimitsConfig,
    BatchStatus,
    LineConfigModel
)
from app.services.vision_service import PawnVisionService

router = APIRouter(prefix="", tags=["Chess Piece Counting & Batch Control"])

_service_instance: PawnVisionService = None

def set_vision_service(service: PawnVisionService):
    global _service_instance
    _service_instance = service

def get_vision_service() -> PawnVisionService:
    if _service_instance is None:
        raise HTTPException(status_code=503, detail="Servicio de visión no inicializado.")
    return _service_instance

@router.get("/", response_class=HTMLResponse)
@router.get("/panel", response_class=HTMLResponse)
def get_control_panel():
    """Retorna la interfaz web interactiva para configurar recetas de lote y monitorear conteos."""
    template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "panel.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Plantilla panel.html no encontrada.")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@router.post("/limits", response_model=BatchStatus)
@router.post("/api/v1/stream/pawn-counter/limits", response_model=BatchStatus)
def set_batch_limits(
    payload: BatchLimitsConfig,
    service: PawnVisionService = Depends(get_vision_service)
):
    """
    Establece los límites requeridos de piezas simultáneas para el lote actual.
    Ejemplo: {"peon_blanco": 5, "torre_negra": 6}
    """
    strict = payload.reject_unconfigured_pieces if payload.reject_unconfigured_pieces is not None else payload.strict_mode
    return service.set_batch_limits(payload.limits, strict if strict is not None else True)

@router.get("/limits")
@router.get("/api/v1/stream/pawn-counter/limits")
def get_batch_limits(service: PawnVisionService = Depends(get_vision_service)):
    """Retorna los límites configurados actualmente para el lote."""
    return service.get_batch_limits()

@router.post("/process-frame", response_model=FrameProcessResponse)
@router.post("/api/process-frame", response_model=FrameProcessResponse)
async def process_frame(
    file: UploadFile = File(..., description="Archivo de imagen del fotograma"),
    return_annotated: bool = Query(False, description="Incluir imagen procesada en Base64 con anotaciones"),
    service: PawnVisionService = Depends(get_vision_service)
):
    """
    Recibe un fotograma, actualiza el tracking (ByteTrack), valida el cruce de línea
    y evalúa el limitador de lote (alertando si hay piezas no permitidas o exceso de cantidad).
    """
    try:
        contents = await file.read()
        np_arr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("No se pudo decodificar el archivo como imagen válida.")
        
        return service.process_frame(frame, return_annotated=return_annotated)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno procesando frame: {str(e)}")

@router.post("/process-frame-base64", response_model=FrameProcessResponse)
@router.post("/api/process-frame-base64", response_model=FrameProcessResponse)
def process_frame_base64(
    payload: Base64FrameRequest,
    service: PawnVisionService = Depends(get_vision_service)
):
    """Alternativa para cámaras IoT / Edge: envía fotograma codificado en Base64."""
    try:
        raw_b64 = payload.image_base64
        if "," in raw_b64:
            raw_b64 = raw_b64.split(",")[1]
        raw_bytes = base64.b64decode(raw_b64)
        np_arr = np.frombuffer(raw_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("No se pudo decodificar la cadena Base64 como imagen.")

        return service.process_frame(frame, return_annotated=payload.return_annotated)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno procesando base64: {str(e)}")

@router.post("/process-video", response_model=VideoProcessResponse)
async def process_video(
    file: UploadFile = File(..., description="Archivo de video MP4, AVI o MKV"),
    service: PawnVisionService = Depends(get_vision_service)
):
    """Procesa un archivo de video completo y devuelve estadísticas consolidadas."""
    suffix = os.path.splitext(file.filename or ".mp4")[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        shutil.copyfileobj(file.file, tmp)

    try:
        response = service.process_video_file(tmp_path)
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando video: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@router.get("/status")
@router.get("/api/v1/stream/pawn-counter/status")
def get_status(service: PawnVisionService = Depends(get_vision_service)):
    """
    Retorna el estado del lote en formato compatible con el panel web de ajedrez (ajedrez.html).
    """
    metrics = service.get_metrics()

    counts_per_piece = {}
    for cls_name, dir_counts in metrics.counts_by_class.items():
        counts_per_piece[cls_name] = dir_counts.total

    batch_limits = dict(service.batch_limits)

    status_str = "READY"
    if metrics.batch.has_errors:
        status_str = "ERROR_LIMIT_EXCEEDED"
    elif metrics.batch.status == "COMPLETADO":
        status_str = "BATCH_COMPLETED"
    elif metrics.batch.status == "EN_PROCESO":
        status_str = "IN_PROGRESS"

    line_cfg = service.get_line_config()
    last_crossing = service.last_crossing_event

    return {
        "status": status_str,
        "total_pieces_counted": metrics.total,
        "counts_per_piece": counts_per_piece,
        "batch_limits": batch_limits,
        "errors": metrics.batch.error_messages,
        "in_count": metrics.in_count,
        "out_count": metrics.out_count,
        "metrics": metrics,
        "line_config": line_cfg,
        "last_crossing": last_crossing
    }

@router.get("/metrics", response_model=LineZoneMetrics)
@router.get("/api/v1/stream/pawn-counter/metrics", response_model=LineZoneMetrics)
def get_metrics(service: PawnVisionService = Depends(get_vision_service)):
    """Retorna el estado actual del contador, estado del lote y alertas activas."""
    return service.get_metrics()

@router.post("/reset", response_model=ResetResponse)
@router.post("/api/v1/stream/pawn-counter/reset", response_model=ResetResponse)
def reset_counters(service: PawnVisionService = Depends(get_vision_service)):
    """Reinicia acumuladores, alertas y registros de identificadores para un nuevo lote."""
    metrics = service.reset_state()
    return ResetResponse(
        status="success",
        message="Contadores, historial de identificadores y alertas reiniciados exitosamente.",
        metrics=metrics
    )

@router.get("/line-config", response_model=LineConfigModel)
@router.get("/api/v1/stream/pawn-counter/line-config", response_model=LineConfigModel)
def get_line_config(service: PawnVisionService = Depends(get_vision_service)):
    """Retorna la configuración actual de la línea virtual (orientación, posición, sonido)."""
    return service.get_line_config()

@router.post("/line-config", response_model=LineConfigModel)
@router.post("/api/v1/stream/pawn-counter/line-config", response_model=LineConfigModel)
def set_line_config(
    payload: LineConfigModel,
    service: PawnVisionService = Depends(get_vision_service)
):
    """
    Configura la orientación ('horizontal' o 'vertical'), posición (0.05 a 0.95) y sonido de la línea.
    """
    return service.set_line_config(
        orientation=payload.orientation,
        position=payload.position,
        sound_enabled=payload.sound_enabled
    )

@router.post("/test-beep")
@router.post("/api/v1/stream/pawn-counter/test-beep")
def test_beep(tipo: str = Query("cruce", description="'cruce' o 'rechazado'")):
    """Prueba de sonido inmediata en altavoces locales del hardware."""
    try:
        from microservices.shared.sound_effects import emitir_pitido
        emitir_pitido(tipo)
        return {"status": "success", "mensaje": f"Pitido '{tipo}' emitido correctamente."}
    except Exception as e:
        return {"status": "error", "mensaje": str(e)}
