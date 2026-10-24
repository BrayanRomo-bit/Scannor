import os
import sys
import math
import wave
import struct
import platform
import subprocess
import threading
from pathlib import Path
from typing import Optional

_BEEP_FILE_LOCK = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BEEP_PATH = os.path.join(_PROJECT_ROOT, "cruce_beep.wav")
_ALARM_PATH = os.path.join(_PROJECT_ROOT, "alarma.wav")

def asegurar_archivo_beep(freq: int = 880, duration: float = 0.10) -> str:
    """Genera un archivo PCM WAV de pitido limpio si no existe aún."""
    with _BEEP_FILE_LOCK:
        if os.path.exists(_BEEP_PATH) and os.path.getsize(_BEEP_PATH) > 100:
            return _BEEP_PATH
        try:
            sample_rate = 22050
            n_samples = int(sample_rate * duration)
            with wave.open(_BEEP_PATH, 'w') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                attack = int(sample_rate * 0.01)
                release = int(sample_rate * 0.02)
                for i in range(n_samples):
                    envelope = 1.0
                    if i < attack:
                        envelope = i / float(attack)
                    elif i > (n_samples - release):
                        envelope = (n_samples - i) / float(release)

                    val = int(32767.0 * 0.65 * envelope * math.sin(2.0 * math.pi * freq * i / sample_rate))
                    w.writeframes(struct.pack('<h', val))
            return _BEEP_PATH
        except Exception:
            return _BEEP_PATH

asegurar_archivo_beep()

def _reproducir_audio_hilo(ruta_archivo: str, es_alarma: bool = False):
    """Ejecuta el reproductor de sonido en segundo plano sin bloquear inferencias."""
    sistema = platform.system()
    try:
        if sistema == "Windows":
            import winsound
            if es_alarma:
                winsound.Beep(1200, 350)
            else:
                winsound.Beep(880, 100)
        else:
            if os.path.exists(ruta_archivo):
                res = subprocess.run(["aplay", "-q", ruta_archivo], stderr=subprocess.DEVNULL, timeout=1.0)
                if res.returncode != 0:
                    subprocess.run(["paplay", ruta_archivo], stderr=subprocess.DEVNULL, timeout=1.0)
            else:
                subprocess.run(["beep", "-f", "880", "-l", "100"], stderr=subprocess.DEVNULL, timeout=0.5)
    except Exception:
        pass

def emitir_pitido(tipo: str = "cruce"):
    """
    Dispara un pitido sonoro instantáneo en altavoces locales.
    - 'cruce' / 'ok': pitido ágil de confirmación (880Hz, 100ms).
    - 'rechazado' / 'alerta': alarma industrial de rechazo (alarma.wav).
    """
    es_rechazado = tipo in ["rechazado", "alerta", "error", "exceso", "no_permitida"]
    target_file = _ALARM_PATH if (es_rechazado and os.path.exists(_ALARM_PATH)) else asegurar_archivo_beep()

    t = threading.Thread(target=_reproducir_audio_hilo, args=(target_file, es_rechazado), daemon=True)
    t.start()
