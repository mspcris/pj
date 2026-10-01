"""Login por e-mail + senha para PJs SEM idCamim (ex.: quem só tem @gmail).

Convive com o IDCamim: quem é da Camim continua entrando pelo SSO; os PJs
externos entram por senha. Os dois caminhos respeitam a MESMA whitelist
(UsuarioPermitido ativo) — senha certa sem linha ativa não entra em nada.

O primeiro acesso e o "esqueci a senha" usam o MESMO link: um token de uso
único gerado pelo Django (default_token_generator), que se invalida sozinho
assim que a senha é definida (o hash muda).
"""
import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .models import AuditLog, UsuarioPermitido

log = logging.getLogger(__name__)
_BACKEND = 'django.contrib.auth.backends.ModelBackend'
SENHA_MIN = 8


def _whitelist_ativa(email):
    return UsuarioPermitido.objects.filter(email=email, ativo=True).first()


def entrar(request):
    """Tela única de entrada: e-mail + senha (PJ externo) OU botão IDCamim."""
    if request.user.is_authenticated:
        return redirect('home')
    erro = ''
    email = ''
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        senha = request.POST.get('senha') or ''
        up = _whitelist_ativa(email)
        user = authenticate(request, username=email, password=senha)
        if user is not None and up is not None:
            auth_login(request, user, backend=_BACKEND)
            up.ultimo_login = timezone.now()
            up.save(update_fields=['ultimo_login'])
            AuditLog.registrar(AuditLog.Evento.LOGIN_OK, request, ator=email,
                               detalhe='login por senha')
            return redirect('home')
        AuditLog.registrar(AuditLog.Evento.LOGIN_NEGADO, request, ator=email,
                           detalhe='senha incorreta ou fora da whitelist')
        erro = ('E-mail ou senha incorretos. Se é seu primeiro acesso ou '
                'esqueceu a senha, use "Esqueci minha senha" abaixo.')
    return render(request, 'entrar.html', {'erro': erro, 'email': email})


def sair(request):
    """Logout simples (serve para senha e para IDCamim)."""
    auth_logout(request)
    return redirect('entrar')


def disparar_link_senha(request, email, nome=''):
    """Garante um usuário Django para o e-mail e manda o link de definir senha.
    Usado pelo 'esqueci a senha' E pelo botão do admin em Usuários."""
    User = get_user_model()
    user = User.objects.filter(username=email).first() \
        or User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create(username=email, email=email)
        user.set_unusable_password()
        user.save()
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = request.build_absolute_uri(
        reverse('definir_senha', args=[uid, token]))
    ola = f'Olá, {nome.split()[0]}!' if nome else 'Olá!'
    corpo = (
        f'{ola}\n\n'
        'Você está definindo a senha de acesso ao portal de prestadores da '
        'Camim (controle de boletos/pagamentos).\n\n'
        'Toque no link abaixo para criar sua senha (vale por tempo limitado '
        'e só pode ser usado uma vez):\n\n'
        f'{link}\n\n'
        'Depois, é só entrar com seu e-mail e a senha que você criar, em:\n'
        f'{request.build_absolute_uri(reverse("entrar"))}\n\n'
        'Se você não pediu isto, ignore este e-mail — nada muda.\n\n'
        '— Camim')
    send_mail('Acesso ao portal de PJs da Camim — defina sua senha',
              corpo, settings.DEFAULT_FROM_EMAIL, [email],
              fail_silently=False)
    AuditLog.registrar(AuditLog.Evento.STATUS, request, ator=email,
                       detalhe='link de definição de senha enviado')


def recuperar_senha(request):
    """Pede o e-mail e manda o link — sem revelar se o e-mail existe."""
    enviado = False
    erro = ''
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        up = _whitelist_ativa(email)
        if up is not None:
            try:
                disparar_link_senha(request, email, up.nome)
            except Exception as e:  # SMTP fora do ar, etc.
                log.error('Falha ao enviar link de senha para %s: %s',
                          email, e)
                erro = ('Não consegui enviar o e-mail agora. Tente de novo '
                        'em instantes ou fale com o Cristiano.')
        # Mensagem idêntica exista ou não o e-mail (anti-enumeração).
        enviado = not erro
    return render(request, 'recuperar_senha.html',
                  {'enviado': enviado, 'erro': erro})


def definir_senha(request, uidb64, token):
    """Valida o token do link e deixa a pessoa criar a senha."""
    User = get_user_model()
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    up = _whitelist_ativa(user.email.lower()) if user is not None else None
    valido = (user is not None and up is not None
              and default_token_generator.check_token(user, token))
    if not valido:
        return render(request, 'definir_senha.html', {'invalido': True})

    erro = ''
    if request.method == 'POST':
        s1 = request.POST.get('senha1') or ''
        s2 = request.POST.get('senha2') or ''
        if len(s1) < SENHA_MIN:
            erro = f'A senha precisa de pelo menos {SENHA_MIN} caracteres.'
        elif s1 != s2:
            erro = 'As duas senhas não conferem.'
        else:
            user.set_password(s1)
            user.is_active = True
            user.save()
            up.ultimo_login = timezone.now()
            up.save(update_fields=['ultimo_login'])
            auth_login(request, user, backend=_BACKEND)
            AuditLog.registrar(AuditLog.Evento.STATUS, request, ator=user.email,
                               detalhe='senha definida/redefinida')
            return redirect('home')
    return render(request, 'definir_senha.html',
                  {'valido': True, 'erro': erro, 'nome': up.nome})
