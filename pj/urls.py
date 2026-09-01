from django.urls import include, path, re_path
from django.views.generic import RedirectView
from mozilla_django_oidc.views import (
    OIDCAuthenticationCallbackView,
    OIDCAuthenticationRequestView,
    OIDCLogoutView,
)

# O callback fica em /auth/callback — bate com o redirect_uri registrado
# no IDCamim (https://pj.camim.com.br/auth/callback).
urlpatterns = [
    # Navegadores pedem /favicon.ico por conta própria
    path('favicon.ico', RedirectView.as_view(url='/static/favicon.svg',
                                             permanent=True)),
    path('oidc/authenticate/', OIDCAuthenticationRequestView.as_view(),
         name='oidc_authentication_init'),
    path('oidc/logout/', OIDCLogoutView.as_view(), name='oidc_logout'),
    re_path(r'^auth/callback/?$', OIDCAuthenticationCallbackView.as_view(),
            name='oidc_authentication_callback'),
    path('', include('core.urls')),
]
