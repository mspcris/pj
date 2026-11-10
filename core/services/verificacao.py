"""Orquestração da verificação de boleto.

Fluxo: upload → e-mail "recebemos" → extrai texto do PDF → IA extrai o valor
→ compara com o valor acordado:
  * bate      → APROVADO: e-mail p/ pagador (equipe@) com o boleto anexo
                + e-mail p/ o PJ avisando que foi para pagamento
  * não bate  → DIVERGENTE: e-mail p/ o PJ pedindo para ligar
  * não leu   → MANUAL: e-mail p/ o Cristiano verificar no painel

A decisão de comparação é 100% nossa (Decimal, tolerância de 1 centavo).
A IA só extrai número e redige frase — nunca decide pagamento.
"""
import json
import logging
import mimetypes
import re
import threading
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..models import AuditLog, Boleto, Configuracao, Prestador
from . import boletos as svc_boletos
from . import emails, frases, ia, pdf

log = logging.getLogger(__name__)

TOLERANCIA = Decimal('0.01')
MAX_TENTATIVAS = 3

_MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
          'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']


def competencia_extenso(d):
    return f'{_MESES[d.month - 1]}/{d.year}'


def _moeda(v):
    if v is None:
        return '—'
    s = f'{v:,.2f}'
    return s.replace(',', '_').replace('.', ',').replace('_', '.')


def _fatos(boleto):
    posto = boleto.posto_efetivo
    prest = boleto.prestador
    fatos = {
        'prestador': prest.nome,
        'contato': prest.contato,  # pessoa: "Prezado(a) Guido"
        'alvo': posto.nome if posto else 'boleto único',
        # "competencia" = mês do PAGAMENTO (régua/assunto); o serviço pode
        # ter sido no mês anterior (regime POSTERIOR).
        'competencia': competencia_extenso(boleto.competencia),
        'servico_prestado_em': competencia_extenso(
            prest.mes_servico(boleto.competencia)),
        'valor': _moeda(boleto.valor_extraido or boleto.valor_esperado),
    }
    if boleto.parcial:
        # Boleto PARCIAL: o "valor" é o DESTE boleto (nunca o combinado do
        # mês, senão o PJ lê "R$ 1.000,00" num boleto de R$ 499,93).
        sit = situacao_parcial(boleto)
        fatos['valor'] = _moeda(sit['este'] if sit else None)
        fatos['parcial_frase'] = frase_parcial(boleto, sit)
        if sit:
            fatos['pagamento_parcial'] = {
                'combinado_do_mes': _moeda(sit['total']),
                'ja_entregue_antes': _moeda(sit['antes']),
                'este_boleto': _moeda(sit['este']),
                'entregue_ate_agora': _moeda(sit['soma']),
                'ainda_falta': _moeda(sit['falta']),
                'situacao': ('MENSALIDADE COMPLETA' if sit['completa']
                             else f'FALTA R$ {_moeda(sit["falta"])}'),
            }
    return fatos


def situacao_parcial(boleto):
    """Conta da mensalidade paga em partes: combinado, o que já veio antes,
    este boleto, soma e o que falta. None se ainda não dá para contar."""
    if not boleto.parcial:
        return None
    total = boleto.valor_esperado
    if total is None:
        total = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)
    este = boleto.valor_extraido or valor_da_linha(boleto.linha_digitavel)
    if total is None or este is None:
        return None
    anteriores = list(svc_boletos.parciais_anteriores(boleto))
    antes = sum((b.valor_extraido or Decimal('0') for b in anteriores),
                Decimal('0'))
    soma = antes + este
    falta = max(Decimal('0'), total - soma)
    return {'total': total, 'antes': antes, 'anteriores': anteriores,
            'este': este, 'soma': soma, 'falta': falta,
            'completa': falta <= TOLERANCIA}


def frase_parcial(boleto, sit=None):
    """UMA frase, em português claro, dizendo o que este boleto parcial
    representa e o que ainda falta — vai no texto do e-mail (IA e modelo
    pronto) para ninguém precisar deduzir pelos números."""
    if sit is None:
        sit = situacao_parcial(boleto)
    comp = competencia_extenso(boleto.competencia)
    if sit is None:
        return (f'Atenção: este boleto é apenas uma PARTE da mensalidade de '
                f'{comp}; o fechamento do valor será conferido.')
    if sit['completa']:
        return (f'Este boleto é a última parte da mensalidade de {comp}: '
                f'com ele o valor combinado de R$ {_moeda(sit["total"])} '
                f'fica completo — tudo certo.')
    if sit['antes'] > 0:
        return (f'Atenção: este boleto é uma PARTE da mensalidade de {comp} '
                f'(combinado: R$ {_moeda(sit["total"])}). Já havia '
                f'R$ {_moeda(sit["antes"])} entregue antes; com este, '
                f'R$ {_moeda(sit["soma"])}. Ainda falta gerar um boleto de '
                f'R$ {_moeda(sit["falta"])}.')
    return (f'Atenção: este boleto é uma PARTE da mensalidade de {comp} '
            f'(combinado: R$ {_moeda(sit["total"])}). Recebido '
            f'R$ {_moeda(sit["este"])}; ainda falta gerar um boleto de '
            f'R$ {_moeda(sit["falta"])}.')


def linha_representante(boleto):
    """'Representante: <nome social ou representante>' quando a pessoa
    não é a própria razão social (RABISCO → Guido Cerqueira)."""
    prest = boleto.prestador
    if prest.contato and prest.contato != prest.nome:
        return [f'Representante: {prest.contato}']
    return []


def linhas_competencia(boleto):
    """Bloco de datas dos e-mails, no vocabulário do Cristiano:
    'Serviço prestado em' + 'Competência' (= mês do serviço) e, quando o
    pagamento é no mês seguinte, 'Pagamento em'. Mais o dia previsto de
    pagamento do contrato, se cadastrado."""
    prest = boleto.prestador
    servico = competencia_extenso(prest.mes_servico(boleto.competencia))
    pagamento = competencia_extenso(boleto.competencia)
    linhas = [f'Serviço prestado em: {servico}', f'Competência: {servico}']
    if prest.regime_pagamento == prest.Regime.POSTERIOR:
        linhas.append(f'Pagamento em: {pagamento}')
    prev = boleto.pagamento_previsto
    if prev:
        regra = (f'{boleto.regra_pagamento} — ' if boleto.regra_dia_util
                 else '')
        linhas.append(f'Pagamento previsto: dia {prev.day} de {pagamento} '
                      f'({regra}contrato)')
    return linhas


def linha_vencimento(boleto):
    if boleto.vencimento:
        return [f'Vencimento: {boleto.vencimento:%d/%m/%Y}']
    if boleto.prestador.dia_vencimento:
        return [f'Vencimento: dia {boleto.prestador.dia_vencimento} '
                '(previsto no contrato)']
    return []


def linha_aprovacao(boleto):
    """'Aprovado por' no e-mail ao financeiro (pedido de 04/09/2026):
    manual → quem e quando; automático → agente autônomo + modelo."""
    quando = (timezone.localtime(boleto.verificado_em)
              .strftime('%d/%m/%Y %H:%M') if boleto.verificado_em else '')
    if not boleto.aprovado_por:
        return []
    if boleto.aprovado_por == 'sistema':
        conf = (f', confiança {boleto.ia_confianca}%'
                if boleto.ia_confianca is not None else '')
        return [f'Aprovado por: agente autônomo (IA {settings.IA_MODEL}'
                f'{conf}) em {quando}']
    return [f'Aprovado manualmente por: {boleto.aprovado_por} em {quando}']


def valor_da_linha(linha):
    """Valor embutido no código de barras — conferência 100% determinística.

    Linha digitável de boleto bancário (47 díg.): valor = últimos 10 dígitos,
    em centavos. Guia de arrecadação (48 díg.): remove o DV de cada bloco de
    12 e lê o valor nas posições 5-15 do código de barras resultante.
    """
    ld = re.sub(r'\D', '', linha or '')
    try:
        if len(ld) == 47:
            v = Decimal(ld[-10:]) / 100
        elif len(ld) == 48 and ld.startswith('8'):
            # Guia de arrecadação REAL sempre começa com 8. Um boleto bancário
            # lido com um dígito a mais (48 em vez de 47) NÃO é arrecadação —
            # cair aqui inventaria um valor fantasma de milhões e derrubaria o
            # boleto por falsa inconsistência. Nesse caso, melhor não ter valor.
            cb = ''.join(d for i, d in enumerate(ld) if (i + 1) % 12 != 0)
            v = Decimal(cb[4:15]) / 100
        else:
            return None
        return v.quantize(Decimal('0.01')) if v > 0 else None
    except Exception:
        return None


def dados_pagamento(boleto, fatos):
    """Bloco determinístico com os dados de pagamento — anexado ao corpo do
    e-mail do pagador DEPOIS da redação (a IA nunca toca nesses dados)."""
    partes = ['', '-' * 40,
              f'Prestador: {fatos["prestador"]} — {fatos["alvo"]}']
    partes.extend(linha_representante(boleto))
    partes.extend(linhas_competencia(boleto))
    partes.append(f'Valor: R$ {fatos["valor"]}')
    partes.extend(linha_vencimento(boleto))
    if boleto.linha_digitavel:
        partes.append(f'Linha digitável: {boleto.linha_digitavel}')
    if boleto.chave_pix:
        partes.append(f'Chave PIX: {boleto.chave_pix}')
    partes.extend(linha_aprovacao(boleto))
    if boleto.extra:
        partes.append('Obs.: cobrança EXTRA/avulsa — não faz parte da '
                      'mensalidade do posto.')
    if boleto.parcial:
        partes.append('Obs.: boleto PARCIAL — é só uma parte da mensalidade '
                      'deste posto; o fechamento do mês está logo abaixo.')
    dif = diferenca_do_combinado(boleto)
    if dif is not None and boleto.valor_livre:
        sinal = '+' if dif > 0 else '−'
        partes.append(
            f'Obs.: valor DIFERENTE do combinado (R$ '
            f'{_moeda(boleto.valor_esperado)}): {sinal}R$ {_moeda(abs(dif))} '
            f'— aceito pelo admin no cadastro ("aceitar este valor").')
    if (not boleto.extra and not boleto.parcial
            and boleto.valor_esperado is not None
            and boleto.valor_extraido is not None
            and boleto.valor_esperado - boleto.valor_extraido > TOLERANCIA):
        if fatos.get('motivo_menor'):
            partes.append(f'Obs.: valor abaixo do combinado '
                          f'(R$ {_moeda(boleto.valor_esperado)}). '
                          f'Justificativa: {fatos["motivo_menor"]}')
        else:
            partes.append(f'Obs.: valor abaixo do combinado '
                          f'(R$ {_moeda(boleto.valor_esperado)}) — acordo '
                          'com o prestador.')
    for vale, n in svc_boletos.vales_aplicaveis(
            boleto.prestador, boleto.posto, boleto.competencia):
        partes.append(f'Desconto: {vale.descricao} — parcela '
                      f'{n}/{vale.parcelas_total} — '
                      f'R$ {_moeda(vale.valor_parcela)} (já abatido do '
                      'valor esperado)')
    if boleto.observacao:
        partes.append(f'Obs. do mês: {boleto.observacao}')
    partes.extend(resumo_parcial(boleto))
    return '\n'.join(partes)


