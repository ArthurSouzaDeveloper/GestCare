"""Testes da tela Hoje, das visões gerais (Exames, Visitas, Evoluções, Medicamentos) e do menu."""
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gc  # noqa: E402

HOJE = date.today().isoformat()
ONTEM = (date.today() - timedelta(days=3)).isoformat()


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'teste.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up' / 'exames'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    gc.init_db()
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa A', 'casa-a'), (2, 'Casa B', 'casa-b')")
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, quarto, casa_id) VALUES (1, 'Maria Souza', '1940-01-01', '12', 1)")
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, quarto, casa_id) VALUES (2, 'João Pereira', '1940-01-01', '14', 1)")
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, quarto, casa_id) VALUES (3, 'Outra Casa', '1940-01-01', '1', 2)")
    # Maria: remédio às 00:01 e 23:59 (um já passou, outro ainda não), nenhuma dose registrada
    con.execute("INSERT INTO medicamentos (id, paciente_id, nome, dosagem) VALUES (1, 1, 'Losartana', '50mg')")
    con.execute("INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (1, '00:01'), (1, '23:59')")
    # João: mesmo remédio às 00:01 e já registrado hoje
    con.execute("INSERT INTO medicamentos (id, paciente_id, nome, eh_antibiotico, data_fim_tratamento) VALUES (2, 2, 'Amoxicilina', 1, ?)", (HOJE,))
    con.execute("INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (2, '00:01')")
    con.execute("INSERT INTO doses_administradas (medicamento_id, horario_previsto, administrado_por) VALUES (2, ?, 'Enf')", (HOJE + 'T00:01:00',))
    # remédio de outra casa nunca aparece
    con.execute("INSERT INTO medicamentos (id, paciente_id, nome) VALUES (3, 3, 'Secreto')")
    con.execute("INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (3, '00:01')")
    con.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame) VALUES (1, 'Hemograma', ?)", (HOJE,))
    con.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame, resultado) VALUES (2, 'Urina', ?, 'normal')", (HOJE,))
    con.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame) VALUES (3, 'ExameOutraCasa', ?)", (HOJE,))
    con.execute("INSERT INTO visitas (paciente_id, visitante_nome, data_visita) VALUES (1, 'Filha Ana', ?)", (HOJE,))
    con.execute("INSERT INTO visitas (paciente_id, visitante_nome, data_visita) VALUES (2, 'Primo Velho', ?)", (ONTEM,))
    con.execute("INSERT INTO evolucoes_medicas (paciente_id, data_evolucao, profissional, descricao) VALUES (1, ?, 'Dra. Lia', 'Boa evolução')", (HOJE,))
    con.execute("INSERT INTO evolucoes_medicas (paciente_id, data_evolucao, profissional, descricao) VALUES (2, ?, 'Dr. Antigo', 'Antiga')", (ONTEM,))
    senha = gc.hash_senha('x')
    con.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (1, 'Enf', 'e@a', ?, 'enfermeiro', 1)", (senha,))
    con.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (2, 'Adm', 'a@a', ?, 'admin', 1)", (senha,))
    con.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (3, 'Resp', 'r@a', ?, 'responsavel', 1)", (senha,))
    con.commit(); con.close()
    gc.app.config['TESTING'] = True
    c = gc.app.test_client()
    entrar(c, 1, 'enfermeiro')
    return c


def entrar(c, uid, perfil):
    with c.session_transaction() as s:
        s.clear()
        s.update(usuario_id=uid, usuario_nome='U', usuario_perfil=perfil, casa_id=1,
                 paciente_id=None, casa_slug='casa-a', csrf_token='tok')


def pag(c, url):
    r = c.get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


def test_doses_sem_registro_usam_a_regra_da_ordem():
    # relógio fixo: 10:00 — Maria tem 00:01 (passou) e 23:59 (depois); João já deu a dose das 00:01
    agora = datetime.now().replace(hour=10, minute=0)
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.executescript('''
        CREATE TABLE pacientes (id INTEGER PRIMARY KEY, nome TEXT, quarto TEXT, casa_id INT, ativo INT DEFAULT 1);
        CREATE TABLE medicamentos (id INTEGER PRIMARY KEY, paciente_id INT, nome TEXT, dosagem TEXT, ativo INT DEFAULT 1);
        CREATE TABLE horarios_medicamento (id INTEGER PRIMARY KEY, medicamento_id INT, horario TEXT);
        CREATE TABLE doses_administradas (id INTEGER PRIMARY KEY, medicamento_id INT, horario_previsto TEXT);
        INSERT INTO pacientes VALUES (1,'Maria','12',1,1);
        INSERT INTO medicamentos VALUES (1,1,'L','50mg',1);
        INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (1,'08:00'),(1,'09:40'),(1,'11:00'),(1,'13:00');
    ''')
    atr, prox = gc._doses_sem_registro(con, 1, agora)
    assert [a['horario'] for a in atr] == ['08:00']          # 09:40 ainda está na tolerância de 30 min
    assert [p['horario'] for p in prox] == ['09:40', '11:00']
    con.execute("INSERT INTO doses_administradas (medicamento_id, horario_previsto) VALUES (1, ?)", (agora.strftime('%Y-%m-%d') + 'T08:00:00',))
    atr, _ = gc._doses_sem_registro(con, 1, agora)
    assert atr == []


