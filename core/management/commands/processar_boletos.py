"""Rede de segurança da verificação: reprocessa boletos RECEBIDOS que a
thread do upload não conseguiu concluir (deploy no meio, Groq fora, etc.).

Cron sugerido (a cada 10 min):
    */10 * * * * cd /opt/pj && .venv/bin/python manage.py processar_boletos
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Boleto
from core.services.verificacao import MAX_TENTATIVAS, processar


class Command(BaseCommand):
    help = 'Verifica boletos RECEBIDOS pendentes (rede de segurança do cron).'

    def handle(self, *args, **opts):
        corte = timezone.now() - timedelta(minutes=5)
        pendentes = Boleto.objects.filter(
            status=Boleto.Status.RECEBIDO,
            criado_em__lt=corte,
            tentativas__lt=MAX_TENTATIVAS,
        ).values_list('pk', flat=True)
        for pk in pendentes:
            self.stdout.write(f'Processando boleto #{pk}...')
            processar(pk)
        self.stdout.write(self.style.SUCCESS(
            f'{len(pendentes)} boleto(s) processado(s).'))
