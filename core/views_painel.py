"""Painel administrativo do Cristiano.

O coração é o dashboard mensal: a RÉGUA (quem deveria mandar boleto e de
quanto) × o que chegou — para NUNCA esquecer um pagamento.
"""
from datetime import date
from functools import wraps

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import PostoForm, PrestadorForm, UsuarioForm, ValorBRField
from .models import (AuditLog, Boleto, Contrato, EmailLog, Posto, Prestador,
                     PrestadorPosto, UsuarioPermitido)
from .views import com_usuario


def admin_required(view):
    @wraps(view)
    @com_usuario
    def wrapper(request, up, *args, **kwargs):
        if not up.is_admin:
            return redirect('home')
        return view(request, up, *args, **kwargs)
    return wrapper


def _mes_param(request):
    try:
        return date.fromisoformat(request.GET.get('m', '') + '-01')
    except ValueError:
        return timezone.localdate().replace(day=1)


@admin_required
def dashboard(request, up):
    from .services.verificacao import competencia_extenso
    mes = _mes_param(request)
    ant = (mes.replace(day=1) - timezone.timedelta(days=1)).replace(day=1)
    prox = (mes.replace(day=28) + timezone.timedelta(days=6)).replace(day=1)

    boletos_mes = list(
        Boleto.objects.filter(competencia=mes)
        .exclude(status=Boleto.Status.SUBSTITUIDO)
        .select_related('prestador', 'posto', 'prestador__posto_cobranca'))

    linhas, casados = [], set()
    for prestador in (Prestador.objects.filter(ativo=True)
                      .prefetch_related('vinculos__posto')):
        for posto, valor in prestador.boletos_esperados():
            achado = None
            for b in boletos_mes:
                if b.prestador_id != prestador.pk:
                    continue
                if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
                    if b.posto_id is None:
                        achado = b
                        break
                elif posto and b.posto_id == posto.pk:
                    achado = b
                    break
            if achado:
                casados.add(achado.pk)
            linhas.append({'prestador': prestador, 'posto': posto,
                           'valor': valor, 'boleto': achado})

    fora_da_regua = [b for b in boletos_mes if b.pk not in casados]

    def peso(linha):  # pendências primeiro
        b = linha['boleto']
        if b is None:
            return 0
        ordem = {Boleto.Status.DIVERGENTE: 1, Boleto.Status.MANUAL: 1,
                 Boleto.Status.RECEBIDO: 2, Boleto.Status.APROVADO: 3,
                 Boleto.Status.PAGO: 4}
        return ordem.get(b.status, 2)
    linhas.sort(key=lambda l: (peso(l), l['prestador'].nome))

    resumo = {
        'faltando': sum(1 for l in linhas if l['boleto'] is None),
        'atencao': sum(1 for l in linhas if l['boleto'] and l['boleto'].status
                       in (Boleto.Status.DIVERGENTE, Boleto.Status.MANUAL)),
        'aguardando_pgto': sum(1 for l in linhas if l['boleto'] and
                               l['boleto'].status == Boleto.Status.APROVADO),
        'pagos': sum(1 for l in linhas if l['boleto'] and
                     l['boleto'].status == Boleto.Status.PAGO),
    }
    return render(request, 'painel/dashboard.html', {
        'mes': mes, 'mes_extenso': competencia_extenso(mes).capitalize(),
        'ant': ant, 'prox': prox, 'linhas': linhas,
        'fora_da_regua': fora_da_regua, 'resumo': resumo, 'up': up})


@admin_required
@require_POST
def boleto_acao(request, up, pk, acao):
    boleto = get_object_or_404(Boleto, pk=pk)
    if acao == 'pagar' and boleto.status in (Boleto.Status.APROVADO,
                                             Boleto.Status.MANUAL,
                                             Boleto.Status.DIVERGENTE):
        boleto.status = Boleto.Status.PAGO
        boleto.pago_em = timezone.now()
        boleto.save(update_fields=['status', 'pago_em'])
        messages.success(request, f'{boleto} marcado como PAGO.')
    elif acao == 'reprocessar':
        boleto.status = Boleto.Status.RECEBIDO
        boleto.tentativas = 0
        boleto.save(update_fields=['status', 'tentativas'])
        from .services.verificacao import fluxo_completo_async
        fluxo_completo_async(boleto.pk)
        messages.success(request, f'{boleto} voltou para verificação.')
    elif acao == 'aprovar' and boleto.status in (Boleto.Status.MANUAL,
                                                 Boleto.Status.DIVERGENTE,
                                                 Boleto.Status.RECEBIDO):
        # Aprovação manual: o Cristiano conferiu no olho → envia p/ pagamento.
        from django.conf import settings
        from .services import emails, frases
        from .services.verificacao import _fatos, _moeda
        boleto.status = Boleto.Status.APROVADO
        boleto.verificado_em = timezone.now()
        boleto.save(update_fields=['status', 'verificado_em'])
        fatos = _fatos(boleto)
        fatos['valor'] = _moeda(boleto.valor_esperado or boleto.valor_extraido)
        emails.enviar(
            settings.EMAIL_PAGADOR,
            f'Pagamento — {fatos["prestador"]} — {fatos["alvo"]} — '
            f'{fatos["competencia"]} — R$ {fatos["valor"]}',
            frases.corpo('aprovado_pagador', fatos),
            boleto=boleto, anexo_field=boleto.arquivo)
        messages.success(request, f'{boleto} aprovado e enviado p/ pagamento.')
    else:
        messages.error(request, 'Ação não permitida para este status.')
    AuditLog.registrar(AuditLog.Evento.STATUS, request,
                       detalhe=f'Ação "{acao}" no boleto #{pk}')
    return redirect(request.POST.get('voltar') or 'painel_dashboard')


