from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import logout
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.hashers import make_password
from django.contrib.auth import authenticate, login
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.db import IntegrityError
from django.conf import settings
from .models import RegistroInspeccion, Usuario, Empresa, ValidacionHumana, Dispositivo, IntentoConexionEdge
from urllib.parse import urlparse
from datetime import datetime
import requests
import json 
import secrets
import logging
import os

logger = logging.getLogger(__name__)

@csrf_exempt
def api_provisionar_dispositivo(request):
    """
    Endpoint para Zero-Touch Provisioning y Modo Descubrimiento.
    """
    if request.method == 'POST':
        try:
            datos = json.loads(request.body)
            mac = datos.get('mac_address')

            if not mac:
                return JsonResponse({"error": "MAC Address no proporcionada"}, status=400)

            try:
                dispositivo = Dispositivo.objects.get(mac_address__iexact=mac, activo=True)
                
                IntentoConexionEdge.objects.filter(mac_address__iexact=mac).delete()
                
                host_actual = request.get_host()
                
                return JsonResponse({
                    "status": "success",
                    "env_data": {
                        "SCANNOR_DEVICE_ID": dispositivo.device_id,
                        "SCANNOR_DEVICE_KEY": dispositivo.api_key,
                        "SCANNOR_CENTRAL_SECRET": "device-secret-planta1",
                        "SCANNOR_API_KEY": "admin-secret-2026",
                        "SCANNOR_STREAM_TOKEN": "visor-token-123",
                        "DJANGO_API_URL": f"http://{host_actual}/api/guardar-falla/"
                    }
                }, status=200)

            except Dispositivo.DoesNotExist:
                ip_cliente = request.META.get('REMOTE_ADDR')
                
                IntentoConexionEdge.objects.update_or_create(
                    mac_address=mac,
                    defaults={'ip_origen': ip_cliente}
                )
                
                logger.warning(f"Intento fallido. MAC registrada en Descubrimiento: {mac}")
                return JsonResponse({"error": "Dispositivo pendiente de autorización. Revise el panel Staff."}, status=404)

        except Exception as e:
            logger.error(f"Error en aprovisionamiento MAC: {e}", exc_info=True)
            return JsonResponse({"error": "Error interno del servidor"}, status=500)

    return JsonResponse({"error": "Método no permitido"}, status=405)

def es_superadmin(user):
    return user.is_authenticated and user.is_superuser

def es_admin_empresa(user):
    return user.is_authenticated and user.rol == 'admin_empresa'