def test_virada_do_dia_nao_marca_tudo_como_atrasado():
    con = sqlite3.connect(':memory:'); con.row_factory = sqlite3.Row
    con.executescript('''
        CREATE TABLE pacientes (id INTEGER PRIMARY KEY, nome TEXT, quarto TEXT, casa_id INT, ativo INT DEFAULT 1);
        CREATE TABLE medicamentos (id INTEGER PRIMARY KEY, paciente_id INT, nome TEXT, dosagem TEXT, ativo INT DEFAULT 1);
        CREATE TABLE horarios_medicamento (id INTEGER PRIMARY KEY, medicamento_id INT, horario TEXT);
        CREATE TABLE doses_administradas (id INTEGER PRIMARY KEY, medicamento_id INT, horario_previsto TEXT);
        INSERT INTO pacientes VALUES (1,'Maria','12',1,1);
        INSERT INTO medicamentos VALUES (1,1,'L','50mg',1);
        INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (1,'08:00'),(1,'23:50');
    ''')
    atr, prox = gc._doses_sem_registro(con, 1, datetime.now().replace(hour=0, minute=10))
    assert atr == []
    atr, prox = gc._doses_sem_registro(con, 1, datetime.now().replace(hour=23, minute=0))
    assert [p['horario'] for p in prox] == ['23:50'] and [a['horario'] for a in atr] == ['08:00']


def test_hoje_mostra_so_a_casa_e_so_o_que_pede_atencao(cliente):
    html = pag(cliente, '/hoje')
    assert 'Maria Souza' in html and 'Losartana' in html
    assert 'Secreto' not in html and 'Outra Casa' not in html
    assert 'Amoxicilina' in html and 'Termina hoje' in html


def test_login_leva_equipe_para_hoje():
    assert "url_for('hoje')" in open(gc.__file__).read()


def test_exames_abre_so_com_pendentes_e_busca_por_paciente(cliente):
    html = pag(cliente, '/exames')
    assert 'Hemograma' in html and 'Urina' not in html and 'ExameOutraCasa' not in html
    assert 'Urina' in pag(cliente, '/exames?situacao=todos')
    assert 'Hemograma' not in pag(cliente, '/exames?situacao=todos&q=joao')


def test_visitas_hoje_e_periodos(cliente):
    assert 'Filha Ana' in pag(cliente, '/visitas') and 'Primo Velho' not in pag(cliente, '/visitas')
    assert 'Primo Velho' in pag(cliente, '/visitas?periodo=todas')


def test_evolucoes_24h(cliente):
    assert 'Dra. Lia' in pag(cliente, '/evolucoes') and 'Dr. Antigo' not in pag(cliente, '/evolucoes')
    assert 'Dr. Antigo' in pag(cliente, '/evolucoes?periodo=todas')


def test_medicamentos_visoes(cliente):
    assert 'Amoxicilina' in pag(cliente, '/medicamentos') and 'Losartana' not in pag(cliente, '/medicamentos')
    todos = pag(cliente, '/medicamentos?visao=todos')
    assert 'Losartana' in todos and 'Secreto' not in todos
    assert 'Maria Souza' in pag(cliente, '/medicamentos?visao=atrasadas')


def test_valores_estranhos_nao_quebram(cliente):
    for url in ['/exames?situacao=x', '/visitas?periodo=zzz&pagina=abc', '/medicamentos?visao=%27', '/evolucoes?pagina=999',
                '/exames?q=%25', '/registros']:
        pag(cliente, url)


def test_menu_novo_para_equipe_e_admin(cliente):
    html = pag(cliente, '/hoje')
    for rotulo in ('Hoje', 'Pacientes', 'Agenda', 'Registros', 'Exames', 'Visitas', 'Evoluções', 'Medicamentos'):
        assert f'<span>{rotulo}</span>' in html
    assert '<span>Usuários</span>' not in html
    entrar(cliente, 2, 'admin')
    html = pag(cliente, '/hoje')
    assert '<span>Usuários</span>' in html and '<span>Repositório</span>' in html


def test_responsavel_nao_entra_nas_visoes(cliente):
    entrar(cliente, 3, 'responsavel')
    for url in ['/hoje', '/exames', '/visitas', '/evolucoes', '/medicamentos', '/registros']:
        assert cliente.get(url).status_code == 302