# ---------------------------------------------------------------------------
# CRUDs
# ---------------------------------------------------------------------------
@admin_required
def prestadores(request, up):
    if request.method == 'POST':
        form = PrestadorForm(request.POST)
        if form.is_valid():
            p = form.save()
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Prestador criado: {p}')
            messages.success(request, f'{p.nome} criado. Agora defina os '
                                      'postos, valores e usuários.')
            return redirect('painel_prestador', pk=p.pk)
    else:
        form = PrestadorForm()
    lista = Prestador.objects.all().prefetch_related('vinculos__posto',
                                                     'usuarios')
    return render(request, 'painel/prestadores.html',
                  {'lista': lista, 'form': form, 'up': up})


@admin_required
def prestador_detalhe(request, up, pk):
    prestador = get_object_or_404(Prestador, pk=pk)
    postos = Posto.objects.filter(ativo=True)
    form = PrestadorForm(instance=prestador)

    if request.method == 'POST':
        qual = request.POST.get('qual')
        if qual == 'dados':
            form = PrestadorForm(request.POST, instance=prestador)
            if form.is_valid():
                form.save()
                messages.success(request, 'Dados salvos.')
                AuditLog.registrar(AuditLog.Evento.CRUD, request,
                                   detalhe=f'Prestador editado: {prestador}')
                return redirect('painel_prestador', pk=pk)
        elif qual == 'valores':
            campo = ValorBRField(required=False)
            with transaction.atomic():
                for posto in postos:
                    bruto = (request.POST.get(f'valor_{posto.pk}') or '').strip()
                    try:
                        valor = campo.clean(bruto) if bruto else None
                    except Exception:
                        messages.error(request,
                                       f'Valor inválido em {posto.nome}.')
                        return redirect('painel_prestador', pk=pk)
                    vinculo = PrestadorPosto.objects.filter(
                        prestador=prestador, posto=posto).first()
                    if valor is None:
                        if vinculo:
                            vinculo.ativo = False
                            vinculo.save(update_fields=['ativo'])
                    elif vinculo:
                        vinculo.valor_mensal = valor
                        vinculo.ativo = True
                        vinculo.save()
                    else:
                        PrestadorPosto.objects.create(
                            prestador=prestador, posto=posto,
                            valor_mensal=valor)
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Valores de {prestador} atualizados')
            messages.success(request, 'Postos e valores salvos.')
            return redirect('painel_prestador', pk=pk)

    vinculos = {v.posto_id: v for v in
                PrestadorPosto.objects.filter(prestador=prestador, ativo=True)}
    linhas_postos = [{'posto': p, 'vinculo': vinculos.get(p.pk)}
                     for p in postos]
    contratos = Contrato.objects.filter(prestador=prestador) \
        .select_related('posto')
    return render(request, 'painel/prestador_form.html', {
        'prestador': prestador, 'form': form, 'linhas_postos': linhas_postos,
        'contratos': contratos, 'usuarios': prestador.usuarios.all(),
        'up': up})


@admin_required
def postos(request, up):
    if request.method == 'POST':
        pk = request.POST.get('pk')
        instancia = get_object_or_404(Posto, pk=pk) if pk else None
        form = PostoForm(request.POST, instance=instancia)
        if form.is_valid():
            p = form.save()
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Posto salvo: {p} (ativo={p.ativo})')
            messages.success(request, f'Posto "{p.nome}" salvo.')
            return redirect('painel_postos')
    else:
        form = PostoForm()
    return render(request, 'painel/postos.html',
                  {'lista': Posto.objects.all(), 'form': form, 'up': up})


@admin_required
def usuarios(request, up):
    if request.method == 'POST':
        pk = request.POST.get('pk')
        instancia = get_object_or_404(UsuarioPermitido, pk=pk) if pk else None
        form = UsuarioForm(request.POST, instance=instancia)
        if form.is_valid():
            u = form.save()
            AuditLog.registrar(
                AuditLog.Evento.CRUD, request,
                detalhe=f'Usuário salvo: {u.email} '
                        f'(admin={u.is_admin}, ativo={u.ativo})')
            messages.success(request, f'{u.email} salvo.')
            return redirect('painel_usuarios')
    else:
        form = UsuarioForm()
    lista = UsuarioPermitido.objects.select_related('prestador')
    return render(request, 'painel/usuarios.html',
                  {'lista': lista, 'form': form, 'up': up})


@admin_required
def emails_log(request, up):
    return render(request, 'painel/emails.html',
                  {'lista': EmailLog.objects.select_related('boleto')[:150],
                   'up': up})


@admin_required
def auditoria(request, up):
    return render(request, 'painel/auditoria.html',
                  {'lista': AuditLog.objects.all()[:300], 'up': up})
