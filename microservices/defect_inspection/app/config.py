import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Union
from pydantic import field_validator

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    model_path: str = os.getenv("MODEL_PATH", "models/best_abolladura.pt")
    conf_threshold: float = 0.50
    iou_threshold: float = 0.45
    device: str = "cpu"

    qr_required: bool = True
    defect_classes: Union[str, List[str]] = "abolladura,grieta,poro,rayon,bloque_fisura,bloque_rotura"

    host: str = "0.0.0.0"
    port: int = 8001

    @field_validator("defect_classes", mode="after")
    @classmethod
    def parse_defect_classes(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [item.strip().lower() for item in v.split(",") if item.strip()]
        return [item.lower() for item in v]

settings = Settings()
