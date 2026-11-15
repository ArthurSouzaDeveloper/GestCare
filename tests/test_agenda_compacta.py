"""Agenda do dia em linhas compactas: mesma informação e mesmas ações, em menos espaço."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hoje_visoes import cliente, entrar  # noqa: E402,F401  (fixture reaproveitada)


def _html(c):
    r = c.get('/agenda')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_cada_dose_e_uma_linha_com_texto_e_acoes(cliente):
    html = _html(cliente)
    itens = html.count('class="agenda-item"')
    # Maria tem 00:01 e 23:59, João 00:01; a dose da outra casa não aparece
    assert itens == 3
    assert html.count('class="agenda-texto"') == itens
    assert html.count('class="agenda-acoes"') == itens
    assert 'Secreto' not in html


def test_administrado_continua_registrando_a_mesma_dose(cliente):
    html = _html(cliente)
    forms = re.findall(r'<form method="POST"\s+action="([^"]+)">\s*'
                       r'<input type="hidden" name="horario_previsto" value="([^"]+)">', html)
    alvos = {(a, h[:5]) for a, h in forms}
    assert ('/medicamento/1/administrar', '00:01') in alvos
    assert ('/medicamento/1/administrar', '23:59') in alvos
    assert ('/medicamento/2/administrar', '00:01') in alvos
    assert html.count('agenda-administrar') == 3
    assert 'Administrado' in html


def test_ver_paciente_vira_botao_de_icone_com_nome_acessivel(cliente):
    html = _html(cliente)
    assert 'aria-label="Ver paciente Maria Souza"' in html
    assert 'aria-label="Ver paciente João Pereira"' in html
    # o texto continua no HTML para leitor de tela e impressão de acessibilidade
    assert html.count('class="agenda-ver-texto">Ver paciente<') == 3
    # sem o estilo antigo de botão esticado na largura toda
    assert 'width:100%;text-align:center' not in html


def test_post_de_administrar_continua_registrando(cliente):
    import sqlite3
    import app as gc
    antes = sqlite3.connect(gc.DB_PATH).execute('SELECT COUNT(*) FROM doses_administradas WHERE medicamento_id = 1').fetchone()[0]
    r = cliente.post('/medicamento/1/administrar', data={'horario_previsto': '23:59', 'csrf_token': 'tok'})
    assert r.status_code in (200, 302)
    depois = sqlite3.connect(gc.DB_PATH).execute('SELECT COUNT(*) FROM doses_administradas WHERE medicamento_id = 1').fetchone()[0]
    assert depois == antes + 1
