"""Views do lado PJ — a interface 'idiota de tão simples':
dois botões gigantes (ANEXAR BOLETO / CONTRATOS) e pronto."""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import BoletoForm, ContratoForm
from .models import AuditLog, Boleto, Contrato, Posto, Prestador, UsuarioPermitido


def _usuario(request):
    if not request.user.is_authenticated:
        return None
    return (UsuarioPermitido.objects
            .filter(email=request.user.email.lower(), ativo=True)
            .select_related('prestador').first())


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
            competencia = form.cleaned_data['competencia']
            posto = form.cleaned_data.get('posto')
            if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
                posto = None
                valor_esperado = prestador.valor_esperado_unico()
            else:
                vinculo = prestador.vinculos_ativos().filter(
                    posto=posto).first()
                valor_esperado = vinculo.valor_mensal if vinculo else None

            # Novo arquivo para a mesma competência substitui o anterior
            # ainda não pago (o PJ pode reenviar corrigido).
            (Boleto.objects
             .filter(prestador=prestador, posto=posto, competencia=competencia)
             .exclude(status__in=[Boleto.Status.PAGO,
                                  Boleto.Status.SUBSTITUIDO])
             .update(status=Boleto.Status.SUBSTITUIDO))

            arq = form.cleaned_data['arquivo']
            boleto = Boleto.objects.create(
                prestador=prestador, posto=posto, competencia=competencia,
                arquivo=arq, nome_original=arq.name[:255],
                enviado_por=up.email, valor_esperado=valor_esperado)
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
    contratos = Contrato.objects.filter(prestador=up.prestador, posto=posto)
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
    if not _pode_ver(up, obj.prestador_id):
        AuditLog.registrar(AuditLog.Evento.DOWNLOAD_NEGADO, request,
                           detalhe=f'{tipo} #{pk}')
        raise Http404
    AuditLog.registrar(AuditLog.Evento.DOWNLOAD, request,
                       detalhe=f'{tipo} #{pk}')
    nome = obj.nome_original or obj.arquivo.name.rsplit('/', 1)[-1]
    return FileResponse(obj.arquivo.open('rb'),
                        as_attachment=True, filename=nome)