def dados_pj(boleto, fatos):
    """Bloco formal do aviso ao prestador: quem paga, CNPJs, competência,
    valor e vencimento — mesmo padrão do e-mail do financeiro."""
    prest = boleto.prestador
    partes = ['', '-' * 40,
              f'Prestador: {prest.nome}'
              + (f' — CNPJ {prest.cnpj}' if prest.cnpj else '')]
    partes.extend(linha_representante(boleto))
    posto = boleto.posto_efetivo
    if posto is not None:
        pagador = posto.razao_social or posto.nome
        linha = f'Empresa pagadora: {pagador}'
        if posto.razao_social and posto.razao_social.upper() != \
                posto.nome.upper():
            linha += f' ({posto.nome})'
        if posto.cnpj:
            linha += f' — CNPJ {posto.cnpj}'
        partes.append(linha)
    partes.extend(linhas_competencia(boleto))
    if fatos.get('valor') and fatos['valor'] != '—':
        partes.append(f'Valor: R$ {fatos["valor"]}')
    partes.extend(linha_vencimento(boleto))
    partes.extend(resumo_parcial(boleto))
    return '\n'.join(partes)


def resumo_parcial(boleto):
    """A "continha" da mensalidade paga em partes — bloco determinístico
    que fecha o e-mail (vira tabela no HTML). Lê-se de cima para baixo:
    combinado, o que já veio, este boleto, soma, o que falta, veredito."""
    if not boleto.parcial:
        return []
    posto = boleto.posto_efetivo
    onde = f'{posto.nome}, ' if posto else ''
    comp = competencia_extenso(boleto.competencia)
    cab = ['', f'🧩 Mensalidade em partes — {onde}{comp}:']
    sit = situacao_parcial(boleto)
    if sit is None:
        return cab + ['Este boleto é uma PARTE da mensalidade; o valor '
                      'ainda vai ser conferido para fechar a conta.']
    n = len(sit['anteriores'])
    antes = (f'R$ {_moeda(sit["antes"])} ({n} boleto{"s" if n > 1 else ""})'
             if n else 'R$ 0,00 (nenhum — este é o primeiro)')
    linhas = cab + [
        f'Valor combinado do mês: R$ {_moeda(sit["total"])}',
        f'Já entregue antes: {antes}',
        f'Este boleto: R$ {_moeda(sit["este"])}',
        f'Entregue até agora: R$ {_moeda(sit["soma"])}',
        f'Ainda falta: R$ {_moeda(sit["falta"])}',
    ]
    if sit['completa']:
        linhas.append(f'✅ Fechou! Com este boleto a mensalidade de {comp} '
                      'está completa. Nada mais a gerar.')
    else:
        linhas.append(f'⏳ Ainda NÃO fechou: falta gerar mais um boleto de '
                      f'R$ {_moeda(sit["falta"])} para completar '
                      f'R$ {_moeda(sit["total"])}.')
    return linhas


def diferenca_do_combinado(boleto):
    """valor − combinado, ou None quando não há discrepância (ou não dá
    para comparar: parcial/extra/sem valor)."""
    if boleto.parcial or boleto.extra:
        return None
    if boleto.valor_esperado is None or boleto.valor_extraido is None:
        return None
    dif = boleto.valor_extraido - boleto.valor_esperado
    return dif if abs(dif) > TOLERANCIA else None


def _conf_ia(dados):
    """Confiança 0-100 que a IA declarou; 50 quando não declarou ou veio
    ilegível (trata como incerto)."""
    try:
        return max(0, min(100, int(dados.get('confianca'))))
    except (TypeError, ValueError):
        return 50


def _imagem_do_boleto(boleto):
    """(bytes, mime) de UMA imagem do boleto para a IA ENXERGAR (fallback de
    visão): a 1ª página renderizada quando é PDF, ou o próprio arquivo quando
    já é imagem. (b'', '') quando não dá."""
    nome = (boleto.arquivo.name or '').lower()
    if nome.endswith('.pdf'):
        return pdf.primeira_pagina_png(boleto.arquivo.path), 'image/png'
    if nome.endswith(('.jpg', '.jpeg', '.png', '.webp')):
        try:
            with open(boleto.arquivo.path, 'rb') as fh:
                return fh.read(), (mimetypes.guess_type(nome)[0]
                                   or 'image/jpeg')
        except Exception as e:
            log.warning('não li a imagem do boleto #%s: %s', boleto.pk, e)
    return b'', ''


def ler_dados_do_pdf(boleto):
    """Preenche valor, vencimento e linha digitável a partir do PDF quando
    a verificação não chegou a ler (boleto que caiu em MANUAL antes da IA
    e foi aprovado no olho). Sem isso o e-mail ao financeiro saía com o
    valor COMBINADO no lugar do valor do BOLETO (Meriti, 04/09/2026:
    R$ 7.175,70 no e-mail, R$ 7.095,70 no boleto). Boleto sem texto
    (vetorial) cai no fallback de visão (opus). Nunca levanta."""
    if boleto.valor_extraido is not None or not boleto.arquivo:
        return False
    try:
        eh_pdf = boleto.arquivo.name.lower().endswith('.pdf')
        texto = pdf.extrair_texto(boleto.arquivo.path) if eh_pdf else ''
        bruto = ''
        valor_pdf = None
        if texto.strip():
            valor_pdf, bruto = ia.extrair_valor(texto)
        if valor_pdf is None:  # sem texto, ou a IA não achou → opus enxerga
            img, mime = _imagem_do_boleto(boleto)
            if img:
                valor_pdf, bruto = ia.extrair_valor_imagem(img, mime=mime)
        if valor_pdf is None:
            if bruto:
                boleto.ia_resposta = bruto[:4000]
                boleto.save(update_fields=['ia_resposta'])
            return False
        boleto.ia_resposta = bruto[:4000]
        dados = ia.carregar_json(bruto)
        boleto.valor_extraido = valor_pdf
        if not boleto.linha_digitavel:
            ld = re.sub(r'\D', '', str(dados.get('linha_digitavel') or ''))
            if 40 <= len(ld) <= 48:
                boleto.linha_digitavel = ld
        if boleto.vencimento is None:
            try:
                boleto.vencimento = datetime.strptime(
                    str(dados.get('vencimento') or ''), '%d/%m/%Y').date()
            except ValueError:
                pass
        try:
            boleto.ia_confianca = max(0, min(100, int(dados.get('confianca'))))
        except (TypeError, ValueError):
            pass
        boleto.save(update_fields=['valor_extraido', 'linha_digitavel',
                                   'vencimento', 'ia_resposta',
                                   'ia_confianca'])
        return True
    except Exception as e:
        log.warning('ler_dados_do_pdf falhou no boleto #%s: %s', boleto.pk, e)
        return False


