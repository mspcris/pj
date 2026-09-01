"""
Controle dos PJs — pj.camim.com.br

Portal onde os prestadores PJ anexam boletos mensais e contratos, e o
Cristiano controla o que falta pagar. Login via IDCamim (OIDC) com whitelist
em core.UsuarioPermitido — quem não está cadastrado NÃO entra.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
load_dotenv('/opt/pj/.env')

SECRET_KEY = os.getenv('SECRET_KEY', 'dev-insecure-troque-no-env')
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'

ALLOWED_HOSTS = [h for h in os.getenv(
    'ALLOWED_HOSTS', 'pj.camim.com.br,localhost,127.0.0.1').split(',') if h]
CSRF_TRUSTED_ORIGINS = ['https://pj.camim.com.br']
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'mozilla_django_oidc',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'pj.urls'
WSGI_APPLICATION = 'pj.wsgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'core.context.usuario_pj',
    ]},
}]

DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': os.getenv('SQLITE_PATH', str(BASE_DIR / 'db.sqlite3')),
}}

LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# Arquivos (boletos/contratos) NUNCA são servidos direto pelo nginx —
# só pela view core.views.baixar_arquivo, que checa a permissão.
MEDIA_ROOT = Path(os.getenv('MEDIA_ROOT', str(BASE_DIR / 'media')))
MEDIA_URL = '/media-interno/'  # não roteado publicamente

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# -----------------------------------------------------------------------------
# Login IDCamim (OIDC) + whitelist — mesmo padrão do painel da intranet
# -----------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = ['core.auth.PJOIDCBackend']

LOGIN_URL = '/oidc/authenticate/'
LOGIN_REDIRECT_URL = '/'
LOGIN_REDIRECT_URL_FAILURE = '/sem-acesso/'
LOGOUT_REDIRECT_URL = '/'

# Cristiano nunca fica trancado para fora, mesmo sem linha na whitelist.
SUPERADMIN_PROTEGIDO = os.getenv('SUPERADMIN_PROTEGIDO', 'cristiano@camim.com.br')

OIDC_RP_CLIENT_ID = os.getenv('IDCAMIM_CLIENT_ID', '')
OIDC_RP_CLIENT_SECRET = os.getenv('IDCAMIM_CLIENT_SECRET', '')
OIDC_RP_SIGN_ALGO = 'ES256'  # IDCamim assina ID tokens com EC P-256
OIDC_RP_SCOPES = 'openid email profile'
OIDC_USE_NONCE = True
OIDC_CREATE_USER = True
OIDC_STORE_ACCESS_TOKEN = False
OIDC_STORE_ID_TOKEN = False
OIDC_AUTHENTICATION_CALLBACK_URL = 'oidc_authentication_callback'

# Discovery UMA vez no boot (mesma abordagem da intranet).
_DISC_URL = os.getenv('IDCAMIM_DISCOVERY_URL', '')
OIDC_OP_AUTHORIZATION_ENDPOINT = ''
OIDC_OP_TOKEN_ENDPOINT = ''
OIDC_OP_USER_ENDPOINT = ''
OIDC_OP_JWKS_ENDPOINT = ''
OIDC_OP_LOGOUT_ENDPOINT = ''
if _DISC_URL and OIDC_RP_CLIENT_ID:
    try:
        import requests as _r
        _d = _r.get(_DISC_URL, timeout=5).json()
        OIDC_OP_AUTHORIZATION_ENDPOINT = _d.get('authorization_endpoint', '')
        OIDC_OP_TOKEN_ENDPOINT = _d.get('token_endpoint', '')
        OIDC_OP_USER_ENDPOINT = _d.get('userinfo_endpoint', '')
        OIDC_OP_JWKS_ENDPOINT = _d.get('jwks_uri', '')
        OIDC_OP_LOGOUT_ENDPOINT = _d.get('end_session_endpoint', '')
    except Exception as _e:
        print(f'[settings] WARN: discovery IDCamim falhou ({_DISC_URL}): {_e}',
              file=sys.stderr)

# -----------------------------------------------------------------------------
# E-mail — Gmail do Cristiano (mesmo app password do robô PJ do relatorio_h_t)
# -----------------------------------------------------------------------------
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '465'))
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'true').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', 'cristiano@camim.com.br')
EMAIL_HOST_PASSWORD = (os.getenv('EMAIL_HOST_PASSWORD') or '').replace(' ', '')
# Remetente padrão (e-mails para os PJs): o alias pj@camim.com.br.
DEFAULT_FROM_EMAIL = os.getenv(
    'DEFAULT_FROM_EMAIL', 'Cristiano Camim <pj@camim.com.br>')
# E-mail p/ o financeiro pagar sai do e-mail PESSOAL do Cristiano.
EMAIL_FROM_PAGADOR = os.getenv(
    'EMAIL_FROM_PAGADOR', 'Cristiano Camim <cristiano@camim.com.br>')

EMAIL_PAGADOR = os.getenv('EMAIL_PAGADOR', 'equipe@camim.com.br')
# Caixas que recebem boletos por e-mail (robô importar_emails_pj).
# prestadores@ é o endereço comunicado oficialmente aos PJs em 21/08/2026;
# pj@ fica junto porque também está em uso. Ambos caem na caixa do Cristiano.
EMAIL_INTAKE_ALIASES = [
    a.strip().lower() for a in os.getenv(
        'EMAIL_INTAKE_ALIASES',
        'prestadores@camim.com.br,pj@camim.com.br').split(',') if a.strip()]
IMAP_HOST = os.getenv('IMAP_HOST', 'imap.gmail.com')
IMAP_DIAS = int(os.getenv('IMAP_DIAS', '10'))
EMAIL_ADMIN = os.getenv('EMAIL_ADMIN', 'cristiano@camim.com.br')
# Modo de teste: TODO e-mail sai só para EMAIL_ADMIN (nada chega no PJ/equipe).
EMAIL_MODO_TESTE = os.getenv('EMAIL_MODO_TESTE', 'false').lower() == 'true'

# -----------------------------------------------------------------------------
# IA — Groq (mesma chave "kpis" do relatorio_h_t de produção)
# -----------------------------------------------------------------------------
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')

# Upload
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 2592000  # 30 dias; site é só HTTPS (certbot)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}
