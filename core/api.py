"""API para PJs desenvolvedores anexarem boleto + nota fiscal via script.

Autenticação: header `Authorization: Bearer <token>` — o token é gerado
pelo admin na página Usuários e pertence a um usuário de prestador.

POST /api/boletos/  (multipart/form-data)
    competencia     "YYYY-MM" do mês do PAGAMENTO, não do serviço
                    (opcional; padrão = mês atual — na dúvida, omitir)
    arquivo         PDF do boleto (obrigatório)
    nota_fiscal     PDF da NF — junto ou depois (endpoint abaixo). Quem
                    exige NF e manda o boleto sem ela: entra, mas fica
                    retido até a nota chegar ("aguardando_nota_fiscal")
    posto           letra ou nome (um envio por posto no modo por-posto)
    linha_digitavel opcional
    → 201 {"id", "competencia", "posto", "posto_letra", "status",
           "valor_esperado", "tem_nota_fiscal", "aguardando_nota_fiscal"}

POST /api/boletos/<id>/nota/  (multipart/form-data, campo "nota_fiscal")
    → 200 {boleto} — anexa a NF a um boleto que já está no sistema.

POST /api/boletos/<YYYY-MM>/nota/  (campo "nota_fiscal"; "posto" opcional)
    → 200 {boleto} — o mesmo, sem precisar do id: vale o boleto do
    prestador naquele mês; havendo vários, o posto sai do campo "posto"
    ou do TOMADOR da própria nota.

POST /api/boletos/<YYYY-MM>/boleto/
    → 201 {boleto} — igual ao POST /api/boletos/, com o mês na URL.

GET /api/boletos/?competencia=YYYY-MM
    → 200 {"boletos": [...]} — os boletos do próprio prestador no mês.
"""
import re
from datetime import date

from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import AuditLog, Boleto, Prestador, UsuarioPermitido

MAX_UPLOAD = 15 * 1024 * 1024


def _autenticar(request):
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    if not token:
        return None
    return (UsuarioPermitido.objects
            .filter(api_token=token, ativo=True, prestador__isnull=False,
                    prestador__ativo=True,
                    prestador__excluido_em__isnull=True)
            .select_related('prestador').first())


def _erro(msg, status=400):
    return JsonResponse({'erro': msg}, status=status)


def _serializar(b):
    posto = b.posto_efetivo
    return {
        'id': b.pk,
        'competencia': b.competencia.strftime('%Y-%m'),
        'posto': posto.nome if posto else None,
        # a mesma letra que o POST aceita no campo "posto" (None: posto sem
        # letra, como os manuais)
        'posto_letra': (posto.codigo or None) if posto else None,
        'status': b.status,
        'situacao': b.get_status_display(),
        'valor_esperado': str(b.valor_esperado) if b.valor_esperado else None,
        'valor_extraido': str(b.valor_extraido) if b.valor_extraido else None,
        'tem_nota_fiscal': bool(b.nota_fiscal),
        # o prestador exige NF e ela ainda não veio: o boleto fica retido
        # (não vai para pagamento) até chegar por /api/boletos/<id>/nota/
        'aguardando_nota_fiscal': bool(b.prestador.exige_nf
                                       and not b.nota_fiscal),
        'criado_em': b.criado_em.isoformat(),
    }


# Boleto que não conta mais: substituído por outro, excluído ou recusado.
_FORA = [Boleto.Status.SUBSTITUIDO, Boleto.Status.DESCARTADO,
         Boleto.Status.NAO_RECONHECIDO]

_COMPETENCIA_INVALIDA = 'competencia inválida — use YYYY-MM'


def _competencia(raw):
    """"YYYY-MM" → dia 1 daquele mês; None se não for um mês válido."""
    try:
        return date.fromisoformat(raw.strip()[:7] + '-01')
    except ValueError:
        return None