def enviar_para_pagamento(boleto, fatos=None, reenviar=False):
    """ÚNICO caminho do e-mail "Pagamento — …" para a equipe@ (+ aviso ao
    PJ). TRAVA contra pagar duas vezes: se este boleto JÁ foi ao financeiro
    com o mesmo valor, não vai de novo (só com reenviar=True — o botão
    "Reenviar e-mails"). Valor diferente do já enviado → vai como CORREÇÃO,
    com aviso ao admin. Retorna a frase do que aconteceu."""
    # Aprovação manual de boleto que a IA não chegou a ler: o e-mail tem de
    # trazer o valor DO BOLETO (e o combinado na observação), não o contrário.
    if ler_dados_do_pdf(boleto):
        fatos = None
    fatos = fatos or _fatos(boleto)
    valor = boleto.valor_extraido or boleto.valor_esperado
    fatos['valor'] = _moeda(valor)
    ja = boleto.pagamento_enviado_em is not None
    mesmo = (ja and boleto.pagamento_enviado_valor is not None
             and valor is not None
             and abs(boleto.pagamento_enviado_valor - valor) <= TOLERANCIA)
    quando = (timezone.localtime(boleto.pagamento_enviado_em)
              .strftime('%d/%m/%Y %H:%M') if ja else '')
    if ja and mesmo and not reenviar:
        AuditLog.registrar(
            AuditLog.Evento.STATUS, ator='sistema',
            detalhe=f'Boleto #{boleto.pk} reaprovado, mas JÁ estava com o '
                    f'financeiro desde {quando} (R$ {fatos["valor"]}) — '
                    'nada reenviado (trava contra pagar 2x)')
        return (f'já estava com o financeiro desde {quando} com o mesmo '
                'valor — NADA foi reenviado, para não pagar duas vezes. '
                'Se precisar mesmo, use "Reenviar e-mails".')
    correcao = ja and not mesmo
    valor_anterior = boleto.pagamento_enviado_valor

    assunto = assunto_parcial(
        fatos,
        f'Pagamento — {fatos["prestador"]} — {fatos["alvo"]} — '
        f'{fatos["competencia"]} — R$ {fatos["valor"]}')
    aviso = ''
    if correcao:
        assunto = f'CORREÇÃO — {assunto}'
        aviso = (f'⚠️ ATENÇÃO: este e-mail SUBSTITUI o enviado em {quando} '
                 f'com R$ {_moeda(boleto.pagamento_enviado_valor)} para este '
                 'mesmo boleto. Pagar SOMENTE este valor, uma vez só.\n\n')
    elif reenviar and ja:
        aviso = (f'⚠️ REENVIO do e-mail de {quando} — é o MESMO boleto, '
                 'pagar UMA vez só.\n\n')
    # Quem lê este e-mail é o FINANCEIRO — a saudação é para a equipe,
    # nunca para o prestador (que é o assunto do e-mail, não o leitor).
    fatos_eq = dict(fatos, contato='equipe do setor financeiro')
    agrupar = (boleto.prestador.fin_email_agrupado
               and not correcao and not (reenviar and ja))
    if boleto.prestador.fin_sem_email:
        # Financeiro acompanha este prestador pelo painel /financeiro/:
        # nada de e-mail à equipe@ (a chave fica no próprio painel deles).
        AuditLog.registrar(
            AuditLog.Evento.STATUS, ator='sistema',
            detalhe=f'Boleto #{boleto.pk} liberado ao financeiro pelo PAINEL '
                    '(prestador marcado "sem e-mail de pagamento")'
                    + (' — correção' if correcao else ''))
    elif agrupar:
        # "Enviar e-mail agrupado" (10/11/2026): fica na fila; o cron
        # email_agrupado (2 em 2 h) manda um e-mail só com todos os PDFs.
        # Correção e reenvio continuam saindo na hora.
        boleto.fin_agrupar = True
        boleto.save(update_fields=['fin_agrupar'])
        AuditLog.registrar(
            AuditLog.Evento.STATUS, ator='sistema',
            detalhe=f'Boleto #{boleto.pk} liberado ao financeiro — aguardando '
                    'o E-MAIL AGRUPADO (prestador na opção "e-mail agrupado")')
    else:
        emails.enviar(
            settings.EMAIL_PAGADOR, assunto,
            aviso + frases.corpo(
                'aprovado_pagador', fatos_eq,
                instrucao_ia=('Escreva para a EQUIPE DO SETOR FINANCEIRO da '
                              'CAMIM (o leitor é a equipe, não o prestador): '
                              'abra com "Prezada equipe do setor financeiro," '
                              'e peça para pagar o boleto em anexo do '
                              'prestador informado, dizendo que o valor já '
                              'foi conferido e que os dados de pagamento '
                              'seguem abaixo da assinatura.'
                              + _instrucao_parcial(fatos)))
            + dados_pagamento(boleto, fatos),
            boleto=boleto,
            anexo_field=boleto.arquivo if boleto.arquivo else None,
            anexos=([(boleto.nota_fiscal,
                      boleto.nota_fiscal_nome or 'nota-fiscal.pdf')]
                    if boleto.nota_fiscal else None),
            de=settings.EMAIL_FROM_PAGADOR, cc=cc_gerente(boleto))
    boleto.pagamento_enviado_em = timezone.now()
    boleto.pagamento_enviado_valor = valor
    boleto.save(update_fields=['pagamento_enviado_em',
                               'pagamento_enviado_valor'])

    if boleto.parcial:
        instr_pj = ('Escreva em tom FORMAL para o prestador, informando '
                    'que o boleto foi conferido e já foi encaminhado ao '
                    'setor financeiro para pagamento. Diga que os dados '
                    'da cobrança seguem abaixo da assinatura.'
                    + _instrucao_parcial(fatos))
    else:
        instr_pj = ('Escreva em tom FORMAL para o prestador, '
                    'informando que o boleto foi conferido, o '
                    'valor está de acordo com o contratado e o '
                    'documento já foi encaminhado ao setor '
                    'financeiro para pagamento. Diga que os dados '
                    'da cobrança seguem abaixo da assinatura.')
    if not (reenviar and ja) and not correcao:
        emails.enviar(
            destinatarios_pj(boleto),
            assunto_parcial(
                fatos,
                f'Boleto aprovado e enviado p/ pagamento — '
                f'{fatos["competencia"]}'),
            frases.corpo('aprovado_pj', fatos, instrucao_ia=instr_pj)
            + dados_pj(boleto, fatos),
            boleto=boleto, cc=cc_gerente(boleto))

    if not (reenviar and ja) and not correcao:
        avisar_vencimento_antes_do_previsto(boleto, fatos)

    _alertar_discrepancia(boleto, fatos, correcao, quando)
    if correcao:
        return (f'CORREÇÃO enviada ao financeiro (o valor mudou: antes '
                f'R$ {_moeda(valor_anterior)}, agora '
                f'R$ {fatos["valor"]}).')
    if reenviar and ja:
        return 'e-mail de pagamento REENVIADO à equipe (marcado como reenvio).'
    return 'enviado p/ pagamento (equipe e prestador avisados).'


def avisar_vencimento_antes_do_previsto(boleto, fatos=None):
    """10/11/2026: boleto aprovado cujo vencimento é ANTERIOR à data
    prevista de pagamento do contrato (só quando há dia cadastrado — o
    contrato da Salliseg não diz o dia). E-mail PADRONIZADO, formal e
    direto, ao prestador: já foi ao financeiro, mas pode ser negado pelo
    vencimento. Uma vez por boleto. Texto fixo, sem IA."""
    if boleto.aviso_vencimento_em or not boleto.vence_antes_do_previsto:
        return False
    fatos = fatos or _fatos(boleto)
    previsto = boleto.prestador.pagamento_previsto_em(boleto.competencia)
    venc = boleto.vencimento.strftime('%d/%m/%Y')
    prev = previsto.strftime('%d/%m/%Y')
    contato = fatos.get('contato') or fatos['prestador']
    corpo = (
        f'Prezado(a) {contato},\n\n'
        f'Recebemos o boleto referente a {fatos["competencia"]} '
        f'({fatos["prestador"]} — unidade {fatos["alvo"]}). Ele foi '
        'conferido e já está com o setor financeiro para pagamento.\n\n'
        'Pedimos a sua atenção a um ponto importante: o boleto vence em '
        f'{venc}, e a data prevista de pagamento no contrato é {prev}. '
        'Como o pagamento é feito na data prevista em contrato, o setor '
        'financeiro pode não aceitar um boleto que vença antes dela. Nesse '
        'caso, o boleto será negado e será necessário emitir um novo, com '
        f'vencimento igual ou posterior a {prev}.\n\n'
        'Para os próximos meses, pedimos que o boleto seja emitido com '
        'vencimento a partir da data prevista de pagamento. Assim evitamos '
        'devoluções e o pagamento segue sem contratempos.\n\n'
        'Qualquer dúvida, é só responder a este e-mail.\n\n'
        'Atenciosamente,\nCristiano — CAMIM')
    emails.enviar(
        destinatarios_pj(boleto),
        f'Atenção ao vencimento do boleto — {fatos["alvo"]} — '
        f'{fatos["competencia"]}',
        corpo + dados_pj(boleto, fatos),
        boleto=boleto, cc=cc_gerente(boleto))
    boleto.aviso_vencimento_em = timezone.now()
    boleto.save(update_fields=['aviso_vencimento_em'])
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator='sistema',
        detalhe=f'Boleto #{boleto.pk}: prestador avisado — vence em {venc}, '
                f'antes da data prevista de pagamento ({prev}); pode ser '
                'negado pelo financeiro')
    return True


def nome_anexo_boleto(boleto, usados):
    """primeiro-nome_posto.pdf (mesma regra do ZIP do financeiro), sem
    repetir nome dentro do mesmo e-mail."""
    from ..views_financeiro import nome_arquivo_boleto
    nome = nome_arquivo_boleto(boleto)
    n = usados.get(nome, 0) + 1
    usados[nome] = n
    return nome if n == 1 else nome[:-4] + f'_{n}.pdf'


def enviar_email_agrupado():
    """Cron de 2 em 2 horas (10/11/2026): UM e-mail à equipe@ com TODOS os
    boletos aprovados que ficaram na fila do "e-mail agrupado" — de todos
    os prestadores com a opção — com os PDFs (boleto e nota) anexos e os
    dados de pagamento de cada um. Sem cópia a gerente: são vários postos
    num e-mail só (trava do sigilo por posto). Devolve os pks enviados."""
    pendentes = list(
        Boleto.objects
        .filter(fin_agrupar=True, fin_agrupado_em__isnull=True,
                pagamento_enviado_em__isnull=False,
                prestador__fin_email_agrupado=True,
                prestador__fin_sem_email=False,
                status__in=[Boleto.Status.APROVADO,
                            Boleto.Status.FIN_RECEBIDO, Boleto.Status.PAGO])
        .select_related('prestador', 'posto', 'prestador__posto_cobranca')
        .order_by('prestador__nome', 'posto__nome', 'pk'))
    if not pendentes:
        return []
    anexos, blocos, usados = [], [], {}
    total = Decimal('0')
    for b in pendentes:
        fatos = _fatos(b)
        valor = b.valor_extraido or b.valor_esperado or Decimal('0')
        fatos['valor'] = _moeda(valor)
        total += valor
        nome = nome_anexo_boleto(b, usados)
        if b.arquivo:
            anexos.append((b.arquivo, nome))
        if b.nota_fiscal:
            anexos.append((b.nota_fiscal, nome[:-4] + '_nota.pdf'))
        blocos.append(f'📎 {nome}' + dados_pagamento(b, fatos))
    n_prest = len({b.prestador_id for b in pendentes})
    corpo = (
        'Prezada equipe do setor financeiro,\n\n'
        f'Seguem, num único e-mail, os {len(pendentes)} boleto(s) de '
        f'{n_prest} prestador(es) conferidos e aprovados para pagamento '
        f'desde o último envio, no total de R$ {_moeda(total)}. Os PDFs '
        'estão anexos, nomeados por prestador e posto; os dados de cada '
        'pagamento seguem abaixo da assinatura.\n\n'
        'Atenciosamente,\nCristiano — CAMIM\n'
        + '\n'.join(blocos))
    ok = emails.enviar(
        settings.EMAIL_PAGADOR,
        f'Pagamento (agrupado) — {len(pendentes)} boleto(s) — '
        f'{n_prest} prestador(es) — R$ {_moeda(total)}',
        corpo, anexos=anexos, de=settings.EMAIL_FROM_PAGADOR)
    if not ok:
        log.error('E-mail agrupado ao financeiro falhou; fica para a próxima')
        return []
    agora = timezone.now()
    pks = [b.pk for b in pendentes]
    Boleto.objects.filter(pk__in=pks).update(fin_agrupado_em=agora)
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator='sistema',
        detalhe=f'E-mail AGRUPADO ao financeiro: {len(pks)} boleto(s) '
                f'({", ".join(f"#{p}" for p in pks)}) — R$ {_moeda(total)}')
    return pks


