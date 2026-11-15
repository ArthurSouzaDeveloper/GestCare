"""Portal da família: telas do responsável, isolamento por idoso e direção visual por perfil."""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as gestcare  # noqa: E402


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(gestcare, 'DB_PATH', str(tmp_path / 'teste.db'))
    gestcare.app.config['TESTING'] = True
    gestcare.init_db()
    hoje = datetime.now().strftime('%Y-%m-%d')
    recente = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    antigo = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    fim = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d')
    with gestcare.get_db() as c:
        c.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa Teste', 'casa-teste')")
        c.execute("INSERT INTO pacientes (id, nome, data_nascimento, quarto, casa_id) "
                  "VALUES (1, 'Alzira Monteiro', '1942-03-10', '12', 1)")
        c.execute("INSERT INTO pacientes (id, nome, data_nascimento, quarto, casa_id) "
                  "VALUES (2, 'Outro Morador', '1940-01-01', '7', 1)")
        c.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, paciente_id, casa_id) "
                  "VALUES (10, 'Marcos Monteiro', 'm@x.com', 'x', 'responsavel', 1, 1)") \
            if 'casa_id' in [r[1] for r in c.execute('PRAGMA table_info(usuarios)')] else \
            c.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, paciente_id) "
                      "VALUES (10, 'Marcos Monteiro', 'm@x.com', 'x', 'responsavel', 1)")
        # Identidade do responsável aprovada pela casa (sem isso o portal fica bloqueado).
        c.execute("INSERT INTO consentimentos_biometria (id, casa_id, tipo_pessoa, pessoa_id, "
                  "consentido_por_nome, relacao, versao_termo) "
                  "VALUES (1, 1, 'responsavel', 10, 'Marcos Monteiro', 'Próprio', 'v1')")
        c.execute("INSERT INTO verificacoes_identidade (casa_id, tipo_pessoa, pessoa_id, consentimento_id, "
                  "foto_documento, foto_rosto, status) "
                  "VALUES (1, 'responsavel', 10, 1, 'd.jpg', 'r.jpg', 'aprovado')")
        c.execute("INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) "
                  "VALUES (11, 'Carla Enfermeira', 'c@x.com', 'x', 'enfermeiro', 1)")
        c.execute("INSERT INTO medicamentos (id, paciente_id, nome, dosagem, eh_antibiotico, data_fim_tratamento) "
                  "VALUES (1, 1, 'Amoxicilina', '500 mg', 1, ?)", (fim,))
        c.execute("INSERT INTO medicamentos (id, paciente_id, nome, dosagem) VALUES (2, 2, 'Remedio Secreto', '1 mg')")
        for h in ('00:01', '23:58'):
            c.execute("INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (1, ?)", (h,))
        c.execute("INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (2, '09:00')")
        c.execute("INSERT INTO doses_administradas (medicamento_id, horario_previsto, administrado_por) "
                  "VALUES (1, ?, 'Carla')", (hoje + 'T00:01',))
        c.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame, laboratorio, resultado, "
                  "arquivo_nome, observacao_familia, observacoes) "
                  "VALUES (1, 'Hemograma completo', ?, 'Lab Vida', 'Hemoglobina baixa', 'abc.pdf', "
                  "'Anemia leve, sem preocupação.', 'NOTA INTERNA')", (recente,))
        c.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame) VALUES (1, 'Glicemia', ?)", (antigo,))
        c.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame) VALUES (2, 'Exame do Outro', ?)", (recente,))
        c.execute("INSERT INTO visitas (paciente_id, visitante_nome, visitante_parentesco, data_visita) "
                  "VALUES (1, 'Helena', 'neta', ?)", (recente,))
        c.execute("INSERT INTO evolucoes_medicas (paciente_id, data_evolucao, profissional, descricao) "
                  "VALUES (1, ?, 'Dr. Paulo', 'Pressão controlada.')", (recente,))
    return gestcare.app.test_client()


def entrar(cliente, perfil='responsavel', paciente_id=1):
    with cliente.session_transaction() as s:
        s.update(usuario_id=10 if perfil == 'responsavel' else 11,
                 usuario_nome='Marcos Monteiro' if perfil == 'responsavel' else 'Carla Enfermeira',
                 usuario_perfil=perfil,
                 casa_id=1, casa_nome='Casa Teste', paciente_id=paciente_id)


