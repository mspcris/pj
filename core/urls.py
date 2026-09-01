from django.urls import path

from . import views, views_painel

urlpatterns = [
    path('', views.home, name='home'),
    path('sem-acesso/', views.sem_acesso, name='sem_acesso'),
    path('boleto/', views.anexar_boleto, name='anexar_boleto'),
    path('contratos/', views.contratos_postos, name='contratos_postos'),
    path('contratos/<int:posto_id>/', views.contratos_lista,
         name='contratos_lista'),
    path('arquivo/<str:tipo>/<int:pk>/', views.baixar_arquivo,
         name='baixar_arquivo'),

    path('painel/', views_painel.dashboard, name='painel_dashboard'),
    path('painel/boleto/<int:pk>/<str:acao>/', views_painel.boleto_acao,
         name='painel_boleto_acao'),
    path('painel/prestadores/', views_painel.prestadores,
         name='painel_prestadores'),
    path('painel/prestadores/<int:pk>/', views_painel.prestador_detalhe,
         name='painel_prestador'),
    path('painel/postos/', views_painel.postos, name='painel_postos'),
    path('painel/usuarios/', views_painel.usuarios, name='painel_usuarios'),
    path('painel/emails/', views_painel.emails_log, name='painel_emails'),
    path('painel/auditoria/', views_painel.auditoria, name='painel_auditoria'),
]
