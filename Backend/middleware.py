# Backend/middleware.py
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from .models import Dispositivo

class SuspensionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A. Bloqueo de Hardware (Cámaras Edge)
        if request.path.startswith('/api/guardar-falla/'):
            auth_header = request.headers.get('X-Device-Auth', '')
            if auth_header:
                dispositivo = Dispositivo.objects.filter(api_key=auth_header).select_related('empresa').first()
                if dispositivo and (dispositivo.empresa.estado_plan == 'suspendido' or not dispositivo.empresa.activa):
                    return JsonResponse({"error": "Payment Required: Suscripción inactiva."}, status=402)

        # B. Bloqueo de Navegación (Operadores y Gerentes)
        if request.user.is_authenticated and not request.user.is_superuser:
            empresa = getattr(request.user, 'empresa', None)
            if empresa and (empresa.estado_plan == 'suspendido' or not empresa.activa):
                # Dejar pasar solo si van a cerrar sesión o a la sala de espera
                if request.path not in [reverse('logout'), reverse('sala_espera')]:
                    return redirect('sala_espera')

        return self.get_response(request)