def test_inicio_mostra_situacao_das_doses(cliente):
    entrar(cliente)
    html = cliente.get('/meu-paciente').get_data(as_text=True)
    assert 'Como está Alzira hoje' in html
    assert 'Dado' in html and 'registrado por Carla' in html
    assert 'Sem registro ainda' in html or 'Mais tarde' in html


def test_doses_do_dia_casadas_por_ordem():
    agora = datetime(2026, 11, 15, 12, 0)
    # Evita depender do banco: monta a lógica com um conn falso de SQLite em memória.
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE medicamentos (id INTEGER, paciente_id INTEGER, nome TEXT, dosagem TEXT, ativo INTEGER,
            eh_antibiotico INTEGER, data_fim_tratamento TEXT);
        CREATE TABLE horarios_medicamento (medicamento_id INTEGER, horario TEXT);
        CREATE TABLE doses_administradas (id INTEGER PRIMARY KEY, medicamento_id INTEGER,
            horario_previsto TEXT, administrado_por TEXT);
        INSERT INTO medicamentos VALUES (1, 1, 'X', '1 mg', 1, 1, '2026-11-17');
        INSERT INTO horarios_medicamento VALUES (1, '08:00'), (1, '11:00'), (1, '14:00'), (1, '20:00');
        INSERT INTO doses_administradas (medicamento_id, horario_previsto, administrado_por)
            VALUES (1, '2026-11-15T08:00', 'Carla'), (1, '2026-11-14T20:00', 'Ontem');
    """)
    r = gestcare._remedios_do_paciente(conn, 1, agora)[0]
    assert [d['situacao'] for d in r['doses_hoje']] == ['dado', 'sem_registro', 'mais_tarde', 'mais_tarde']
    assert r['doses_hoje'][0]['por'] == 'Carla'
    assert r['dias_restantes'] == 2


def test_exames_com_recado_pdf_e_sem_nota_interna(cliente):
    entrar(cliente)
    html = cliente.get('/meu-paciente/exames').get_data(as_text=True)
    assert 'Recado da casa' in html and 'Anemia leve' in html
    assert '/exame/arquivo/abc.pdf' in html
    assert 'NOTA INTERNA' not in html
    assert 'Recente' in html
    assert 'Exame do Outro' not in html


def test_saude_nao_vaza_remedio_de_outro_idoso(cliente):
    entrar(cliente)
    html = cliente.get('/meu-paciente/saude').get_data(as_text=True)
    assert 'Amoxicilina' in html and 'Antibiótico' in html
    assert 'Remedio Secreto' not in html


def test_visitas(cliente):
    entrar(cliente)
    assert 'Helena' in cliente.get('/meu-paciente/visitas').get_data(as_text=True)


@pytest.mark.parametrize('rota', ['/meu-paciente', '/meu-paciente/saude', '/meu-paciente/exames', '/meu-paciente/visitas'])
def test_equipe_e_anonimo_nao_entram_no_portal(cliente, rota):
    assert cliente.get(rota).status_code == 302          # sem login
    entrar(cliente, perfil='enfermeiro')
    resp = cliente.get(rota)
    assert resp.status_code == 302 and 'Alzira' not in resp.get_data(as_text=True)


def test_sem_idoso_vinculado_vai_para_vinculo(cliente):
    entrar(cliente, paciente_id=None)
    assert cliente.get('/meu-paciente/exames').headers['Location'].endswith('/responsavel/vincular-idoso')


def test_identidade_nao_aprovada_bloqueia_todas_as_telas(cliente, monkeypatch):
    """Quando o repositório de identidade existir, nenhuma tela do portal abre sem aprovação."""
    monkeypatch.setattr(gestcare, 'responsavel_verificado', lambda conn, uid: False, raising=False)
    entrar(cliente)
    for rota in ('/meu-paciente', '/meu-paciente/saude', '/meu-paciente/exames', '/meu-paciente/visitas'):
        resp = cliente.get(rota)
        assert resp.status_code == 302, rota
        assert 'Alzira' not in resp.get_data(as_text=True)
    monkeypatch.setattr(gestcare, 'responsavel_verificado', lambda conn, uid: True, raising=False)
    assert cliente.get('/meu-paciente').status_code == 200


def test_direcao_visual_por_perfil(cliente):
    entrar(cliente)
    assert 'data-direcao="B"' in cliente.get('/meu-paciente').get_data(as_text=True)
    entrar(cliente, perfil='enfermeiro')
    assert 'data-direcao="A"' in cliente.get('/').get_data(as_text=True)
    anonimo = gestcare.app.test_client()
    assert 'data-direcao="A"' in anonimo.get('/login').get_data(as_text=True)
