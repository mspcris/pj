"""Relê os contratos já anexados para guardar, em cada um, o dia de
pagamento e a cláusula (10/11/2026). Só os ativos que ainda não foram
lidos; --todos relê mesmo os já lidos.

    .venv/bin/python manage.py ler_prazos_contratos [--todos] [--prestador ID]
"""
from django.core.management.base import BaseCommand

from core.models import Contrato
from core.services.contratos import aplicar_dados


class Command(BaseCommand):
    help = 'Lê dia de pagamento e cláusula dos contratos anexados.'

    def add_arguments(self, parser):
        parser.add_argument('--todos', action='store_true')
        parser.add_argument('--prestador', type=int)

    def handle(self, *args, **opts):
        qs = Contrato.objects.filter(desativado_em__isnull=True) \
            .exclude(arquivo='').select_related('prestador', 'posto')
        if not opts['todos']:
            qs = qs.filter(prazos_lidos_em__isnull=True)
        if opts['prestador']:
            qs = qs.filter(prestador_id=opts['prestador'])
        n = 0
        for c in qs.order_by('prestador__nome', 'posto__nome', 'pk'):
            if opts['todos']:
                c.prazos_lidos_em = None
            aplicar_dados(c)
            c.refresh_from_db()
            n += 1
            self.stdout.write(
                f'#{c.pk} {c.prestador.nome[:30]} @ {c.posto or "geral"}: '
                f'dia={c.dia_pagamento} | {c.clausula_pagamento or "-"} | '
                f'{(c.trecho_pagamento or "")[:70]}')
        self.stdout.write(self.style.SUCCESS(f'{n} contrato(s) lido(s).'))
