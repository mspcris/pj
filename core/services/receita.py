"""Ficha da empresa pelo CNPJ — BrasilAPI (pública, sem chave).

Só preenche o que está EM BRANCO no cadastro; situação cadastral, sócios e
data da consulta são sempre atualizados (são fatos da Receita, não
escolha do Cristiano)."""
import logging
from datetime import date

import requests
from django.utils import timezone

from ..models import AuditLog

log = logging.getLogger(__name__)
URL = 'https://brasilapi.com.br/api/cnpj/v1/{cnpj}'


def consultar_cnpj(cnpj):
    """Dict normalizado ou levanta exceção (CNPJ inválido / API fora)."""
    d = ''.join(c for c in (cnpj or '') if c.isdigit())
    if len(d) != 14:
        raise ValueError('CNPJ precisa ter 14 dígitos.')
    r = requests.get(URL.format(cnpj=d), timeout=15)
    if r.status_code == 404:
        raise ValueError('CNPJ não encontrado na Receita.')
    r.raise_for_status()
    j = r.json()

    def t(k, n=200):
        v = j.get(k)
        return str(v).strip()[:n] if v not in (None, '') else ''
    endereco = ' '.join(x for x in (
        f'{t("descricao_tipo_de_logradouro")} {t("logradouro")}'.strip(),
        f'nº {t("numero")}' if t('numero') else '',
        t('complemento')) if x).strip()
    socios = []
    for q in j.get('qsa') or []:
        nome = str(q.get('nome_socio') or '').strip()
        qual = str(q.get('qualificacao_socio') or '').strip()
        if nome:
            socios.append(f'{nome} — {qual}' if qual else nome)
    tel = t('ddd_telefone_1', 40)
    if tel and len(tel) >= 10 and tel.isdigit():
        tel = f'({tel[:2]}) {tel[2:]}'
    abertura = None
    try:
        abertura = date.fromisoformat(t('data_inicio_atividade', 10))
    except ValueError:
        pass
    return {
        'cnpj': f'{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}',
        'razao_social': t('razao_social'),
        'nome_fantasia': t('nome_fantasia'),
        'endereco': endereco[:200], 'bairro': t('bairro', 80),
        'cidade': t('municipio', 80), 'uf': t('uf', 2).upper(),
        'cep': (lambda c: f'{c[:5]}-{c[5:]}' if len(c) == 8 else '')(
            ''.join(ch for ch in t('cep', 12) if ch.isdigit())),
        'telefone': tel, 'email_empresa': t('email', 254).lower(),
        'situacao_cadastral': t('descricao_situacao_cadastral', 60),
        'abertura': abertura, 'socios': socios,
    }


def aplicar(prestador, dados, ator=''):
    """Preenche o que está em branco; situação/sócios/consulta sempre.
    Retorna os campos alterados."""
    alterados = {}
    if prestador.cnpj_digitos == ''.join(
            c for c in dados['cnpj'] if c.isdigit()) \
            and prestador.cnpj != dados['cnpj']:
        prestador.cnpj = dados['cnpj']  # só o formato (00.000.000/0000-00)
        alterados['cnpj'] = dados['cnpj']
    for campo in ('razao_social', 'nome_fantasia', 'endereco',
                  'bairro', 'cidade', 'uf', 'cep', 'telefone',
                  'email_empresa', 'abertura'):
        v = dados.get(campo)
        if v and not getattr(prestador, campo):
            setattr(prestador, campo, v)
            alterados[campo] = v
    # Representante em branco (ou igual ao nome): o sócio-administrador
    rep_vazio = (not prestador.representante
                 or prestador.representante.strip().lower()
                 == (prestador.nome or '').strip().lower())
    if rep_vazio:
        adm = next((s for s in dados['socios']
                    if 'administrador' in s.lower()), None) or (
            dados['socios'][0] if dados['socios'] else None)
        if adm:
            prestador.representante = adm.split(' — ')[0][:120].title()
            alterados['representante'] = prestador.representante
    prestador.situacao_cadastral = dados['situacao_cadastral']
    prestador.socios = '\n'.join(dados['socios'])
    prestador.receita_consultado_em = timezone.now()
    prestador.save()
    AuditLog.registrar(AuditLog.Evento.CRUD, ator=ator or 'sistema',
                       detalhe=f'{prestador}: ficha da Receita aplicada '
                               f'({dados["situacao_cadastral"]}); '
                               f'preenchidos: {list(alterados)}')
    return alterados
