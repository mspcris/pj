"""Espelha os gerentes dos postos a partir do CADASTRO DE GESTORES DO CRM
(Postgres do /opt/crm, tabelas gestores + id_posto = id_endereco).

Mesmas regras do sync_gerentes do relatorio_h_t (decisão 10/08/2026):
  * o CRM é a FONTE ÚNICA — edite lá (crm.camim.com.br/admin); aqui espelha;
  * posto com mais de um gestor ativo: ganha o de MENOR id (mais antigo);
  * falha de conexão NÃO apaga nada — mantém o espelho anterior.

Cron sugerido (diário):
    30 7 * * * cd /opt/pj && .venv/bin/python manage.py sync_gerentes
"""
import os

from django.core.management.base import BaseCommand

from core.models import Posto


class Command(BaseCommand):
    help = 'Espelha gerente_nome/gerente_email dos postos a partir do CRM.'

    def handle(self, *args, **opts):
        try:
            import psycopg2
            from dotenv import dotenv_values
        except ImportError as e:
            self.stderr.write(f'dependência ausente: {e}')
            return
        caminho = os.getenv('CRM_ENV_PATH', '/opt/crm/.env')
        cfg = dotenv_values(caminho)
        if not cfg.get('DB_HOST'):
            self.stderr.write(f'{caminho} sem DB_HOST — nada espelhado '
                              '(espelho anterior mantido).')
            return
        try:
            conn = psycopg2.connect(
                host=cfg['DB_HOST'], port=cfg.get('DB_PORT', 5432),
                dbname=cfg['DB_NAME'], user=cfg['DB_USER'],
                password=cfg['DB_PASSWORD'], connect_timeout=10)
        except Exception as e:
            self.stderr.write(f'CRM inacessível ({e}) — espelho anterior '
                              'mantido.')
            return
        try:
            cur = conn.cursor()
            cur.execute('SELECT id, nome, email, id_posto FROM gestores '
                        'WHERE ativo = 1 ORDER BY id')
            titular = {}
            for gid, nome, email, id_posto in cur.fetchall():
                titular.setdefault(id_posto, (nome, email))  # menor id ganha
        finally:
            conn.close()

        n = 0
        for id_posto, (nome, email) in titular.items():
            atualizado = Posto.objects.filter(
                id_endereco_legado=id_posto).update(
                gerente_nome=(nome or '')[:120],
                gerente_email=(email or '').strip().lower())
            if atualizado:
                n += 1
                self.stdout.write(f'  id_posto {id_posto}: {nome} '
                                  f'<{email}>')
        self.stdout.write(self.style.SUCCESS(
            f'{n} posto(s) com gerente espelhado do CRM.'))
