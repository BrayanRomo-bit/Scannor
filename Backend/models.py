from django.db import models
from django.contrib.auth.models import AbstractUser
import os
import secrets 
from django.core.validators import RegexValidator 

mac_validator = RegexValidator(
    regex=r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$',
    message="La dirección MAC debe tener el formato exacto XX:XX:XX:XX:XX:XX."
)

class Empresa(models.Model):
    nombre = models.CharField(max_length=255, unique=True)
    activa = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    limite_usuarios = models.IntegerField(default=5, help_text="Cantidad máxima de operadores")
    ESTADOS_PLAN = (
        ('trial', 'Periodo de Prueba'),
        ('activo', 'Suscripción Activa'),
        ('suspendido', 'Suspendido por Pago')
    )
    estado_plan = models.CharField(max_length=30, choices=ESTADOS_PLAN, default='activo')

    def __str__(self):
        return self.nombre

class Usuario(AbstractUser):
    ROLES = (
        ('superadmin', 'Staff ScannorQA'),
        ('admin_empresa', 'Admin de Empresa'),
        ('operador', 'Operador de Línea')
    )
    rol = models.CharField(max_length=20, choices=ROLES, default='operador')
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, null=True)

    def __str__(self):
        return f"{self.username} ({self.get_rol_display()})"

class ModeloIA(models.Model):
    INDUSTRIAS = (
        ('metales', 'Inspección de Metales (Rayones, Abolladuras)'),
        ('ladrillos', 'Inspección de Ladrillos/Bloques (Grietas, Poros)'),
        ('textiles', 'Inspección de Textiles (Desgarres, Manchas)'),
        ('ajedrez', 'Conteo y Control de Lotes (Piezas de Ajedrez)'),
        ('general', 'Propósito General')
    )
    nombre = models.CharField(max_length=100, unique=True, help_text="Ej. Detector de Metales Línea 1")
    tipo_industria = models.CharField(max_length=50, choices=INDUSTRIAS, default='general')
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_industria_display()})"

    @property
    def es_ajedrez(self):
        nombre_lower = (self.nombre or "").lower()
        tipo_lower = (self.tipo_industria or "").lower()
        return any(k in nombre_lower or k in tipo_lower for k in ['ajedrez', 'peon', 'pawn', 'chess'])

    @property
    def version_activa(self):
        return self.versiones.filter(activa=True).order_by('-fecha_subida').first()

class VersionModelo(models.Model):
    modelo = models.ForeignKey(ModeloIA, on_delete=models.CASCADE, related_name='versiones')
    version = models.CharField(max_length=50, help_text="Ej. v1.0, v2.1")
    archivo = models.FileField(upload_to='modelos_ia/') 
    precision = models.FloatField(blank=True, null=True)
    notas_entrenamiento = models.TextField(blank=True, null=True)
    fecha_subida = models.DateTimeField(auto_now_add=True)
    activa = models.BooleanField(default=True, help_text="Si es true, las cámaras descargarán este archivo vía OTA")

    class Meta:
        unique_together = ('modelo', 'version')

    def save(self, *args, **kwargs):
        if self.activa:
            VersionModelo.objects.filter(modelo=self.modelo).update(activa=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.modelo.nombre} - {self.version}"

def generar_token():
    return secrets.token_hex(32)