def _alertar_discrepancia(boleto, fatos, correcao=False, quando=''):
    """Aviso ao admin sempre que o dinheiro sai diferente do combinado:
    valor MAIOR aceito à mão, ou CORREÇÃO de um envio anterior."""
    motivos = []
    dif = diferenca_do_combinado(boleto)
    if dif is not None and dif > 0:
        motivos.append(f'o valor pago (R$ {fatos["valor"]}) é MAIOR que o '
                       f'combinado (R$ {_moeda(boleto.valor_esperado)}): '
                       f'+R$ {_moeda(dif)}')
    if correcao:
        motivos.append(f'este boleto já tinha ido ao financeiro em {quando} '
                       'com outro valor — foi enviada uma CORREÇÃO; confira '
                       'com a equipe@ que só um pagamento será feito')
    if not motivos:
        return
    emails.enviar(
        settings.EMAIL_ADMIN,
        f'⚠️ Discrepância no pagamento — {fatos["prestador"]} — '
        f'{fatos["alvo"]} — {fatos["competencia"]}',
        'Cristiano,\n\nO boleto abaixo foi para pagamento, mas atenção:\n'
        + '\n'.join(f'• {m}' for m in motivos)
        + '\n\nPainel: https://pj.camim.com.br/painel/\n\n— Controle dos PJs'
        + dados_pagamento(boleto, fatos),
        boleto=boleto)


def _mes_seguinte_fim(competencia):
    """Último dia do mês seguinte ao da competência."""
    m = competencia.month + 2
    ano = competencia.year + (m - 1) // 12
    mes = (m - 1) % 12 + 1
    return date(ano, mes, 1)  # exclusivo: vencimento < este dia


def cc_gerente(boleto):
    """CC obrigatório do gerente do posto (espelho do CRM) em todo e-mail
    de boleto que tem posto identificado."""
    posto = boleto.posto_efetivo
    if posto is not None and posto.gerente_email:
        return [posto.gerente_email]
    return None


def enviar_nota_posterior(boleto, substituicao=False):
    """Nota fiscal anexada DEPOIS que o boleto já foi ao financeiro (ex.: o
    emissor da NFS-e estava fora do ar na hora do pagamento). Manda a nota
    como COMPLEMENTO do pagamento já enviado — mesmo boleto, nada novo a
    pagar; não reenvia o boleto nem mexe no valor. Retorna True se enviou.

    `substituicao`: o boleto JÁ tinha nota e o prestador mandou outra no
    lugar (02/10/2026, Robson emitiu as 8 com a competência errada) — o
    e-mail diz com todas as letras que a nota anterior não vale mais."""
    if not boleto.nota_fiscal:
        return False
    fatos = _fatos(boleto)
    quando = (timezone.localtime(boleto.pagamento_enviado_em).strftime('%d/%m')
              if boleto.pagamento_enviado_em else None)
    enviado = f', já enviado para pagamento em {quando}' if quando else ''
    if substituicao:
        assunto = assunto_parcial(
            fatos, f'SUBSTITUIÇÃO de nota fiscal — {fatos["prestador"]} — '
            f'{fatos["alvo"]} — {fatos["competencia"]}')
        corpo = (
            'Prezada equipe do setor financeiro,\n\n'
            f'Segue em anexo a NOVA nota fiscal do prestador '
            f'{fatos["prestador"]} referente ao boleto de {fatos["alvo"]} '
            f'({fatos["competencia"]}){enviado}. Ela SUBSTITUI a nota fiscal '
            'enviada anteriormente para este mesmo boleto — favor '
            'DESCONSIDERAR a nota anterior e anexar esta ao processo.\n\n'
            'É o MESMO pagamento: o boleto e o valor não mudaram — não há '
            'novo valor a pagar.\n')
    else:
        assunto = assunto_parcial(
            fatos, f'Nota fiscal — {fatos["prestador"]} — {fatos["alvo"]} — '
            f'{fatos["competencia"]}')
        corpo = (
            'Prezada equipe do setor financeiro,\n\n'
            f'Segue em anexo a NOTA FISCAL do prestador {fatos["prestador"]} '
            f'referente ao boleto de {fatos["alvo"]} ({fatos["competencia"]})'
            f'{enviado}. O boleto foi liberado antes da nota porque o emissor '
            'da NFS-e estava indisponível; a nota chega agora para anexar ao '
            'processo.\n\n'
            'É o MESMO pagamento — não há novo valor a pagar.\n')
    emails.enviar(
        settings.EMAIL_PAGADOR, assunto, corpo, boleto=boleto,
        anexos=[(boleto.nota_fiscal,
                 boleto.nota_fiscal_nome or 'nota-fiscal.pdf')],
        de=settings.EMAIL_FROM_PAGADOR, cc=cc_gerente(boleto))
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator='sistema',
        detalhe=(f'Boleto #{boleto.pk}: nota fiscal SUBSTITUÍDA — a nova '
                 'foi enviada ao financeiro (a anterior fica no histórico)'
                 if substituicao else
                 f'Boleto #{boleto.pk}: nota fiscal enviada ao financeiro '
                 'como complemento (boleto já havia sido enviado)'))
    return True


def destinatarios_pj(boleto):
    """Para quem vão os avisos do prestador: TODOS os usuários ativos do PJ
    + os "e-mails para avisos" do cadastro (quem não tem login, ex.:
    Rosana). Quem enviou também entra, se for outro endereço."""
    ems = [u.email for u in boleto.prestador.usuarios.filter(ativo=True)]
    for e in boleto.prestador.lista_emails_aviso():  # avisos sem login
        if e not in ems:
            ems.append(e)
    extra = (boleto.enviado_por or '').lower()
    if extra and extra != settings.EMAIL_ADMIN.lower() and extra not in ems:
        ems.append(extra)
    return ems or [settings.EMAIL_ADMIN]


def _instrucao_parcial(fatos):
    """Complemento da instrução da IA quando o boleto é PARCIAL: o texto
    tem de dizer, com todas as letras, o que falta (ou que fechou)."""
    if not fatos.get('parcial_frase'):
        return ''
    return (' IMPORTANTE: este boleto é PARCIAL — cobre só uma PARTE da '
            'mensalidade (veja "pagamento_parcial" nos fatos). Inclua, '
            'em uma frase própria e bem clara, exatamente esta informação: '
            f'"{fatos["parcial_frase"]}" Pode reescrever com suas palavras, '
            'mas mantenha TODOS os valores iguais. NUNCA diga que o valor '
            '"está de acordo com o contrato" — o valor do contrato é o do '
            'mês inteiro.')


def assunto_parcial(fatos, base):
    """Assunto que já conta a história: '(parte: R$ x de R$ y — falta R$ z)'."""
    sit = fatos.get('pagamento_parcial')
    if not sit:
        return base
    if sit['situacao'] == 'MENSALIDADE COMPLETA':
        fim = 'mensalidade completa ✅'
    else:
        fim = f'falta R$ {sit["ainda_falta"]}'
    return (f'{base} · PARCIAL: R$ {sit["este_boleto"]} de '
            f'R$ {sit["combinado_do_mes"]} — {fim}')


def enviar_recebido(boleto):
    fatos = _fatos(boleto)
    corpo = frases.corpo(
        'recebido', fatos,
        instrucao_ia=('Escreva em tom FORMAL confirmando que recebemos o '
                      'boleto do prestador e que ele passará por '
                      'conferência. Diga que os dados do documento seguem '
                      'abaixo da assinatura. NÃO cite valores no texto.'
                      + (_instrucao_parcial(fatos).replace(
                          'NÃO cite', 'cite') if fatos.get('parcial_frase')
                         else '')))
    emails.enviar(destinatarios_pj(boleto),
                  assunto_parcial(fatos,
                                  f'Boleto recebido — {fatos["competencia"]}'),
                  corpo + dados_pj(boleto, fatos), boleto=boleto,
                  cc=cc_gerente(boleto))


def _marcar(boleto, status):
    boleto.status = status
    boleto.verificado_em = timezone.now()
    boleto.save()
    AuditLog.registrar(AuditLog.Evento.STATUS, ator='sistema',
                       detalhe=f'Boleto #{boleto.pk} → {status}')


def _retirar_do_financeiro(boleto, motivo, apagar_pdf=True):
    """Boleto que JÁ tinha ido ao financeiro saiu da aprovação (voltou para
    manual ou divergente): avisa a equipe@ para NÃO pagar o valor enviado
    antes, avisa o Cristiano e ZERA a trava, para que um reenvio futuro
    (quando o valor estiver certo) saia como um pagamento novo e limpo.
    `apagar_pdf=False` mantém o PDF (dívida não reconhecida: é prova)."""
    if boleto.pagamento_enviado_em is None:
        return
    fatos = _fatos(boleto)
    quando = (timezone.localtime(boleto.pagamento_enviado_em)
              .strftime('%d/%m/%Y %H:%M'))
    valor_anterior = boleto.pagamento_enviado_valor
    emails.enviar(
        settings.EMAIL_PAGADOR,
        f'CANCELAMENTO — NÃO pagar — {fatos["prestador"]} — '
        f'{fatos["alvo"]} — {fatos["competencia"]}',
        'Prezada equipe do setor financeiro,\n\n'
        f'⛔ CANCELE o pagamento do boleto que enviamos em {quando} '
        f'(R$ {_moeda(valor_anterior)}) do prestador {fatos["prestador"]} '
        f'— {fatos["alvo"]} — {fatos["competencia"]}.\n\n'
        'O valor está em conferência e NÃO deve ser pago. Se este boleto '
        'ainda não foi pago, apenas o desconsidere. Se JÁ foi pago, avise o '
        'Cristiano imediatamente. Quando estiver tudo certo, você receberá um '
        'NOVO e-mail de pagamento para este mesmo boleto.\n\n'
        f'Motivo: {motivo}\n\n— Controle dos PJs'
        + dados_pagamento(boleto, fatos),
        boleto=boleto, de=settings.EMAIL_FROM_PAGADOR, cc=cc_gerente(boleto))
    emails.enviar(
        settings.EMAIL_ADMIN,
        f'⚠️ Cancelamento no financeiro — {fatos["prestador"]} — '
        f'{fatos["competencia"]}',
        f'Cristiano,\n\nO boleto #{boleto.pk} ({fatos["prestador"]} — '
        f'{fatos["alvo"]} — {fatos["competencia"]}) já tinha ido ao '
        f'financeiro em {quando} com R$ {_moeda(valor_anterior)}, mas foi '
        f'retirado da aprovação.\nMotivo: {motivo}\n\nEnviei um CANCELAMENTO à '
        'equipe@ pedindo para NÃO pagar.\n\n'
        'Painel: https://pj.camim.com.br/painel/\n\n— Controle dos PJs',
        boleto=boleto)
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator='sistema',
        detalhe=f'Boleto #{boleto.pk}: CANCELAMENTO enviado ao financeiro '
                f'(valor anterior R$ {_moeda(valor_anterior)}, de {quando}) — '
                f'motivo: {motivo}')
    # Tira os PDFs que o Robson anexou: o boleto estava errado e ele precisa
    # reanexar o certo. O histórico (linha do tempo + auditoria) guarda o
    # porquê e o nome do que saiu. Quando ele reenviar, nasce um boleto novo
    # e este, já em "precisam de você", vira SUBSTITUÍDO.
    campos = ['pagamento_enviado_em', 'pagamento_enviado_valor']
    removidos = []
    if apagar_pdf and boleto.arquivo:
        removidos.append(boleto.nome_original
                         or boleto.arquivo.name.rsplit('/', 1)[-1])
        boleto.arquivo.delete(save=False)
    if apagar_pdf and boleto.nota_fiscal:
        removidos.append(boleto.nota_fiscal_nome or 'nota fiscal')
        boleto.nota_fiscal.delete(save=False)
        boleto.nota_fiscal_nome = ''
    if removidos:
        boleto.arquivo_removido_em = timezone.now()
        boleto.arquivo_removido_motivo = motivo[:255]
        campos += ['arquivo', 'nota_fiscal', 'nota_fiscal_nome',
                   'arquivo_removido_em', 'arquivo_removido_motivo']
        AuditLog.registrar(
            AuditLog.Evento.STATUS, ator='sistema',
            detalhe=f'Boleto #{boleto.pk}: PDF(s) retirado(s) para o Robson '
                    f'reanexar ({", ".join(removidos)}) — motivo: {motivo}')
    boleto.pagamento_enviado_em = None
    boleto.pagamento_enviado_valor = None
    boleto.save(update_fields=campos)


