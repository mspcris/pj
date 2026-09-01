"""Views do lado PJ — a interface 'idiota de tão simples':
dois botões gigantes (ANEXAR BOLETO / CONTRATOS) e pronto."""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import BoletoForm, ContratoForm
from .models import AuditLog, Boleto, Contrato, Posto, Prestador, UsuarioPermitido


def _usuario_real(request):
    """Whitelist SEM o disfarce do modo 'ver como'."""
    if not request.user.is_authenticated:
        return None
    return (UsuarioPermitido.objects
            .filter(email=request.user.email.lower(), ativo=True)
            .select_related('prestador').first())


def _usuario(request):
    up = _usuario_real(request)
    # Modo "ver como": o admin enxerga o portal exatamente como o PJ vê.
    # Instância virtual (não salva), só para esta requisição.
    pk = request.session.get('ver_como')
    if up is not None and up.is_admin and pk:
        prestador = Prestador.objects.filter(pk=pk, ativo=True).first()
        if prestador is not None:
            return UsuarioPermitido(email=up.email, nome=up.nome,
                                    prestador=prestador, is_admin=False,
                                    ativo=True)
    return up


@login_required
def sair_ver_como(request):
    request.session.pop('ver_como', None)
    return redirect('painel_dashboard')


def com_usuario(view):
    """Exige login + linha ativa na whitelist; injeta `up` na view."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        up = _usuario(request)
        if up is None:
            return redirect('sem_acesso')
        return view(request, up, *args, **kwargs)
    return wrapper


def prestador_required(view):
    @wraps(view)
    @com_usuario
    def wrapper(request, up, *args, **kwargs):
        if up.prestador is None or not up.prestador.ativo:
            if up.is_admin:
                return redirect('painel_dashboard')
            return redirect('sem_acesso')
        return view(request, up, *args, **kwargs)
    return wrapper


def sem_acesso(request):
    return render(request, 'sem_acesso.html', status=403)


@com_usuario
def home(request, up):
    if up.prestador is None and up.is_admin:
        return redirect('painel_dashboard')
    boletos = (Boleto.objects.filter(prestador=up.prestador)
               .exclude(status=Boleto.Status.SUBSTITUIDO)
               .select_related('posto', 'prestador__posto_cobranca')[:6])
    return render(request, 'home.html', {'up': up, 'boletos': boletos})


@prestador_required
def anexar_boleto(request, up):
    prestador = up.prestador
    if request.method == 'POST':
        form = BoletoForm(prestador, request.POST, request.FILES)
        if form.is_valid():
            from .services import boletos as svc_boletos
            arq = form.cleaned_data['arquivo']
            boleto = svc_boletos.registrar(
                prestador, form.cleaned_data['competencia'],
                enviado_por=up.email, posto=form.cleaned_data.get('posto'),
                arquivo=arq, nome_original=arq.name)
            AuditLog.registrar(AuditLog.Evento.UPLOAD_BOLETO, request,
                               detalhe=f'Boleto #{boleto.pk} {boleto}')

            from .services.verificacao import fluxo_completo_async
            fluxo_completo_async(boleto.pk)

            messages.success(
                request,
                'Boleto recebido! Você vai receber um e-mail de confirmação '
                'e outro assim que a verificação terminar.')
            return redirect('home')
    else:
        form = BoletoForm(prestador)
    return render(request, 'boleto_form.html', {'form': form, 'up': up})


@prestador_required
def contratos_postos(request, up):
    postos = Posto.objects.filter(
        vinculos__prestador=up.prestador, vinculos__ativo=True,
        ativo=True).distinct()
    if postos.count() == 1:
        return redirect('contratos_lista', posto_id=postos.first().pk)
    return render(request, 'contrato_postos.html',
                  {'postos': postos, 'up': up})


@prestador_required
def contratos_lista(request, up, posto_id):
    posto = get_object_or_404(
        Posto, pk=posto_id, vinculos__prestador=up.prestador)
    if request.method == 'POST':
        form = ContratoForm(request.POST, request.FILES)
        if form.is_valid():
            arq = form.cleaned_data['arquivo']
            Contrato.objects.create(
                prestador=up.prestador, posto=posto, arquivo=arq,
                nome_original=arq.name[:255], enviado_por=up.email,
                vigencia_inicio=form.cleaned_data['vigencia_inicio'],
                vigencia_fim=form.cleaned_data['vigencia_fim'])
            AuditLog.registrar(AuditLog.Evento.UPLOAD_CONTRATO, request,
                               detalhe=f'{up.prestador} @ {posto}')
            messages.success(request, 'Contrato anexado com sucesso!')
            return redirect('contratos_lista', posto_id=posto.pk)
    else:
        form = ContratoForm()
    from django.db.models import Q
    contratos = Contrato.objects.filter(
        Q(posto=posto) | Q(posto__isnull=True), prestador=up.prestador)
    return render(request, 'contrato_lista.html',
                  {'posto': posto, 'contratos': contratos,
                   'form': form, 'up': up})


# ---------------------------------------------------------------------------
# Download de arquivos — SEMPRE por aqui, nunca direto do nginx.
# PJ só baixa o que é do próprio prestador; admin baixa tudo.
# ---------------------------------------------------------------------------
def _pode_ver(up, prestador_id):
    return up.is_admin or (up.prestador_id == prestador_id)


@com_usuario
def baixar_arquivo(request, up, tipo, pk):
    modelo = {'boleto': Boleto, 'contrato': Contrato}.get(tipo)
    if modelo is None:
        raise Http404
    obj = get_object_or_404(modelo, pk=pk)
    if not obj.arquivo:
        raise Http404
    if not _pode_ver(up, obj.prestador_id):
        AuditLog.registrar(AuditLog.Evento.DOWNLOAD_NEGADO, request,
                           detalhe=f'{tipo} #{pk}')
        raise Http404
    AuditLog.registrar(AuditLog.Evento.DOWNLOAD, request,
                       detalhe=f'{tipo} #{pk}')
    nome = obj.nome_original or obj.arquivo.name.rsplit('/', 1)[-1]
    return FileResponse(obj.arquivo.open('rb'),
                        as_attachment=True, filename=nome)
