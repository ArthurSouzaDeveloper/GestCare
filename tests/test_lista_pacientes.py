"""Testes da lista de pacientes (busca, filtros, páginas) e da busca rápida.

Rodar:  python -m pytest tests -q
"""
import os
import sqlite3
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gc  # noqa: E402


def nascimento(idade):
    h = date.today()
    return date(h.year - idade, h.month, min(h.day, 28)).isoformat()


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'teste.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up' / 'exames'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    gc.init_db()
    gc.init_db()  # idempotente
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa A', 'casa-a'), (2, 'Casa B', 'casa-b')")
    for i in range(1, 61):  # 60 pacientes na casa 1
        con.execute('INSERT INTO pacientes (nome, data_nascimento, quarto, casa_id) VALUES (?, ?, ?, 1)',
                    (f'Paciente {i:02d}', nascimento(60 + i % 40), f'{i % 5 + 1}0{i % 3}'))
    con.execute("INSERT INTO pacientes (nome, data_nascimento, quarto, casa_id) VALUES ('José Ávila', ?, '999', 1)", (nascimento(85),))
    con.execute("INSERT INTO pacientes (nome, data_nascimento, casa_id) VALUES ('Intruso Outra Casa', ?, 2)", (nascimento(80),))
    con.execute("INSERT INTO pacientes (nome, data_nascimento, casa_id, ativo) VALUES ('Removido Silva', ?, 1, 0)", (nascimento(80),))
    jose = con.execute("SELECT id FROM pacientes WHERE nome = 'José Ávila'").fetchone()[0]
    con.execute("INSERT INTO condicoes (paciente_id, nome) VALUES (?, 'Diabetes')", (jose,))
    senha = gc.hash_senha('x')
    con.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (1, 'Enf', 'e@a', ?, 'enfermeiro', 1)", (senha,))
    con.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (2, 'Resp', 'r@a', ?, 'responsavel', 1)", (senha,))
    con.commit()
    con.close()
    gc.app.config['TESTING'] = True
    c = gc.app.test_client()
    entrar(c, 1, 'enfermeiro')
    return c


def entrar(c, uid, perfil):
    with c.session_transaction() as s:
        s.clear()
        s.update(usuario_id=uid, usuario_nome='U', usuario_perfil=perfil, casa_id=1,
                 paciente_id=None, casa_slug='casa-a', csrf_token='tok')


def nomes(html):
    return [linha.split('<')[0] for linha in html.split('card-paciente-nome">')[1:]]


def test_pagina_traz_so_25_e_navega(cliente):
    r = cliente.get('/')
    assert r.status_code == 200
    n1 = nomes(r.get_data(as_text=True))
    assert len(n1) == 25
    assert 'Página 1 de 3' in r.get_data(as_text=True)
    n3 = nomes(cliente.get('/?pagina=3').get_data(as_text=True))
    assert len(n3) == 11  # 61 ativos da casa 1 = 25 + 25 + 11
    assert not set(n1) & set(n3)


def test_pagina_invalida_nao_quebra(cliente):
    assert cliente.get('/?pagina=abc').status_code == 200
    assert cliente.get('/?pagina=999').status_code == 200
    assert cliente.get('/?pagina=-4').status_code == 200


def test_total_da_casa_ignora_outra_casa_e_removidos(cliente):
    html = cliente.get('/').get_data(as_text=True)
    assert '61 moradores' in html
    assert 'Intruso' not in html and 'Removido' not in cliente.get('/?q=removido').get_data(as_text=True).split('card-paciente-nome')[-1][:5]


def test_busca_ignora_acento_e_caixa(cliente):
    assert nomes(cliente.get('/?q=jose').get_data(as_text=True)) == ['José Ávila']
    assert nomes(cliente.get('/?q=AVILA').get_data(as_text=True)) == ['José Ávila']


def test_busca_nao_acha_outra_casa(cliente):
    assert 'Intruso' not in cliente.get('/?q=intruso').get_data(as_text=True)


def test_filtros_quarto_condicao_idade(cliente):
    assert nomes(cliente.get('/?quarto=999').get_data(as_text=True)) == ['José Ávila']
    assert nomes(cliente.get('/?condicao=diabetes').get_data(as_text=True)) == ['José Ávila']
    assert nomes(cliente.get('/?idade=80-89&q=jose').get_data(as_text=True)) == ['José Ávila']
    assert nomes(cliente.get('/?idade=0-69&q=jose').get_data(as_text=True)) == []


def test_faixa_de_idade_confere_com_idade_exibida(cliente):
    html = cliente.get('/?idade=90-200').get_data(as_text=True)
    assert nomes(html)
    idades = [int(x.split(' anos')[0].split()[-1]) for x in html.split('card-info">')[1:]]
    assert all(i >= 90 for i in idades)


def test_filtro_malicioso_e_texto_com_curinga(cliente):
    assert cliente.get("/?q=%27%20OR%201%3D1%20--").status_code == 200
    assert cliente.get('/?idade=abc').status_code == 200
    assert nomes(cliente.get('/?q=%25').get_data(as_text=True)) == []  # '%' não vira curinga


def test_busca_rapida_json(cliente):
    d = cliente.get('/busca?q=jos').get_json()
    assert [r['nome'] for r in d['resultados']] == ['José Ávila']
    assert cliente.get('/busca?q=j').get_json()['resultados'] == []
    assert len(cliente.get('/busca?q=paciente').get_json()['resultados']) == 8
    assert cliente.get('/busca?q=intruso').get_json()['resultados'] == []


def test_recentes_guardam_ultimos_abertos(cliente):
    con = sqlite3.connect(gc.DB_PATH)
    jose = con.execute("SELECT id FROM pacientes WHERE nome = 'José Ávila'").fetchone()[0]
    con.close()
    assert cliente.get('/busca').get_json() == {'recentes': True, 'resultados': []}
    cliente.get(f'/paciente/{jose}')
    d = cliente.get('/busca').get_json()
    assert d['recentes'] and [r['nome'] for r in d['resultados']] == ['José Ávila']


def test_responsavel_nao_usa_busca_nem_lista(cliente):
    entrar(cliente, 2, 'responsavel')
    assert cliente.get('/busca?q=jose').status_code == 302
    assert cliente.get('/').status_code == 302


def test_indices_e_wal(cliente):
    con = sqlite3.connect(gc.DB_PATH)
    idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'idx_pacientes_casa', 'idx_medicamentos_paciente', 'idx_exames_paciente'} <= idx
    assert con.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    con.close()


def test_lista_vazia_mostra_cadastro(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'v.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    gc.init_db()
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa A', 'casa-a')")
    con.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (1, 'Enf', 'e@a', ?, 'enfermeiro', 1)", (gc.hash_senha('x'),))
    con.commit(); con.close()
    c = gc.app.test_client()
    entrar(c, 1, 'enfermeiro')
    assert 'Nenhum morador cadastrado ainda' in c.get('/').get_data(as_text=True)
