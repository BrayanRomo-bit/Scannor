from django.db import models
from django.contrib.auth.models import AbstractUser
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
        ('general', 'Propósito General')
    )
    # El Modelo ahora actúa como una "Carpeta Padre"
    nombre = models.CharField(max_length=100, unique=True, help_text="Ej. Detector de Metales Línea 1")
    tipo_industria = models.CharField(max_length=50, choices=INDUSTRIAS, default='general')
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_industria_display()})"

    @property
    def version_activa(self):
        # Retorna el archivo .pt que actualmente está en producción
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
        unique_together = ('modelo', 'version') # Evita duplicar la versión v1.0 en el mismo modelo

    def save(self, *args, **kwargs):
        if self.activa:
            # Apaga automáticamente las versiones anteriores del mismo modelo
            VersionModelo.objects.filter(modelo=self.modelo).update(activa=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.modelo.nombre} - {self.version}"

class Dispositivo(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name='dispositivos')
    nombre = models.CharField(max_length=100, help_text="Ej. Línea 1 - Ensamblaje Frontal")
    device_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    mac_address = models.CharField(max_length=17, unique=True, null=True, blank=True, validators=[mac_validator])
    api_key = models.CharField(max_length=64, unique=True, editable=False)
    activo = models.BooleanField(default=True)
    ultimo_sync = models.DateTimeField(null=True, blank=True)
    modelo_asignado = models.ForeignKey(ModeloIA, on_delete=models.SET_NULL, null=True, blank=True)
    ip_local = models.CharField(max_length=50, blank=True, null=True, help_text="Ej: 192.168.1.50:8001")

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = secrets.token_hex(32)
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