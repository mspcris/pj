"""Tipos de aviso que o sistema manda por e-mail — fonte única.

Usado em dois lugares:
  * filtro do log de e-mails do painel (já classificava pelo assunto);
  * preferências de cópia interna: cada observador (EMAIL_COPIA_OCULTA)
    escolhe, no painel Notificações, quais tipos quer receber em cópia
    oculta.

A classificação é pelo ASSUNTO, que é nosso e determinístico (mesmo
critério do filtro do painel): o primeiro trecho que casa define o tipo.
"""

# (codigo, rótulo no painel, trecho que identifica o assunto)
TIPOS_EMAIL = [
    ('recebido', 'Boleto recebido', 'Boleto recebido'),
    ('pagamento', 'Pagamento (equipe@)', 'Pagamento — '),
    ('aprovado', 'Aprovado (aviso ao PJ)', 'Boleto aprovado'),
    ('financeiro', 'Financeiro recebeu', 'Boleto com o financeiro'),
    ('nota', 'Nota fiscal (equipe@)', 'nota fiscal — '),
    ('divergente', 'Valor a confirmar', 'valor a confirmar'),
    ('manual', 'Verificar manualmente', 'Verificar boleto manualmente'),
    ('lembrete', 'Lembrete diário', 'Lembrete'),
]

# Tudo que não casa com os trechos acima (alertas ⚠️ de discrepância,
# cancelamento, remetente não cadastrado, boleto sem leitura, etc.) cai aqui
# — assim a preferência "desligar" cobre TODO e-mail, não só os 8 tipos.
OUTROS = ('outros', 'Outros avisos (alertas ⚠️ e demais)')

# Catálogo que a tela de preferências mostra: os 8 tipos + o catch-all.
TIPOS_PREF = [(c, r) for c, r, _ in TIPOS_EMAIL] + [OUTROS]
CODIGOS_PREF = [c for c, _ in TIPOS_PREF]


def classificar(assunto):
    """Código do tipo a partir do assunto. Nunca None: o que não casa com
    nenhum trecho conhecido vira 'outros'."""
    a = (assunto or '').lower()
    for codigo, _rotulo, trecho in TIPOS_EMAIL:
        if trecho.lower() in a:
            return codigo
    return OUTROS[0]
