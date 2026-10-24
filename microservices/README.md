# Scannor Vision Microservices

Arquitectura desacoplada de visión artificial con FastAPI, YOLOv8 y Docker.

## Estructura
```
microservices/
├── docker-compose.yml
├── shared/models/                  # Modelos .pt montados en volumen
│   ├── best_abolladura.pt
│   └── best_block.pt
├── defect_inspection/              # Servicio A: Puerto 8001
└── inventory_counting/             # Servicio B: Puerto 8002
```

## Microservicio A: Inspección de Defectos y QR (Puerto 8001)
- **Modelo:** YOLOv8 (`MODEL_PATH`, admite cualquier archivo `.pt`).
- **Regla:** Requiere QR válido Y 0 defectos -> `APROBADO`. Si hay defectos o falta QR -> `RECHAZADO`.
- **Endpoints:**
  - `POST /inspect/file`: multipart/form-data
  - `POST /inspect/base64`: JSON con string base64
  - `GET /health`: Estado y modelo cargado

## Microservicio B: Conteo e Inventario de Bloques (Puerto 8002)
- **Modelo:** YOLOv8 + ByteTrack (`MODEL_PATH`, admite cualquier archivo `.pt`).
- **Regla:** Conteo en cruce de línea virtual sin duplicación de IDs. Conteo simplificado: `aprobados` (`bloque_ok`), `rechazados` (`bloque_fisura`, `bloque_rotura`) y `total`.
- **Endpoints:**
  - `POST /count/frame`: Fotograma multipart
  - `POST /count/frame-base64`: Fotograma base64
  - `POST /count/video`: Subida de archivo de video
  - `GET /count/metrics`: Métricas actuales acumuladas
  - `POST /count/reset`: Reinicio de contadores
  - `GET /health`: Estado del servicio

## Despliegue con Docker Compose
```bash
cd microservices
docker compose up --build -d
```
