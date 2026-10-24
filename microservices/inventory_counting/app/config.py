import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Union
from pydantic import field_validator

try:
    import torch
    default_device = "0" if torch.cuda.is_available() else "cpu"
except Exception:
    default_device = "cpu"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    model_path: str = os.getenv("MODEL_PATH", "models/best_block.pt")
    conf_threshold: float = 0.50
    iou_threshold: float = 0.45
    tracker_config: str = "bytetrack.yaml"
    device: str = os.getenv("DEVICE", default_device)

    line_position_y: float = 0.50

    approved_classes: Union[str, List[str]] = "bloque_ok"

    host: str = "0.0.0.0"
    port: int = 8002

    @field_validator("approved_classes", mode="after")
    @classmethod
    def parse_approved_classes(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [item.strip().lower() for item in v.split(",") if item.strip()]
        return [item.lower() for item in v]

settings = Settings()
