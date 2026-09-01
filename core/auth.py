"""Backend OIDC com whitelist: só entra quem tem UsuarioPermitido ativo.

Mesmo padrão do painel da intranet. Exceção única: o superadmin protegido
(cristiano@camim.com.br) nunca fica trancado para fora — se a linha não
existir, ela é criada na hora com is_admin=True.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from .models import AuditLog, UsuarioPermitido

log = logging.getLogger(__name__)
User = get_user_model()


def _email(claims):
    return (claims.get('email') or '').strip().lower()


class PJOIDCBackend(OIDCAuthenticationBackend):

    def _whitelist(self, email):
        if email == (settings.SUPERADMIN_PROTEGIDO or '').lower():
            up, _ = UsuarioPermitido.objects.get_or_create(
                email=email, defaults={'is_admin': True, 'nome': 'Cristiano'})
            if not (up.is_admin and up.ativo):
                up.is_admin = True
                up.ativo = True
                up.save(update_fields=['is_admin', 'ativo'])
            return up
        try:
            return UsuarioPermitido.objects.get(email=email, ativo=True)
        except UsuarioPermitido.DoesNotExist:
            return None

    def verify_claims(self, claims):
        email = _email(claims)
        if not email:
            AuditLog.registrar(AuditLog.Evento.LOGIN_NEGADO,
                               getattr(self, 'request', None),
                               detalhe='token OIDC sem email')
            return False
        up = self._whitelist(email)
        if up is None:
            AuditLog.registrar(AuditLog.Evento.LOGIN_NEGADO,
                               getattr(self, 'request', None), ator=email,
                               detalhe='fora da whitelist (UsuarioPermitido)')
            log.warning('Login negado (fora da whitelist): %s', email)
            return False
        return True

    def filter_users_by_claims(self, claims):
        email = _email(claims)
        return User.objects.filter(email__iexact=email)

    def create_user(self, claims):
        email = _email(claims)
        nome = claims.get('name') or claims.get('preferred_username') or ''
        first, _, last = (nome or '').partition(' ')
        user = User.objects.create_user(username=email, email=email,
                                        first_name=first[:30],
                                        last_name=last[:150])
        user.set_unusable_password()
        user.save()
        return user

    def update_user(self, user, claims):
        email = _email(claims)
        up = self._whitelist(email)
        if up is not None:
            up.ultimo_login = timezone.now()
            nome = claims.get('name') or ''
            if nome and not up.nome:
                up.nome = nome[:120]
            up.save(update_fields=['ultimo_login', 'nome'])
        AuditLog.registrar(AuditLog.Evento.LOGIN_OK,
                           getattr(self, 'request', None), ator=email)
        return user
