"""API para PJs desenvolvedores anexarem boleto + nota fiscal via script.

Autenticação: header `Authorization: Bearer <token>` — o token é gerado
pelo admin na página Usuários e pertence a um usuário de prestador.

POST /api/boletos/  (multipart/form-data)
    competencia     "YYYY-MM" (opcional; padrão = mês atual)
    arquivo         PDF do boleto (obrigatório)
    nota_fiscal     PDF da NF (obrigatório se o prestador exige NF)
    posto           letra ou nome (só no modo por-posto com vários postos)
    linha_digitavel opcional
    → 201 {"id", "competencia", "posto", "status", "valor_esperado"}

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
    return {
        'id': b.pk,
        'competencia': b.competencia.strftime('%Y-%m'),
        'posto': b.posto_efetivo.nome if b.posto_efetivo else None,
        'status': b.status,
        'situacao': b.get_status_display(),
        'valor_esperado': str(b.valor_esperado) if b.valor_esperado else None,
        'valor_extraido': str(b.valor_extraido) if b.valor_extraido else None,
        'tem_nota_fiscal': bool(b.nota_fiscal),
        'criado_em': b.criado_em.isoformat(),
    }


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
        try:
            competencia = date.fromisoformat(comp_raw[:7] + '-01')
        except ValueError:
            return _erro('competencia inválida — use YYYY-MM')
    else:
        competencia = timezone.localdate().replace(day=1)

    if request.method == 'GET':
        qs = (Boleto.objects
              .filter(prestador=prestador, competencia=competencia)
              .exclude(status__in=[Boleto.Status.SUBSTITUIDO,
                                   Boleto.Status.DESCARTADO]))
        return JsonResponse({'competencia': competencia.strftime('%Y-%m'),
                             'boletos': [_serializar(b) for b in qs]})

    # POST — anexar boleto
    arquivo = request.FILES.get('arquivo') or request.FILES.get('boleto')
    if not arquivo:
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
    elif prestador.exige_nf:
        return _erro(f'{prestador.nome} exige nota fiscal anexa — envie o '
                     'campo "nota_fiscal"')

    posto = None
    if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
        vinculos = list(prestador.vinculos_ativos().select_related('posto'))
        pedido = (request.POST.get('posto') or '').strip()
        if pedido:
            posto = next((v.posto for v in vinculos
                          if v.posto.codigo.upper() == pedido.upper()
                          or v.posto.nome.lower() == pedido.lower()), None)
            if posto is None:
                return _erro(f'posto "{pedido}" não está entre os postos '
                             'ativos deste prestador')
        elif len(vinculos) == 1:
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