def _posto_pedido(prestador, pedido):
    """Posto ATIVO do prestador pela letra ou pelo nome; None se não é dele."""
    return next((v.posto for v in
                 prestador.vinculos_ativos().select_related('posto')
                 if v.posto.codigo.upper() == pedido.upper()
                 or v.posto.nome.lower() == pedido.lower()), None)


def _posto_nao_e_dele(pedido):
    return _erro(f'posto "{pedido}" não está entre os postos ativos deste '
                 'prestador')


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def boletos(request):
    up = _autenticar(request)
    if up is None:
        return _erro('token ausente ou inválido', status=401)
    prestador = up.prestador

    comp_raw = (request.POST.get('competencia')
                or request.GET.get('competencia') or '').strip()
    if comp_raw:
        competencia = _competencia(comp_raw)
        if competencia is None:
            return _erro(_COMPETENCIA_INVALIDA)
    else:
        competencia = timezone.localdate().replace(day=1)

    if request.method == 'GET':
        qs = (Boleto.objects
              .filter(prestador=prestador, competencia=competencia)
              .exclude(status__in=_FORA)
              .select_related('prestador', 'posto',
                              'prestador__posto_cobranca'))
        return JsonResponse({'competencia': competencia.strftime('%Y-%m'),
                             'boletos': [_serializar(b) for b in qs]})

    return _criar_boleto(request, up, competencia)


@csrf_exempt
@require_http_methods(['POST'])
def boleto_do_mes(request, competencia):
    """O POST de /api/boletos/ com o mês na URL (sugestão do Robson,
    01/10/2026). Mesmas regras — inclusive a do mês vigente: boleto de
    outro mês entra, mas não vai sozinho ao financeiro.

    POST /api/boletos/<YYYY-MM>/boleto/  → 201 {boleto} | 4xx {"erro": ...}
    """
    up = _autenticar(request)
    if up is None:
        return _erro('token ausente ou inválido', status=401)
    comp = _competencia(competencia)
    if comp is None:
        return _erro(_COMPETENCIA_INVALIDA)
    return _criar_boleto(request, up, comp)


def _criar_boleto(request, up, competencia):
    prestador = up.prestador
    arquivo = request.FILES.get('arquivo') or request.FILES.get('boleto')
    if not arquivo:
        if request.FILES.get('nota_fiscal') or request.FILES.get('nota'):
            # só a nota, sem boleto: a nota avulsa tem rota própria
            return _erro('esta rota cria um boleto novo e exige o campo '
                         '"arquivo". Para mandar SÓ a nota fiscal de um '
                         'boleto que já está no sistema, use POST '
                         '/api/boletos/<competencia>/nota/ (ex.: '
                         '/api/boletos/2026-10/nota/, campo "nota_fiscal") '
                         'ou /api/boletos/<id>/nota/')
        return _erro('envie o campo "arquivo" com o PDF do boleto')
    if arquivo.size > MAX_UPLOAD:
        return _erro('arquivo maior que 15 MB')
    if not arquivo.name.lower().endswith('.pdf') or \
            arquivo.read(5) != b'%PDF-':
        return _erro('arquivo do boleto precisa ser um PDF válido')
    arquivo.seek(0)

    nf = request.FILES.get('nota_fiscal')
    if nf:
        if nf.size > MAX_UPLOAD:
            return _erro('nota fiscal maior que 15 MB')
        if nf.name.lower().endswith('.pdf'):
            if nf.read(5) != b'%PDF-':
                return _erro('nota fiscal não é um PDF válido')
            nf.seek(0)
    # Quem exige NF pode mandar o boleto ANTES da nota (01/10/2026): ele
    # entra, mas a verificação o retém em "precisam de você" — nada vai
    # ao financeiro até a nota chegar (ou o admin liberar na mão).

    posto = None
    if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
        pedido = (request.POST.get('posto') or '').strip()
        if pedido:
            posto = _posto_pedido(prestador, pedido)
            if posto is None:
                return _posto_nao_e_dele(pedido)
        else:
            vinculos = list(prestador.vinculos_ativos()
                            .select_related('posto'))
            if len(vinculos) == 1:
                posto = vinculos[0].posto
        # sem posto e vários vínculos: segue sem — o CNPJ do sacado no PDF
        # destina sozinho na verificação.

    linha = re.sub(r'\D', '', request.POST.get('linha_digitavel', ''))
    if linha and not 40 <= len(linha) <= 48:
        return _erro('linha_digitavel deve ter 47 ou 48 dígitos')

    from .services import boletos as svc_boletos
    boleto = svc_boletos.registrar(
        prestador, competencia, enviado_por=up.email, posto=posto,
        arquivo=arquivo, nome_original=arquivo.name,
        linha_digitavel=linha, nota_fiscal=nf,
        nota_fiscal_nome=nf.name if nf else '')
    AuditLog.registrar(AuditLog.Evento.UPLOAD_BOLETO, request,
                       ator=up.email,
                       detalhe=f'(api) Boleto #{boleto.pk} {boleto}')
    from .services.verificacao import fluxo_completo_async
    fluxo_completo_async(boleto.pk)
    return JsonResponse(_serializar(boleto), status=201)


