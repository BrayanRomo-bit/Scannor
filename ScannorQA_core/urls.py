"""
URL configuration for ScannorQA_core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views 
from Backend import views

urlpatterns = [
    path('admin/', admin.site.urls),

    path('', views.dashboard, name='dashboard'),
    path('personalizacion/', views.personalizacion, name='personalizacion'),

    path('sala-de-espera/', views.sala_espera, name='sala_espera'),
    path('api/guardar-falla/', views.recibir_falla_api, name='recibir_falla'),
    path('api/resultados/', views.api_obtener_resultados, name='api_obtener_resultados'),
    path('api/archivar-sesion/', views.api_archivar_sesion, name='api_archivar_sesion'), 
    path('login/', views.login_usuario, name='login'),
    path('logout/', views.cerrar_sesion, name='logout'),

    path('planta/', views.gestion_planta, name='gestion_planta'),
    path('planta/crear-operador/', views.crear_operador, name='crear_operador'),

    path('api/auditar/falso-positivo/<str:uid>/', views.registrar_falso_positivo, name='registrar_falso_positivo'),
    path('staff/empresas/', views.staff_empresas, name='staff_empresas'),
    path('staff/dispositivos/', views.staff_dispositivos, name='staff_dispositivos'),
    path('staff/usuarios/', views.staff_usuarios, name='staff_usuarios'),
    path('staff/empresa/toggle/<int:empresa_id>/', views.toggle_empresa, name='toggle_empresa'),
    path('staff/usuario/toggle/<int:usuario_id>/', views.toggle_usuario, name='toggle_usuario'),
    path('staff/dispositivo/descargar-env/<int:dispositivo_id>/', views.descargar_env_dispositivo, name='descargar_env_dispositivo'),
    path('planta/descargar-instalador/<str:os_type>/', views.descargar_instalador, name='descargar_instalador'),
    path('api/provisionar/', views.api_provisionar_dispositivo, name='api_provisionar_dispositivo'),

]