def conferencia_nao_reconhecida(boleto, fatos, motivo=''):
    """Bloco determinístico do e-mail de dívida não reconhecida: a mesma
    conta do painel (combinado − descontos do mês = esperado), o motivo que
    o admin escreveu e o aviso de que NÃO será pago."""
    prest, posto = boleto.prestador, boleto.posto
    combinado = svc_boletos.valor_esperado_para(prest, posto)  # cheio
    esperado = boleto.valor_esperado
    if esperado is None:
        esperado = svc_boletos.valor_esperado_para(prest, posto,
                                                   boleto.competencia)
    partes = ['', '-' * 40, 'Conferência da CAMIM:']
    if combinado is not None:
        partes.append(f'Valor combinado: R$ {_moeda(combinado)}')
    for vale, n in svc_boletos.vales_aplicaveis(prest, posto,
                                                boleto.competencia):
        partes.append(f'Desconto: {vale.descricao} — parcela '
                      f'{n}/{vale.parcelas_total} — '
                      f'R$ {_moeda(vale.valor_parcela)}')
    if esperado is not None:
        partes.append(f'Valor esperado em {fatos["competencia"]}: '
                      f'R$ {_moeda(esperado)}')
    if motivo:
        partes.append(f'Motivo: {motivo}')
    partes += ['', 'Este boleto foi cancelado no nosso controle e NÃO será '
               'pago. Se entender que a cobrança é devida, fale com o '
               'Cristiano antes de emitir um novo boleto.']
    return '\n'.join(partes)


def nao_reconhecer(boleto, motivo, quem):
    """Botão "Não reconheço esta dívida": a CAMIM não deve esse valor. O
    boleto sai da régua (fica no banco, COM o PDF — é prova), o prestador
    recebe o aviso com a conferência e o motivo e, se o boleto já tinha
    ido ao financeiro, a equipe@ recebe o CANCELAMENTO. ÚNICO caminho
    desse e-mail."""
    motivo = (motivo or '').strip()[:500]
    _retirar_do_financeiro(
        boleto, 'dívida não reconhecida pela CAMIM'
        + (f' — {motivo}' if motivo else ''), apagar_pdf=False)
    boleto.status = Boleto.Status.NAO_RECONHECIDO
    boleto.nao_reconhecido_em = timezone.now()
    boleto.nao_reconhecido_motivo = motivo
    boleto.save(update_fields=['status', 'nao_reconhecido_em',
                               'nao_reconhecido_motivo'])
    fatos = _fatos(boleto)
    emails.enviar(
        destinatarios_pj(boleto),
        f'Dívida não reconhecida — {fatos["alvo"]} — '
        f'{fatos["competencia"]}',
        # Aviso formal: texto FIXO (modelos de frases.py), sem IA — em
        # 01/10/2026 a IA omitiu o "não reconhece" e inventou um bloco de
        # dados no e-mail da Amanda.
        frases.corpo('nao_reconhecido', fatos)
        + dados_pj(boleto, fatos)
        + conferencia_nao_reconhecida(boleto, fatos, motivo),
        boleto=boleto, cc=cc_gerente(boleto))
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=quem,
        detalhe=f'Boleto #{boleto.pk}: dívida NÃO reconhecida — prestador '
                'avisado' + (f' — motivo: {motivo}' if motivo else ''))


def confirmar_fin_recebido(boleto, ator, origem=''):
    """Financeiro deu ciência de que recebeu o boleto — pela resposta ao
    e-mail (robô) ou pelo botão do painel do financeiro. Status
    FIN_RECEBIDO, auditoria e aviso ao PJ ("está com o financeiro").
    Retorna False se o boleto não estava aprovado."""
    if boleto.status != Boleto.Status.APROVADO:
        return False
    boleto.status = Boleto.Status.FIN_RECEBIDO
    boleto.fin_recebido_em = timezone.now()
    boleto.fin_recebido_por = (ator or '')[:255]
    boleto.save(update_fields=['status', 'fin_recebido_em',
                               'fin_recebido_por'])
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=ator,
        detalhe=f'Boleto #{boleto.pk} confirmado recebido pelo financeiro'
                + (f' ({origem})' if origem else ''))
    fatos = _fatos(boleto)
    if not boleto.parcial:
        fatos['valor'] = _moeda(boleto.valor_extraido or boleto.valor_esperado)
    emails.enviar(
        destinatarios_pj(boleto),
        assunto_parcial(fatos,
                        f'Boleto com o financeiro — {fatos["competencia"]}'),
        frases.corpo(
            'fin_recebido', fatos,
            instrucao_ia=('Escreva em tom FORMAL e positivo informando ao '
                          'PRESTADOR que o setor financeiro da CAMIM '
                          'confirmou o recebimento do boleto dele e que o '
                          'pagamento (da CAMIM para o prestador) está em '
                          'processamento — nenhuma ação é necessária, é só '
                          'aguardar. NÃO cite valores no texto. Diga que os '
                          'dados seguem abaixo da assinatura.'
                          + _instrucao_parcial(fatos)))
        + dados_pj(boleto, fatos),
        boleto=boleto, cc=cc_gerente(boleto))
    return True


def fin_enviou_para_pagamento(boleto, ator):
    """Financeiro registrou que enviou o boleto para pagamento. 24h depois
    o cron (processar_boletos) marca PAGO sozinho."""
    if boleto.status not in (Boleto.Status.APROVADO,
                             Boleto.Status.FIN_RECEBIDO):
        return False
    if boleto.status == Boleto.Status.APROVADO:
        confirmar_fin_recebido(boleto, ator, origem='painel do financeiro')
    boleto.fin_pagamento_em = timezone.now()
    boleto.fin_pagamento_por = (ator or '')[:255]
    boleto.save(update_fields=['fin_pagamento_em', 'fin_pagamento_por'])
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=ator,
        detalhe=f'Boleto #{boleto.pk}: financeiro enviou para pagamento — '
                'PAGO automático em 24h')
    return True


def pagar_automatico(agora=None):
    """Boletos que o financeiro enviou para pagamento há 24h ou mais
    viram PAGO. Chamado pelo cron processar_boletos. Devolve os pks."""
    from datetime import timedelta
    agora = agora or timezone.now()
    corte = agora - timedelta(hours=24)
    pagos = []
    for b in Boleto.objects.filter(
            fin_pagamento_em__lte=corte,
            status__in=[Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO]):
        b.status = Boleto.Status.PAGO
        b.pago_em = agora
        b.save(update_fields=['status', 'pago_em'])
        AuditLog.registrar(
            AuditLog.Evento.STATUS, ator='sistema',
            detalhe=f'Boleto #{b.pk} marcado PAGO automaticamente — 24h após '
                    f'o financeiro enviar para pagamento '
                    f'({timezone.localtime(b.fin_pagamento_em):%d/%m %H:%M}, '
                    f'{b.fin_pagamento_por})')
        pagos.append(b.pk)
    return pagos


def avisar_correcao_competencia(boleto, de, quem, motivo=''):
    """E-mail oficial ao PJ, ligado ao boleto, avisando que a competência
    foi corrigida (de `de` para a atual) e que o boleto segue normalmente.
    Texto fixo. Guido, 03/11/2026: a integração mandou outubro em vez de
    novembro."""
    fatos = _fatos(boleto)
    antes = competencia_extenso(de)
    corpo = (
        f'Prezado(a) {fatos["contato"]},\n\n'
        f'Informamos que o boleto de {fatos["alvo"]} enviado com a '
        f'competência {antes} foi corrigido no nosso controle para '
        f'{fatos["competencia"]}, que é o mês correto do pagamento. '
        'Nada precisa ser feito da sua parte: o boleto foi conferido e '
        'segue o fluxo normal de pagamento. Os dados corrigidos estão '
        'abaixo.\n\n'
        'Para os próximos meses, atenção à competência informada no envio: '
        'um mês que já foi fechado e pago não aceita mais boleto.\n\n'
        'Atenciosamente,\nCristiano — CAMIM')
    ok = emails.enviar(
        destinatarios_pj(boleto),
        f'Competência corrigida — {fatos["alvo"]} — {antes} → '
        f'{fatos["competencia"]}',
        corpo + dados_pj(boleto, fatos), boleto=boleto,
        cc=cc_gerente(boleto))
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=quem,
        detalhe=f'Boleto #{boleto.pk}: prestador avisado da correção de '
                f'competência ({antes} → {fatos["competencia"]})'
                + (f' — {motivo}' if motivo else ''))
    return ok


