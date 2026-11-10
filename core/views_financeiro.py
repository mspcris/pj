"""Painel do FINANCEIRO (03/11/2026): a equipe que paga vê só o que já foi
aprovado pelo admin — um card por prestador; clicou, abre um modal só
daquele prestador. Lá eles dão ciência ("recebi o boleto"), registram o
envio para pagamento (24h depois o boleto vira PAGO sozinho, pelo cron) e
podem desligar o e-mail de pagamento daquele prestador, passando a
controlar por aqui. Nada de editar, devolver, não reconhecer: isso é de
antes da aprovação e fica com o admin.
"""
import io
import re
import unicodedata
import zipfile
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import AuditLog, Boleto, Contrato, Prestador
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

# Os 4 pontos do kanban (03/11/2026): "só de olhar para o quadrado de cada
# empresa, saberei em que ponto está". Cada ponto tem a dica (hint).
ETAPAS = [
    ('anexado', '1. Boleto anexado pelo prestador'),
    ('validado', '2. Contrato e boleto validados pelo sistema (aprovado)'),
    ('recebido', '3. Financeiro deu ciência do recebimento'),
    ('pagamento', '4. Financeiro enviou para pagamento (PAGO em 24h)'),
]


def etapas_do_boleto(b):
    """[True/False] por etapa, para um boleto do painel do financeiro."""
    pago = b.status == Boleto.Status.PAGO
    recebido = pago or b.status == Boleto.Status.FIN_RECEBIDO \
        or b.fin_recebido_em is not None
    enviado = pago or b.fin_pagamento_em is not None
    return [True,
            b.status in (*ABERTOS, Boleto.Status.PAGO),
            recebido, enviado]


def contratos_por_posto(prestador):
    """Modal "Contratos" do financeiro (10/11/2026): TODOS os postos do
    prestador (vínculos ativos) + postos que só têm contrato + o contrato
    geral (sem posto), cada um com seus contratos ativos (desativado é só
    do admin). Posto sem contrato aparece mesmo assim, avisando."""
    contratos = (Contrato.objects
                 .filter(prestador=prestador, desativado_em__isnull=True)
                 .select_related('posto')
                 .order_by('-vigencia_inicio', '-pk'))
    por_posto = {}
    for v in prestador.vinculos.filter(ativo=True).select_related('posto'):
        por_posto.setdefault(v.posto_id, {'posto': v.posto, 'contratos': []})
    geral = []
    for c in contratos:
        if c.posto_id is None:
            geral.append(c)
        else:
            por_posto.setdefault(c.posto_id,
                                 {'posto': c.posto, 'contratos': []})
            por_posto[c.posto_id]['contratos'].append(c)
    grupos = sorted(por_posto.values(), key=lambda g: g['posto'].nome)
    if geral:
        grupos.append({'posto': None, 'contratos': geral})
    return grupos


def pontos(feitos_por_boleto):
    """Pontos do CARD (agregado): por etapa, {rotulo, feitos, total,
    classe}: verde = todos, âmbar = alguns, cinza = nenhum."""
    total = len(feitos_por_boleto)
    saida = []
    for i, (chave, rotulo) in enumerate(ETAPAS):
        n = sum(1 for f in feitos_por_boleto if f[i])
        classe = 'ok' if n == total and total else ('meio' if n else 'nao')
        saida.append({'chave': chave, 'rotulo': rotulo, 'feitos': n,
                      'total': total, 'classe': classe})
    return saida


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
        b.etapas = [{'rotulo': r, 'ok': ok} for (_, r), ok
                    in zip(ETAPAS, etapas_do_boleto(b))]
        g = grupos.setdefault(b.prestador_id, {
            'prestador': b.prestador, 'boletos': [], 'abertos': 0,
            'pagos': 0, 'total_aberto': Decimal('0'),
            'a_receber': 0,
            # Data prevista de pagamento do contrato, neste mês (10/11/2026:
            # ia só no e-mail; o financeiro passa a ver aqui).
            'pagamento_previsto': b.prestador.pagamento_previsto_em(mes)})
        g['boletos'].append(b)
        if b.status == Boleto.Status.PAGO:
            g['pagos'] += 1
        else:
            g['abertos'] += 1
            g['total_aberto'] += b.valor_extraido or b.valor_esperado or 0
            if b.status == Boleto.Status.APROVADO:
                g['a_receber'] += 1
    for g in grupos.values():
        g['pontos'] = pontos([etapas_do_boleto(b) for b in g['boletos']])
        g['contratos'] = contratos_por_posto(g['prestador'])
        g['n_contratos'] = sum(len(x['contratos']) for x in g['contratos'])
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
    elif acao == 'recusar':
        # 10/11/2026: boleto fora da data prevista de pagamento — o
        # financeiro recusa e o prestador recebe o texto (IA ou à mão).
        from .services.verificacao import recusar_pelo_financeiro
        texto = (request.POST.get('texto') or '').strip()
        if len(texto) < 20:
            messages.error(request, f'{boleto}: escreva (ou gere) o texto '
                                    'do aviso ao prestador antes de recusar.')
        elif recusar_pelo_financeiro(boleto, texto, quem=up.email):
            messages.success(
                request, f'{boleto}: recusado — o prestador foi avisado por '
                         'e-mail e o posto voltou a "sem boleto".')
        else:
            messages.info(request, f'{boleto}: não está aberto para recusa.')
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
def texto_recusa(request, up, pk):
    """Botão "Gerar texto por IA" da recusa: devolve o texto do aviso ao
    prestador (IA; modelo pronto se a IA falhar) para o financeiro ler,
    ajustar e enviar."""
    from .services.verificacao import texto_recusa_financeiro
    boleto = get_object_or_404(
        Boleto.objects.select_related('prestador', 'posto',
                                      'prestador__posto_cobranca'), pk=pk)
    observacao = (request.POST.get('observacao') or '').strip()[:500]
    texto, da_ia = texto_recusa_financeiro(boleto, observacao)
    return JsonResponse({'texto': texto, 'ia': da_ia})