class Dispositivo(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name='dispositivos')
    nombre = models.CharField(max_length=100, help_text="Ej. Línea 1 - Ensamblaje Frontal")
    device_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    mac_address = models.CharField(max_length=17, unique=False, null=True, blank=True, validators=[mac_validator])
    activo = models.BooleanField(default=True)
    ultimo_sync = models.DateTimeField(null=True, blank=True)
    modelo_asignado = models.ForeignKey(ModeloIA, on_delete=models.SET_NULL, null=True, blank=True)
    modelo_operativo = models.BooleanField(default=False)
    ip_local = models.CharField(max_length=50, blank=True, null=True, help_text="Ej: 192.168.1.50:8001")
    api_key = models.CharField(max_length=64, unique=True, editable=False, default=generar_token)
    admin_key = models.CharField(max_length=64, unique=True, editable=False, default=generar_token)
    stream_token = models.CharField(max_length=64, unique=True, editable=False, default=generar_token)

    @property
    def es_modelo_ajedrez(self):
        if not self.modelo_asignado:
            return False
        return self.modelo_asignado.es_ajedrez

    @property
    def puerto_servicio(self):
        if self.es_modelo_ajedrez:
            return int(os.getenv("PORT_PAWN_COUNTER", "8003"))
        if self.modelo_asignado:
            nombre = (self.modelo_asignado.nombre or "").lower()
            tipo = (self.modelo_asignado.tipo_industria or "").lower()
            if any(k in nombre or k in tipo for k in ['bloque', 'ladrillo', 'inventario', 'inventory', 'block']):
                return int(os.getenv("PORT_INVENTORY_COUNTING", "8002"))
        return int(os.getenv("PORT_DEFECT_INSPECTION", "8001"))

    @property
    def ip_con_puerto(self):
        default_host = os.getenv("DEFAULT_EDGE_HOST", "127.0.0.1")
        if not self.ip_local:
            return f"{default_host}:{self.puerto_servicio}"
        ip_limpia = self.ip_local.strip()
        host_part = ip_limpia.split("://")[-1].split("/")[0]
        if ":" in host_part:
            return ip_limpia
        return f"{ip_limpia}:{self.puerto_servicio}"

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = secrets.token_hex(32)
        if self.ip_local:
            self.ip_local = self.ip_local.strip()
            host_part = self.ip_local.split("://")[-1].split("/")[0]
            if ":" not in host_part:
                self.ip_local = f"{self.ip_local}:{self.puerto_servicio}"
                if 'update_fields' in kwargs and kwargs['update_fields'] and 'ip_local' not in kwargs['update_fields']:
                    kwargs['update_fields'] = list(kwargs['update_fields']) + ['ip_local']
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nombre} | {self.empresa.nombre}"

class RegistroInspeccion(models.Model):
    uid = models.CharField(max_length=150, primary_key=True)
    dispositivo = models.ForeignKey(Dispositivo, on_delete=models.PROTECT, related_name='inspecciones')
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)     
    timestamp = models.DateTimeField()
    lote_qr = models.CharField(max_length=100)
    falla = models.CharField(max_length=100)
    confianza = models.FloatField()
    estado = models.CharField(max_length=20)
    imagen = models.URLField(max_length=500)
    archivado = models.BooleanField(default=False)

    def __str__(self):
        return f"[{self.dispositivo_id}] {self.lote_qr} - {self.estado}"
    
class ValidacionHumana(models.Model):
    inspeccion = models.OneToOneField(RegistroInspeccion, on_delete=models.CASCADE, related_name='auditoria')
    validado_por = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True)
    fecha_validacion = models.DateTimeField(auto_now_add=True)
    modelo_acerto = models.BooleanField()
    
    CLASES_PERMITIDAS = (
        ('rayon', 'Rayón'),
        ('abolladura', 'Abolladura'),
        ('mancha', 'Mancha/Pintura'),
        ('grieta', 'Grieta Estructural'),
        ('falso_positivo', 'Falso Positivo (Pieza Limpia)'),
        ('defecto_nuevo', 'Defecto No Catalogado')
    )
    falla_real = models.CharField(max_length=50, choices=CLASES_PERMITIDAS, blank=True, null=True)
    justificacion = models.TextField(blank=True, null=True)
    curado_para_dataset = models.BooleanField(default=False)
    incluido_en_version_ia = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"Auditoría de {self.inspeccion.uid}"

class IntentoConexionEdge(models.Model):
    mac_address = models.CharField(max_length=17, unique=True)
    ip_origen = models.CharField(max_length=50, null=True, blank=True)
    ultimo_intento = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Intento pendiente: {self.mac_address}"