@login_required
def descargar_env_dispositivo(request, dispositivo_id):
    dispositivo = get_object_or_404(Dispositivo, id=dispositivo_id)
    
    if not request.user.is_superuser and request.user.empresa != dispositivo.empresa:
        return HttpResponse("No tienes permisos para acceder a las credenciales de esta empresa.", status=403)
        
    host_actual = request.get_host()
    
    contenido_env = f"""# ==========================================
# CONFIGURACIÓN AUTO-GENERADA SCANNORQA
# Cliente: {dispositivo.empresa.nombre}
# Dispositivo: {dispositivo.nombre}
# Fecha de generación: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# ==========================================

SCANNOR_DEVICE_ID={dispositivo.device_id}
SCANNOR_DEVICE_KEY={dispositivo.api_key}

# --- CONFIGURACIONES GLOBALES DEL SERVIDOR ---
SCANNOR_CENTRAL_SECRET=device-secret-planta1
SCANNOR_API_KEY=admin-secret-2026
SCANNOR_STREAM_TOKEN=visor-token-123
DJANGO_API_URL=http://{host_actual}/api/guardar-falla/
"""
    
    response = HttpResponse(contenido_env, content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="scannor_{dispositivo.device_id}.env"'
    
    return response

@user_passes_test(es_superadmin)
def staff_empresas(request):
    if request.method == 'POST':
        nombre_empresa = request.POST.get('nombre')
        limite = request.POST.get('limite_usuarios', 5)
        
        admin_username = request.POST.get('admin_username')
        admin_email = request.POST.get('admin_email', '')
        admin_password = request.POST.get('admin_password')
        
        if nombre_empresa and admin_username and admin_password:
            if Empresa.objects.filter(nombre=nombre_empresa).exists():
                messages.error(request, f"La empresa '{nombre_empresa}' ya está registrada.")
                return redirect('staff_empresas')
                
            if Usuario.objects.filter(username=admin_username).exists():
                messages.error(request, f"El usuario '@{admin_username}' ya está en uso.")
                return redirect('staff_empresas')
                
            nueva_empresa = Empresa.objects.create(
                nombre=nombre_empresa,
                limite_usuarios=int(limite),
                estado_plan='activo'
            )
            
            Usuario.objects.create_user(
                username=admin_username,
                email=admin_email,
                password=admin_password,
                empresa=nueva_empresa,
                rol='admin_empresa',
                is_active=True
            )
            
            messages.success(request, f"¡Fábrica '{nombre_empresa}' y Admin '@{admin_username}' creados exitosamente!")
            return redirect('staff_empresas')

    empresas_activas = Empresa.objects.all().order_by('-id')
    return render(request, 'staff/empresas.html', {'empresas': empresas_activas})

@login_required
def staff_dispositivos(request):
    if request.method == 'POST':
        nombre = request.POST.get('nombre')
        device_id = request.POST.get('device_id')
        mac_address = request.POST.get('mac_address')
        empresa_id = request.POST.get('empresa_id')
        modelo_id = request.POST.get('modelo_id') # ATRAPAMOS EL MODELO

        if nombre and device_id and mac_address and empresa_id:
            try:
                empresa = Empresa.objects.get(id=empresa_id)
                from .models import ModeloIA
                modelo_obj = ModeloIA.objects.get(id=modelo_id) if modelo_id else None
                
                Dispositivo.objects.create(
                    nombre=nombre,
                    device_id=device_id,
                    mac_address=mac_address.upper(), 
                    empresa=empresa,
                    modelo_asignado=modelo_obj, 
                    api_key=secrets.token_hex(32)
                )
                
                IntentoConexionEdge.objects.filter(mac_address__iexact=mac_address).delete()
                
                messages.success(request, f"Dispositivo '{nombre}' registrado exitosamente.")
            except Empresa.DoesNotExist:
                messages.error(request, "La empresa seleccionada no existe.")
            except Exception as e:
                messages.error(request, f"Error: Verifica que el ID Técnico o la MAC no estén duplicados.")
        else:
            messages.error(request, "Todos los campos principales son obligatorios.")
            
        return redirect('staff_dispositivos')

    dispositivos = Dispositivo.objects.all().order_by('-id')
    empresas = Empresa.objects.all()
    intentos_conexion = IntentoConexionEdge.objects.all().order_by('-ultimo_intento')
    
    from .models import ModeloIA
    modelos_ia = ModeloIA.objects.all().order_by('-id') # ENVIAMOS LOS MODELOS A LA WEB

    context = {
        'dispositivos': dispositivos,
        'empresas': empresas,
        'intentos_conexion': intentos_conexion,
        'modelos': modelos_ia
    }
    return render(request, 'staff/dispositivos.html', context)

Usuario = get_user_model()
@user_passes_test(es_superadmin)
def staff_usuarios(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        empresa_id = request.POST.get('empresa_id')

        if username and password and empresa_id:
            try:
                empresa_obj = Empresa.objects.get(id=empresa_id)
                Usuario.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    rol='admin_empresa',
                    empresa=empresa_obj,
                    is_active=True
                )
                messages.success(request, f"Gerente {username} creado exitosamente para la empresa {empresa_obj.nombre}.")
            except IntegrityError:
                messages.error(request, f"Error: El usuario '{username}' ya existe en el sistema.")
            except Empresa.DoesNotExist:
                messages.error(request, "Error: La empresa seleccionada no existe.")
                
        return redirect('staff_usuarios')

    usuarios = Usuario.objects.filter(is_superuser=False).select_related('empresa').order_by('-date_joined')
    empresas = Empresa.objects.filter(activa=True)
    
    return render(request, 'staff/usuarios.html', {
        'usuarios': usuarios,
        'empresas': empresas
    })

@user_passes_test(es_superadmin)
def toggle_empresa(request, empresa_id):
    empresa = get_object_or_404(Empresa, id=empresa_id)
    empresa.activa = not empresa.activa
    
    if empresa.activa:
        empresa.estado_plan = 'activo'
    else:
        empresa.estado_plan = 'suspendido'
        
    empresa.save()
    return redirect('staff_empresas')

@user_passes_test(es_superadmin)
def toggle_usuario(request, usuario_id):
    usuario = get_object_or_404(Usuario, id=usuario_id)
    if not usuario.is_superuser: 
        usuario.is_active = not usuario.is_active
        usuario.save()
    return redirect('staff_usuarios')

@login_required
@user_passes_test(es_admin_empresa)
def gestion_planta(request):
    empresa_actual = request.user.empresa
    operadores = Usuario.objects.filter(empresa=empresa_actual, rol='operador')
    usuarios_activos = Usuario.objects.filter(empresa=empresa_actual, is_active=True).count()
    
    dispositivos_empresa = Dispositivo.objects.filter(empresa=empresa_actual, activo=True)
    
    return render(request, 'paneles/gestion_planta.html', {
        'empresa': empresa_actual,
        'operadores': operadores,
        'usuarios_activos': usuarios_activos,
        'limite': empresa_actual.limite_usuarios,
        'dispositivos': dispositivos_empresa # Se lo pasamos al HTML
    })

@login_required
@user_passes_test(es_admin_empresa)
def crear_operador(request):
    if request.method == 'POST':
        empresa_actual = request.user.empresa
        
        usuarios_activos = Usuario.objects.filter(empresa=empresa_actual, is_active=True).count()
        if usuarios_activos >= empresa_actual.limite_usuarios:
            messages.error(request, f"¡Límite alcanzado! Tu plan permite un máximo de {empresa_actual.limite_usuarios} usuarios.")
            return redirect('gestion_planta')
            
        nuevo_username = request.POST.get('username')
        nuevo_email = request.POST.get('email')
        nueva_password = request.POST.get('password')
        
        if nuevo_username and nueva_password:
            if Usuario.objects.filter(username=nuevo_username).exists():
                messages.error(request, f"El usuario @{nuevo_username} ya está en uso. Elige otro.")
                return redirect('gestion_planta')
                
            Usuario.objects.create_user(
                username=nuevo_username,
                email=nuevo_email,
                password=nueva_password, 
                empresa=empresa_actual,
                rol='operador',
                is_active=True 
            )
            messages.success(request, f"Operador @{nuevo_username} creado exitosamente.")
            
    return redirect('gestion_planta')

def login_usuario(request):
    if request.method == 'POST':
        usuario_ingresado = request.POST.get('username')
        password_ingresada = request.POST.get('password')
        
        user = authenticate(request, username=usuario_ingresado, password=password_ingresada)
        
        if user is not None:
            if user.is_active:
                login(request, user)
                
                if user.is_superuser or user.rol == 'superadmin':
                    return redirect('staff_empresas')
                else:
                    return redirect('dashboard')
            else:
                return redirect('sala_espera')
        else:
            error = "Usuario o contraseña incorrectos."
            return render(request, 'paneles/login.html', {'error': error})
            
    return render(request, 'paneles/login.html')

def sala_espera(request):
    return render(request, 'paneles/sala_espera.html')

def cerrar_sesion(request):
    logout(request)
    return redirect('login')

@login_required
def dashboard(request):
    return render(request, 'base.html')

@login_required
def personalizacion(request):
    dispositivos = []
    
    if request.user.is_superuser or request.user.rol == 'superadmin':
        dispositivos = Dispositivo.objects.filter(activo=True)
        
    elif hasattr(request.user, 'empresa') and request.user.empresa:
        dispositivos = Dispositivo.objects.filter(empresa=request.user.empresa, activo=True)
        
    return render(request, 'paneles/personalizacion.html', {'dispositivos': dispositivos})

@csrf_exempt 
def recibir_falla_api(request):
    if request.method == 'POST':
        try:
            datos = json.loads(request.body)
            campos_requeridos = ['uid', 'dispositivo_id', 'timestamp', 'lote', 'falla', 'confianza', 'estado', 'imagen']
            if not all(k in datos for k in campos_requeridos):
                return JsonResponse({"error": "Payload incompleto"}, status=400)
            
            try:
                dispositivo_obj = Dispositivo.objects.get(device_id=datos['dispositivo_id'])
            except Dispositivo.DoesNotExist:
                ip_cliente = request.META.get('REMOTE_ADDR')
                IntentoConexionEdge.objects.update_or_create(
                    mac_address=datos['dispositivo_id'], 
                    defaults={'ip_origen': ip_cliente}
                )
                logger.warning(f"Cámara no autorizada detectada en el Radar: {datos['dispositivo_id']}")
                return JsonResponse({"error": f"Dispositivo técnico '{datos['dispositivo_id']}' no encontrado. Revisa el radar Staff."}, status=404)
            
            auth_header = request.headers.get('X-Device-Auth', '')
            if not secrets.compare_digest(auth_header, dispositivo_obj.api_key):
                logger.warning(f"Intento de acceso denegado para cámara: {dispositivo_obj.nombre}")
                return JsonResponse({"error": "No autorizado: API Key incorrecta"}, status=403)

            fecha_utc = datetime.fromisoformat(datos['timestamp'])
            
            inspeccion, creado = RegistroInspeccion.objects.update_or_create(
                uid=datos['uid'],
                defaults={
                    'dispositivo': dispositivo_obj,
                    'empresa': dispositivo_obj.empresa,
                    'timestamp': fecha_utc,
                    'lote_qr': datos['lote'],
                    'falla': datos['falla'],
                    'confianza': float(datos['confianza']),
                    'estado': datos['estado'],
                    'imagen': datos['imagen']
                }
            )
            
            dispositivo_obj.ultimo_sync = fecha_utc
            dispositivo_obj.save(update_fields=['ultimo_sync'])
            
            return JsonResponse({"status": "Guardado exitosamente", "uid": inspeccion.uid}, status=201)
            
        except ValueError as ve:
            return JsonResponse({"error": f"Error de formato: {str(ve)}"}, status=400)
        except Exception as e:
            logger.error(f"Error interno en DB: {e}", exc_info=True)
            return JsonResponse({"error": "Error interno del servidor"}, status=500)
            
    return JsonResponse({"error": "Método no permitido"}, status=405)

@login_required
def api_obtener_resultados(request):
    if request.user.is_superuser or request.user.rol == 'superadmin':
        inspecciones = RegistroInspeccion.objects.select_related('dispositivo', 'empresa').filter(archivado=False).order_by('-timestamp')
    elif request.user.empresa:
        inspecciones = RegistroInspeccion.objects.select_related('dispositivo', 'empresa').filter(empresa=request.user.empresa, archivado=False).order_by('-timestamp')
    else:
        inspecciones = RegistroInspeccion.objects.none()
    data = []
    for insp in inspecciones:
        data.append({
            'uid': insp.uid,
            'dispositivo': insp.dispositivo.nombre if insp.dispositivo else 'N/A',
            'timestamp': insp.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'lote': insp.lote_qr,
            'falla': insp.falla,
            'confianza': f"{insp.confianza * 100:.1f}%" if insp.confianza <= 1 else f"{insp.confianza:.1f}%",
            'estado': insp.estado,
            'imagen': insp.imagen
        })
    
    return JsonResponse({'resultados': data})

@csrf_exempt
@login_required
def registrar_falso_positivo(request, uid):
    if request.method == 'POST':
        try:
            inspeccion = RegistroInspeccion.objects.select_related('dispositivo__modelo_asignado').get(uid=uid)
        except RegistroInspeccion.DoesNotExist:
            return JsonResponse({
                "error": "La pieza aún se está sincronizando desde la línea o el dispositivo no está correctamente enlazado. Espera 5 segundos e intenta de nuevo."
            }, status=404)
        
        if not request.user.is_superuser and inspeccion.empresa != request.user.empresa:
            return JsonResponse({"error": "No tienes permiso para alterar esta pieza"}, status=403)
            
        try:
            data = json.loads(request.body)
            justificacion_texto = data.get('justificacion', 'Sin justificación')
            es_acierto = data.get('modelo_acerto', False)
            
            validacion, created = ValidacionHumana.objects.update_or_create(
                inspeccion=inspeccion,
                defaults={
                    'validado_por': request.user,
                    'modelo_acerto': es_acierto,
                    'falla_real': inspeccion.falla if es_acierto else 'falso_positivo',
                    'justificacion': justificacion_texto
                }
            )
            
            if es_acierto:
                inspeccion.estado = 'Rechazado (Auditado)'
                
                try:
                    modelo = inspeccion.dispositivo.modelo_asignado
                    industria = modelo.tipo_industria if modelo else 'general'
                    falla_limpia = inspeccion.falla.replace(" ", "_").lower()
                    
                    ruta_destino = os.path.join(settings.MEDIA_ROOT, 'dataset_curado', industria, falla_limpia)
                    os.makedirs(ruta_destino, exist_ok=True)
                    
                    nombre_base = os.path.basename(urlparse(inspeccion.imagen).path)
                    if not nombre_base:
                        nombre_base = f"{uid}.jpg"
                        
                    ruta_img = os.path.join(ruta_destino, nombre_base)
                    ruta_txt = os.path.join(ruta_destino, nombre_base.replace('.jpg', '.txt'))
                    
                    res_img = requests.get(inspeccion.imagen, timeout=5)
                    if res_img.status_code == 200:
                        with open(ruta_img, 'wb') as f:
                            f.write(res_img.content)
                            
                        res_txt = requests.get(inspeccion.imagen.replace('.jpg', '.txt'), timeout=5)
                        if res_txt.status_code == 200:
                            with open(ruta_txt, 'wb') as f:
                                f.write(res_txt.content)
                                
                        validacion.curado_para_dataset = True
                        validacion.save()
                        
                except Exception as ex_mlops:
                    logger.error(f"Fallo en MLOps Curación para {uid}: {ex_mlops}")

            else:
                inspeccion.estado = 'Aprobado (Manual)'
                
            inspeccion.save()
            return JsonResponse({"status": "Auditoría guardada y dataset curado." if validacion.curado_para_dataset else "Auditoría guardada."})
            
        except Exception as e:
            return JsonResponse({"error": f"Error interno: {str(e)}"}, status=500)
            
    return JsonResponse({"error": "Método inválido"}, status=405)

@csrf_exempt
@login_required
def api_archivar_sesion(request):
    if request.method == 'POST':
        if request.user.is_superuser:
            RegistroInspeccion.objects.filter(archivado=False).update(archivado=True)
        elif request.user.empresa:
            RegistroInspeccion.objects.filter(empresa=request.user.empresa, archivado=False).update(archivado=True)
        return JsonResponse({"status": "Sesión archivada."})
    return JsonResponse({"error": "Método no permitido"}, status=405)


@login_required
def descargar_instalador(request, os_type):
    """ Generador temporal del instalador. En el siguiente paso lo conectaremos con tu compilado real """
    if os_type == 'windows':
        filename = "ScannorQA_Edge_Setup.exe"
        content = "ESTE ES UN ARCHIVO PLACEHOLDER SIMULANDO EL EJECUTABLE.\nEn el siguiente paso usaremos PyInstaller para compilar el motor y ponerlo aquí."
    else:
        filename = "install_scannor.sh"
        content = "#!/bin/bash\necho 'Instalando ScannorQA Edge en Linux...'\n"
        
    response = HttpResponse(content, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response