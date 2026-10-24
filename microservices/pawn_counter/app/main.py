import os
import cv2
import sqlite3
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.services.vision_service import PawnVisionService
from app.api.endpoints import router as vision_router, set_vision_service

try:
    from microservices.shared.edge_credentials import asegurar_credenciales_edge
except ImportError:
    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.edge_credentials import asegurar_credenciales_edge

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("[*] Iniciando Microservicio de Conteo de Peones en Banda...")
    asegurar_credenciales_edge(service_name="Microservicio Conteo de Peones", exit_on_failure=True)
    print(f"[*] Modelo: {settings.MODEL_PATH} ({settings.MODEL_TYPE})")
    print(f"[*] Tracker: {settings.TRACKER_CONFIG_PATH} | Conf: {settings.CONFIDENCE_THRESHOLD}")
    print(f"[*] Línea Virtual: ({settings.LINE_START_X}, {settings.LINE_START_Y}) -> ({settings.LINE_END_X}, {settings.LINE_END_Y})")
    print(f"[*] Dispositivo: {settings.DEVICE}")
    print("=" * 60)

    vision_service = PawnVisionService()
    set_vision_service(vision_service)
    camera_streamer.set_frame_processor(vision_service.process_camera_frame)
    print("[+] Servicio de visión, inferencia y streaming operativo.")

    yield

    print("[-] Deteniendo Microservicio de Conteo de Peones.")

app = FastAPI(
    title="Microservicio de Conteo de Peones de Ajedrez",
    description="Detección, seguimiento y conteo con YOLOv8, ByteTrack y Supervision LineZone sin duplicados.",
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

from fastapi.staticfiles import StaticFiles

try:
    from microservices.shared.offline_sync import get_offline_sync_manager
    from microservices.shared.camera_streamer import get_camera_stream_manager
except ImportError:
    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "microservices").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
            break
    from microservices.shared.offline_sync import get_offline_sync_manager
    from microservices.shared.camera_streamer import get_camera_stream_manager

sync_manager = get_offline_sync_manager()
camera_streamer = get_camera_stream_manager()
app.mount("/capturas", StaticFiles(directory=sync_manager.captures_dir), name="capturas")

app.include_router(vision_router)
app.include_router(vision_router, prefix="/api/v1/stream/pawn-counter")