@csrf_exempt
@require_http_methods(['POST'])
def nota(request, pk):
    """Anexa a NOTA FISCAL a um boleto que já existe — inclusive um já enviado
    para pagamento (o emissor da NFS-e caiu e a nota veio depois). Valida que
    é uma NFS-e do prestador; se o boleto já foi ao financeiro, a nota segue
    como complemento do pagamento. POST multipart, campo "nota_fiscal" (PDF).

    POST /api/boletos/<id>/nota/  → 200 {boleto} | 4xx {"erro": ...}
    """
    up = _autenticar(request)
    if up is None:
        return _erro('token ausente ou inválido', status=401)
    boleto = (Boleto.objects
              .filter(pk=pk, prestador=up.prestador)
              .exclude(status__in=_FORA)
              .first())
    if boleto is None:
        return _erro('boleto não encontrado para este prestador', status=404)
    nf, erro = _nota_enviada(request)
    if erro:
        return _erro(erro)
    return _anexar_nota(request, up, boleto, nf)


@csrf_exempt
@require_http_methods(['POST'])
def nota_do_mes(request, competencia):
    """Como `nota`, sem precisar do id (sugestão do Robson, 01/10/2026): vale
    o boleto do prestador naquele mês. Quem atende vários postos tem um por
    posto — aí o posto sai do campo "posto" ou do TOMADOR da própria nota.
    Mais de um boleto do mesmo posto (parciais, extra): só pelo id.

    POST /api/boletos/<YYYY-MM>/nota/  → 200 {boleto} | 4xx {"erro": ...}
    """
    up = _autenticar(request)
    if up is None:
        return _erro('token ausente ou inválido', status=401)
    prestador = up.prestador
    comp = _competencia(competencia)
    if comp is None:
        return _erro(_COMPETENCIA_INVALIDA)
    nf, erro = _nota_enviada(request)
    if erro:
        return _erro(erro)

    mes = comp.strftime('%Y-%m')
    # DUPLICADO fica de fora: a nota é do boleto que valeu, não do repetido.
    vivos = list(Boleto.objects
                 .filter(prestador=prestador, competencia=comp)
                 .exclude(status__in=_FORA + [Boleto.Status.DUPLICADO])
                 .select_related('prestador', 'posto',
                                 'prestador__posto_cobranca')
                 .order_by('pk'))
    onde = ''
    if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
        posto = None
        pedido = (request.POST.get('posto') or '').strip()
        if pedido:
            posto = _posto_pedido(prestador, pedido)
            if posto is None:
                return _posto_nao_e_dele(pedido)
        elif len(vivos) > 1:
            from .services import boletos as svc_boletos, pdf as svc_pdf
            posto = svc_boletos.identificar_posto(
                svc_pdf.extrair_texto_bytes(nf.read()))
            nf.seek(0)
            if posto is None:
                return _erro(f'há {len(vivos)} boletos em {mes} e não deu '
                             'para ler na nota de qual posto ela é — envie '
                             'também o campo "posto" (letra ou nome)')
        if posto is not None:
            vivos = [b for b in vivos if b.posto_id == posto.pk]
            onde = f' de {posto.nome}'

    if not vivos:
        return _erro(f'nenhum boleto{onde} em {mes} para receber a nota',
                     status=404)
    if len(vivos) > 1:
        ids = ', '.join(str(b.pk) for b in vivos)
        return _erro(f'há {len(vivos)} boletos{onde} em {mes} (ids {ids}) — '
                     'diga qual por /api/boletos/<id>/nota/')
    return _anexar_nota(request, up, vivos[0], nf)


