import os
import shutil
import tempfile
import base64
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.model import ObjectTrackingModel
from app.service import InventoryCountingService
from app.schemas import FrameCountResponse, VideoCountResponse, ConteoActual, Base64FrameRequest, ResetResponse

try:
    from microservices.shared.camera_streamer import get_camera_stream_manager
except ImportError:
    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.camera_streamer import get_camera_stream_manager

camera_streamer = get_camera_stream_manager()

try:
    from microservices.shared.edge_credentials import asegurar_credenciales_edge
except ImportError:
    from microservices.shared.edge_credentials import asegurar_credenciales_edge

service_instance: InventoryCountingService = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global service_instance
    print(f"[*] Iniciando Microservicio de Conteo e Inventario...")
    asegurar_credenciales_edge(service_name="Microservicio de Conteo de Bloques", exit_on_failure=True)
    print(f"[*] Cargando modelo YOLO desde: {settings.model_path}")
    print(f"[*] Tracker: {settings.tracker_config} | Dispositivo: {settings.device}")
    
    model = ObjectTrackingModel(
        model_path=settings.model_path,
        tracker_config=settings.tracker_config,
        device=settings.device,
        conf_threshold=settings.conf_threshold,
        iou_threshold=settings.iou_threshold
    )
    service_instance = InventoryCountingService(model=model)
    camera_streamer.set_frame_processor(service_instance.process_camera_frame)
    camera_streamer.start()
    print("[+] Tracker, Conteo e Inferencia en Streaming listos.")
    yield
    print("[-] Deteniendo servicio de conteo.")
    camera_streamer.stop()

app = FastAPI(
    title="Microservicio de Conteo de Bloques e Inventario",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_service() -> InventoryCountingService:
    if service_instance is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")
    return service_instance

@app.get("/")
def root():
    return {
        "service": "Microservicio de Conteo de Bloques e Inventario",
        "status": "online",
        "port": settings.port,
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "motor_activo": camera_streamer.motor_activo,
        "service": "inventory_counting",
        "model_path": settings.model_path,
        "device": settings.device,
        "line_position_y": settings.line_position_y
    }

@app.get("/estado-camaras")
@app.get("/api/estado-camaras")
def estado_camaras():
    return camera_streamer.get_estado_camaras()

@app.post("/api/start")
@app.post("/start")
def api_start():
    camera_streamer.start()
    return {"status": "started", "motor_activo": True, "message": "Motor de inventario activo."}

@app.post("/api/stop")
@app.post("/stop")
def api_stop():
    camera_streamer.stop()
    return {"status": "stopped", "motor_activo": False, "message": "Motor de inventario detenido."}

@app.post("/api/guardar-configuracion")
@app.post("/guardar-configuracion")
async def api_guardar_configuracion(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return camera_streamer.guardar_configuracion(data)

@app.delete("/api/clear")
def api_clear():
    return {"status": "Memoria limpia."}

@app.get("/api/escanear-hardware")
def api_escanear_hardware():
    return camera_streamer.escanear_hardware()

@app.get("/video_feed/{cam_id}")
@app.get("/api/video_feed/{cam_id}")
def api_video_feed(cam_id: str):
    return camera_streamer.get_stream_response(cam_id)

@app.get("/count/metrics", response_model=ConteoActual)
def get_current_metrics(service: InventoryCountingService = Depends(get_service)):
    return service.get_counts()

@app.post("/count/reset", response_model=ResetResponse)
def reset_counts(service: InventoryCountingService = Depends(get_service)):
    counts = service.reset()
    return ResetResponse(
        status="reiniciado",
        aprobados=counts.aprobados,
        rechazados=counts.rechazados,
        total=counts.total
    )

@app.post("/count/frame", response_model=FrameCountResponse)
async def count_frame_file(
    file: UploadFile = File(...),
    service: InventoryCountingService = Depends(get_service)
):
    try:
        contents = await file.read()
        return service.process_frame_bytes(contents)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando fotograma: {str(e)}")

@app.post("/count/frame-base64", response_model=FrameCountResponse)
def count_frame_base64(
    payload: Base64FrameRequest,
    service: InventoryCountingService = Depends(get_service)
):
    try:
        raw_b64 = payload.image_base64
        if "," in raw_b64:
            raw_b64 = raw_b64.split(",")[1]
        raw_bytes = base64.b64decode(raw_b64)
        return service.process_frame_bytes(raw_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando frame base64: {str(e)}")

@app.post("/count/video", response_model=VideoCountResponse)
async def count_video_file(
    file: UploadFile = File(...),
    service: InventoryCountingService = Depends(get_service)
):
    suffix = os.path.splitext(file.filename or ".mp4")[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        shutil.copyfileobj(file.file, tmp)

    try:
        result = service.process_video_file(tmp_path)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando video: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
