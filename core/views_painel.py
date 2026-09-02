"""Painel administrativo do Cristiano.

O coração é o dashboard mensal: a RÉGUA (quem deveria mandar boleto e de
quanto) × o que chegou — para NUNCA esquecer um pagamento.
"""
import re
from datetime import date
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (BoletoAdminForm, ContratoAdminForm, PostoForm,
                    PrestadorForm, UsuarioForm, ValeForm, ValorBRField)
from .models import (AuditLog, Boleto, Configuracao, Contrato, EmailLog,
                     Posto, Prestador, PrestadorPosto, UsuarioPermitido, Vale)
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
                             Boleto.Status.DESCARTADO])
        .select_related('prestador', 'posto', 'prestador__posto_cobranca'))

    from .services import boletos as svc_boletos
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
            if linhas[-1]['diferenca'] is not None:
                from .services.verificacao import _moeda
                d = linhas[-1]['diferenca']
                linhas[-1]['diferenca_txt'] = (
                    ('+' if d > 0 else '−') + 'R$ ' + _moeda(abs(d)))

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
    resumo_filtro = {
        'previsto': previsto, 'entrou': entrou,
        'falta': max(Decimal('0'), previsto - entrou),
        'passou': max(Decimal('0'), entrou - previsto),
        'pendentes': pendentes_valor,
        'postos': len(linhas),
        'sem_boleto': sum(1 for l in linhas
                          if l['boleto'] is None and not l['parciais']),
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
                        if l['boleto'] is None and not l['parciais']),
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
        'ant': ant, 'prox': prox, 'linhas': linhas, 'extras': extras,
        'parciais_mes': parciais_mes,
        'pendentes_baixo': len(parciais_mes) + len(fora_da_regua)
                           + len(extras),
        'fora_da_regua': fora_da_regua, 'resumo': resumo, 'up': up})


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
        boleto.verificado_em = timezone.now()
        boleto.save(update_fields=['status', 'verificado_em'])
        resultado = enviar_para_pagamento(boleto, reenviar=reenviar)
        if 'NADA' in resultado:
            messages.warning(request, f'{boleto}: {resultado}')
        else:
            messages.success(request, f'{boleto} — {resultado}')
    else:
        messages.error(request, 'Ação não permitida para este status.')
    AuditLog.registrar(AuditLog.Evento.STATUS, request,
                       detalhe=f'Ação "{acao}" no boleto #{pk}')
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
                observacao=form.cleaned_data['observacao'])
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
        form = BoletoEditForm(boleto, request.POST)
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
            boleto.valor_esperado = (
                None if d['extra'] else svc_boletos.valor_esperado_para(
                    prestador, posto, d['competencia']))
            if mudou and boleto.status not in (Boleto.Status.PAGO,
                                               Boleto.Status.SUBSTITUIDO):
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
            AuditLog.registrar(AuditLog.Evento.CRUD, request,
                               detalhe=f'Boleto #{boleto.pk} editado')
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
    lista = (Prestador.objects.filter(excluido_em__isnull=True)
             .prefetch_related('vinculos__posto', 'usuarios'))
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
                Contrato.objects.create(
                    prestador=prestador, posto=cform.cleaned_data['posto'],
                    arquivo=arq, nome_original=arq.name[:255],
                    enviado_por=up.email,
                    vigencia_inicio=cform.cleaned_data['vigencia_inicio'],
                    vigencia_fim=cform.cleaned_data['vigencia_fim'])
                AuditLog.registrar(AuditLog.Evento.UPLOAD_CONTRATO, request,
                                   detalhe=f'(painel) {prestador} — '
                                           f'{arq.name[:60]}')
                messages.success(request, 'Contrato anexado.')
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
    return render(request, 'painel/prestador_form.html', {
        'prestador': prestador, 'form': form, 'linhas_postos': linhas_postos,
        'contratos': contratos, 'usuarios': prestador.usuarios.all(),
        'vales': vales, 'vale_form': ValeForm(),
        'contrato_form': ContratoAdminForm(), 'up': up})


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


@admin_required
def email_detalhe(request, up, pk):
    """O e-mail como foi enviado: cabeçalhos, texto e a versão HTML."""
    from .services.emails import _render_html
    e = get_object_or_404(EmailLog.objects.select_related('boleto'), pk=pk)
    return render(request, 'painel/email_detalhe.html',
                  {'e': e, 'html': _render_html(e.corpo), 'up': up})


@admin_required
def auditoria(request, up):
    return render(request, 'painel/auditoria.html',
                  {'lista': AuditLog.objects.all()[:300], 'up': up})