def _slug(texto):
    s = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore')
    s = re.sub(r'[^a-z0-9]+', '_', s.decode().lower()).strip('_')
    return s or 'x'


def nome_arquivo_boleto(boleto):
    """'caio_realengo.pdf', 'desenlike_anchieta.pdf': PRIMEIRO nome do
    prestador + nome do POSTO (não a razão social) — pedido de 03/11/2026
    para o financeiro baixar tudo de uma vez."""
    primeiro = (boleto.prestador.nome or '').split()[0] if \
        (boleto.prestador.nome or '').split() else 'prestador'
    posto = boleto.posto_efetivo
    return f'{_slug(primeiro)}_{_slug(posto.nome if posto else "unico")}.pdf'


@financeiro_required
def baixar_todos(request, up, pk):
    """ZIP com os PDFs dos boletos do prestador no mês, renomeados."""
    prestador = get_object_or_404(Prestador, pk=pk)
    mes = _mes(request)
    boletos = (Boleto.objects
               .filter(prestador=prestador, competencia=mes,
                       status__in=[*ABERTOS, Boleto.Status.PAGO])
               .exclude(arquivo='')
               .select_related('prestador', 'posto',
                               'prestador__posto_cobranca')
               .order_by('posto__nome', 'pk'))
    buf = io.BytesIO()
    usados = {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for b in boletos:
            nome = nome_arquivo_boleto(b)
            n = usados.get(nome, 0) + 1
            usados[nome] = n
            if n > 1:
                nome = nome[:-4] + f'_{n}.pdf'
            with b.arquivo.open('rb') as fh:
                z.writestr(nome, fh.read())
    AuditLog.registrar(
        AuditLog.Evento.DOWNLOAD, request, ator=up.email,
        detalhe=f'Financeiro baixou ZIP com {len(usados)} boleto(s) de '
                f'{prestador} — {mes:%m/%Y}')
    resp = HttpResponse(buf.getvalue(), content_type='application/zip')
    primeiro = _slug((prestador.nome or 'prestador').split()[0])
    resp['Content-Disposition'] = (
        f'attachment; filename="boletos_{primeiro}_{mes:%Y-%m}.zip"')
    return resp


@financeiro_required
@require_POST
def email_toggle(request, up, pk):
    prestador = get_object_or_404(Prestador, pk=pk)
    prestador.fin_sem_email = request.POST.get('sem_email') == 'on'
    prestador.fin_email_agrupado = request.POST.get('agrupado') == 'on'
    prestador.save(update_fields=['fin_sem_email', 'fin_email_agrupado'])
    if prestador.fin_sem_email:
        modo = 'sem e-mail de pagamento — controle pelo painel'
        msg = ('você não receberá mais e-mail de pagamento deste '
               'prestador — acompanhe por aqui.')
    elif prestador.fin_email_agrupado:
        modo = 'E-MAIL AGRUPADO (um e-mail a cada 2h com todos os boletos)'
        msg = ('os boletos deste prestador virão num e-mail agrupado, a '
               'cada 2 horas, junto com os dos outros prestadores nessa '
               'opção.')
    else:
        modo = 'um e-mail por boleto (padrão)'
        msg = 'e-mails de pagamento religados, um por boleto.'
    AuditLog.registrar(
        AuditLog.Evento.CRUD, request, ator=up.email,
        detalhe=f'Financeiro: e-mail de pagamento de {prestador}: {modo}')
    messages.success(request, f'{prestador}: {msg}')
    return _voltar(request, prestador_pk=prestador.pk)
