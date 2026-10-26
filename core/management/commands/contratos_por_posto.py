"""Divide o contrato de um prestador em UM CONTRATO POR POSTO (e um boleto
por posto), gerando os PDFs a partir de um modelo de texto e cadastrando
tudo com assinatura gov.br ligada — o mesmo procedimento feito à mão para
a Victória (23/10/2026) e o Caio (26/10/2026). Ver docs/contratos-por-posto.md.

Exemplos (no servidor, como www-data):

  # só gerar os PDFs numa pasta para o Cristiano conferir (não mexe no banco)
  manage.py contratos_por_posto --prestador 1 --postos A,N,I,X,G,Y,B,R \\
      --modelo ti --total 3990 --so-pdf /tmp/conferir

  # valer: vínculos, modo POR_POSTO, 8 contratos, desativa os antigos 43 e 44
  manage.py contratos_por_posto --prestador 1 --postos A,N,I,X,G,Y,B,R \\
      --modelo ti --total 3990 --inicio 2026-10-26 --desativar 43,44

  # sem --total nem --por-posto: usa o valor que JÁ está em cada vínculo
  manage.py contratos_por_posto --prestador 6 --postos A,N,I,X,G,Y,B,R \\
      --modelo consultoria_comercial

O que NÃO faz: não manda e-mail a ninguém (o "📨 Avisar" é clique do
gestor na ficha) e não assina nada.
"""
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, Contrato, Posto, Prestador, PrestadorPosto
from core.services import contratos_pdf
from core.services.extenso import valor_formatado

ATOR_PADRAO = 'cristiano@camim.com.br (comando contratos_por_posto)'


