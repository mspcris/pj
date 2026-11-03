"""Painel do FINANCEIRO (03/11/2026): a equipe que paga vê só o que já foi
aprovado pelo admin — um card por prestador; clicou, abre um modal só
daquele prestador. Lá eles dão ciência ("recebi o boleto"), registram o
envio para pagamento (24h depois o boleto vira PAGO sozinho, pelo cron) e
podem desligar o e-mail de pagamento daquele prestador, passando a
controlar por aqui. Nada de editar, devolver, não reconhecer: isso é de
antes da aprovação e fica com o admin.
"""
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import AuditLog, Boleto, Prestador
from .views import com_usuario


def financeiro_required(view):
    @wraps(view)
    @com_usuario
    def wrapper(request, up, *args, **kwargs):
        if not (up.is_financeiro or up.is_admin):
            return redirect('home')
        return view(request, up, *args, **kwargs)
    return wrapper


def _mes(request):
    from datetime import date
    try:
        return date.fromisoformat(request.GET.get('m', '') + '-01')
    except ValueError:
        return timezone.localdate().replace(day=1)


ABERTOS = (Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO)


@financeiro_required
def painel(request, up):
    mes = _mes(request)
    ant = (mes - timedelta(days=1)).replace(day=1)
    prox = (mes.replace(day=28) + timedelta(days=6)).replace(day=1)
    boletos = (Boleto.objects
               .filter(competencia=mes,
                       status__in=[*ABERTOS, Boleto.Status.PAGO])
               .select_related('prestador', 'posto',
                               'prestador__posto_cobranca')
               .order_by('prestador__nome', 'posto__nome', 'pk'))
    grupos = {}
    for b in boletos:
        b.pago_previsto = (b.fin_pagamento_em + timedelta(hours=24)
                           if b.fin_pagamento_em else None)
        g = grupos.setdefault(b.prestador_id, {
            'prestador': b.prestador, 'boletos': [], 'abertos': 0,
            'pagos': 0, 'total_aberto': Decimal('0'),
            'a_receber': 0})
        g['boletos'].append(b)
        if b.status == Boleto.Status.PAGO:
            g['pagos'] += 1
        else:
            g['abertos'] += 1
            g['total_aberto'] += b.valor_extraido or b.valor_esperado or 0
            if b.status == Boleto.Status.APROVADO:
                g['a_receber'] += 1
    grupos = sorted(grupos.values(),
                    key=lambda g: (g['abertos'] == 0, g['prestador'].nome))
    return render(request, 'financeiro/painel.html', {
        'up': up, 'mes': mes, 'ant': ant, 'prox': prox, 'grupos': grupos,
        'abrir': request.GET.get('abrir', ''),
        'total_abertos': sum(g['abertos'] for g in grupos),
        'total_a_receber': sum(g['a_receber'] for g in grupos),
    })


def _voltar(request, boleto=None, prestador_pk=None):
    m = request.POST.get('m') or ''
    pk = prestador_pk or (boleto.prestador_id if boleto else '')
    return redirect(f"/financeiro/?m={m}&abrir={pk}")


@financeiro_required
@require_POST
def acao(request, up, pk, acao):
    from .services.verificacao import (confirmar_fin_recebido,
                                       fin_enviou_para_pagamento)
    boleto = get_object_or_404(Boleto.objects.select_related('prestador'),
                               pk=pk)
    if acao == 'recebi':
        if confirmar_fin_recebido(boleto, ator=up.email,
                                  origem='painel do financeiro'):
            messages.success(request, f'{boleto}: recebimento registrado.')
        else:
            messages.info(request, f'{boleto}: já estava registrado.')
    elif acao == 'enviei':
        if fin_enviou_para_pagamento(boleto, ator=up.email):
            messages.success(
                request, f'{boleto}: enviado para pagamento. Vira PAGO '
                         'automaticamente em 24h.')
        else:
            messages.info(request, f'{boleto}: não está aberto para '
                                   'pagamento.')
    return _voltar(request, boleto)


@financeiro_required
@require_POST
def email_toggle(request, up, pk):
    prestador = get_object_or_404(Prestador, pk=pk)
    prestador.fin_sem_email = request.POST.get('sem_email') == 'on'
    prestador.save(update_fields=['fin_sem_email'])
    AuditLog.registrar(
        AuditLog.Evento.CRUD, request, ator=up.email,
        detalhe=f'Financeiro: e-mail de pagamento de {prestador} '
                + ('DESLIGADO — controle pelo painel' if prestador.fin_sem_email
                   else 'religado'))
    messages.success(
        request,
        f'{prestador}: ' + ('você não receberá mais e-mail de pagamento '
                            'deste prestador — acompanhe por aqui.'
                            if prestador.fin_sem_email else
                            'e-mails de pagamento religados.'))
    return _voltar(request, prestador_pk=prestador.pk)
