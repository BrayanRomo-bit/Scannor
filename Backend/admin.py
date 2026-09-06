from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Empresa, Usuario, Dispositivo, RegistroInspeccion, ValidacionHumana, ModeloIA

admin.site.register(ModeloIA)

@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'activa', 'estado_plan', 'fecha_creacion')
    list_filter = ('activa', 'estado_plan')
    search_fields = ('nombre',)

@admin.register(Usuario)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'rol', 'empresa', 'is_staff')
    list_filter = ('rol', 'empresa', 'is_staff', 'is_superuser')
    fieldsets = UserAdmin.fieldsets + (
        ('Roles y Tenant (Multi-Planta)', {'fields': ('rol', 'empresa')}),
    )

@admin.register(Dispositivo)
class DispositivoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'empresa', 'activo', 'api_key', 'ultimo_sync')
    list_filter = ('activo', 'empresa')
    readonly_fields = ('api_key',)

@admin.register(RegistroInspeccion)
class RegistroInspeccionAdmin(admin.ModelAdmin):
    list_display = ('uid', 'empresa', 'dispositivo', 'falla', 'estado', 'timestamp')
    list_filter = ('estado', 'empresa', 'falla')
    search_fields = ('uid', 'lote_qr')

@admin.register(ValidacionHumana)
class ValidacionHumanaAdmin(admin.ModelAdmin):
    list_display = ('inspeccion', 'validado_por', 'modelo_acerto', 'curado_para_dataset')
    list_filter = ('modelo_acerto', 'curado_para_dataset', 'falla_real')