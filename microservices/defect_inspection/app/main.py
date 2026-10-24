from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.model import DefectDetectionModel
from app.service import DefectInspectionService
from app.schemas import InspectionResponse, Base64InspectionRequest

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

service_instance: DefectInspectionService = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global service_instance
    print(f"[*] Iniciando Microservicio de Inspección...")
    asegurar_credenciales_edge(service_name="Microservicio de Inspección de Defectos", exit_on_failure=True)
    print(f"[*] Cargando modelo YOLO desde: {settings.model_path}")
    print(f"[*] Dispositivo de inferencia: {settings.device}")
    
    model = DefectDetectionModel(
        model_path=settings.model_path,
        device=settings.device,
        conf_threshold=settings.conf_threshold,
        iou_threshold=settings.iou_threshold
    )
    service_instance = DefectInspectionService(model=model)
    camera_streamer.set_frame_processor(service_instance.process_camera_frame)
    print("[+] Modelo, Inferencia y Streaming Anotado listos.")
    yield
    print("[-] Deteniendo servicio de inspección.")

app = FastAPI(
    title="Microservicio de Inspección de Defectos y QR",
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

def get_service() -> DefectInspectionService:
    if service_instance is None:
        raise HTTPException(status_code=503, detail="Servicio no inicializado.")
    return service_instance

@app.get("/")
def root():
    return {
        "service": "Microservicio de Inspección de Defectos y QR",
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
        "motor_activo": True,
        "service": "defect_inspection",
        "model_path": settings.model_path,
        "device": settings.device,
        "qr_required": settings.qr_required
    }

@app.get("/estado-camaras")
@app.get("/api/estado-camaras")
def estado_camaras():
    return camera_streamer.get_estado_camaras()

@app.post("/api/start")
@app.post("/start")
def api_start():
    camera_streamer.motor_activo = True
    return {"status": "started", "motor_activo": True, "message": "Motor de inspección activo."}

@app.post("/api/stop")
@app.post("/stop")
def api_stop():
    camera_streamer.motor_activo = False
    return {"status": "stopped", "motor_activo": False, "message": "Motor de inspección detenido."}

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

@app.post("/inspect/file", response_model=InspectionResponse)
async def inspect_file(
    file: UploadFile = File(...),
    service: DefectInspectionService = Depends(get_service)
):
    try:
        contents = await file.read()
        return service.process_image(contents)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno procesando imagen: {str(e)}")

@app.post("/inspect/base64", response_model=InspectionResponse)
def inspect_base64(
    payload: Base64InspectionRequest,
    service: DefectInspectionService = Depends(get_service)
):
    try:
        return service.process_base64(payload.image_base64)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno procesando base64: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
