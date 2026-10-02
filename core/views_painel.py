"""Painel administrativo do Cristiano.

O coração é o dashboard mensal: a RÉGUA (quem deveria mandar boleto e de
quanto) × o que chegou — para NUNCA esquecer um pagamento.
"""
import re
from calendar import monthrange
from datetime import date
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (BoletoAdminForm, ContratoAdminForm, PostoForm,
                    PrestadorForm, UsuarioForm, ValeForm, ValorBRField)
from .models import (AjusteDiferenca, AuditLog, Boleto, Configuracao,
                     Contrato, EmailLog, EmailRecebido, Posto, Prestador,
                     PrestadorPosto, UsuarioPermitido, Vale)
from django.contrib.auth.decorators import login_required

from .views import _usuario_real, com_usuario


def admin_required(view):
    @wraps(view)
    @com_usuario
    def wrapper(request, up, *args, **kwargs):
        if not up.is_admin:
            return redirect('home')
        return view(request, up, *args, **kwargs)
    return wrapper


def admin_real_required(view):
    """Como admin_required, mas ignora o modo 'ver como' — para as ações que
    o admin precisa alcançar mesmo estando disfarçado de PJ (trocar de
    prestador no 'ver como', por exemplo)."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        up = _usuario_real(request)
        if up is None or not up.is_admin:
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
        .exclude(status__in=[Boleto.Status.SUBSTITUIDO,
                             Boleto.Status.DESCARTADO,
                             Boleto.Status.NAO_RECONHECIDO])
        .select_related('prestador', 'posto', 'prestador__posto_cobranca'))

    from .services import boletos as svc_boletos
    ajustes = {(a.prestador_id, a.posto_id): a
               for a in AjusteDiferenca.objects.filter(competencia=mes)}
    linhas, casados = [], set()
    for prestador in (Prestador.objects.filter(ativo=True)
                      .prefetch_related('vinculos__posto')):
        for posto, _valor in prestador.boletos_esperados():
            # valor esperado do MÊS (já com parcelas de vale abatidas)
            valor = svc_boletos.valor_esperado_para(prestador, posto, mes)
            achado = None
            for b in boletos_mes:
                if b.prestador_id != prestador.pk:
                    continue
                if b.status == Boleto.Status.DUPLICADO or b.extra \
                        or b.parcial:
                    continue  # duplicado/extra/parcial não representam
                              # sozinhos a régua
                if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
                    if b.posto_id is None:
                        achado = b
                        break
                elif posto and b.posto_id == posto.pk:
                    achado = b
                    break
            if achado:
                casados.add(achado.pk)
            # Parciais desta chave: a régua mostra a SOMA delas
            parciais_linha = [
                b for b in boletos_mes
                if b.parcial and b.prestador_id == prestador.pk
                and (b.posto_id is None
                     if prestador.modo_boleto == Prestador.ModoBoleto.UNICO
                     else (posto and b.posto_id == posto.pk))]
            soma_parc = sum((b.valor_extraido or 0 for b in parciais_linha
                             if b.status in (Boleto.Status.APROVADO,
                                             Boleto.Status.FIN_RECEBIDO,
                                             Boleto.Status.PAGO)),
                            Decimal('0'))
            aprov = (Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO,
                     Boleto.Status.PAGO)
            linhas.append({
                'prestador': prestador, 'posto': posto,
                'valor': valor, 'boleto': achado,
                'parciais': parciais_linha,
                'parciais_soma': soma_parc,
                'parciais_falta': (max(Decimal('0'), valor - soma_parc)
                                   if valor is not None else None),
                'parciais_valores': ' + '.join(
                    f'{b.valor_extraido:.2f}'.replace('.', ',')
                    for b in parciais_linha
                    if b.status in aprov and b.valor_extraido is not None),
                'parciais_pendentes': [b for b in parciais_linha
                                       if b.status not in aprov],
                'diferenca': (achado.valor_extraido - valor
                              if achado is not None and valor is not None
                              and achado.valor_extraido is not None
                              and not achado.extra
                              and abs(achado.valor_extraido - valor)
                              > Decimal('0.01') else None),
            })
            l = linhas[-1]
            from .services.verificacao import _moeda
            # Vale(s) que abatem a linha neste mês — a conta vai no card:
            # combinado cheio − vale = esperado (o 'valor' já é o líquido).
            vales_linha = svc_boletos.vales_aplicaveis(prestador, posto, mes)
            l['vale_total'] = sum((v.valor_parcela for v, _ in vales_linha),
                                  Decimal('0'))
            l['vales'] = [{'descricao': v.descricao, 'valor': v.valor_parcela,
                           'n': n, 'total': v.parcelas_total}
                          for v, n in vales_linha]
            l['combinado_bruto'] = (valor + l['vale_total']
                                    if valor is not None else None)
            if l['diferenca'] is not None:
                d = l['diferenca']
                l['diferenca_txt'] = (('+' if d > 0 else '−') + 'R$ '
                                      + _moeda(abs(d)))
            # Diferença PEQUENA (< R$ 5) que falta: parciais que não
            # fecharam, ou boleto único abaixo do combinado. Dá para
            # resolver em dinheiro / não pagar — e a linha fica quitada.
            falta = None
            if l['parciais'] and l['parciais_falta']:
                falta = l['parciais_falta']
            elif (achado is not None and achado.status in aprov
                  and l['diferenca'] is not None and l['diferenca'] < 0):
                falta = -l['diferenca']
            elif (achado is None and not l['parciais']
                  and valor is not None):
                falta = valor  # SEM BOLETO com combinado ínfimo (R$ 0,40)
            l['ajuste'] = ajustes.get((prestador.pk,
                                       posto.pk if posto else None))
            l['falta_pequena'] = (falta if falta is not None
                                  and Decimal('0') < falta
                                  < AjusteDiferenca.LIMITE
                                  and l['ajuste'] is None else None)
            if l['ajuste'] is not None:
                l['parciais_falta'] = Decimal('0')

    # FILTRO por prestador ou posto: a régua vira uma tela de conferência
    # ("Elias: previsto 9.000, boletos até aqui 8.999,40, falta 0,60").
    filtro = {'prestador': None, 'posto': None}
    g = request.GET
    if g.get('prestador', '').isdigit():
        filtro['prestador'] = Prestador.objects.filter(
            pk=int(g['prestador'])).first()
    if g.get('posto', '').isdigit():
        filtro['posto'] = Posto.objects.filter(pk=int(g['posto'])).first()

    def bate(prestador_id, posto_id):
        if filtro['prestador'] and prestador_id != filtro['prestador'].pk:
            return False
        if filtro['posto'] and posto_id != filtro['posto'].pk:
            return False
        return True
    if filtro['prestador'] or filtro['posto']:
        linhas = [l for l in linhas
                  if bate(l['prestador'].pk,
                          l['posto'].pk if l['posto'] else None)]
        boletos_mes = [b for b in boletos_mes
                       if bate(b.prestador_id,
                               b.posto_efetivo.pk if b.posto_efetivo
                               else None)]

    # Quadro "cancelados": o que o admin tirou da régua — dívida NÃO
    # reconhecida e excluídos (substituído/duplicado são automáticos e
    # ficam de fora). Continuam no banco, com o PDF: dá para ver e
    # restaurar.
    cancelados = [
        b for b in (Boleto.objects
                    .filter(competencia=mes,
                            status__in=[Boleto.Status.NAO_RECONHECIDO,
                                        Boleto.Status.DESCARTADO])
                    .select_related('prestador', 'posto',
                                    'prestador__posto_cobranca')
                    .order_by('-pk'))
        if bate(b.prestador_id,
                b.posto_efetivo.pk if b.posto_efetivo else None)]
    for b in cancelados:
        b.cancelado_em = b.nao_reconhecido_em
        if b.cancelado_em is None:  # excluído: a data está na auditoria
            a = (AuditLog.objects
                 .filter(detalhe__regex=r'^Ação "descartar" no boleto '
                                        rf'#{b.pk}(\D|$)')
                 .order_by('-pk').first())
            b.cancelado_em = a.criado_em if a else None

    aprov = (Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO,
             Boleto.Status.PAGO)
    previsto = sum((l['valor'] for l in linhas if l['valor'] is not None),
                   Decimal('0'))
    entrou = Decimal('0')
    pendentes_valor = 0
    for l in linhas:
        b = l['boleto']
        if b is not None:
            if b.status in aprov and b.valor_extraido is not None:
                entrou += b.valor_extraido
            elif b.status not in aprov:
                pendentes_valor += 1
        entrou += l['parciais_soma']
        pendentes_valor += len(l['parciais_pendentes'])
    ajustado = sum((l['ajuste'].valor for l in linhas if l.get('ajuste')),
                   Decimal('0'))
    resumo_filtro = {
        'previsto': previsto, 'entrou': entrou, 'ajustado': ajustado,
        'falta': max(Decimal('0'), previsto - entrou - ajustado),
        'passou': max(Decimal('0'), entrou - previsto),
        'pendentes': pendentes_valor,
        'postos': len(linhas),
        'sem_boleto': sum(1 for l in linhas
                          if l['boleto'] is None and not l['parciais']
                          and not l.get('ajuste')),
    }

    sobras = [b for b in boletos_mes if b.pk not in casados]
    # Extras e parciais são cobranças LEGÍTIMAS — seções próprias, sem tom
    # de anomalia; "fora da régua" fica só para o que não casou mesmo.
    extras = [b for b in sobras if b.extra]
    parciais_mes = [b for b in sobras if b.parcial and not b.extra]
    # Parciais AGRUPADAS por prestador/posto: cabeçalho com "já entrou X de
    # Y, falta Z" e os boletos embaixo — sem ter que somar de cabeça.
    grupos_parciais = []
    for l in linhas:
        if l['parciais']:
            grupos_parciais.append(l)
    soltas = [b for b in parciais_mes
              if not any(b in l['parciais'] for l in grupos_parciais)]
    fora_da_regua = [b for b in sobras if not b.extra and not b.parcial]

    def peso(linha):  # pendências primeiro
        b = linha['boleto']
        if b is None:
            return 0
        ordem = {Boleto.Status.DIVERGENTE: 1, Boleto.Status.MANUAL: 1,
                 Boleto.Status.RECEBIDO: 2, Boleto.Status.APROVADO: 3,
                 Boleto.Status.FIN_RECEBIDO: 4, Boleto.Status.PAGO: 5}
        return ordem.get(b.status, 2)
    linhas.sort(key=lambda l: (peso(l), l['prestador'].nome))

    resumo = {
        'faltando': sum(1 for l in linhas
                        if l['boleto'] is None and not l['parciais']
                        and not l.get('ajuste')),
        'atencao': sum(1 for l in linhas if l['boleto'] and l['boleto'].status
                       in (Boleto.Status.DIVERGENTE, Boleto.Status.MANUAL)),
        'aguardando_pgto': sum(1 for l in linhas if l['boleto'] and
                               l['boleto'].status in
                               (Boleto.Status.APROVADO,
                                Boleto.Status.FIN_RECEBIDO)),
        'pagos': sum(1 for l in linhas if l['boleto'] and
                     l['boleto'].status == Boleto.Status.PAGO),
    }
    resumo['aguardando_pgto'] += sum(
        1 for b in (extras + parciais_mes)
        if b.status in (Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO))
    resumo['pagos'] += sum(1 for b in (extras + parciais_mes)
                           if b.status == Boleto.Status.PAGO)
    resumo['cancelados'] = len(cancelados)

    # Os 4 quadros viram FILTROS da régua. Por padrão o painel abre na
    # situação "enviados p/ pagamento" — o que o Cristiano acompanha no dia a
    # dia; os "sem boleto" (dezenas) só aparecem quando ele toca no quadro.
    # Os CONTADORES dos quadros continuam somando o mês inteiro (não mudam);
    # só a lista de baixo é que filtra. 'todos' é o escape p/ ver tudo.
    def _categoria(l):
        b = l['boleto']
        if b is not None:
            if b.status in (Boleto.Status.DIVERGENTE, Boleto.Status.MANUAL):
                return 'atencao'
            if b.status in (Boleto.Status.APROVADO,
                            Boleto.Status.FIN_RECEBIDO):
                return 'pagamento'
            if b.status == Boleto.Status.PAGO:
                return 'pagos'
            return 'verificacao'          # RECEBIDO — robô ainda conferindo
        if l['parciais']:
            return 'parciais'
        if l.get('ajuste'):
            return 'quitado'
        return 'sem_boleto'

    for l in linhas:
        l['categoria'] = _categoria(l)
    vista = request.GET.get('vista', 'pagamento')
    if vista not in ('sem_boleto', 'atencao', 'pagamento', 'pagos', 'todos',
                     'cancelados'):
        vista = 'pagamento'
    linhas_vista = (linhas if vista == 'todos'
                    else [l for l in linhas if l['categoria'] == vista])

    # Vistas "sem boleto" e "enviados p/ pagamento": agrupa por PJ — um
    # cabeçalho clicável por empresa (total + postos), detalhe por posto ao
    # expandir. Só muda a tela; cada linha ganha 'pj_resumo' (igual p/ as
    # linhas do mesmo PJ). Reordena por PJ p/ as linhas ficarem contíguas.
    if vista in ('sem_boleto', 'pagamento'):
        linhas_vista.sort(key=lambda l: (l['prestador'].nome,
                                         l['prestador'].pk,
                                         str(l['posto'] or '')))

        def _valor_pj(l):
            b = l['boleto']
            if b is not None and b.valor_extraido is not None:
                return b.valor_extraido   # pagamento: o que vai ser pago
            return l['valor']             # sem boleto: o combinado

        suf = 'sem boleto' if vista == 'sem_boleto' else 'enviado p/ pagamento'
        grupos_pj = {}
        for l in linhas_vista:
            grupos_pj.setdefault(l['prestador'].pk, []).append(l)
        for itens in grupos_pj.values():
            total = sum((_valor_pj(l) for l in itens
                         if _valor_pj(l) is not None), Decimal('0'))
            postos = ', '.join(str(l['posto']) if l['posto'] else '(único)'
                               for l in itens)
            resumo_pj = {'total': total, 'postos': postos,
                         'n': len(itens), 'suf': suf}
            if vista == 'pagamento':
                # Botão "TODOS BOLETOS PAGOS": tudo deste PJ que está com
                # o financeiro no mês — os postos do grupo e também as
                # parciais/extras do fim da página. Os ids vão no botão:
                # só é dado baixa no que estava na tela ao clicar.
                a_pagar = [b for b in boletos_mes
                           if b.prestador_id == itens[0]['prestador'].pk
                           and b.status in (Boleto.Status.APROVADO,
                                            Boleto.Status.FIN_RECEBIDO)]
                resumo_pj['pagar_ids'] = ','.join(str(b.pk) for b in a_pagar)
                resumo_pj['pagar_n'] = len(a_pagar)
                resumo_pj['pagar_total'] = sum(
                    (b.valor_extraido for b in a_pagar
                     if b.valor_extraido is not None), Decimal('0'))
                resumo_pj['pagar_fora'] = len(a_pagar) - len(itens)
            for l in itens:
                l['pj_resumo'] = resumo_pj

    qs_filtro = ''.join(
        f'&{k}={v.pk}' for k, v in filtro.items() if v is not None)
    return render(request, 'painel/dashboard.html', {
        'grupos_parciais': grupos_parciais, 'parciais_soltas': soltas,
        'filtro': filtro, 'qs_filtro': qs_filtro,
        'resumo_filtro': resumo_filtro,
        'prestadores': Prestador.objects.filter(ativo=True).order_by('nome'),
        'postos': Posto.objects.filter(ativo=True, excluido_em__isnull=True)
                               .order_by('nome'),
        'mes': mes, 'mes_extenso': competencia_extenso(mes).capitalize(),
        'ant': ant, 'prox': prox, 'linhas': linhas_vista, 'vista': vista,
        'extras': extras,
        'parciais_mes': parciais_mes,
        'pendentes_baixo': len(parciais_mes) + len(fora_da_regua)
                           + len(extras),
        'fora_da_regua': fora_da_regua, 'resumo': resumo,
        'cancelados': cancelados, 'up': up})


@admin_required
@require_POST
def ajuste_diferenca(request, up):
    """Botões "Pagar em dinheiro" / "Não será pago" (diferença < R$ 5) e
    "desfazer". Resolve a linha (prestador, posto, mês) sem boleto novo."""
    g = request.POST
    prestador = get_object_or_404(Prestador, pk=g.get('prestador'))
    posto = (get_object_or_404(Posto, pk=g['posto'])
             if g.get('posto') else None)
    try:
        competencia = date.fromisoformat(g.get('competencia', ''))
        competencia = competencia.replace(day=1)
        valor = Decimal(str(g.get('valor', '')).replace(',', '.'))
    except (ValueError, ArithmeticError):
        messages.error(request, 'Pedido inválido.')
        return redirect('painel_dashboard')
    voltar = f'/painel/?m={competencia:%Y-%m}'
    modo = g.get('modo')
    if modo == 'DESFAZER':
        n, _ = AjusteDiferenca.objects.filter(
            prestador=prestador, posto=posto, competencia=competencia
        ).delete()
        AuditLog.registrar(AuditLog.Evento.CRUD, request,
                           detalhe=f'Ajuste desfeito: {prestador} — '
                                   f'{posto or "único"} — {competencia:%m/%Y}')
        messages.success(request, 'Ajuste desfeito — a diferença volta a '
                                  'aparecer como pendente.')
        return redirect(voltar)
    if modo not in AjusteDiferenca.Modo.values or not (
            Decimal('0') < valor < AjusteDiferenca.LIMITE):
        messages.error(request, 'Só diferenças menores que R$ 5,00 podem '
                                'ser resolvidas assim.')
        return redirect(voltar)
    a, _ = AjusteDiferenca.objects.update_or_create(
        prestador=prestador, posto=posto, competencia=competencia,
        defaults={'valor': valor, 'modo': modo, 'por': up.email})
    AuditLog.registrar(AuditLog.Evento.CRUD, request, detalhe=f'Ajuste: {a}')
    messages.success(request, f'{prestador.nome} — {posto or "único"}: '
                              f'diferença de R$ {valor} '
                              f'{a.get_modo_display().lower()}. '
                              'Linha quitada.')
    return redirect(voltar)


@admin_required
@require_POST
def boleto_acao(request, up, pk, acao):
    boleto = get_object_or_404(Boleto, pk=pk)
    if acao == 'pagar' and boleto.status in (Boleto.Status.APROVADO,
                                             Boleto.Status.FIN_RECEBIDO,
                                             Boleto.Status.MANUAL,
                                             Boleto.Status.DIVERGENTE):
        boleto.status = Boleto.Status.PAGO
        boleto.pago_em = timezone.now()
        boleto.save(update_fields=['status', 'pago_em'])
        messages.success(request, f'{boleto} marcado como PAGO.')
    elif acao == 'descartar' and boleto.status != Boleto.Status.PAGO:
        # Soft delete do boleto: some das listas, fica no banco (auditável).
        boleto.status = Boleto.Status.DESCARTADO
        boleto.save(update_fields=['status'])
        messages.success(request, f'{boleto} descartado (nada apagado — '
                                  'fica na auditoria).')
    elif acao == 'nao_reconhecer' and boleto.status != Boleto.Status.PAGO:
        # "Não reconheço esta dívida": cancela a cobrança e avisa o
        # prestador (e o financeiro, se o boleto já tinha ido). Caminho
        # único em verificacao.nao_reconhecer — fica no banco, com o PDF.
        from .services.verificacao import nao_reconhecer
        motivo = (request.POST.get('motivo') or '').strip()[:500]
        nao_reconhecer(boleto, motivo, quem=up.email)
        messages.success(request, f'{boleto}: dívida não reconhecida — o '
                                  'prestador foi avisado por e-mail e o '
                                  'boleto saiu da régua (fica na auditoria).')
    elif acao == 'restaurar' and boleto.status in (
            Boleto.Status.DESCARTADO, Boleto.Status.NAO_RECONHECIDO):
        # Desfaz o cancelamento (quadro "cancelados"): volta para a
        # verificação com o esperado RECALCULADO (vale/valor podem ter
        # mudado desde então) e sem repetir o e-mail de "recebemos".
        boleto.status = Boleto.Status.RECEBIDO
        boleto.tentativas = 0
        boleto.verificado_em = None
        boleto.valor_esperado = None
        boleto.save(update_fields=['status', 'tentativas', 'verificado_em',
                                   'valor_esperado'])
        from .services.verificacao import processar_async
        processar_async(boleto.pk)
        messages.success(request, f'{boleto} restaurado — voltou para a '
                                  'verificação.')
    elif acao == 'despagar' and boleto.status == Boleto.Status.PAGO:
        # Clique errado no "Marcar PAGO": volta para APROVADO, sem e-mails.
        boleto.status = Boleto.Status.APROVADO
        boleto.pago_em = None
        boleto.save(update_fields=['status', 'pago_em'])
        messages.success(request, f'{boleto} voltou para "enviado p/ '
                                  'pagamento" (PAGO desfeito).')
    elif acao == 'reprocessar':
        boleto.status = Boleto.Status.RECEBIDO
        boleto.tentativas = 0
        boleto.save(update_fields=['status', 'tentativas'])
        from .services.verificacao import fluxo_completo_async
        fluxo_completo_async(boleto.pk)
        messages.success(request, f'{boleto} voltou para verificação.')
    elif acao == 'aprovar' and boleto.status in (Boleto.Status.MANUAL,
                                                 Boleto.Status.DIVERGENTE,
                                                 Boleto.Status.RECEBIDO,
                                                 Boleto.Status.APROVADO):
        # Aprovação manual (o Cristiano conferiu no olho) OU "Reenviar
        # e-mails" de um já aprovado. O caminho é único e tem a trava
        # contra mandar o mesmo boleto duas vezes ao financeiro.
        from .services.verificacao import enviar_para_pagamento
        reenviar = boleto.status == Boleto.Status.APROVADO
        boleto.status = Boleto.Status.APROVADO
        if not reenviar or not boleto.aprovado_por:
            boleto.aprovado_por = (up.nome or up.email)[:120]
            boleto.verificado_em = timezone.now()
        boleto.save(update_fields=['status', 'verificado_em',
                                   'aprovado_por'])
        resultado = enviar_para_pagamento(boleto, reenviar=reenviar)
        if 'NADA' in resultado:
            messages.warning(request, f'{boleto}: {resultado}')
        else:
            messages.success(request, f'{boleto} — {resultado}')
    elif acao == 'corrigir_valor':
        # O admin leu o boleto no olho: a IA errou o valor. Grava o valor
        # certo À MÃO (a reverificação NÃO roda, senão a IA sobrescreveria) e,
        # se o boleto JÁ foi ao financeiro, dispara a CORREÇÃO automática —
        # usando a mesma trava contra pagar duas vezes.
        from .services.verificacao import enviar_para_pagamento, _moeda
        try:
            novo = ValorBRField().to_python(request.POST.get('valor', ''))
        except Exception:
            novo = None
        bloqueado = (Boleto.Status.PAGO, Boleto.Status.SUBSTITUIDO,
                     Boleto.Status.DESCARTADO, Boleto.Status.DUPLICADO,
                     Boleto.Status.NAO_RECONHECIDO)
        if novo is None or novo <= 0:
            messages.error(request, 'Valor inválido. Escreva assim: 4.548,14')
        elif boleto.status in bloqueado:
            messages.error(request, f'{boleto}: não dá para corrigir o valor '
                                    'de um boleto já pago/descartado.')
        else:
            antigo = boleto.valor_extraido
            boleto.valor_extraido = novo
            boleto.ia_confianca = 100
            quem = (up.nome or up.email)[:80]
            marca = (f'[corrigido] valor ajustado à mão por {quem}: '
                     f'R$ {_moeda(novo)}'
                     + (f' (a IA havia lido R$ {_moeda(antigo)})'
                        if antigo is not None else ''))
            boleto.ia_resposta = (
                (boleto.ia_resposta or '') + '\n' + marca).strip()[:4000]
            if boleto.verificado_em is None:
                boleto.verificado_em = timezone.now()
            boleto.save(update_fields=['valor_extraido', 'ia_confianca',
                                       'ia_resposta', 'verificado_em'])
            if boleto.pagamento_enviado_em is not None:
                resultado = enviar_para_pagamento(boleto)
                messages.success(
                    request, f'{boleto}: valor corrigido para R$ '
                             f'{_moeda(novo)} — {resultado}')
            else:
                messages.success(
                    request, f'{boleto}: valor corrigido para R$ '
                             f'{_moeda(novo)}. Confira a situação e aprove '
                             'quando quiser.')
    else:
        messages.error(request, 'Ação não permitida para este status.')
    AuditLog.registrar(AuditLog.Evento.STATUS, request,
                       detalhe=f'Ação "{acao}" no boleto #{pk}')
    return redirect(request.POST.get('voltar') or 'painel_dashboard')


@admin_required
@require_POST
def pagar_todos(request, up, pk):
    """Botão "TODOS BOLETOS PAGOS" do grupo do PJ: o financeiro avisou que
    pagou tudo daquele fornecedor — dá baixa de uma vez, em vez de um por
    um. Só nos boletos que estavam na tela (ids do formulário), só deste
    prestador e só nos que estão com o financeiro (enviado p/ pagamento ou
    recebido pelo financeiro). Nenhum e-mail é enviado; cada boleto fica na
    auditoria e pode ser desfeito em "Desfazer PAGO"."""
    from .services.verificacao import _moeda
    prestador = get_object_or_404(Prestador, pk=pk)
    ids = [int(n) for n in re.findall(r'\d+', request.POST.get('ids', ''))]
    agora = timezone.now()
    pagos, total = [], Decimal('0')
    with transaction.atomic():
        for boleto in (Boleto.objects
                       .filter(pk__in=ids[:500], prestador=prestador,
                               status__in=[Boleto.Status.APROVADO,
                                           Boleto.Status.FIN_RECEBIDO])
                       .order_by('pk')):
            boleto.status = Boleto.Status.PAGO
            boleto.pago_em = agora
            boleto.save(update_fields=['status', 'pago_em'])
            pagos.append(boleto.pk)
            total += boleto.valor_extraido or Decimal('0')
    for bpk in pagos:
        AuditLog.registrar(
            AuditLog.Evento.STATUS, request,
            detalhe=f'Ação "pagar" no boleto #{bpk} — botão TODOS BOLETOS '
                    f'PAGOS ({prestador.nome})')
    fora = len(set(ids)) - len(pagos)
    if pagos:
        messages.success(
            request,
            f'{len(pagos)} boleto(s) de {prestador.nome} marcado(s) como '
            f'PAGO — R$ {_moeda(total)}.'
            + (f' {fora} já não estava(m) "enviado p/ pagamento" e '
               'ficou(aram) como estava(m).' if fora else ''))
    else:
        messages.warning(request, f'Nenhum boleto de {prestador.nome} '
                                  'estava "enviado p/ pagamento" — nada '
                                  'foi alterado.')
    return redirect(request.POST.get('voltar') or 'painel_dashboard')


@admin_required
@require_POST
def verificar_email_agora(request, up):
    """Botão "Verificar e-mail agora": dispara na hora o robô que lê as
    caixas de boleto (que no cron roda de 10 em 10 min) + a verificação,
    para o Cristiano não ter de esperar o ciclo quando sabe que um boleto
    acabou de chegar. Só leitura da caixa (BODY.PEEK), igual ao cron."""
    from io import StringIO

    from django.core.management import call_command

    from django.http import JsonResponse

    # O botão caprichado chama por fetch e espera JSON (mostra o resultado
    # dentro dele mesmo). Sem JS, cai no fallback que recarrega a página.
    ajax = request.headers.get('X-Requested-With') == 'fetch'

    antes = Boleto.objects.count()
    buf = StringIO()
    try:
        call_command('importar_emails_pj', stdout=buf, stderr=buf)
        call_command('processar_boletos', stdout=buf, stderr=buf)
    except Exception as e:
        if ajax:
            return JsonResponse({'ok': False,
                                 'msg': f'Não consegui ler o e-mail: {e}'})
        messages.error(request, f'Não consegui ler o e-mail agora: {e}')
        return redirect(request.POST.get('voltar') or 'painel_dashboard')
    novos = Boleto.objects.count() - antes
    AuditLog.registrar(AuditLog.Evento.STATUS, request,
                       detalhe='Verificação manual de e-mail (botão)')
    if ajax:
        if novos > 0:
            msg = (f'{novos} boleto(s) novo(s)!' if novos > 1
                   else '1 boleto novo!')
        else:
            msg = 'Nada novo no e-mail'
        return JsonResponse({'ok': True, 'novos': novos, 'msg': msg})
    if novos > 0:
        messages.success(request, f'Olhei o e-mail agora — {novos} '
                         f'boleto(s) novo(s) entraram.')
    else:
        messages.info(request, 'Olhei o e-mail agora — nenhum boleto novo '
                      'chegou ainda.')
    return redirect(request.POST.get('voltar') or 'painel_dashboard')


@admin_required
def boleto_novo(request, up):
    """Cadastro de boleto pelo admin — ex.: boleto que chegou pelo zap.
    Entra no MESMO fluxo de verificação do upload do PJ."""
    if request.method == 'POST':
        form = BoletoAdminForm(request.POST, request.FILES)
        if form.is_valid():
            from .services import boletos as svc_boletos
            prestador = form.cleaned_data['prestador']
            arq = form.cleaned_data['arquivo']
            nf = form.cleaned_data.get('nota_fiscal')
            boleto = svc_boletos.registrar(
                prestador, form.cleaned_data['competencia'],
                enviado_por=up.email, posto=form.cleaned_data['posto'],
                arquivo=arq, nome_original=arq.name if arq else '',
                nota_fiscal=nf, nota_fiscal_nome=nf.name if nf else '',
                linha_digitavel=form.cleaned_data['linha_digitavel'],
                chave_pix=form.cleaned_data['chave_pix'],
                valor_livre=form.cleaned_data['valor_livre'],
                extra=form.cleaned_data['extra'],
                parcial=form.cleaned_data['parcial'],
                observacao=form.cleaned_data['observacao'],
                origem=Boleto.Origem.PAINEL)
            AuditLog.registrar(AuditLog.Evento.UPLOAD_BOLETO, request,
                               detalhe=f'(admin) Boleto #{boleto.pk} {boleto}')
            from .services.verificacao import fluxo_completo_async
            fluxo_completo_async(boleto.pk)
            messages.success(request,
                             f'Boleto de {prestador.nome} cadastrado — '
                             'entrou na fila de verificação.')
            return redirect('painel_dashboard')
    else:
        # Pré-preenchido pelo botão "➕ parcial" da régua
        ini = {}
        g = request.GET
        if g.get('prestador', '').isdigit():
            ini['prestador'] = int(g['prestador'])
        if g.get('posto', '').isdigit():
            ini['posto'] = int(g['posto'])
        if g.get('competencia'):
            ini['competencia'] = g['competencia'][:7] + '-01'
        if g.get('parcial'):
            ini['parcial'] = True
            ini['valor_livre'] = True
        form = BoletoAdminForm(initial=ini)
    return render(request, 'painel/boleto_form.html', {'form': form, 'up': up})


@admin_required
def parciais_status(request, up):
    """JSON p/ o cadastro: quanto JÁ entrou deste prestador/posto/mês, quanto
    falta e como fica com o boleto que está sendo digitado (pela linha
    digitável). É o "quanto já coloquei e quanto falta" ao vivo."""
    from django.http import JsonResponse
    from .services import boletos as svc_boletos
    from .services.verificacao import _moeda, valor_da_linha
    g = request.GET
    try:
        prestador = Prestador.objects.get(pk=int(g.get('prestador') or 0))
    except (Prestador.DoesNotExist, ValueError):
        return JsonResponse({'texto': ''})
    posto = None
    if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
        posto = Posto.objects.filter(pk=g.get('posto') or 0).first()
        if posto is None:
            return JsonResponse({'texto': 'Escolha o posto para ver quanto '
                                          'já entrou e quanto falta.'})
    try:
        comp = date.fromisoformat((g.get('competencia') or '')[:10])
    except ValueError:
        return JsonResponse({'texto': ''})
    comp = comp.replace(day=1)
    combinado = svc_boletos.valor_esperado_para(prestador, posto, comp)
    bs = list(Boleto.objects.filter(prestador=prestador, posto=posto,
                                    competencia=comp, extra=False)
              .exclude(status__in=[Boleto.Status.SUBSTITUIDO,
                                   Boleto.Status.DESCARTADO,
                                   Boleto.Status.NAO_RECONHECIDO,
                                   Boleto.Status.DUPLICADO])
              .order_by('criado_em'))
    ok = (Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO,
          Boleto.Status.PAGO)
    entrou = sum((b.valor_extraido or Decimal('0') for b in bs
                  if b.status in ok), Decimal('0'))
    pendentes = [b for b in bs if b.status not in ok]
    este = valor_da_linha(g.get('linha') or '')
    alvo = posto.nome if posto else prestador.nome
    mes = f'{comp:%m/%Y}'
    if combinado is None:
        return JsonResponse({'texto': f'{alvo} {mes}: sem valor combinado '
                                      'cadastrado.', 'nivel': 'ruim'})
    partes = [f'{alvo} {mes} — combinado R$ {_moeda(combinado)}.']
    if bs:
        lista = ', '.join(
            f'R$ {_moeda(b.valor_extraido)}' if b.valor_extraido
            else b.get_status_display().lower()
            for b in bs if b.status in ok)
        partes.append(f'Já entrou R$ {_moeda(entrou)}'
                      + (f' ({lista})' if lista else '') + '.')
        if pendentes:
            partes.append(f'{len(pendentes)} boleto(s) ainda em verificação/'
                          'pendente(s) — não contam ainda.')
    else:
        partes.append('Nenhum boleto deste mês ainda.')
    falta = max(Decimal('0'), combinado - entrou)
    nivel = 'ok'
    if este is not None:
        depois = entrou + este
        partes.append(f'Este boleto (pela linha digitável): R$ {_moeda(este)} '
                      f'→ ficará R$ {_moeda(depois)} de R$ {_moeda(combinado)}.')
        if depois - combinado > Decimal('0.01'):
            partes.append(f'⚠️ PASSA do combinado em R$ '
                          f'{_moeda(depois - combinado)} — vai cair em '
                          'verificação manual.')
            nivel = 'ruim'
        elif combinado - depois > Decimal('0.01'):
            partes.append(f'⏳ Ainda faltará R$ {_moeda(combinado - depois)}.')
            nivel = 'medio'
        else:
            partes.append('✅ Fecha a mensalidade.')
        if entrou > 0 and not any(b.parcial for b in bs if b.status in ok):
            partes.append('⚠️ Já existe boleto CHEIO aprovado neste mês — '
                          'este seria duplicidade (marque PARCIAL ou EXTRA '
                          'se for o caso).')
            nivel = 'ruim'
    else:
        partes.append(f'Falta R$ {_moeda(falta)}.' if falta > 0
                      else '✅ Mensalidade já completa — um boleto a mais '
                           'seria duplicidade (ou marque EXTRA).')
        nivel = 'medio' if falta > 0 else 'ok'
    return JsonResponse({'texto': ' '.join(partes), 'nivel': nivel})


@admin_required
def boleto_editar(request, up, pk):
    """Editar boleto: destinar posto (PDFs que chegaram juntos por e-mail),
    acertar competência, linha digitável e a observação do mês. Mudança que
    afeta a conferência manda o boleto de volta para verificação."""
    from .forms import BoletoEditForm
    from .services import boletos as svc_boletos
    boleto = get_object_or_404(
        Boleto.objects.select_related('prestador'), pk=pk)
    prestador = boleto.prestador

    if request.method == 'POST':
        form = BoletoEditForm(boleto, request.POST, request.FILES)
        if form.is_valid():
            d = form.cleaned_data
            posto = d['posto']
            if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
                posto = None
            mudou = (posto != boleto.posto
                     or d['competencia'] != boleto.competencia
                     or d['linha_digitavel'] != boleto.linha_digitavel
                     or d['valor_livre'] != boleto.valor_livre
                     or d['extra'] != boleto.extra
                     or d['parcial'] != boleto.parcial)
            boleto.posto = posto
            boleto.competencia = d['competencia']
            boleto.linha_digitavel = d['linha_digitavel']
            boleto.chave_pix = d['chave_pix'].strip()
            boleto.valor_livre = d['valor_livre']
            boleto.extra = d['extra']
            boleto.parcial = d['parcial']
            boleto.observacao = d['observacao'].strip()
            nf = d.get('nota_fiscal')
            # Boleto que JÁ foi ao financeiro: anexar a nota não reenvia nem
            # reverifica — só guarda e manda a nota como complemento.
            ja_enviado = (boleto.pagamento_enviado_em is not None
                          or boleto.status in (Boleto.Status.APROVADO,
                                               Boleto.Status.FIN_RECEBIDO,
                                               Boleto.Status.PAGO))
            if nf:
                boleto.nota_fiscal = nf
                boleto.nota_fiscal_nome = nf.name
            boleto.valor_esperado = (
                None if d['extra'] else svc_boletos.valor_esperado_para(
                    prestador, posto, d['competencia']))
            if nf and ja_enviado:
                boleto.save()
                from .services.verificacao import enviar_nota_posterior
                enviar_nota_posterior(boleto)
                messages.success(request, f'{boleto} salvo — nota fiscal '
                                 'anexada e enviada ao financeiro.')
            elif (mudou or nf) and boleto.status not in (
                    Boleto.Status.PAGO, Boleto.Status.SUBSTITUIDO):
                boleto.status = Boleto.Status.RECEBIDO
                boleto.tentativas = 0
                boleto.verificado_em = None
                boleto.save()
                from .services.verificacao import processar_async
                processar_async(boleto.pk)
                messages.success(request,
                                 f'{boleto} salvo — verificando de novo.')
            else:
                boleto.save()
                messages.success(request, f'{boleto} salvo.')
            AuditLog.registrar(
                AuditLog.Evento.CRUD, request,
                detalhe=f'Boleto #{boleto.pk} editado'
                + (' — nota fiscal anexada' if nf else ''))
            return redirect(f'/painel/?m={boleto.competencia:%Y-%m}')
    else:
        form = BoletoEditForm(boleto, initial={
            'posto': boleto.posto_id,
            'competencia': boleto.competencia.isoformat(),
            'linha_digitavel': boleto.linha_digitavel,
            'chave_pix': boleto.chave_pix,
            'valor_livre': boleto.valor_livre,
            'extra': boleto.extra,
            'parcial': boleto.parcial,
            'observacao': boleto.observacao,
        })
    return render(request, 'painel/boleto_edit.html',
                  {'form': form, 'boleto': boleto, 'up': up})


@admin_real_required
@require_POST
def ver_como(request, up, pk):
    prestador = get_object_or_404(Prestador, pk=pk, ativo=True)
    request.session['ver_como'] = prestador.pk
    messages.success(request,
                     f'Você está vendo o portal como {prestador.nome}.')
    return redirect('home')


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
    lista = list(Prestador.objects.filter(excluido_em__isnull=True)
                 .select_related('posto_cobranca')
                 .prefetch_related('vinculos__posto', 'usuarios',
                                   'contratos'))
    hoje = timezone.localdate()
    for p in lista:
        # Contrato vigente por posto (contrato "geral", sem posto, cobre
        # todos). Posto sem contrato vigente = vermelho + link p/ anexar.
        todos = list(p.contratos.all())
        vigentes = [c for c in todos if c.vigente]
        geral = any(c.posto_id is None for c in vigentes)
        cobertos = {c.posto_id for c in vigentes}
        # Já teve contrato (do posto ou geral) = vencido; senão, nunca teve.
        geral_antigo = any(c.posto_id is None for c in todos)
        antigos = {c.posto_id for c in todos}
        if p.modo_boleto == Prestador.ModoBoleto.UNICO:
            postos = ([(p.posto_cobranca, p.valor_esperado_unico())]
                      if p.posto_cobranca else [])
        else:
            postos = [(v.posto, v.valor_mensal) for v in p.vinculos.all()
                      if v.ativo and v.posto.ativo]
        p.postos_info = [{'posto': x, 'valor': valor,
                          'ok': geral or x.pk in cobertos}
                         for x, valor in postos]
        faltam = [i for i in p.postos_info if not i['ok']]
        p.vencidos = [i['posto'].nome for i in faltam
                      if geral_antigo or i['posto'].pk in antigos]
        p.nunca = [i['posto'].nome for i in faltam
                   if i['posto'].nome not in p.vencidos]
        # Linha em vermelho suave: algum posto descoberto, ou nenhum
        # contrato vigente (boleto único sem posto de cobrança).
        p.sem_contrato = bool(faltam) or not vigentes
        p.total_mensal = sum((v for _, v in postos if v is not None),
                             Decimal('0'))
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
        elif qual == 'receita':
            from .services import receita
            try:
                dados = receita.consultar_cnpj(prestador.cnpj)
                alterados = receita.aplicar(prestador, dados, ator=up.email)
                messages.success(
                    request,
                    f'Receita: {dados["razao_social"]} — '
                    f'{dados["situacao_cadastral"]}. '
                    + (f'Preenchidos: {", ".join(alterados)}.' if alterados
                       else 'Nada em branco para preencher; situação e '
                            'sócios atualizados.'))
            except Exception as e:
                messages.error(request, f'Não consegui consultar a Receita: '
                                        f'{e}')
            return redirect('painel_prestador', pk=pk)
        elif qual == 'valores':
            campo = ValorBRField(required=False)
            with transaction.atomic():
                for posto in postos:
                    atende = bool(request.POST.get(f'atende_{posto.pk}'))
                    bruto = (request.POST.get(f'valor_{posto.pk}') or '').strip()
                    try:
                        valor = campo.clean(bruto) if bruto else None
                    except Exception:
                        messages.error(request,
                                       f'Valor inválido em {posto.nome}.')
                        return redirect('painel_prestador', pk=pk)
                    if atende and valor is None:
                        messages.error(
                            request, f'{posto.nome} está marcado como '
                                     '"atende", mas sem valor mensal — '
                                     'nada foi salvo.')
                        return redirect('painel_prestador', pk=pk)
                    vinculo = PrestadorPosto.objects.filter(
                        prestador=prestador, posto=posto).first()
                    if not atende:
                        if vinculo and vinculo.ativo:
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
        elif qual == 'vale':
            vale_form = ValeForm(request.POST)
            if vale_form.is_valid():
                d = vale_form.cleaned_data
                if (prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO
                        and not d['posto']):
                    messages.error(request, 'Este prestador é um boleto por '
                                            'posto — escolha de qual posto '
                                            'o vale desconta.')
                    return redirect('painel_prestador', pk=pk)
                v = Vale.objects.create(
                    prestador=prestador, posto=d['posto'],
                    descricao=d['descricao'],
                    valor_parcela=d['valor_parcela'],
                    parcelas_total=d['parcelas_total'],
                    primeira_competencia=d['primeira_competencia'])
                AuditLog.registrar(AuditLog.Evento.CRUD, request,
                                   detalhe=f'Vale criado: {v}')
                messages.success(request, f'Vale "{v.descricao}" criado — '
                                          'as parcelas já abatem o valor '
                                          'esperado dos próximos boletos.')
            else:
                messages.error(request, f'Vale inválido: {vale_form.errors}')
            return redirect('painel_prestador', pk=pk)
        elif qual == 'contrato':
            cform = ContratoAdminForm(request.POST, request.FILES)
            if cform.is_valid():
                arq = cform.cleaned_data['arquivo']
                contrato = Contrato.objects.create(
                    prestador=prestador, posto=cform.cleaned_data['posto'],
                    arquivo=arq, nome_original=arq.name[:255],
                    enviado_por=up.email,
                    vigencia_inicio=cform.cleaned_data['vigencia_inicio'],
                    vigencia_fim=cform.cleaned_data['vigencia_fim'])
                AuditLog.registrar(AuditLog.Evento.UPLOAD_CONTRATO, request,
                                   detalhe=f'(painel) {prestador} — '
                                           f'{arq.name[:60]}')
                from .services.contratos import aplicar_dados_async
                aplicar_dados_async(contrato.pk)
                messages.success(
                    request,
                    'Contrato anexado. Estou lendo o PDF em segundo plano '
                    'para completar o cadastro (endereço, representante, '
                    'prazos) — só o que estiver em branco; em até 1 minuto '
                    'recarregue a página e confira a ficha. O que foi '
                    'preenchido fica na Auditoria.')
            else:
                messages.error(request, f'Contrato inválido: {cform.errors}')
            return redirect('painel_prestador', pk=pk)
        elif qual == 'vale_toggle':
            v = get_object_or_404(Vale, pk=request.POST.get('vale_pk'),
                                  prestador=prestador)
            v.ativo = not v.ativo
            v.save(update_fields=['ativo'])
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Vale {"reativado" if v.ativo else "encerrado"}: {v}')
            messages.success(request, f'Vale {"reativado" if v.ativo else "encerrado"}.')
            return redirect('painel_prestador', pk=pk)

    vinculos = {v.posto_id: v for v in
                PrestadorPosto.objects.filter(prestador=prestador, ativo=True)}
    postos_com_contrato = set(
        Contrato.objects.filter(prestador=prestador, posto__isnull=False)
        .values_list('posto_id', flat=True))
    linhas_postos = [{'posto': p, 'vinculo': vinculos.get(p.pk),
                      'tem_contrato': p.pk in postos_com_contrato}
                     for p in postos]
    contratos = Contrato.objects.filter(prestador=prestador) \
        .select_related('posto')
    mes_atual = timezone.localdate().replace(day=1)
    vales = []
    for v in prestador.vales.all().select_related('posto'):
        pendentes = v.parcelas_pendentes() if v.ativo else []
        vales.append({'vale': v, 'parcela_atual': v.parcela_em(mes_atual),
                      'pendentes': pendentes,
                      'descontadas': v.parcelas_total - len(pendentes)})
    # ?anexar=<posto> (vindo do vermelho da lista): abre "Anexar contrato"
    # já com o posto escolhido.
    anexar = Posto.objects.filter(pk=request.GET.get('anexar') or 0).first()
    contrato_form = ContratoAdminForm(
        initial={'posto': anexar.pk} if anexar else None)
    return render(request, 'painel/prestador_form.html', {
        'prestador': prestador, 'form': form, 'linhas_postos': linhas_postos,
        'contratos': contratos, 'usuarios': prestador.usuarios.all(),
        'vales': vales, 'vale_form': ValeForm(),
        'contrato_form': contrato_form, 'anexar': anexar, 'up': up})


@admin_required
@require_POST
def prestador_excluir(request, up, pk):
    """Soft delete — regra do projeto: NUNCA apagar de verdade. O prestador
    some das listas e os usuários dele são bloqueados; boletos, contratos e
    histórico ficam intactos no banco (auditáveis para sempre)."""
    prestador = get_object_or_404(Prestador, pk=pk)
    prestador.excluido_em = timezone.now()
    prestador.ativo = False
    prestador.save(update_fields=['excluido_em', 'ativo'])
    prestador.usuarios.update(ativo=False)
    if request.session.get('ver_como') == pk:
        request.session.pop('ver_como', None)
    AuditLog.registrar(
        AuditLog.Evento.CRUD, request,
        detalhe=f'Prestador excluído (soft): {prestador.nome} '
                f'(boletos={prestador.boletos.count()}, '
                f'contratos={prestador.contratos.count()}, '
                f'usuários bloqueados={prestador.usuarios.count()})')
    messages.success(request,
                     f'{prestador.nome} excluído (nada foi apagado do banco '
                     '— dá para restaurar pela página dele).')
    return redirect('painel_prestadores')


@admin_required
@require_POST
def prestador_restaurar(request, up, pk):
    prestador = get_object_or_404(Prestador, pk=pk)
    prestador.excluido_em = None
    prestador.ativo = True
    prestador.save(update_fields=['excluido_em', 'ativo'])
    AuditLog.registrar(AuditLog.Evento.CRUD, request,
                       detalhe=f'Prestador restaurado: {prestador.nome}')
    messages.success(request,
                     f'{prestador.nome} restaurado. Os usuários dele '
                     'continuam bloqueados — reative em Usuários quem deve '
                     'voltar a entrar.')
    return redirect('painel_prestador', pk=pk)


@admin_required
def postos(request, up):
    if request.method == 'POST':
        pk = request.POST.get('pk')
        if request.POST.get('acao') == 'excluir' and pk:
            posto = get_object_or_404(Posto, pk=pk)
            if posto.id_endereco_legado is not None:
                messages.error(request,
                               f'{posto.nome} é posto canônico do legado — '
                               'não se exclui, no máximo desmarque "ativo".')
            else:
                posto.excluido_em = timezone.now()
                posto.ativo = False
                posto.save(update_fields=['excluido_em', 'ativo'])
                posto.vinculos.update(ativo=False)
                AuditLog.registrar(AuditLog.Evento.CRUD, request,
                                   detalhe=f'Posto excluído (soft): {posto}')
                messages.success(request, f'{posto.nome} excluído (soft '
                                          'delete — continua no banco).')
            return redirect('painel_postos')
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
                  {'lista': Posto.objects.filter(excluido_em__isnull=True),
                   'form': form, 'up': up})


@admin_required
def usuarios(request, up):
    if request.method == 'POST':
        pk = request.POST.get('pk')
        if request.POST.get('acao') == 'token' and pk:
            import secrets
            u = get_object_or_404(UsuarioPermitido, pk=pk,
                                  prestador__isnull=False)
            u.api_token = secrets.token_hex(24)
            u.api_token_criado_em = timezone.now()
            u.save(update_fields=['api_token', 'api_token_criado_em'])
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Token de API gerado p/ {u.email}')
            messages.success(
                request,
                f'Token de API de {u.email} (COPIE AGORA — não será '
                f'mostrado de novo): {u.api_token}')
            return redirect('painel_usuarios')
        if request.POST.get('acao') == 'enviar_senha' and pk:
            u = get_object_or_404(UsuarioPermitido, pk=pk)
            if not u.ativo:
                messages.error(request, f'{u.email} está bloqueado — reative '
                                        'antes de enviar o acesso.')
                return redirect('painel_usuarios')
            from .views_auth import disparar_link_senha
            try:
                disparar_link_senha(request, u.email, u.nome)
                messages.success(
                    request, f'Link para criar/redefinir a senha enviado '
                             f'para {u.email}.')
            except Exception as e:
                messages.error(request, f'Não consegui enviar o e-mail para '
                                        f'{u.email} agora: {e}')
            return redirect('painel_usuarios')
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
def gerentes(request, up):
    """Posto × gerente. Fonte: CRM (espelho diário); aqui dá para forçar a
    sincronização e editar só os postos que não existem no CRM."""
    if request.method == 'POST':
        if request.POST.get('acao') == 'sync':
            import io
            from django.core.management import call_command
            saida = io.StringIO()
            call_command('sync_gerentes', stdout=saida, stderr=saida)
            messages.success(request,
                             'Sincronização executada: '
                             + saida.getvalue().strip().splitlines()[-1])
        elif request.POST.get('acao') == 'salvar':
            posto = get_object_or_404(Posto, pk=request.POST.get('pk'))
            posto.gerente_nome = (request.POST.get('gerente_nome')
                                  or '')[:120].strip()
            posto.gerente_email = (request.POST.get('gerente_email')
                                   or '').strip().lower()
            # Posto do CRM editado aqui = exceção FIXA (o espelho diário
            # não desfaz). Posto manual não tem espelho — nada a fixar.
            posto.gerente_fixo = posto.id_endereco_legado is not None
            posto.save(update_fields=['gerente_nome', 'gerente_email',
                                      'gerente_fixo'])
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Gerente de {posto.nome}: '
                                       f'{posto.gerente_nome} '
                                       f'<{posto.gerente_email}>'
                                       + (' (fixado — não espelha do CRM)'
                                          if posto.gerente_fixo else ''))
            messages.success(request, f'Gerente de {posto.nome} salvo'
                             + (' e fixado: o espelho do CRM não vai mais '
                                'sobrescrever.' if posto.gerente_fixo
                                else '.'))
        elif request.POST.get('acao') == 'liberar':
            posto = get_object_or_404(Posto, pk=request.POST.get('pk'),
                                      id_endereco_legado__isnull=False)
            posto.gerente_fixo = False
            posto.save(update_fields=['gerente_fixo'])
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Gerente de {posto.nome} volta a '
                                       'espelhar o CRM')
            import io
            from django.core.management import call_command
            saida = io.StringIO()
            call_command('sync_gerentes', stdout=saida, stderr=saida)
            messages.success(request, f'{posto.nome} voltou a espelhar o '
                             'CRM.')
        return redirect('painel_gerentes')
    lista = Posto.objects.filter(ativo=True, excluido_em__isnull=True)
    return render(request, 'painel/gerentes.html',
                  {'lista': lista, 'up': up})


@admin_required
def configuracoes(request, up):
    if request.method == 'POST':
        bruto = (request.POST.get('limiar_confianca') or '').strip()
        try:
            limiar = int(bruto)
            if not 0 <= limiar <= 100:
                raise ValueError
        except ValueError:
            messages.error(request, 'Limiar deve ser um número de 0 a 100.')
            return redirect('painel_config')
        Configuracao.definir('limiar_confianca', limiar)
        AuditLog.registrar(AuditLog.Evento.CRUD, request,
                           detalhe=f'Config: limiar_confianca={limiar}%')
        messages.success(request, f'Limiar de convicção salvo: {limiar}%.')
        return redirect('painel_config')
    return render(request, 'painel/config.html', {
        'limiar': Configuracao.get_int('limiar_confianca', 99), 'up': up})


# Tipo do e-mail pelo começo do assunto (o assunto é nosso e determinístico)
TIPOS_EMAIL = [
    ('recebido', 'Boleto recebido', 'Boleto recebido'),
    ('pagamento', 'Pagamento (equipe@)', 'Pagamento — '),
    ('aprovado', 'Aprovado (aviso ao PJ)', 'Boleto aprovado'),
    ('financeiro', 'Financeiro recebeu', 'Boleto com o financeiro'),
    ('divergente', 'Valor a confirmar', 'valor a confirmar'),
    ('manual', 'Verificar manualmente', 'Verificar boleto manualmente'),
    ('lembrete', 'Lembrete diário', 'Lembrete'),
]


@admin_required
def emails_log(request, up):
    """Lista dos e-mails enviados com filtros COMBINÁVEIS: destinatário
    (para/cc), prestador, posto, tipo, competência, status e texto livre.
    Ex.: tudo que foi para equipe@ do prestador X. Clique abre o e-mail."""
    g = request.GET
    f = {k: (g.get(k) or '').strip()
         for k in ('q', 'para', 'prestador', 'posto', 'tipo', 'mes', 'ok')}
    qs = EmailLog.objects.select_related('boleto', 'boleto__prestador',
                                         'boleto__posto')
    if f['q']:
        qs = qs.filter(Q(destinatario__icontains=f['q'])
                       | Q(assunto__icontains=f['q'])
                       | Q(corpo__icontains=f['q']))
    if f['para']:
        qs = qs.filter(destinatario__icontains=f['para'])
    if f['prestador']:
        qs = qs.filter(boleto__prestador_id=f['prestador'])
    if f['posto']:
        qs = qs.filter(boleto__posto_id=f['posto'])
    if f['tipo']:
        prefixo = {t[0]: t[2] for t in TIPOS_EMAIL}.get(f['tipo'])
        if prefixo:
            qs = qs.filter(assunto__icontains=prefixo)
    if f['mes']:  # YYYY-MM da competência do boleto
        try:
            ano, mes = (int(x) for x in f['mes'].split('-'))
            qs = qs.filter(boleto__competencia=date(ano, mes, 1))
        except ValueError:
            pass
    if f['ok'] == 'sim':
        qs = qs.filter(ok=True)
    elif f['ok'] == 'nao':
        qs = qs.filter(ok=False)

    destinos = set()
    for rot in EmailLog.objects.values_list('destinatario', flat=True):
        for parte in re.split(r'[,\s]+', rot.replace('+cc:', ' ')):
            if '@' in parte:
                destinos.add(parte.strip().lower())
    ativo = any(f.values())
    return render(request, 'painel/emails.html', {
        'lista': qs[:200], 'f': f, 'ativo': ativo,
        'destinos': sorted(destinos),
        'prestadores': Prestador.objects.filter(excluido_em__isnull=True)
                                        .order_by('nome'),
        'postos': Posto.objects.filter(ativo=True, excluido_em__isnull=True)
                               .order_by('nome'),
        'tipos': TIPOS_EMAIL, 'up': up})


def _periodo_param(request):
    """Período ?de=YYYY-MM-DD&ate=YYYY-MM-DD; padrão: do dia 1 ao último
    dia do mês vigente."""
    hoje = timezone.localdate()
    de = hoje.replace(day=1)
    ate = hoje.replace(day=monthrange(hoje.year, hoje.month)[1])
    try:
        de = date.fromisoformat(request.GET.get('de', ''))
    except ValueError:
        pass
    try:
        ate = date.fromisoformat(request.GET.get('ate', ''))
    except ValueError:
        pass
    if ate < de:
        de, ate = ate, de
    return de, ate


@admin_required
def emails_nao_reconhecidos(request, up):
    """E-mails que chegaram nas caixas de boleto (prestadores@/pj@) de
    remetente que não está na whitelist de nenhum prestador. Só lista, por
    data — nada a fazer aqui. Quando o e-mail é cadastrado num prestador, o
    robô reprocessa sozinho e a linha some da lista."""
    de, ate = _periodo_param(request)
    lista = (EmailRecebido.objects
             .filter(resultado=EmailRecebido.Resultado.SEM_PRESTADOR,
                     criado_em__date__gte=de, criado_em__date__lte=ate)
             .order_by('-criado_em'))
    return render(request, 'painel/emails_nao_reconhecidos.html', {
        'lista': lista, 'de': de, 'ate': ate,
        'caixas': settings.EMAIL_INTAKE_ALIASES, 'up': up})


@admin_required
def email_detalhe(request, up, pk):
    """O e-mail como foi enviado: cabeçalhos, texto e a versão HTML."""
    from .services.emails import _render_html
    e = get_object_or_404(EmailLog.objects.select_related('boleto'), pk=pk)
    return render(request, 'painel/email_detalhe.html',
                  {'e': e, 'html': _render_html(e.corpo), 'up': up})


@admin_required
def email_recebido_detalhe(request, up, pk):
    """O e-mail que CHEGOU na caixa de boletos — a origem do registro. Só
    texto (HTML de fora não é exibido) + os boletos que saíram dele.
    Registro antigo, de antes de o texto ser guardado: busca na caixa pelo
    Message-ID (somente leitura) na primeira vez que alguém abre."""
    e = get_object_or_404(EmailRecebido, pk=pk)
    tem_conteudo = bool(e.corpo or e.anexos or e.enviado_em)
    erro = ''
    if not tem_conteudo and (request.method == 'POST'
                             or e.corpo_buscado_em is None):
        from .services import email_recebido as svc_recebido
        try:
            tem_conteudo = svc_recebido.buscar_na_caixa(e)
        except Exception as exc:
            erro = str(exc)[:200] or exc.__class__.__name__
        if request.method == 'POST' and not erro:
            return redirect('painel_email_recebido', pk=pk)
    boletos = (e.boletos.select_related('prestador', 'posto',
                                        'prestador__posto_cobranca')
               .order_by('pk'))
    return render(request, 'painel/email_recebido.html', {
        'e': e, 'boletos': boletos, 'anexos': e.lista_anexos(),
        'tem_conteudo': tem_conteudo, 'erro': erro, 'up': up})


@admin_required
def auditoria(request, up):
    return render(request, 'painel/auditoria.html',
                  {'lista': AuditLog.objects.all()[:300], 'up': up})