class Command(BaseCommand):
    help = ('Um contrato (PDF) e um boleto por posto para um prestador, a '
            'partir de um modelo de texto. Ver docs/contratos-por-posto.md.')

    def add_arguments(self, p):
        p.add_argument('--prestador', type=int, required=True, help='pk')
        p.add_argument('--postos', required=True,
                       help='letras na ordem (ex.: A,N,I,X,G,Y,B,R); sobra de '
                            'centavos vai para os primeiros')
        p.add_argument('--modelo', required=True,
                       help='nome em core/modelos_contrato/ (ti, '
                            'consultoria_comercial) ou caminho de um .json')
        g = p.add_mutually_exclusive_group()
        g.add_argument('--total', type=Decimal,
                       help='valor mensal TOTAL, dividido entre os postos')
        g.add_argument('--por-posto', type=Decimal, dest='por_posto',
                       help='valor mensal de CADA posto')
        p.add_argument('--inicio', type=date.fromisoformat,
                       default=timezone.localdate(),
                       help='início da vigência (YYYY-MM-DD; padrão hoje)')
        p.add_argument('--meses', type=int, default=12)
        p.add_argument('--dia', type=int,
                       help='dia do vencimento (padrão: dia_pagamento do '
                            'prestador, ou 10)')
        p.add_argument('--desativar', default='',
                       help='pks de contratos antigos a desativar (43,44)')
        p.add_argument('--manter-modo', action='store_true', dest='manter_modo',
                       help='NÃO trocar modo_boleto para POR_POSTO')
        p.add_argument('--por-posto-desde', dest='por_posto_desde',
                       metavar='YYYY-MM',
                       help='mês a partir do qual a régua espera um boleto '
                            'por posto (padrão: mês do 1º pagamento; antes '
                            'disso o mês segue como boleto único)')
        p.add_argument('--manter-outros-vinculos', action='store_true',
                       dest='manter_outros',
                       help='NÃO desativar vínculos de postos fora da lista')
        p.add_argument('--so-pdf', dest='so_pdf', metavar='PASTA',
                       help='só gera os PDFs nessa pasta; não mexe no banco')
        p.add_argument('--ator', default=ATOR_PADRAO,
                       help='quem fica na auditoria')
        p.add_argument('--enviado-por', dest='enviado_por',
                       default='cristiano@camim.com.br')
        p.add_argument('--nome-curto', dest='nome_curto', default='',
                       help='nome no arquivo ("Caio Guimarães"); padrão: '
                            'primeiro e último nome do representante')

    # ------------------------------------------------------------------
    def handle(self, *a, **o):
        try:
            import reportlab  # noqa: F401
        except ImportError:
            raise CommandError('instale o reportlab: pip install reportlab')
        prestador = Prestador.objects.get(pk=o['prestador'])
        letras = [x.strip().upper() for x in o['postos'].split(',') if x.strip()]
        postos = []
        for letra in letras:
            try:
                postos.append(Posto.objects.get(codigo=letra, ativo=True,
                                                excluido_em__isnull=True))
            except Posto.DoesNotExist:
                raise CommandError(f'posto com letra {letra!r} não existe/ativo')
            except Posto.MultipleObjectsReturned:
                raise CommandError(f'mais de um posto ativo com a letra {letra!r}')
        if len(set(letras)) != len(letras):
            raise CommandError('posto repetido na lista')
        modelo = contratos_pdf.carregar_modelo(o['modelo'])

        # valores por posto
        if o['total'] is not None:
            valores = contratos_pdf.dividir(o['total'], len(postos))
        elif o['por_posto'] is not None:
            valores = [Decimal(o['por_posto']).quantize(Decimal('0.01'))] * len(postos)
        else:
            valores = []
            for posto in postos:
                v = PrestadorPosto.objects.filter(prestador=prestador,
                                                  posto=posto).first()
                if not v:
                    raise CommandError(f'{posto}: sem vínculo e sem --total/'
                                       '--por-posto — não sei o valor')
                valores.append(v.valor_mensal)
        total = sum(valores)

        inicio = o['inicio']
        fim = _mais_meses(inicio, o['meses']) - timedelta(days=1)
        dia = o['dia'] or prestador.dia_pagamento or 10
        hoje = timezone.localdate()
        nome_curto = o['nome_curto'] or _nome_curto(prestador)

        self.stdout.write(f'{prestador} — {len(postos)} postos, total mensal '
                          f'{valor_formatado(total)}, vigência {inicio:%d/%m/%Y}'
                          f'–{fim:%d/%m/%Y}, vencimento dia {dia}')
        for posto, valor in zip(postos, valores):
            self.stdout.write(f'   {posto.codigo} {posto.nome}: {valor_formatado(valor)}'
                              + ('' if posto.sede else '  ⚠️ SEM SEDE cadastrada'))

        pasta = Path(o['so_pdf']) if o['so_pdf'] else Path(tempfile.mkdtemp(
            prefix='contratos_por_posto_'))
        pasta.mkdir(parents=True, exist_ok=True)
        arquivos = []
        for posto, valor in zip(postos, valores):
            ctx = contratos_pdf.contexto(prestador, posto, valor, inicio, fim,
                                         o['meses'], dia, hoje, total=total)
            nome = f'Contrato {nome_curto} - {posto.nome} - {hoje:%Y-%m-%d}.pdf'
            arquivos.append((posto, valor, nome,
                             contratos_pdf.gerar_pdf(modelo, ctx, pasta / nome)))
        if o['so_pdf']:
            self.stdout.write(self.style.SUCCESS(
                f'{len(arquivos)} PDFs em {pasta} — banco intocado (--so-pdf).'))
            return

        ator = o['ator']
        with transaction.atomic():
            if not o['manter_modo'] and (
                    prestador.modo_boleto != Prestador.ModoBoleto.POR_POSTO
                    or prestador.posto_cobranca_id or prestador.valor_unico):
                antes = {'modo_boleto': prestador.modo_boleto,
                         'posto_cobranca': str(prestador.posto_cobranca),
                         'valor_unico': str(prestador.valor_unico),
                         'por_posto_desde': str(prestador.por_posto_desde)}
                desde = ''
                if prestador.modo_boleto != Prestador.ModoBoleto.POR_POSTO:
                    # Era boleto ÚNICO: os meses já pagos num boleto só
                    # ficam como estão; a régua por posto começa no 1º
                    # pagamento dos contratos novos (ou no mês pedido).
                    if o['por_posto_desde']:
                        prestador.por_posto_desde = date.fromisoformat(
                            o['por_posto_desde'] + '-01')
                    else:
                        prestador.por_posto_desde = contratos_pdf.\
                            primeiro_pagamento(inicio, dia).replace(day=1)
                    desde = (f', um por posto a partir de '
                             f'{prestador.por_posto_desde:%m/%Y}')
                prestador.modo_boleto = Prestador.ModoBoleto.POR_POSTO
                prestador.posto_cobranca = None
                prestador.valor_unico = None
                prestador.save(update_fields=['modo_boleto', 'posto_cobranca',
                                              'valor_unico', 'por_posto_desde'])
                AuditLog.registrar(AuditLog.Evento.CRUD, ator=ator, detalhe=(
                    f'Prestador {prestador.pk} ({nome_curto}): modo de boleto → '
                    f'POR_POSTO ({valor_formatado(total)} em {len(postos)} '
                    f'postos{desde}). Antes: {antes}'))

            ativos = {}
            for posto, valor in zip(postos, valores):
                v, _ = PrestadorPosto.objects.get_or_create(
                    prestador=prestador, posto=posto,
                    defaults={'valor_mensal': valor})
                v.valor_mensal = valor
                v.ativo = True
                v.save(update_fields=['valor_mensal', 'ativo'])
                ativos[posto.codigo] = str(valor)
            n_outros = 0
            if not o['manter_outros']:
                outros = PrestadorPosto.objects.filter(
                    prestador=prestador, ativo=True).exclude(
                    posto__in=postos)
                n_outros = outros.count()
                outros.update(ativo=False)
            AuditLog.registrar(AuditLog.Evento.CRUD, ator=ator, detalhe=(
                f'Prestador {prestador.pk}: vínculos {ativos} ativos (soma '
                f'{valor_formatado(total)}); {n_outros} outros desativados'))

            novos = []
            for posto, valor, nome, caminho in arquivos:
                c = Contrato(prestador=prestador, posto=posto, nome_original=nome,
                             enviado_por=o['enviado_por'], vigencia_inicio=inicio,
                             vigencia_fim=fim, assinatura_govbr=True)
                with open(caminho, 'rb') as f:
                    c.arquivo.save(nome, File(f), save=True)
                novos.append(c)
                AuditLog.registrar(AuditLog.Evento.UPLOAD_CONTRATO, ator=ator,
                                   detalhe=(
                    f'(carga) {prestador.nome} — {nome} [assinatura gov.br, '
                    f'{valor_formatado(valor)}, vigência {inicio:%d/%m/%Y}–'
                    f'{fim:%d/%m/%Y}, modelo {o["modelo"]}]'))

            pks = [int(x) for x in o['desativar'].split(',') if x.strip()]
            agora = timezone.now()
            for c in Contrato.objects.filter(pk__in=pks, prestador=prestador,
                                             desativado_em__isnull=True):
                c.desativado_em = agora
                c.desativado_por = o['enviado_por']
                c.save(update_fields=['desativado_em', 'desativado_por'])
                AuditLog.registrar(AuditLog.Evento.CRUD, ator=ator, detalhe=(
                    f'Contrato #{c.pk} ({c.nome_original}) DESATIVADO — '
                    f'substituído pelos {len(novos)} contratos por posto de '
                    f'{hoje:%d/%m/%Y}'))
            faltou = set(pks) - {c.pk for c in Contrato.objects.filter(
                pk__in=pks, prestador=prestador)}
            if faltou:
                raise CommandError(f'--desativar: contratos {sorted(faltou)} '
                                   f'não são deste prestador — nada gravado')

        self.stdout.write(self.style.SUCCESS(
            f'OK: contratos #{novos[0].pk}–#{novos[-1].pk} criados, modo '
            f'{prestador.modo_boleto}, {len(pks)} antigos desativados. '
            f'Falta: conferir um PDF, "📨 Avisar" o prestador e assinar como '
            f'gestor em /painel/assinaturas/?prestador={prestador.pk}'))


def _nome_curto(prestador):
    """'Caio Augusto Teixeira Guimarães' → 'Caio Guimarães' (nome do arquivo)."""
    base = (prestador.representante_nome_social or prestador.representante
            or prestador.nome)
    palavras = [w for w in base.split() if not any(ch.isdigit() for ch in w)]
    if not palavras:
        return prestador.nome
    if len(palavras) == 1:
        return palavras[0].title()
    return f'{palavras[0].title()} {palavras[-1].title()}'


def _mais_meses(d, meses):
    ano = d.year + (d.month - 1 + meses) // 12
    mes = (d.month - 1 + meses) % 12 + 1
    from calendar import monthrange
    return date(ano, mes, min(d.day, monthrange(ano, mes)[1]))