def _nota_enviada(request):
    """(arquivo, '') com o PDF da nota do request, ou (None, motivo)."""
    nf = request.FILES.get('nota_fiscal') or request.FILES.get('nota')
    if not nf:
        return None, 'envie o campo "nota_fiscal" com o PDF da nota'
    if nf.size > MAX_UPLOAD:
        return None, 'nota fiscal maior que 15 MB'
    if not nf.name.lower().endswith('.pdf') or nf.read(5) != b'%PDF-':
        return None, 'nota fiscal precisa ser um PDF válido'
    nf.seek(0)
    return nf, ''


def _anexar_nota(request, up, boleto, nf):
    """Caminho único da nota avulsa (por id ou por competência): valida,
    anexa e segue como complemento ou devolve o boleto à verificação."""
    from .services import boletos as svc_boletos, pdf as svc_pdf
    from .services.verificacao import (enviar_nota_posterior,
                                       processar_async)
    boleto.nota_fiscal = nf
    boleto.nota_fiscal_nome = nf.name
    boleto.save(update_fields=['nota_fiscal', 'nota_fiscal_nome'])
    # Conteúdo: precisa ser NFS-e do prestador. Se falhar, desfaz e recusa —
    # pela API o prestador reenvia o arquivo certo.
    ok_nf, motivo = svc_boletos.validar_nf(
        svc_pdf.extrair_texto(boleto.nota_fiscal.path), boleto.prestador,
        posto=boleto.posto)
    if not ok_nf:
        boleto.nota_fiscal.delete(save=False)
        boleto.nota_fiscal = None
        boleto.nota_fiscal_nome = ''
        boleto.save(update_fields=['nota_fiscal', 'nota_fiscal_nome'])
        return _erro(f'nota fiscal recusada: {motivo}')

    ja_enviado = (boleto.pagamento_enviado_em is not None
                  or boleto.status in (Boleto.Status.APROVADO,
                                       Boleto.Status.FIN_RECEBIDO,
                                       Boleto.Status.PAGO))
    AuditLog.registrar(AuditLog.Evento.UPLOAD_BOLETO, request, ator=up.email,
                       detalhe=f'(api) nota fiscal anexada ao boleto '
                               f'#{boleto.pk}')
    if ja_enviado:
        enviar_nota_posterior(boleto)
    else:
        # Ainda não foi ao financeiro (ex.: retido esperando esta nota):
        # volta para a verificação, igual à edição no painel — com o
        # esperado recalculado e SEM repetir o e-mail de "recebemos".
        boleto.status = Boleto.Status.RECEBIDO
        boleto.tentativas = 0
        boleto.verificado_em = None
        boleto.valor_esperado = None
        boleto.save(update_fields=['status', 'tentativas', 'verificado_em',
                                   'valor_esperado'])
        processar_async(boleto.pk)
    return JsonResponse(_serializar(boleto), status=200)
