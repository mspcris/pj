from django.urls import path

from . import api, views, views_painel

urlpatterns = [
    path('', views.home, name='home'),
    path('sem-acesso/', views.sem_acesso, name='sem_acesso'),
    path('boleto/', views.anexar_boleto, name='anexar_boleto'),
    path('contratos/', views.contratos_postos, name='contratos_postos'),
    path('contratos/<int:posto_id>/', views.contratos_lista,
         name='contratos_lista'),
    path('arquivo/<str:tipo>/<int:pk>/', views.baixar_arquivo,
         name='baixar_arquivo'),

    path('sair-ver-como/', views.sair_ver_como, name='sair_ver_como'),
    path('api/boletos/', api.boletos, name='api_boletos'),

    path('painel/', views_painel.dashboard, name='painel_dashboard'),
    path('painel/boleto/novo/', views_painel.boleto_novo,
         name='painel_boleto_novo'),
    path('painel/boleto/<int:pk>/editar/', views_painel.boleto_editar,
         name='painel_boleto_editar'),
    path('painel/ver-como/<int:pk>/', views_painel.ver_como,
         name='painel_ver_como'),
    path('painel/boleto/<int:pk>/<str:acao>/', views_painel.boleto_acao,
         name='painel_boleto_acao'),
    path('painel/prestadores/', views_painel.prestadores,
         name='painel_prestadores'),
    path('painel/prestadores/<int:pk>/', views_painel.prestador_detalhe,
         name='painel_prestador'),
    path('painel/prestadores/<int:pk>/excluir/',
         views_painel.prestador_excluir, name='painel_prestador_excluir'),
    path('painel/prestadores/<int:pk>/restaurar/',
         views_painel.prestador_restaurar, name='painel_prestador_restaurar'),
    path('painel/postos/', views_painel.postos, name='painel_postos'),
    path('painel/usuarios/', views_painel.usuarios, name='painel_usuarios'),
    path('painel/gerentes/', views_painel.gerentes, name='painel_gerentes'),
    path('painel/config/', views_painel.configuracoes, name='painel_config'),
    path('painel/emails/', views_painel.emails_log, name='painel_emails'),
    path('painel/auditoria/', views_painel.auditoria, name='painel_auditoria'),
]
