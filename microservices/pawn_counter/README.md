# Microservicio de Conteo y Rastreo de Peones de Ajedrez

Microservicio industrial desarrollado con **FastAPI**, **YOLOv8** (detección/segmentación), **ByteTrack** y **Roboflow Supervision (`sv.LineZone`)** para el seguimiento y conteo de piezas sobre una banda transportadora continua sin duplicados.

---

## 1. ¿Cómo interactúa `track_id` con `LineZone` para evitar duplicados?

El problema clásico en visión artificial industrial en bandas transportadoras es la **sobreestimación por duplicación**: si una pieza permanece varios fotogramas sobre o cerca de la línea virtual, o si el centroide oscila entre fotogramas, un algoritmo ingenuo volvería a incrementar el contador.

Para garantizar consistencia matemática absoluta, este microservicio implementa una **doble barrera de validación**:

```
                       Fotograma T (Pieza cruzando la banda)
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │ YOLOv8 Inferencia In-GPU  │
                        └─────────────┬─────────────┘
                                      │ Cajas / Máscaras
                                      ▼
                        ┌───────────────────────────┐
                        │ ByteTrack (Filtro Kalman) │
                        └─────────────┬─────────────┘
                                      │ Asigna track_id único persistente
                                      ▼
                        ┌───────────────────────────┐
                        │   sv.LineZone.trigger()   │
                        └─────────────┬─────────────┘
                                      │
               ¿El centroide cambió de lado respecto al vector de línea?
                                      │
                         ┌────────────┴────────────┐
                         │ SÍ                      │ NO
                         ▼                         ▼
            ┌─────────────────────────┐     [Sin disparo]
            │ crossed_in / out = True │
            └────────────┬────────────┘
                         │
        ¿track_id YA EXISTE en counted_ids? (Segunda Barrera)
                         │
             ┌───────────┴───────────┐
             │ NO                    │ SÍ (Ya cruzó previamente)
             ▼                       ▼
    [Incrementar Contador]     [Descartar incremento]
    [Registrar track_id]       (Evita falsos duplicados por oscilación)
```

1. **Rastreo Temporal Consistente (ByteTrack):**
   - El detector YOLOv8 ubica las piezas en cada fotograma.
   - ByteTrack asocia las detecciones presentes con las pasadas utilizando un Filtro de Kalman espacial y la similitud IoU.
   - A cada peón se le asigna un `track_id` único e inmutable durante todo su recorrido por la escena.

2. **Detección Geométrica de Transición (`sv.LineZone`):**
   - Se traza un vector 2D orientado entre `Point(start)` y `Point(end)`.
   - `LineZone` calcula el producto vectorial (determinante 2D) entre el vector de la línea y la posición del centroide/base de la pieza.
   - Cuando el signo de este producto cambia entre dos fotogramas consecutivos para un mismo `track_id`, `line_zone.trigger()` detecta la transición (`crossed_in` o `crossed_out`).

3. **Garantía Inmutable de Unicidad (`counted_in_ids` y `counted_out_ids`):**
   - Aunque la pieza se detenga justo sobre la línea virtual o el detector sufra leves vibraciones de bounding box, el servicio valida si `track_id in self.counted_in_ids`.
   - Si ya está registrado, la suma no se repite bajo ninguna circunstancia.

---

## 2. Estructura del Proyecto

```
pawn_counter/
├── Dockerfile                      # Imagen ligera Debian-Slim con OpenCV y FFmpeg
├── requirements.txt                # Dependencias fijadas
├── .env.example                    # Plantilla de variables de entorno
├── weights/                        # Directorio para pesos .pt
│   └── best_peones.pt
└── app/
    ├── core/
    │   └── config.py               # Configuración centralizada vía pydantic-settings
    ├── models/
    │   └── schemas.py              # Esquemas Pydantic v2 para peticiones y respuestas
    ├── services/
    │   └── vision_service.py       # YOLOv8 + ByteTrack + LineZone + Anotaciones
    ├── api/
    │   └── endpoints.py            # Rutas HTTP (/process-frame, /process-video, etc.)
    └── main.py                     # Ciclo de vida lifespan y arranque FastAPI
```

---

## 3. Variables de Entorno (`.env`)

| Variable | Descripción | Valor por Defecto |
| :--- | :--- | :--- |
| `MODEL_PATH` | Ruta al archivo de pesos YOLOv8 | `weights/best_peones.pt` |
| `MODEL_TYPE` | Tipo de arquitectura | `detection` o `segmentation` |
| `CONFIDENCE_THRESHOLD` | Umbral mínimo de confianza | `0.50` |
| `IOU_THRESHOLD` | Umbral de solapamiento NMS | `0.45` |
| `TRACKER_CONFIG_PATH` | Archivo de configuración del tracker | `bytetrack.yaml` |
| `LINE_START_X`, `LINE_START_Y` | Punto inicial relativo de la línea virtual (0.0-1.0) | `0.10`, `0.50` |
| `LINE_END_X`, `LINE_END_Y` | Punto final relativo de la línea virtual (0.0-1.0) | `0.90`, `0.50` |
| `TARGET_CLASSES` | Clases de peones a filtrar separadas por coma | `peon,pawn,peon_blanco,peon_negro` |
| `DEVICE` | Dispositivo de ejecución (`cpu`, `cuda`, `0`) | `cpu` |
| `PORT` | Puerto HTTP expuesto | `8003` |

---

## 4. Endpoints Principales

### `POST /process-frame`
Procesa un fotograma individual enviado como `multipart/form-data`.
- Parámetro opcional: `?return_annotated=true` para recibir la imagen con anotaciones (cajas, IDs y línea virtual) en Base64.
- Respuesta:
```json
{
  "metrics": {
    "in_count": 5,
    "out_count": 0,
    "total": 5,
    "counts_by_class": {
      "peon_blanco": { "in_count": 3, "out_count": 0, "total": 3 },
      "peon_negro": { "in_count": 2, "out_count": 0, "total": 2 }
    }
  },
  "active_tracks": 4,
  "detections": [
    {
      "track_id": 12,
      "class_name": "peon_blanco",
      "confidence": 0.9412,
      "bbox": [120.5, 340.2, 175.8, 450.1],
      "has_mask": false
    }
  ],
  "newly_counted_ids": [12],
  "inference_time_ms": 14.8,
  "annotated_image_base64": null
}
```

### `POST /process-video`
Recibe un archivo de video completo (`.mp4`, `.avi`, `.mkv`), ejecuta el tracking continuo fotograma a fotograma y devuelve el resumen del lote.

### `GET /metrics`
Consulta las métricas actuales acumuladas sin enviar imágenes.

### `POST /reset`
Pone a cero los contadores de entradas/salidas y limpia el registro de `track_id`s para iniciar una nueva tanda de producción.

---

## 5. Ejecución

### Local con Python:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8003
```

### Con Docker:
```bash
docker build -t scannor-pawn-counter .
docker run -d -p 8003:8003 --name pawn_counter_service scannor-pawn-counter
```
