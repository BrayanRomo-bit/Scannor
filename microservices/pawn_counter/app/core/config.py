import os
import torch
from typing import List, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    _default_weights = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "weights", "best_peones.pt")
    MODEL_PATH: str = os.getenv("MODEL_PATH", _default_weights if os.path.exists(_default_weights) else "weights/best_peones.pt")
    MODEL_TYPE: str = "segmentation"
    CONFIDENCE_THRESHOLD: float = 0.45
    IOU_THRESHOLD: float = 0.45
    TRACKER_CONFIG_PATH: str = "bytetrack.yaml"
    DEVICE: str = os.getenv("DEVICE", "0" if torch.cuda.is_available() else "cpu")
    IMG_SIZE: int = int(os.getenv("IMG_SIZE", "384"))
    INFERENCE_CADENCE: int = int(os.getenv("INFERENCE_CADENCE", "1"))

    MAX_ASPECT_RATIO: float = float(os.getenv("MAX_ASPECT_RATIO", "0.76"))
    MIN_ASPECT_RATIO: float = float(os.getenv("MIN_ASPECT_RATIO", "0.15"))
    MIN_REL_AREA: float = float(os.getenv("MIN_REL_AREA", "0.001"))
    MAX_REL_AREA: float = float(os.getenv("MAX_REL_AREA", "0.15"))
    MAX_WHITE_SATURATION: float = float(os.getenv("MAX_WHITE_SATURATION", "115.0"))
    MAX_BLACK_VALUE: float = float(os.getenv("MAX_BLACK_VALUE", "130.0"))

    LINE_START_X: float = 0.10
    LINE_START_Y: float = 0.50
    LINE_END_X: float = 0.90
    LINE_END_Y: float = 0.50

    TARGET_CLASSES: Union[str, List[str]] = os.getenv("TARGET_CLASSES", "*")

    HOST: str = "0.0.0.0"
    PORT: int = 8003

    @field_validator("TARGET_CLASSES", mode="after")
    @classmethod
    def parse_target_classes(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            if v.strip() in ("", "*"):
                return []
            return [c.strip().lower() for c in v.split(",") if c.strip()]
        return [c.lower() for c in v]

settings = Settings()