def mudar_pagador(boleto, novo_posto, motivo, quem):
    """Boleto passa a ser pago por OUTRO posto (03/11/2026: o boleto do
    Lucas na Ótica LUTI, a pedido do Pereira, passou para a Anchieta).
    Troca o posto e o esperado; se o boleto JÁ foi ao financeiro, a
    equipe@ recebe uma CORREÇÃO dizendo qual empresa paga agora — mesmo
    boleto, mesmo valor, pagar uma vez só. Auditoria com quem mudou."""
    antigo = boleto.posto_efetivo
    boleto.posto = novo_posto
    boleto.valor_esperado = svc_boletos.valor_esperado_para(
        boleto.prestador, novo_posto, boleto.competencia)
    boleto.save(update_fields=['posto', 'valor_esperado'])
    nome_antigo = antigo.nome if antigo else 'boleto único'
    ja = boleto.pagamento_enviado_em is not None
    if ja:
        fatos = _fatos(boleto)
        fatos['valor'] = _moeda(boleto.pagamento_enviado_valor
                                or boleto.valor_extraido
                                or boleto.valor_esperado)
        quando = (timezone.localtime(boleto.pagamento_enviado_em)
                  .strftime('%d/%m/%Y %H:%M'))
        pagador = novo_posto.razao_social or novo_posto.nome
        if novo_posto.razao_social and \
                novo_posto.razao_social.upper() != novo_posto.nome.upper():
            pagador += f' ({novo_posto.nome})'
        if novo_posto.cnpj:
            pagador += f' — CNPJ {novo_posto.cnpj}'
        emails.enviar(
            settings.EMAIL_PAGADOR,
            f'CORREÇÃO — empresa pagadora — Pagamento — {fatos["prestador"]} '
            f'— {fatos["alvo"]} — {fatos["competencia"]} — '
            f'R$ {fatos["valor"]}',
            'Prezada equipe do setor financeiro,\n\n'
            f'⚠️ O boleto do prestador {fatos["prestador"]} '
            f'({fatos["competencia"]}, R$ {fatos["valor"]}), enviado em '
            f'{quando} para pagamento por {nome_antigo}, passa a ser pago '
            f'por {pagador}. NÃO pagar por {nome_antigo}.\n\n'
            f'Motivo: {motivo}\n\n'
            'É o MESMO boleto (anexo de novo) e o MESMO valor — pagar UMA '
            'vez só, pela empresa indicada acima. Os dados atualizados '
            'seguem abaixo.\n'
            + dados_pagamento(boleto, fatos),
            boleto=boleto, anexo_field=boleto.arquivo if boleto.arquivo
            else None, de=settings.EMAIL_FROM_PAGADOR,
            cc=cc_gerente(boleto))
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=quem,
        detalhe=f'Boleto #{boleto.pk}: empresa pagadora mudou de '
                f'{nome_antigo} para {novo_posto.nome} — {motivo}'
                + (' — CORREÇÃO enviada ao financeiro' if ja else ''))
    return ja


def devolver_valor_errado(boleto, motivo, quem):
    """Botão "Valor errado — devolver ao prestador": a dívida existe, mas
    este boleto não serve (valor diferente do esperado). Sai da régua como
    se nada tivesse chegado — o posto volta a "sem boleto" — e fica no
    histórico, com o PDF. O prestador recebe a conferência (combinado,
    descontos, valor esperado) e o pedido de emitir um NOVO boleto com o
    valor certo. Se já tinha ido ao financeiro, a equipe@ recebe o
    CANCELAMENTO. ÚNICO caminho desse e-mail. Texto fixo, sem IA."""
    motivo = (motivo or '').strip()[:500]
    esperado = boleto.valor_esperado
    if esperado is None:
        esperado = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)
    auto = (f'boleto de R$ {_moeda(boleto.valor_extraido)} × esperado '
            f'R$ {_moeda(esperado)}'
            if boleto.valor_extraido is not None and esperado is not None
            else 'valor diferente do esperado')
    _retirar_do_financeiro(
        boleto, 'boleto com valor errado — devolvido ao prestador'
        + (f' — {motivo}' if motivo else ''), apagar_pdf=False)
    boleto.status = Boleto.Status.DEVOLVIDO
    boleto.devolvido_em = timezone.now()
    boleto.devolvido_motivo = (f'{auto}' + (f' — {motivo}' if motivo else ''))[:500]
    boleto.save(update_fields=['status', 'devolvido_em', 'devolvido_motivo'])
    fatos = _fatos(boleto)
    fatos['valor'] = _moeda(boleto.valor_extraido)
    fatos['valor_esperado'] = _moeda(esperado)
    bloco = conferencia_nao_reconhecida(boleto, fatos, motivo).replace(
        'Este boleto foi cancelado no nosso controle e NÃO será pago. Se '
        'entender que a cobrança é devida, fale com o Cristiano antes de '
        'emitir um novo boleto.',
        'Este boleto foi DESCONSIDERADO e não será pago. Por favor, emita '
        'um novo boleto com o valor esperado acima e envie pela '
        'plataforma (pj.camim.com.br) ou por e-mail. Se discordar do '
        'valor, fale com o Cristiano antes de emitir.')
    emails.enviar(
        destinatarios_pj(boleto),
        f'Boleto com valor errado — emitir outro — {fatos["alvo"]} — '
        f'{fatos["competencia"]}',
        frases.corpo('devolvido', fatos) + dados_pj(boleto, fatos) + bloco,
        boleto=boleto, cc=cc_gerente(boleto))
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=quem,
        detalhe=f'Boleto #{boleto.pk}: valor errado ({auto}) — devolvido '
                'ao prestador, posto volta a "sem boleto"'
                + (f' — motivo: {motivo}' if motivo else ''))


def fatos_recusa_financeiro(boleto, observacao=''):
    fatos = _fatos(boleto)
    previsto = boleto.prestador.pagamento_previsto_em(boleto.competencia)
    fatos['data_prevista_pagamento_contrato'] = (
        previsto.strftime('%d/%m/%Y') if previsto else 'não cadastrada')
    fatos['vencimento_do_boleto'] = (
        boleto.vencimento.strftime('%d/%m/%Y') if boleto.vencimento
        else 'não identificado')
    if observacao:
        fatos['observacao_do_financeiro'] = observacao
    return fatos


def texto_recusa_financeiro(boleto, observacao=''):
    """Texto do aviso de recusa (botão "Gerar texto por IA" do painel do
    financeiro, 10/11/2026). Devolve (texto, veio_da_ia): IA quando está
    no ar, senão o modelo pronto de frases.py."""
    fatos = fatos_recusa_financeiro(boleto, observacao)
    try:
        return ia.redigir_recusa_financeiro(fatos), True
    except Exception as e:
        log.warning('IA da recusa falhou (%s); usando modelo pronto', e)
    return frases.recusa_financeiro(fatos), False


def recusar_pelo_financeiro(boleto, texto, quem):
    """Botão "Recusar boleto" do painel do financeiro (10/11/2026): o
    boleto está fora da data prevista de pagamento do contrato. Sai da fila
    (DEVOLVIDO — o posto volta a "sem boleto"), fica no histórico com o
    PDF; o prestador recebe o texto que o financeiro escreveu/gerou + o
    bloco de dados; o admin é avisado. Não manda CANCELAMENTO à equipe@:
    foi ela mesma que recusou. True se recusou."""
    texto = (texto or '').strip()
    if len(texto) < 20 or boleto.fin_pagamento_em or boleto.status not in (
            Boleto.Status.APROVADO, Boleto.Status.FIN_RECEBIDO):
        return False
    boleto.status = Boleto.Status.DEVOLVIDO
    boleto.devolvido_em = timezone.now()
    boleto.devolvido_motivo = (
        'recusado pelo financeiro — fora da data prevista de pagamento — '
        + texto)[:500]
    boleto.pagamento_enviado_em = None
    boleto.pagamento_enviado_valor = None
    boleto.save(update_fields=['status', 'devolvido_em', 'devolvido_motivo',
                               'pagamento_enviado_em',
                               'pagamento_enviado_valor'])
    fatos = _fatos(boleto)
    emails.enviar(
        destinatarios_pj(boleto),
        f'Boleto recusado — fora da data prevista de pagamento — emitir '
        f'outro — {fatos["alvo"]} — {fatos["competencia"]}',
        texto + dados_pj(boleto, fatos),
        boleto=boleto, cc=cc_gerente(boleto))
    emails.enviar(
        settings.EMAIL_ADMIN,
        f'⛔ Financeiro recusou boleto — {fatos["prestador"]} — '
        f'{fatos["competencia"]}',
        f'Cristiano,\n\n{quem} recusou o boleto #{boleto.pk} '
        f'({fatos["prestador"]} — {fatos["alvo"]} — {fatos["competencia"]}) '
        'no painel do financeiro: fora da data prevista de pagamento. O '
        'prestador foi avisado com o texto abaixo e o posto voltou a "sem '
        f'boleto".\n\n---\n{texto}\n---\n\n'
        'Painel: https://pj.camim.com.br/painel/\n\n— Controle dos PJs',
        boleto=boleto)
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator=quem,
        detalhe=f'Boleto #{boleto.pk}: recusado pelo financeiro (fora da '
                'data prevista de pagamento) — prestador avisado, posto '
                f'volta a "sem boleto" — texto: {texto[:300]}')
    return True


def _para_manual(boleto, motivo):
    _retirar_do_financeiro(boleto, motivo)
    boleto.ia_resposta = (boleto.ia_resposta + f'\n[manual] {motivo}').strip()
    _marcar(boleto, Boleto.Status.MANUAL)
    fatos = _fatos(boleto)
    emails.enviar(settings.EMAIL_ADMIN,
                  f'⚠️ Verificar boleto manualmente — {fatos["prestador"]} — '
                  f'{fatos["competencia"]}',
                  frases.corpo('manual_admin', fatos) + f'\n\nMotivo: {motivo}',
                  boleto=boleto)


def motivo_sem_posto(boleto, n_postos):
    """Por que o boleto não casou com posto nenhum — em português que
    diga o que fazer (03/11/2026: "sem valor acordado" escondia que o
    problema era não saber de qual posto o boleto era)."""
    if boleto.arquivo:
        veio = ('o PDF não traz o CNPJ, a razão social nem o nome de '
                'nenhum deles')
    else:
        veio = ('veio só a linha digitável, que não diz quem é o pagador')
    return (f'não identifiquei o posto: o prestador atende {n_postos} '
            f'postos e {veio}. Escolha o posto em "Editar" e reverifique'
            + (' — o prestador já foi avisado por e-mail'
               if boleto.pedido_posto_em else ''))


