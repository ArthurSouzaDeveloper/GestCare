"""Testes da aba Usuários: editar, excluir (reversível), restaurar e apagar de vez.

Rodar:  python -m pytest tests -q
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gc  # noqa: E402

CPF_OK = '529.982.247-25'  # CPF fictício, dígitos válidos


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'teste.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up' / 'exames'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    gc.init_db()
    gc.init_db()  # idempotente
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa A', 'casa-a'), (2, 'Casa B', 'casa-b')")
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, casa_id) VALUES (1, 'Idoso A', '1940-01-01', 1)")
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, casa_id) VALUES (2, 'Idoso B', '1940-01-01', 2)")
    senha = gc.hash_senha('senha-antiga')
    for u in [(1, 'Admin A', 'a@a', 'admin', 1), (2, 'Enf A', 'e@a', 'enfermeiro', 1),
              (3, 'Resp A', 'r@a', 'responsavel', 1), (4, 'Admin B', 'a@b', 'admin', 2)]:
        con.execute('INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (?, ?, ?, ?, ?, ?)',
                    (*u[:3], senha, *u[3:]))
    con.commit()
    con.close()
    gc.app.config['TESTING'] = True
    return gc.app.test_client()


PERFIS = {1: ('admin', 1), 2: ('enfermeiro', 1), 3: ('responsavel', 1), 4: ('admin', 2)}


def entrar(c, uid):
    perfil, casa = PERFIS[uid]
    with c.session_transaction() as s:
        s.clear()
        s.update(usuario_id=uid, usuario_nome='U', usuario_perfil=perfil, casa_id=casa,
                 paciente_id=None, casa_slug='casa-a', csrf_token='tok')


def post(c, url, **dados):
    dados.setdefault('csrf_token', 'tok')
    return c.post(url, data=dados)


def usuario(uid):
    con = sqlite3.connect(gc.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute('SELECT * FROM usuarios WHERE id = ?', (uid,)).fetchone()
    finally:
        con.close()


def form_edicao(**extra):
    d = dict(nome='Enf A', email='e@a', perfil='enfermeiro', senha='', confirmar_senha='')
    d.update(extra)
    return d


# ── Migração ────────────────────────────────────────────────────────────────

def test_migracao_adiciona_colunas_em_banco_antigo(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'antigo.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    os.makedirs(tmp_path / 'db')
    con = sqlite3.connect(gc.DB_PATH)
    con.execute('CREATE TABLE usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, '
                'email TEXT NOT NULL UNIQUE, senha TEXT NOT NULL, perfil TEXT NOT NULL DEFAULT "enfermeiro", '
                'ativo INTEGER NOT NULL DEFAULT 1)')
    con.execute("INSERT INTO usuarios (nome, email, senha) VALUES ('Velho', 'v@v', 'x')")
    con.commit()
    con.close()
    gc.init_db()
    con = sqlite3.connect(gc.DB_PATH)
    colunas = {r[1] for r in con.execute('PRAGMA table_info(usuarios)')}
    linha = con.execute("SELECT excluido_em, ativo FROM usuarios WHERE email = 'v@v'").fetchone()
    con.close()
    assert {'excluido_em', 'excluido_por', 'atualizado_em'} <= colunas
    assert linha == (None, 1)  # quem já existia continua ativo e não excluído


# ── Editar ──────────────────────────────────────────────────────────────────

def test_lista_mostra_botao_editar_em_destaque(cliente):
    entrar(cliente, 1)
    html = cliente.get('/usuarios').get_data(as_text=True)
    assert '/usuarios/2/editar' in html
    assert 'botao botao-primario botao-acao-principal' in html


def test_admin_edita_dados_sem_trocar_senha(cliente):
    entrar(cliente, 1)
    antes = usuario(2)['senha']
    r = post(cliente, '/usuarios/2/editar', **form_edicao(nome='Enfermeira Ana', email='ANA@a'))
    assert r.status_code == 302
    u = usuario(2)
    assert (u['nome'], u['email'], u['senha']) == ('Enfermeira Ana', 'ana@a', antes)
    assert u['atualizado_em']


def test_admin_troca_senha_com_mesma_regra(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/editar', **form_edicao(senha='123', confirmar_senha='123'))
    assert gc.verificar_senha('senha-antiga', usuario(2)['senha'])  # curta demais: recusada
    post(cliente, '/usuarios/2/editar', **form_edicao(senha='nova-senha', confirmar_senha='outra'))
    assert gc.verificar_senha('senha-antiga', usuario(2)['senha'])  # não confere: recusada
    post(cliente, '/usuarios/2/editar', **form_edicao(senha='nova-senha', confirmar_senha='nova-senha'))
    assert gc.verificar_senha('nova-senha', usuario(2)['senha'])


def test_editar_responsavel_vincula_paciente_e_cpf(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/3/editar', **form_edicao(nome='Resp A', email='r@a', perfil='responsavel',
                                                      paciente_id='1', cpf='52998224725'))
    u = usuario(3)
    assert (u['paciente_id'], u['cpf']) == (1, CPF_OK)


def test_editar_recusa_paciente_de_outra_casa_cpf_invalido_e_superadmin(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/3/editar', **form_edicao(perfil='responsavel', paciente_id='2'))
    post(cliente, '/usuarios/3/editar', **form_edicao(perfil='responsavel', cpf='111.111.111-11'))
    post(cliente, '/usuarios/2/editar', **form_edicao(perfil='superadmin'))
    assert usuario(3)['paciente_id'] is None and usuario(3)['cpf'] is None
    assert usuario(3)['perfil'] == 'responsavel'
    assert usuario(2)['perfil'] == 'enfermeiro'


def test_editar_email_duplicado(cliente):
    entrar(cliente, 1)
    r = post(cliente, '/usuarios/2/editar', **form_edicao(email='r@a'))
    assert r.status_code == 200 and 'já está em uso' in r.get_data(as_text=True)
    assert usuario(2)['email'] == 'e@a'


def test_editar_propria_conta_vai_para_meu_perfil(cliente):
    entrar(cliente, 1)
    r = cliente.get('/usuarios/1/editar')
    assert r.status_code == 302 and r.headers['Location'].endswith('/meu-perfil')
    post(cliente, '/usuarios/1/editar', **form_edicao(nome='X', email='a@a', perfil='enfermeiro'))
    assert usuario(1)['perfil'] == 'admin'


def test_nao_edita_usuario_de_outra_casa_nem_sem_ser_admin(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/4/editar', **form_edicao(nome='Invasor', email='a@b', perfil='admin'))
    assert usuario(4)['nome'] == 'Admin B'
    entrar(cliente, 2)
    post(cliente, '/usuarios/3/editar', **form_edicao(nome='Invasor', email='r@a', perfil='responsavel'))
    assert usuario(3)['nome'] == 'Resp A'


def test_mudar_perfil_derruba_sessao_aberta(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/editar', **form_edicao(perfil='admin'))
    entrar(cliente, 2)  # sessão antiga, ainda como enfermeiro
    r = cliente.get('/')
    assert r.status_code == 302 and '/entrar/casa-a' in r.headers['Location']


def test_novo_usuario_recusa_perfil_superadmin(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/novo', nome='Novo', email='n@a', perfil='superadmin',
         senha='123456', confirmar_senha='123456')
    con = sqlite3.connect(gc.DB_PATH)
    assert con.execute("SELECT COUNT(*) FROM usuarios WHERE email = 'n@a'").fetchone()[0] == 0
    con.close()


# ── Excluir, restaurar, apagar de vez ───────────────────────────────────────

def test_excluir_e_reversivel_e_tira_o_acesso(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/excluir')
    u = usuario(2)
    assert u is not None and u['excluido_em'] and u['excluido_por'] == 1 and u['ativo'] == 0
    html = cliente.get('/usuarios').get_data(as_text=True)
    assert 'e@a' not in html
    excluidos = cliente.get('/usuarios?ver=excluidos').get_data(as_text=True)
    assert 'Enf A' in excluidos and '/usuarios/2/restaurar' in excluidos and 'por Admin A' in excluidos

    # sessão aberta do excluído cai no próximo clique
    entrar(cliente, 2)
    r = cliente.get('/')
    assert r.status_code == 302 and '/entrar/casa-a' in r.headers['Location']
    # e não consegue entrar de novo
    r = cliente.post('/login', data={'email': 'e@a', 'senha': 'senha-antiga', 'csrf_token': 'tok'})
    with cliente.session_transaction() as s:
        assert 'usuario_id' not in s


def test_excluido_nao_pode_ser_editado_nem_ativado(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/excluir')
    post(cliente, '/usuarios/2/toggle')
    post(cliente, '/usuarios/2/editar', **form_edicao(nome='Outro'))
    u = usuario(2)
    assert u['ativo'] == 0 and u['excluido_em'] and u['nome'] == 'Enf A'


def test_restaurar_devolve_acesso_sem_logar_ninguem(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/excluir')
    post(cliente, '/usuarios/2/restaurar')
    u = usuario(2)
    assert u['excluido_em'] is None and u['excluido_por'] is None and u['ativo'] == 1
    with cliente.session_transaction() as s:
        assert s['usuario_id'] == 1  # o admin continua sendo quem está logado
    # o restaurado entra com a mesma senha de antes
    with cliente.session_transaction() as s:
        s.clear()
        s['csrf_token'] = 'tok'
    cliente.post('/login', data={'email': 'e@a', 'senha': 'senha-antiga', 'csrf_token': 'tok'})
    with cliente.session_transaction() as s:
        assert s.get('usuario_id') == 2


def test_restaurar_so_funciona_para_excluido_da_mesma_casa(cliente):
    entrar(cliente, 4)
    post(cliente, '/usuarios/4/excluir')  # não pode excluir a si mesmo
    assert usuario(4)['excluido_em'] is None
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/excluir')
    entrar(cliente, 4)
    post(cliente, '/usuarios/2/restaurar')
    assert usuario(2)['excluido_em'] is not None


def test_email_de_excluido_orienta_a_restaurar(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/excluir')
    r = post(cliente, '/usuarios/novo', nome='Outra', email='e@a', perfil='enfermeiro',
             senha='123456', confirmar_senha='123456')
    assert 'Restaure-o na aba' in r.get_data(as_text=True)


def test_apagar_de_vez_so_depois_de_excluir(cliente):
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/apagar')
    assert usuario(2) is not None  # ainda não estava em Excluídos
    post(cliente, '/usuarios/2/excluir')
    post(cliente, '/usuarios/2/apagar')
    assert usuario(2) is None


def test_ultimo_admin_nao_e_excluido(cliente):
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("UPDATE usuarios SET perfil = 'admin' WHERE id = 2")
    con.commit()
    con.close()
    entrar(cliente, 1)
    post(cliente, '/usuarios/2/excluir')
    assert usuario(2)['excluido_em']  # havia outro admin (o próprio 1)
