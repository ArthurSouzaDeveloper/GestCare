"""Termo LGPD: aceite no cadastro do responsável, aceite no primeiro acesso do
administrador da casa, registro no banco, nova versão e migração sem perda.

Rodar:  python -m pytest tests -q
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gc  # noqa: E402

CPF_OK = '529.982.247-25'  # CPF fictício, dígitos válidos


def _preparar(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'teste.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up' / 'exames'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    monkeypatch.setitem(gc.app.config, 'TERMO_OBRIGATORIO', True)
    gc.app.config['TESTING'] = True


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    _preparar(tmp_path, monkeypatch)
    gc.init_db()
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa Sol', 'casa-sol')")
    senha = gc.hash_senha('senha123')
    for u in [(1, 'Admin', 'a@a', 'admin'), (2, 'Enf', 'e@a', 'enfermeiro'), (3, 'Resp', 'r@a', 'responsavel')]:
        con.execute('INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id) VALUES (?, ?, ?, ?, ?, 1)',
                    (*u[:3], senha, u[3]))
    con.commit()
    con.close()
    return gc.app.test_client()


PERFIS = {1: 'admin', 2: 'enfermeiro', 3: 'responsavel'}


def entrar(c, uid):
    with c.session_transaction() as s:
        s.clear()
        s.update(usuario_id=uid, usuario_nome='U', usuario_perfil=PERFIS[uid], casa_id=1,
                 paciente_id=None, casa_slug='casa-sol', casa_nome='Casa Sol', csrf_token='tok')


def consulta(sql, *p):
    con = sqlite3.connect(gc.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, p).fetchall()
    finally:
        con.close()


def form_cadastro(**extra):
    d = dict(csrf_token='tok', nome='Maria Filha', email='maria@x.com', cpf=CPF_OK,
             senha='segredo1', confirmar_senha='segredo1')
    d.update(extra)
    return d


def _csrf(c):
    with c.session_transaction() as s:
        s['csrf_token'] = 'tok'


# ── Cadastro do responsável ─────────────────────────────────────────────────

def test_cadastro_mostra_termo_e_caixa_obrigatoria(cliente):
    r = cliente.get('/entrar/casa-sol/cadastro')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'name="aceite_termo"' in html and 'required' in html
    assert 'Termo de Uso e Consentimento' in html
    assert 'Casa Sol' in html  # controladora nomeada no texto


def test_cadastro_sem_aceite_nao_cria_conta(cliente):
    _csrf(cliente)
    r = cliente.post('/entrar/casa-sol/cadastro', data=form_cadastro())
    assert 'aceite o termo' in r.get_data(as_text=True)
    assert consulta("SELECT * FROM usuarios WHERE email = 'maria@x.com'") == []
    assert consulta('SELECT * FROM aceites_termo') == []


def test_cadastro_com_aceite_registra_data_versao_e_usuario(cliente):
    _csrf(cliente)
    r = cliente.post('/entrar/casa-sol/cadastro', data=form_cadastro(aceite_termo='sim'),
                     headers={'User-Agent': 'NavegadorTeste/1.0'})
    assert r.status_code == 302
    uid = consulta("SELECT id FROM usuarios WHERE email = 'maria@x.com'")[0]['id']
    aceites = consulta('SELECT * FROM aceites_termo WHERE usuario_id = ?', uid)
    assert len(aceites) == 1
    a = aceites[0]
    assert a['tipo_termo'] == 'responsavel'
    assert a['versao_termo'] == gc.TERMOS['responsavel']['versao']
    assert a['origem'] == 'cadastro'
    assert a['casa_id'] == 1
    assert a['aceito_em']  # data e hora
    assert len(a['hash_texto']) == 64
    assert a['navegador'] == 'NavegadorTeste/1.0'
    # Já aceitou no cadastro: segue direto, sem cair na tela de aceite.
    r = cliente.get('/responsavel/vincular-idoso')
    assert '/termo/aceitar' not in (r.headers.get('Location') or '')


# ── Contas antigas e administrador ──────────────────────────────────────────

def test_responsavel_antigo_precisa_aceitar_antes_de_usar(cliente):
    entrar(cliente, 3)
    r = cliente.get('/meu-paciente')
    assert r.status_code == 302 and '/termo/aceitar' in r.headers['Location']
    pagina = cliente.get(r.headers['Location']).get_data(as_text=True)
    assert 'Aceitar e continuar' in pagina and 'Não aceito, sair' in pagina

    # Sem marcar a caixa: não registra
    cliente.post('/termo/aceitar', data={'csrf_token': 'tok'})
    assert consulta('SELECT * FROM aceites_termo') == []

    r = cliente.post('/termo/aceitar', data={'csrf_token': 'tok', 'aceite_termo': 'sim',
                                             'proximo': '/meu-paciente'})
    assert r.status_code == 302 and r.headers['Location'].endswith('/meu-paciente')
    a = consulta('SELECT * FROM aceites_termo WHERE usuario_id = 3')
    assert len(a) == 1 and a[0]['origem'] == 'primeiro_acesso' and a[0]['tipo_termo'] == 'responsavel'
    r = cliente.get('/meu-paciente')
    assert '/termo/aceitar' not in (r.headers.get('Location') or '')


def test_admin_aceita_termo_da_instituicao(cliente):
    entrar(cliente, 1)
    r = cliente.get('/')
    assert '/termo/aceitar' in r.headers['Location']
    pagina = cliente.get('/termo/aceitar').get_data(as_text=True)
    assert 'Instituição' in pagina and 'em nome de Casa Sol' in pagina
    cliente.post('/termo/aceitar', data={'csrf_token': 'tok', 'aceite_termo': 'sim'})
    a = consulta('SELECT * FROM aceites_termo WHERE usuario_id = 1')
    assert len(a) == 1 and a[0]['tipo_termo'] == 'instituicao'
    assert cliente.get('/').status_code == 200


def test_enfermeiro_nao_e_bloqueado(cliente):
    entrar(cliente, 2)
    assert cliente.get('/').status_code == 200


def test_nova_versao_pede_aceite_de_novo(cliente, monkeypatch):
    entrar(cliente, 3)
    cliente.post('/termo/aceitar', data={'csrf_token': 'tok', 'aceite_termo': 'sim'})
    monkeypatch.setitem(gc.TERMOS, 'responsavel', dict(gc.TERMOS['responsavel'], versao='2.0'))
    r = cliente.get('/meu-paciente')
    assert '/termo/aceitar' in r.headers['Location']
    assert 'versão 1.0' in cliente.get('/termo/aceitar').get_data(as_text=True)
    cliente.post('/termo/aceitar', data={'csrf_token': 'tok', 'aceite_termo': 'sim'})
    versoes = [(a['versao_termo'], a['origem'])
               for a in consulta('SELECT * FROM aceites_termo WHERE usuario_id = 3 ORDER BY id')]
    assert versoes == [('1.0', 'primeiro_acesso'), ('2.0', 'nova_versao')]


def test_aceite_nao_redireciona_para_outro_site(cliente):
    entrar(cliente, 3)
    r = cliente.post('/termo/aceitar', data={'csrf_token': 'tok', 'aceite_termo': 'sim',
                                             'proximo': '//site-malicioso.com'})
    assert 'site-malicioso' not in r.headers['Location']


def test_sair_continua_funcionando_sem_aceite(cliente):
    entrar(cliente, 3)
    r = cliente.get('/logout')
    assert '/termo/aceitar' not in r.headers['Location']


# ── Leitura pública e contato da casa ───────────────────────────────────────

def test_termos_publicos(cliente):
    r = cliente.get('/termo/responsavel?casa=1')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    for trecho in ('Controladora', 'dados sensíveis', 'art. 18', 'Revogar', 'ANPD', 'Casa Sol'):
        assert trecho in html, trecho
    assert cliente.get('/termo/instituicao').status_code == 200
    assert cliente.get('/termo/qualquer').status_code == 404


def test_admin_define_contato_de_privacidade_e_ele_aparece_no_termo(cliente):
    entrar(cliente, 1)
    cliente.post('/termo/aceitar', data={'csrf_token': 'tok', 'aceite_termo': 'sim'})
    r = cliente.post('/meu-perfil', data={'csrf_token': 'tok', 'nome': 'Admin', 'email': 'a@a',
                                          'nome_casa': 'Casa Sol',
                                          'contato_privacidade': 'Ana — privacidade@casasol.com'})
    assert r.status_code == 302
    perfil = cliente.get('/meu-perfil').get_data(as_text=True)
    assert 'privacidade@casasol.com' in perfil and 'versão 1.0' in perfil
    assert 'privacidade@casasol.com' in cliente.get('/termo/responsavel?casa=1').get_data(as_text=True)


# ── Migração ────────────────────────────────────────────────────────────────

def test_migracao_em_banco_antigo_nao_perde_dados(tmp_path, monkeypatch):
    _preparar(tmp_path, monkeypatch)
    os.makedirs(os.path.dirname(gc.DB_PATH))
    con = sqlite3.connect(gc.DB_PATH)
    con.executescript('''
        CREATE TABLE casas (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, slug TEXT,
                            ativo INTEGER DEFAULT 1, data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO casas (id, nome, slug) VALUES (7, 'Casa Antiga', 'casa-antiga');
    ''')
    con.commit()
    con.close()
    gc.init_db()
    gc.init_db()  # idempotente
    casa = consulta('SELECT * FROM casas WHERE id = 7')[0]
    assert casa['nome'] == 'Casa Antiga' and casa['contato_privacidade'] is None
    assert consulta('SELECT * FROM aceites_termo') == []


# ── Ícone da aba ────────────────────────────────────────────────────────────

def test_icone_da_aba(cliente):
    r = cliente.get('/favicon.ico')
    assert r.status_code == 200 and r.data[:4] == b'\x00\x00\x01\x00'
    svg = cliente.get('/static/icons/favicon.svg').get_data(as_text=True)
    assert 'stroke-width="1.5"' in svg and '#16293d' in svg
    html = cliente.get('/login').get_data(as_text=True)
    assert 'icons/favicon.svg' in html and 'icons/favicon.ico' in html