def pedir_identificacao_posto(boleto, em_resposta_a=None):
    """Resposta ao PJ cujo boleto chegou sem dizer qual unidade (empresa
    pagadora) é a dele — o robô não cadastra o pagamento sem isso. Sai no
    lugar do "recebemos": texto FIXO (sem IA), para as instruções saírem
    exatas, com os dados do boleto e as unidades do cadastro dele abaixo
    da assinatura. Fica ligada ao e-mail que chegou (em_resposta_a) para
    aparecer em "abrir o e-mail". Nunca leva gerente em cópia (não há
    posto). Retorna True se enviou."""
    prest = boleto.prestador
    fatos = _fatos(boleto)
    if boleto.arquivo:
        fatos['como_veio'] = ('o boleto em PDF anexado não traz o CNPJ, a '
                              'razão social nem o nome de nenhuma unidade '
                              'do seu cadastro')
    else:
        fatos['como_veio'] = ('o e-mail trouxe apenas a linha digitável, e '
                              'o código de barras informa somente o banco '
                              'e o valor — não diz quem é o pagador')
    corpo = frases.corpo('pedir_posto', fatos)
    unidades = [v.posto.nome for v in
                prest.vinculos_ativos().select_related('posto')
                .order_by('posto__nome')]
    valor = boleto.valor_extraido or valor_da_linha(boleto.linha_digitavel)
    bloco = ['', '-' * 40,
             f'Prestador: {prest.nome}'
             + (f' — CNPJ {prest.cnpj}' if prest.cnpj else '')]
    bloco.extend(linha_representante(boleto))
    bloco.append('Empresa pagadora: NÃO IDENTIFICADA — é o que precisamos '
                 'que você informe')
    bloco.append('Unidades do seu cadastro: ' + ', '.join(unidades))
    bloco.extend(linhas_competencia(boleto))
    if valor is not None:
        bloco.append(f'Valor (lido no código de barras): R$ {_moeda(valor)}')
    if boleto.linha_digitavel:
        bloco.append(f'Linha digitável: {boleto.linha_digitavel}')
    if boleto.nome_original:
        bloco.append(f'Arquivo recebido: {boleto.nome_original}')
    bloco.append('Situação: pagamento em espera até a unidade ser informada')
    ok = emails.enviar(
        destinatarios_pj(boleto),
        f'Falta identificar a empresa pagadora do boleto — '
        f'{fatos["competencia"]}',
        corpo + '\n'.join(bloco), boleto=boleto,
        em_resposta_a=em_resposta_a or boleto.email_origem)
    if ok:
        boleto.pedido_posto_em = timezone.now()
        boleto.save(update_fields=['pedido_posto_em'])
    AuditLog.registrar(
        AuditLog.Evento.STATUS, ator='sistema',
        detalhe=f'Boleto #{boleto.pk} sem posto identificável — '
                + ('prestador avisado por e-mail (pedida a empresa pagadora)'
                   if ok else 'FALHOU o e-mail ao prestador pedindo a '
                              'empresa pagadora'))
    return ok


def eh_mes_vigente(competencia):
    """A competência (mês do PAGAMENTO) é o mês atual? Só boleto do mês
    vigente vai sozinho ao financeiro — regra de 01/10/2026."""
    return competencia == timezone.localdate().replace(day=1)


