from django.urls import include, path, re_path
from mozilla_django_oidc.views import (
    OIDCAuthenticationCallbackView,
    OIDCAuthenticationRequestView,
    OIDCLogoutView,
)

# O callback fica em /auth/callback — bate com o redirect_uri registrado
# no IDCamim (https://pj.camim.com.br/auth/callback).
urlpatterns = [
    path('oidc/authenticate/', OIDCAuthenticationRequestView.as_view(),
         name='oidc_authentication_init'),
    path('oidc/logout/', OIDCLogoutView.as_view(), name='oidc_logout'),
    re_path(r'^auth/callback/?$', OIDCAuthenticationCallbackView.as_view(),
            name='oidc_authentication_callback'),
    path('', include('core.urls')),
]
