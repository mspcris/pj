"""Testes dos fluxos críticos: whitelist, upload, verificação e permissões."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import Boleto, Posto, Prestador, PrestadorPosto, UsuarioPermitido
from .services import verificacao

User = get_user_model()

PDF_MINI = b'%PDF-1.4 conteudo de teste'


def _pdf(nome='boleto.pdf'):
    return SimpleUploadedFile(nome, PDF_MINI, content_type='application/pdf')


class BaseSetup(TestCase):
    def setUp(self):
        self.posto1 = Posto.objects.get(codigo='A')   # Anchieta (seed)
        self.posto2 = Posto.objects.get(codigo='B')   # Bangu (seed)
        self.prestador = Prestador.objects.create(nome='Limpeza Total LTDA')
        PrestadorPosto.objects.create(prestador=self.prestador,
                                      posto=self.posto1,
                                      valor_mensal=Decimal('1500.00'))
        PrestadorPosto.objects.create(prestador=self.prestador,
                                      posto=self.posto2,
                                      valor_mensal=Decimal('2000.00'))
        self.up = UsuarioPermitido.objects.create(
            email='pj@empresa.com.br', prestador=self.prestador)
        self.user = User.objects.create_user(
            username='pj@empresa.com.br', email='pj@empresa.com.br')

        self.admin_up = UsuarioPermitido.objects.create(
            email='cristiano@camim.com.br', is_admin=True)
        self.admin = User.objects.create_user(
            username='cristiano@camim.com.br', email='cristiano@camim.com.br')

    def login_pj(self):
        self.client.force_login(self.user)

    def login_admin(self):
        self.client.force_login(self.admin)


class WhitelistTest(BaseSetup):
    def test_anonimo_redireciona_para_login(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/oidc/authenticate/', resp.url)

    def test_logado_sem_whitelist_cai_em_sem_acesso(self):
        intruso = User.objects.create_user(username='x@camim.com.br',
                                           email='x@camim.com.br')
        self.client.force_login(intruso)
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/sem-acesso/', resp.url)

    def test_pj_bloqueado_nao_entra(self):
        self.up.ativo = False
        self.up.save()
        self.login_pj()
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)

    def test_seed_postos(self):
        self.assertEqual(
            Posto.objects.filter(id_endereco_legado__isnull=False).count(), 13)
        self.assertFalse(Posto.objects.filter(nome__icontains='Méier').exists())


class UploadBoletoTest(BaseSetup):
    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_upload_por_posto(self, m_async):
        self.login_pj()
        hoje = date.today().replace(day=1)
        resp = self.client.post('/boleto/', {
            'competencia': hoje.isoformat(),
            'posto': self.posto1.pk,
            'arquivo': _pdf(),
        })
        self.assertEqual(resp.status_code, 302)
        b = Boleto.objects.get()
        self.assertEqual(b.valor_esperado, Decimal('1500.00'))
        self.assertEqual(b.status, Boleto.Status.RECEBIDO)
        m_async.assert_called_once_with(b.pk)

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_reenvio_substitui_anterior(self, m_async):
        self.login_pj()
        hoje = date.today().replace(day=1)
        dados = {'competencia': hoje.isoformat(), 'posto': self.posto1.pk}
        self.client.post('/boleto/', {**dados, 'arquivo': _pdf()})
        self.client.post('/boleto/', {**dados, 'arquivo': _pdf('novo.pdf')})
        status = list(Boleto.objects.order_by('pk')
                      .values_list('status', flat=True))
        self.assertEqual(status, [Boleto.Status.SUBSTITUIDO,
                                  Boleto.Status.RECEBIDO])

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_boleto_unico_soma_postos(self, m_async):
        self.prestador.modo_boleto = Prestador.ModoBoleto.UNICO
        self.prestador.posto_cobranca = self.posto1
        self.prestador.save()
        self.login_pj()
        hoje = date.today().replace(day=1)
        self.client.post('/boleto/', {'competencia': hoje.isoformat(),
                                      'arquivo': _pdf()})
        b = Boleto.objects.get()
        self.assertIsNone(b.posto)
        self.assertEqual(b.valor_esperado, Decimal('3500.00'))

    def test_rejeita_nao_pdf(self):
        self.login_pj()
        hoje = date.today().replace(day=1)
        falso = SimpleUploadedFile('boleto.pdf', b'GIF89a nada de pdf')
        resp = self.client.post('/boleto/', {
            'competencia': hoje.isoformat(), 'posto': self.posto1.pk,
            'arquivo': falso})
        self.assertEqual(resp.status_code, 200)  # volta com erro
        self.assertEqual(Boleto.objects.count(), 0)


@override_settings(EMAIL_MODO_TESTE=True)
class VerificacaoTest(BaseSetup):
    def _boleto(self, valor_esperado='1500.00'):
        return Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1),
            arquivo=_pdf(), enviado_por='pj@empresa.com.br',
            valor_esperado=Decimal(valor_esperado))

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'), '{"valor":"1500.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='BOLETO 1500')
    def test_valor_bate_aprova_e_envia_pagador(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        destinos = [c.args[0] for c in m_mail.call_args_list]
        self.assertIn('equipe@camim.com.br', destinos)      # pagador
        self.assertIn('pj@empresa.com.br', destinos)         # aviso ao PJ

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1800.00'), '{"valor":"1800.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='BOLETO 1800')
    def test_valor_diverge_avisa_pj(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.DIVERGENTE)
        destinos = [c.args[0] for c in m_mail.call_args_list]
        self.assertNotIn('equipe@camim.com.br', destinos)  # NÃO paga divergente
        self.assertIn('pj@empresa.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.pdf.extrair_texto', return_value='')
    def test_pdf_ilegivel_vira_manual(self, m_pdf, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        destinos = [c.args[0] for c in m_mail.call_args_list]
        self.assertIn('cristiano@camim.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'), '{}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_nao_processa_duas_vezes(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        chamadas = m_mail.call_count
        verificacao.processar(b.pk)  # já não está RECEBIDO
        self.assertEqual(m_mail.call_count, chamadas)


class PermissoesTest(BaseSetup):
    def test_pj_nao_baixa_boleto_de_outro(self):
        outro = Prestador.objects.create(nome='Outra Empresa')
        b = Boleto.objects.create(prestador=outro, posto=self.posto1,
                                  competencia=date(2026, 9, 1), arquivo=_pdf())
        self.login_pj()
        resp = self.client.get(f'/arquivo/boleto/{b.pk}/')
        self.assertEqual(resp.status_code, 404)

    def test_admin_baixa_tudo(self):
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1), arquivo=_pdf())
        self.login_admin()
        resp = self.client.get(f'/arquivo/boleto/{b.pk}/')
        self.assertEqual(resp.status_code, 200)

    def test_pj_nao_ve_painel(self):
        self.login_pj()
        resp = self.client.get('/painel/')
        self.assertEqual(resp.status_code, 302)

    def test_dashboard_mostra_faltantes(self):
        self.login_admin()
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'SEM BOLETO')
        self.assertContains(resp, 'Limpeza Total')


class PainelAcaoTest(BaseSetup):
    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_aprovar_manual_envia_pagador(self, m_mail):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.MANUAL, valor_esperado=Decimal('1500.00'))
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/aprovar/')
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertEqual(m_mail.call_args.args[0], 'equipe@camim.com.br')

    def test_marcar_pago(self):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.APROVADO)
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/pagar/')
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.PAGO)
        self.assertIsNotNone(b.pago_em)