def processar(boleto_pk):
    """Verifica um boleto RECEBIDO. Seguro para rodar por thread E por cron:
    a claim atômica em `tentativas` garante que só um processa."""
    try:
        boleto = (Boleto.objects.select_related('prestador', 'posto',
                                                'prestador__posto_cobranca')
                  .get(pk=boleto_pk))
    except Boleto.DoesNotExist:
        return

    if boleto.status != Boleto.Status.RECEBIDO:
        return
    claimed = Boleto.objects.filter(
        pk=boleto_pk, status=Boleto.Status.RECEBIDO,
        tentativas=boleto.tentativas,
    ).update(tentativas=boleto.tentativas + 1)
    if not claimed:
        return
    boleto.tentativas += 1

    fatos = _fatos(boleto)

    # 0) NOTA FISCAL: se o prestador exige, boleto sem NF não passa; e a
    # NF anexa (PDF) precisa ser NFS-e emitida pelo CNPJ do prestador.
    if boleto.prestador.exige_nf and not boleto.nota_fiscal:
        _para_manual(boleto, 'o prestador exige nota fiscal anexa e o '
                             'boleto veio sem NF')
        return
    if (boleto.nota_fiscal
            and boleto.nota_fiscal.name.lower().endswith('.pdf')):
        ok_nf, motivo_nf = svc_boletos.validar_nf(
            pdf.extrair_texto(boleto.nota_fiscal.path), boleto.prestador,
            posto=boleto.posto)
        if not ok_nf:
            _para_manual(boleto, motivo_nf)
            return

    # 1) VALOR DO PDF. Primeiro pelo TEXTO (IA barata). Sem texto (boleto
    # vetorial/escaneado — ex.: Meriti), ou a IA não achou o valor, ou a
    # confiança abaixo do limiar → FALLBACK: o opus ENXERGA a imagem
    # (pedido do dono, 08/10/2026). Imagem/foto entra direto pela visão.
    valor_pdf = None
    confianca_pdf = None
    dados = {}
    motivo_conf = ''
    por_visao = False
    limiar = Configuracao.get_int('limiar_confianca', 99)
    eh_pdf = bool(boleto.arquivo
                  and boleto.arquivo.name.lower().endswith('.pdf'))
    eh_imagem = bool(boleto.arquivo and not eh_pdf
                     and boleto.arquivo.name.lower().endswith(
                         ('.jpg', '.jpeg', '.png', '.webp')))

    texto = pdf.extrair_texto(boleto.arquivo.path) if eh_pdf else ''
    if texto:
        try:
            valor_pdf, bruto = ia.extrair_valor(texto)
            boleto.ia_resposta = bruto[:4000]
        except Exception as e:
            log.error('IA falhou no boleto #%s: %s', boleto_pk, e)
            if boleto.tentativas >= MAX_TENTATIVAS:
                _para_manual(boleto, f'IA indisponível após '
                                     f'{MAX_TENTATIVAS} tentativas: {e}')
            else:
                boleto.save()  # continua RECEBIDO; o cron tenta de novo
            return
        if valor_pdf is not None:
            dados = ia.carregar_json(bruto)
            confianca_pdf = _conf_ia(dados)

    # FALLBACK OPUS-VISÃO: só entra quando o texto não resolveu com a
    # precisão cadastrada. NÃO afrouxa nada — as travas (favorecido,
    # código×valor, mês, limiar) continuam valendo; isto só troca a FONTE
    # do valor, do texto para o que o opus enxerga na imagem.
    if (eh_pdf or eh_imagem) and (valor_pdf is None
                                  or (confianca_pdf or 0) < limiar):
        img, mime = _imagem_do_boleto(boleto)
        if img:
            try:
                v_img, bruto_img = ia.extrair_valor_imagem(img, mime=mime)
            except Exception as e:
                log.warning('fallback de visão falhou no boleto #%s: %s',
                            boleto_pk, e)
                v_img, bruto_img = None, ''
            if v_img is not None:
                dados_img = ia.carregar_json(bruto_img)
                conf_img = _conf_ia(dados_img)
                # fica com a visão quando o texto não deu valor OU quando a
                # visão está mais confiante.
                if valor_pdf is None or conf_img > (confianca_pdf or 0):
                    valor_pdf, confianca_pdf, dados = v_img, conf_img, dados_img
                    por_visao = True
                    boleto.ia_resposta = (bruto_img or '')[:4000]

    if valor_pdf is not None:
        # o modelo barato às vezes estropia o nome do campo
        # ("motivo_confiacao", "motivo_confianca?") — aceita qualquer chave
        # que comece com "motivo".
        for _k, _v in dados.items():
            if _k.lower().startswith('motivo') and str(_v or '').strip():
                motivo_conf = str(_v).strip()
                break

        # Dígitos para as conferências abaixo: o texto do PDF quando há; na
        # leitura por imagem, os documentos (CNPJ/CPF) + a linha que o opus
        # enxergou — assim a trava do FAVORECIDO vale mesmo sem texto.
        if por_visao:
            texto_doc = (
                ' '.join(str(d) for d in (dados.get('documentos') or []))
                + ' ' + str(dados.get('linha_digitavel') or ''))
        else:
            texto_doc = texto

        # FAVORECIDO: basta QUALQUER documento do prestador (CNPJ OU CPF do
        # representante) constar no boleto — proteção contra pagar boleto de
        # terceiros. Antes o "bit" recebe_por_cpf exigia SÓ o CPF; mas o C6
        # emite o boleto de quem recebe como pessoa física trazendo o CNPJ
        # do prestador, e o CPF nunca aparece (01/10/2026, Jose Elias). Como
        # bancos variam qual documento imprimem, aceitar os dois cobre todo
        # formato sem abrir a porta a terceiros (teria de bater 11/14 dígitos
        # por acaso).
        digitos_txt = re.sub(r'\D', '', texto_doc)
        docs = [d for d in (boleto.prestador.cnpj,
                            boleto.prestador.representante_cpf) if d]
        if docs and not any(re.sub(r'\D', '', d) in digitos_txt for d in docs):
            _para_manual(boleto,
                         'nenhum documento do prestador (' + ' nem '.join(docs)
                         + ') aparece no boleto — confira o FAVORECIDO antes '
                         'de liberar')
            return

        # Destino do boleto: dica pelo CNPJ do posto (sacado) impresso no
        # PDF. NÃO é rigoroso — muitos PJs emitem contra si mesmos; se não
        # achar, fica aguardando destinação manual no painel.
        if (boleto.posto_id is None
                and boleto.prestador.modo_boleto_em(boleto.competencia) ==
                Prestador.ModoBoleto.POR_POSTO):
            posto = svc_boletos.identificar_posto(texto_doc)
            if posto is None:
                vinculos = list(boleto.prestador.vinculos_ativos())
                if len(vinculos) == 1:  # só atende um posto: é ele
                    posto = vinculos[0].posto
            if posto is not None:
                boleto.posto = posto
                boleto.valor_esperado = svc_boletos.valor_esperado_para(
                    boleto.prestador, posto, boleto.competencia)
                fatos = _fatos(boleto)
                AuditLog.registrar(
                    AuditLog.Evento.STATUS, ator='sistema',
                    detalhe=f'Boleto #{boleto.pk} destinado a {posto} '
                            'pelo CNPJ do sacado')

        if not boleto.linha_digitavel:
            ld = re.sub(r'\D', '', str(dados.get('linha_digitavel') or ''))
            if 40 <= len(ld) <= 48:
                boleto.linha_digitavel = ld
        if not boleto.linha_digitavel and not por_visao:
            # a IA nem sempre devolve a linha; o texto do PDF a traz
            boleto.linha_digitavel = svc_boletos.linha_no_texto(texto)
        if boleto.vencimento is None:
            try:
                boleto.vencimento = datetime.strptime(
                    str(dados.get('vencimento') or ''), '%d/%m/%Y').date()
            except ValueError:
                pass
        # O mesmo boleto já tinha chegado só como linha digitável (sem
        # posto)? Agora que o PDF o destinou, o antigo vira SUBSTITUÍDO.
        boleto.save(update_fields=['linha_digitavel', 'posto',
                                   'valor_esperado'])
        svc_boletos.absorver_sem_posto(boleto)
    elif eh_pdf:
        # PDF sem texto e o opus também não leu → como antes, vai a manual.
        _para_manual(boleto, 'PDF sem texto legível e o opus (visão) também '
                             'não conseguiu ler o valor — confira manualmente')
        return
    elif eh_imagem and not boleto.linha_digitavel:
        # Imagem que o opus não leu e sem linha: nada para conferir.
        _para_manual(boleto, 'arquivo é imagem, o opus (visão) não leu e não '
                             'há linha digitável — nada para conferir')
        return

    # 2) Valor embutido no código de barras (determinístico, sem IA).
    valor_linha = valor_da_linha(boleto.linha_digitavel)

    if valor_pdf is None and valor_linha is None:
        _para_manual(boleto, 'sem PDF legível e sem linha digitável com '
                             'valor — nada para conferir')
        return

    # 3) O CÓDIGO TEM DE BATER COM O VALOR: PDF × código de barras.
    if (valor_pdf is not None and valor_linha is not None
            and abs(valor_pdf - valor_linha) > TOLERANCIA):
        _para_manual(boleto,
                     f'código de barras diz R$ {_moeda(valor_linha)}, mas o '
                     f'PDF diz R$ {_moeda(valor_pdf)} — documento '
                     'inconsistente, NÃO enviado para pagamento')
        return

    valor = valor_pdf if valor_pdf is not None else valor_linha
    boleto.valor_extraido = valor
    fatos['valor'] = _moeda(valor)

    # Confiança final: 100 quando o valor é determinístico (código de
    # barras) ou dupla checagem (PDF × linha batendo); senão, a que a
    # própria IA declarou na extração do PDF.
    if valor_pdf is None or valor_linha is not None:
        boleto.ia_confianca = 100
    else:
        boleto.ia_confianca = confianca_pdf if confianca_pdf is not None else 50

    # 4) NÃO DUPLICIDADE: nunca aprovar duas vezes a mesma competência.
    # Vira DUPLICADO e fica só marcado no painel — sem e-mail, sem drama
    # (o normal é o PJ mandar por e-mail algo que o admin já cadastrou).
    dup = svc_boletos.duplicado_de(boleto)
    if dup is not None:
        boleto.ia_resposta = (boleto.ia_resposta +
                              f'\n[duplicado] o boleto #{dup.pk} desta '
                              f'competência já está '
                              f'"{dup.get_status_display()}"').strip()
        _marcar(boleto, Boleto.Status.DUPLICADO)
        return

    # 5) O MÊS TEM DE BATER: vencimento dentro da janela da competência
    # (do dia 1 da competência até o fim do mês seguinte).
    if (boleto.vencimento is not None
            and not (boleto.competencia <= boleto.vencimento
                     < _mes_seguinte_fim(boleto.competencia))):
        _para_manual(boleto,
                     f'vencimento {boleto.vencimento:%d/%m/%Y} não bate com '
                     f'a competência {fatos["competencia"]}')
        return

    # PARCIAL: N boletos compõem a mensalidade — não compara um a um; a
    # SOMA das parciais aprovadas não pode passar do combinado do posto.
    if boleto.parcial:
        combinado = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)
        boleto.valor_esperado = combinado
        if combinado is None:
            _para_manual(boleto, 'boleto parcial sem valor combinado '
                                 'cadastrado no posto')
            return
        soma = svc_boletos.soma_parciais(boleto) + valor
        if soma - combinado > TOLERANCIA:
            _para_manual(boleto,
                         f'as parciais somariam R$ {_moeda(soma)} e passam '
                         f'do combinado (R$ {_moeda(combinado)}) — nada '
                         'enviado')
            return
        fatos = _fatos(boleto)  # refaz a continha com o valor conferido

    # Valores podem ter sido cadastrados DEPOIS do boleto chegar — na
    # reverificação, recalcula o esperado em vez de reclamar à toa.
    # Cobrança EXTRA não tem combinado: nunca herda o valor do posto.
    if boleto.valor_esperado is None and not boleto.extra:
        boleto.valor_esperado = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)

    if boleto.valor_esperado is None and not boleto.valor_livre:
        if boleto.extra:
            _para_manual(boleto, 'cobrança EXTRA sem valor de referência — '
                                 'confira e libere com "Aprovar assim '
                                 'mesmo" (ou edite marcando "aceitar este '
                                 'valor")')
        elif svc_boletos.sem_posto_identificavel(boleto):
            n = boleto.prestador.vinculos_ativos().count()
            _para_manual(boleto, motivo_sem_posto(boleto, n))
        else:
            _para_manual(boleto, 'sem valor acordado cadastrado no painel')
        return

    # 6) Valor × combinado. Igual → aprova. MENOR: só aprova sozinho se
    # houver observação (do mês ou do cadastro) que a IA CONFIRME que
    # explica a diferença (ex.: "descontada parcela 3/7 do notebook —
    # R$ 600"). Sem obs, obs que não explica, OU IA fora do ar → NÃO
    # aprova: vai para MANUAL ("precisam de você"), para o Cristiano
    # liberar com "Aprovar assim mesmo". MAIOR: NUNCA aprova sozinho.
    menor = (not boleto.valor_livre and not boleto.parcial
             and boleto.valor_esperado is not None
             and (boleto.valor_esperado - valor) > TOLERANCIA)
    if menor:
        obs = ' | '.join(t.strip() for t in
                         [boleto.observacao, boleto.prestador.observacao]
                         if t and t.strip())
        explica, motivo = False, ''
        if obs:
            try:
                explica, motivo = ia.avaliar_diferenca(
                    valor, boleto.valor_esperado, obs)
            except Exception as e:
                log.warning('IA de diferença falhou (%s); '
                            'mandando para conferência manual', e)
                explica, motivo = False, ''
        if not explica:
            _para_manual(
                boleto,
                f'valor ABAIXO do combinado (R$ {_moeda(valor)} × '
                f'R$ {_moeda(boleto.valor_esperado)}) e '
                + ('as observações registradas NÃO explicam a diferença'
                   if obs else 'não há observação que explique')
                + ' — confira e, se estiver certo, libere com "Aprovar '
                'assim mesmo"')
            return
        if motivo:
            fatos['motivo_menor'] = motivo
            boleto.ia_resposta = (boleto.ia_resposta +
                                  f'\n[menor] {motivo}').strip()

    # 7) GATE DE CONVICÇÃO: só envia para pagamento sozinho se a confiança
    # for >= limiar (Configurações; padrão 99%). Abaixo disso, espera o
    # Cristiano liberar no painel ("Aprovar assim mesmo").
    aprovaria = (boleto.valor_livre or boleto.parcial
                 or (valor - boleto.valor_esperado) <= TOLERANCIA)
    limiar = Configuracao.get_int('limiar_confianca', 99)
    if (aprovaria and not boleto.valor_livre
            and (boleto.ia_confianca or 0) < limiar):
        porque = f' Motivo da IA: {motivo_conf}.' if motivo_conf else ''
        _para_manual(boleto,
                     f'valor confere, mas a confiança da IA foi '
                     f'{boleto.ia_confianca}% (limiar: {limiar}%).{porque} '
                     'Nada enviado; libere o envio no painel se estiver ok')
        return

    # 8) GATE DO MÊS VIGENTE (regra do Cristiano, 01/10/2026): boleto de
    # competência que NÃO é o mês atual nunca vai sozinho ao financeiro —
    # espera ação humana ("Aprovar assim mesmo", ou acertar o mês em
    # "Editar"). Os 8 da RABISCO chegaram pela API como setembro e foram
    # ao financeiro no automático em outubro. Só segue direto o que JÁ está
    # lá com o mesmo valor: essa reaprovação não manda nada de novo.
    ja_la = (boleto.pagamento_enviado_em is not None
             and boleto.pagamento_enviado_valor is not None
             and abs(boleto.pagamento_enviado_valor - valor) <= TOLERANCIA)
    if aprovaria and not ja_la and not eh_mes_vigente(boleto.competencia):
        vigente = competencia_extenso(timezone.localdate().replace(day=1))
        _para_manual(boleto,
                     f'valor confere, mas a competência '
                     f'{fatos["competencia"]} não é o mês vigente '
                     f'({vigente}) — fora do mês vigente nada vai sozinho '
                     'ao financeiro. Se o mês estiver errado, corrija em '
                     '"Editar"; se estiver certo, libere com "Aprovar '
                     'assim mesmo"')
        return

    if aprovaria:
        boleto.aprovado_por = 'sistema'
        _marcar(boleto, Boleto.Status.APROVADO)
        enviar_para_pagamento(boleto, fatos)
    else:
        _retirar_do_financeiro(boleto, 'valor divergente do combinado')
        fatos['valor_esperado'] = _moeda(boleto.valor_esperado)
        _marcar(boleto, Boleto.Status.DIVERGENTE)
        emails.enviar(
            destinatarios_pj(boleto),
            f'Boleto {fatos["competencia"]} — valor a confirmar',
            frases.corpo(
                'divergente', fatos,
                instrucao_ia=('Escreva para o prestador dizendo que o valor '
                              'combinado não consta no boleto enviado e peça '
                              'para ligar para o Cristiano para entenderem a '
                              'diferença. NÃO cite números.')),
            boleto=boleto, cc=cc_gerente(boleto))


def processar_async(boleto_pk):
    """Só a verificação, sem reenviar o e-mail de 'recebemos' — para
    reverificação após edição no painel."""
    def _run():
        close_old_connections()
        try:
            processar(boleto_pk)
        except Exception:
            log.exception('reverificação do boleto #%s falhou', boleto_pk)
        finally:
            close_old_connections()
    threading.Thread(target=_run, daemon=True).start()


def fluxo_completo_async(boleto_pk):
    """Dispara em thread: e-mail de recebimento + verificação. O cron
    processar_boletos é a rede de segurança se a thread morrer."""
    def _run():
        close_old_connections()
        try:
            b = Boleto.objects.get(pk=boleto_pk)
            enviar_recebido(b)
            processar(boleto_pk)
        except Exception:
            log.exception('fluxo do boleto #%s falhou (cron retenta)',
                          boleto_pk)
        finally:
            close_old_connections()
    threading.Thread(target=_run, daemon=True).start()