@app.get("/", tags=["Root"])
def root():
    return {
        "service": "Microservicio de Conteo de Peones de Ajedrez",
        "status": "online",
        "port": settings.PORT,
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
def health_check():
    return {
        "status": "healthy",
        "motor_activo": True,
        "service": "pawn_counter",
        "model_path": settings.MODEL_PATH,
        "model_type": settings.MODEL_TYPE,
        "device": settings.DEVICE,
        "offline_sync": sync_manager.get_sync_status(),
        "line_virtual": {
            "start": [settings.LINE_START_X, settings.LINE_START_Y],
            "end": [settings.LINE_END_X, settings.LINE_END_Y]
        }
    }

@app.get("/api/sync-status", tags=["Sincronización Offline"])
def api_sync_status():
    return sync_manager.get_sync_status()

@app.post("/api/sync-flush", tags=["Sincronización Offline"])
def api_sync_flush():
    flushed = sync_manager.flush_sync_now()
    return {"status": "success", "sincronizados": flushed, "telemetria": sync_manager.get_sync_status()}

@app.post("/api/start", tags=["Control"])
@app.post("/start", tags=["Control"])
def api_start():
    camera_streamer.motor_activo = True
    return {"status": "started", "motor_activo": True, "message": "Motor de conteo activo."}

@app.post("/api/stop", tags=["Control"])
@app.post("/stop", tags=["Control"])
def api_stop():
    camera_streamer.motor_activo = False
    return {"status": "stopped", "motor_activo": False, "message": "Motor de conteo detenido."}

@app.get("/estado-camaras", tags=["Hardware"])
@app.get("/api/estado-camaras", tags=["Hardware"])
def api_estado_camaras():
    """Reporta el estado y configuración de hardware de cámaras para los paneles de Django."""
    return camera_streamer.get_estado_camaras()

@app.post("/api/guardar-configuracion", tags=["Hardware"])
@app.post("/guardar-configuracion", tags=["Hardware"])
async def api_guardar_configuracion(request: Request):
    """Guarda el mapeo de puertos de cámaras frontal y laterales desde Personalización/Ajustes."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    return camera_streamer.guardar_configuracion(data)

@app.get("/api/escanear-hardware", tags=["Hardware"])
def api_escanear_hardware():
    """Detecta puertos /dev/video o DirectShow disponibles."""
    return camera_streamer.escanear_hardware()

@app.get("/video_feed/{cam_id}", tags=["Streaming"])
@app.get("/api/video_feed/{cam_id}", tags=["Streaming"])
def api_video_feed(cam_id: str):
    """Stream MJPEG en tiempo real para analisis.html y monitoreo remoto."""
    return camera_streamer.get_stream_response(cam_id)

@app.websocket("/ws/failures")
async def websocket_failures(websocket: WebSocket, token: str = None):
    """WebSocket de eventos y notificaciones en tiempo real para la interfaz web."""
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass

@app.delete("/api/clear", tags=["Mantenimiento"])
def api_clear_history():
    """Purga registros ya sincronizados con Django en SQLite preservando datos pendientes."""
    try:
        with sqlite3.connect(sync_manager.db_path) as conn:
            conn.execute("DELETE FROM inspecciones WHERE sync = 1")
            conn.commit()
    except Exception:
        pass
    return {"status": "Memoria limpia. Datos pendientes preservados."}

@app.api_route("/api/borrar-capturas", methods=["DELETE", "POST"], tags=["Mantenimiento"])
def api_borrar_capturas():
    """Elimina las evidencias fotográficas almacenadas en disco local."""
    total = 0
    if os.path.exists(sync_manager.captures_dir):
        for f in os.listdir(sync_manager.captures_dir):
            fp = os.path.join(sync_manager.captures_dir, f)
            try:
                if os.path.isfile(fp):
                    os.unlink(fp)
                    total += 1
            except Exception:
                pass
    return {"status": "Capturas eliminadas con éxito", "total_borrados": total}

@app.post("/api/actualizar-modelo-ahora", tags=["OTA"])
def api_actualizar_modelo_ahora():
    """Descarga en caliente el modelo asignado en Staff Django vía OTA sin reiniciar el sistema."""
    try:
        base_url = sync_manager.django_api_url.split('/api/')[0]
        ota_url = f"{base_url}/api/descargar-modelo/"
        res = requests.get(ota_url, headers={"X-Device-Auth": sync_manager.device_key}, stream=True, timeout=15)
        if res.status_code == 200:
            with open(settings.MODEL_PATH, 'wb') as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            from app.api.endpoints import get_vision_service
            service = get_vision_service()
            from ultralytics import YOLO
            service.model = YOLO(settings.MODEL_PATH)
            return {"status": "exito", "mensaje": "Modelo recargado exitosamente vía OTA"}
        elif res.status_code == 404:
            return {"status": "sin_cambios", "mensaje": "No hay nuevo modelo asignado en Staff."}
        else:
            raise HTTPException(status_code=res.status_code, detail="Fallo al descargar modelo OTA")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/descargar-dataset", tags=["Dataset"])
def api_descargar_dataset():
    """Empaqueta y descarga en zip EXCLUSIVAMENTE las imágenes limpias (.jpg sin _vis.jpg) y sus etiquetas .txt."""
    import zipfile
    from io import BytesIO
    from fastapi.responses import Response

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(sync_manager.captures_dir):
            for f in os.listdir(sync_manager.captures_dir):
                if f.endswith("_vis.jpg"):
                    continue
                if f.endswith(".jpg") or f.endswith(".txt"):
                    fp = os.path.join(sync_manager.captures_dir, f)
                    if os.path.isfile(fp):
                        zf.write(fp, arcname=f)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="Dataset_Edge_Limpio.zip"'}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
