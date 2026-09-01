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

from .forms import (BoletoAdminForm, PostoForm, PrestadorForm, UsuarioForm,
                    ValeForm, ValorBRField)
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
                if b.status == Boleto.Status.DUPLICADO or b.extra:
                    continue  # duplicado/extra nunca representam a régua
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
    elif acao == 'descartar' and boleto.status not in (Boleto.Status.APROVADO,
                                                       Boleto.Status.PAGO):
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
        # Aprovação manual (o Cristiano conferiu no olho) OU reenvio de um
        # aprovado cujo e-mail não chegou → envia p/ pagamento + avisa o PJ.
        from django.conf import settings
        from .services import emails, frases
        from .services.verificacao import (_fatos, _moeda, dados_pagamento,
                                           dados_pj, destinatarios_pj)
        boleto.status = Boleto.Status.APROVADO
        boleto.verificado_em = timezone.now()
        boleto.save(update_fields=['status', 'verificado_em'])
        fatos = _fatos(boleto)
        fatos['valor'] = _moeda(boleto.valor_extraido or boleto.valor_esperado)
        emails.enviar(
            settings.EMAIL_PAGADOR,
            f'Pagamento — {fatos["prestador"]} — {fatos["alvo"]} — '
            f'{fatos["competencia"]} — R$ {fatos["valor"]}',
            frases.corpo('aprovado_pagador', fatos)
            + dados_pagamento(boleto, fatos),
            boleto=boleto,
            anexo_field=boleto.arquivo if boleto.arquivo else None,
            anexos=([(boleto.nota_fiscal,
                      boleto.nota_fiscal_nome or 'nota-fiscal.pdf')]
                    if boleto.nota_fiscal else None),
            de=settings.EMAIL_FROM_PAGADOR)
        emails.enviar(
            destinatarios_pj(boleto),
            f'Boleto aprovado e enviado p/ pagamento — {fatos["competencia"]}',
            frases.corpo('aprovado_pj', fatos) + dados_pj(boleto, fatos),
            boleto=boleto)
        messages.success(request, f'{boleto} enviado p/ pagamento '
                                  '(equipe e prestador avisados).')
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
        form = BoletoAdminForm()
    return render(request, 'painel/boleto_form.html', {'form': form, 'up': up})


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
                     or d['extra'] != boleto.extra)
            boleto.posto = posto
            boleto.competencia = d['competencia']
            boleto.linha_digitavel = d['linha_digitavel']
            boleto.chave_pix = d['chave_pix'].strip()
            boleto.valor_livre = d['valor_livre']
            boleto.extra = d['extra']
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
    vales = [{'vale': v, 'parcela_atual': v.parcela_em(mes_atual)}
             for v in prestador.vales.all().select_related('posto')]
    return render(request, 'painel/prestador_form.html', {
        'prestador': prestador, 'form': form, 'linhas_postos': linhas_postos,
        'contratos': contratos, 'usuarios': prestador.usuarios.all(),
        'vales': vales, 'vale_form': ValeForm(), 'up': up})


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


@admin_required
def emails_log(request, up):
    return render(request, 'painel/emails.html',
                  {'lista': EmailLog.objects.select_related('boleto')[:150],
                   'up': up})


@admin_required
def auditoria(request, up):
    return render(request, 'painel/auditoria.html',
                  {'lista': AuditLog.objects.all()[:300], 'up': up})
