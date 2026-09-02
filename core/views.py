"""Views do lado PJ — a interface 'idiota de tão simples':
dois botões gigantes (ANEXAR BOLETO / CONTRATOS) e pronto."""
from functools import wraps

from datetime import date

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.utils import timezone
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
    # Últimos 60 dias por padrão (cabe o mês inteiro de quem manda um
    # boleto por posto); "?todos=1" lista tudo.
    todos = request.GET.get('todos') == '1'
    qs = (Boleto.objects.filter(prestador=up.prestador)
          .exclude(status=Boleto.Status.SUBSTITUIDO)
          .select_related('posto', 'prestador__posto_cobranca')
          .order_by('-criado_em'))
    total = qs.count()
    if not todos:
        limite = timezone.now() - timezone.timedelta(days=60)
        qs = qs.filter(criado_em__gte=limite)
    boletos = list(qs)
    return render(request, 'home.html', {
        'up': up, 'boletos': boletos, 'todos': todos,
        'ocultos': total - len(boletos)})


def _postos_do_prestador(prestador):
    if prestador.modo_boleto != Prestador.ModoBoleto.POR_POSTO:
        return []
    return [v.posto for v in prestador.vinculos_ativos()
            .select_related('posto').order_by('posto__nome')]


@prestador_required
def anexar_boleto(request, up):
    """Quem tem vários postos vê um QUADRADO por posto (com/sem boleto no
    mês); toca no posto → formulário só daquele posto → volta aos
    quadrados. Boleto único ou um posto só: formulário direto."""
    prestador = up.prestador
    postos = _postos_do_prestador(prestador)
    varios = len(postos) > 1
    posto_fixo = None
    if varios:
        pk = request.GET.get('posto') or request.POST.get('posto')
        posto_fixo = next((p for p in postos if str(p.pk) == str(pk)), None)
        if posto_fixo is None and request.method != 'POST':
            mes = date.today().replace(day=1)
            boletos = (Boleto.objects.filter(prestador=prestador,
                                             competencia=mes)
                       .exclude(status__in=[Boleto.Status.SUBSTITUIDO,
                                            Boleto.Status.DESCARTADO])
                       .order_by('criado_em'))
            por_posto = {}
            for b in boletos:
                por_posto.setdefault(b.posto_id, []).append(b)
            cartoes = [{'posto': p, 'boletos': por_posto.get(p.pk, [])}
                       for p in postos]
            feitos = sum(1 for c in cartoes if c['boletos'])
            from .services.verificacao import competencia_extenso
            return render(request, 'boleto_postos.html', {
                'up': up, 'cartoes': cartoes, 'feitos': feitos,
                'mes_extenso': competencia_extenso(mes).capitalize()})
    if request.method == 'POST':
        form = BoletoForm(prestador, request.POST, request.FILES)
        if form.is_valid():
            from .services import boletos as svc_boletos
            arq = form.cleaned_data['arquivo']
            nf = form.cleaned_data.get('nota_fiscal')
            boleto = svc_boletos.registrar(
                prestador, form.cleaned_data['competencia'],
                enviado_por=up.email, posto=form.cleaned_data.get('posto'),
                arquivo=arq, nome_original=arq.name,
                nota_fiscal=nf, nota_fiscal_nome=nf.name if nf else '')
            AuditLog.registrar(AuditLog.Evento.UPLOAD_BOLETO, request,
                               detalhe=f'Boleto #{boleto.pk} {boleto}')

            from .services.verificacao import fluxo_completo_async
            fluxo_completo_async(boleto.pk)

            messages.success(
                request,
                f'Boleto{" de " + boleto.posto.nome if boleto.posto else ""}'
                ' recebido! Você vai receber um e-mail de confirmação '
                'e outro assim que a verificação terminar.')
            return redirect('anexar_boleto' if varios else 'home')
    else:
        form = BoletoForm(prestador)
    if posto_fixo is not None:
        form.fields['posto'].initial = posto_fixo
        form.fields['posto'].widget = forms.HiddenInput()
    return render(request, 'boleto_form.html',
                  {'form': form, 'up': up, 'posto_fixo': posto_fixo,
                   'varios': varios})


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
    modelo = {'boleto': Boleto, 'contrato': Contrato, 'nf': Boleto}.get(tipo)
    if modelo is None:
        raise Http404
    obj = get_object_or_404(modelo, pk=pk)
    campo = obj.nota_fiscal if tipo == 'nf' else obj.arquivo
    if not campo:
        raise Http404
    if not _pode_ver(up, obj.prestador_id):
        AuditLog.registrar(AuditLog.Evento.DOWNLOAD_NEGADO, request,
                           detalhe=f'{tipo} #{pk}')
        raise Http404
    AuditLog.registrar(AuditLog.Evento.DOWNLOAD, request,
                       detalhe=f'{tipo} #{pk}')
    nome = ((obj.nota_fiscal_nome if tipo == 'nf' else obj.nome_original)
            or campo.name.rsplit('/', 1)[-1])
    # ?inline=1 → renderiza no navegador (modal); sem ele, baixa.
    inline = request.GET.get('inline') == '1'
    return FileResponse(campo.open('rb'),
                        as_attachment=not inline, filename=nome)
