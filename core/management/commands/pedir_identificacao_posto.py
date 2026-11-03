"""Pede ao prestador a empresa pagadora dos boletos que chegaram SEM posto
identificável (PJ atende vários postos; o e-mail trouxe só a linha
digitável ou um PDF sem CNPJ/razão social de posto nenhum).

O robô da caixa já faz isso sozinho para e-mail novo. Este comando cobre
o que ficou para trás (03/11/2026: os boletos #173–#178 do Elias chegaram
antes da regra existir) e serve para reenviar o pedido à mão.

    manage.py pedir_identificacao_posto            # todos os pendentes
    manage.py pedir_identificacao_posto 173 174    # só estes
    manage.py pedir_identificacao_posto --probe    # só lista, não envia
    manage.py pedir_identificacao_posto --de-novo  # mesmo os já avisados

Mesmo caminho de e-mail do sistema (emails.enviar: log, cópia oculta,
trava de cc). Nunca manda para quem já foi avisado, salvo --de-novo.
"""
from django.core.management.base import BaseCommand

from core.models import Boleto
from core.services import boletos as svc_boletos
from core.services.verificacao import pedir_identificacao_posto


class Command(BaseCommand):
    help = 'Pede ao PJ a empresa pagadora dos boletos sem posto identificável.'

    def add_arguments(self, parser):
        parser.add_argument('boletos', nargs='*', type=int,
                            help='IDs dos boletos (padrão: todos pendentes)')
        parser.add_argument('--probe', action='store_true',
                            help='Só lista o que faria, sem enviar.')
        parser.add_argument('--de-novo', action='store_true',
                            help='Reenvia mesmo a quem já foi avisado.')

    def handle(self, *args, **opts):
        qs = (Boleto.objects
              .filter(posto__isnull=True, extra=False,
                      status__in=[Boleto.Status.RECEBIDO,
                                  Boleto.Status.MANUAL])
              .select_related('prestador', 'email_origem')
              .order_by('pk'))
        if opts['boletos']:
            qs = qs.filter(pk__in=opts['boletos'])
        n = 0
        for b in qs:
            if not svc_boletos.sem_posto_identificavel(b):
                self.stdout.write(f'  #{b.pk} {b}: posto dedutível — pulado')
                continue
            if b.pedido_posto_em and not opts['de_novo']:
                self.stdout.write(
                    f'  #{b.pk} {b}: já avisado em '
                    f'{b.pedido_posto_em:%d/%m/%Y %H:%M} — pulado')
                continue
            if opts['probe']:
                self.stdout.write(f'[probe] #{b.pk} {b} — '
                                  f'{b.prestador.lista_emails_aviso()}')
                continue
            ok = pedir_identificacao_posto(b)
            n += ok
            self.stdout.write(self.style.SUCCESS(f'  #{b.pk} {b}: pedido '
                                                 'enviado') if ok else
                              self.style.ERROR(f'  #{b.pk} {b}: FALHOU'))
        self.stdout.write(f'{n} pedido(s) enviado(s).')
