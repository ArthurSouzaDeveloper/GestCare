"""Testes do Repositório de identidade.

Rodar:  python -m pytest tests -q
Os testes de rota usam motores falsos (rápidos e determinísticos). O teste do
motor real só roda se dlib, face-recognition-models, rapidocr e scikit-image
estiverem instalados; a imagem usada é a 'astronaut' do scikit-image (domínio
público, NASA) e o documento é desenhado na hora com um CPF fictício.
"""
import io
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gc  # noqa: E402
import verificacao_identidade as vid  # noqa: E402

CPF_OK = '529.982.247-25'  # CPF fictício de exemplo, dígitos válidos
def _png_pequeno():
    """PNG 8x8 válido, montado à mão (sem depender do Pillow)."""
    import struct
    import zlib

    def bloco(tipo, dados):
        return (struct.pack('>I', len(dados)) + tipo + dados
                + struct.pack('>I', zlib.crc32(tipo + dados) & 0xffffffff))
    linhas = b''.join(b'\x00' + b'\x80\x80\x80' * 8 for _ in range(8))
    return (b'\x89PNG\r\n\x1a\n' + bloco(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 2, 0, 0, 0))
            + bloco(b'IDAT', zlib.compress(linhas)) + bloco(b'IEND', b''))


PNG_1PX = _png_pequeno()


# ── Funções puras ───────────────────────────────────────────────────────────

def test_cpf_digitos():
    assert vid.cpf_digitos_validos(CPF_OK)
    assert not vid.cpf_digitos_validos('529.982.247-24')
    assert not vid.cpf_digitos_validos('111.111.111-11')
    assert not vid.cpf_digitos_validos('123')


def test_extrair_cpfs_do_texto_ocr():
    texto = 'NOME MARIA\nCPF 529 982 247-25\nRG 12.345.678-9\nOUTRO 52998224724'
    assert vid.extrair_cpfs(texto) == ['52998224725']


class MotorFacialFalso(vid.MotorFacial):
    nome = 'falso'

    def __init__(self, distancia=0.3, sem_rosto=None):
        self.d, self.sem_rosto = distancia, sem_rosto

    def disponivel(self):
        return True

    def descritor(self, rgb):
        return None if self.sem_rosto else 1.0

    def distancia(self, a, b):
        return self.d


class OCRFalso(vid.MotorOCR):
    nome = 'ocr-falso'

    def __init__(self, texto):
        self.texto = texto

    def disponivel(self):
        return True

    def ler_texto(self, caminho):
        return self.texto


@pytest.fixture
def png(tmp_path):
    p = tmp_path / 'x.png'
    p.write_bytes(PNG_1PX)
    return str(p)


@pytest.mark.parametrize('dist,esperado', [(0.3, 'compativel'), (0.55, 'inconclusivo'),
                                           (0.8, 'incompativel')])
def test_faixas_da_comparacao(png, dist, esperado):
    r = vid.analisar(png, png, CPF_OK, MotorFacialFalso(dist), OCRFalso('CPF ' + CPF_OK))
    assert r['resultado_facial'] == esperado
    assert r['cpf_confere'] == 1


def test_cpf_do_documento_diferente_sinaliza(png):
    r = vid.analisar(png, png, CPF_OK, MotorFacialFalso(0.2), OCRFalso('CPF 111.444.777-35'))
    assert r['cpf_confere'] == 0
    assert vid.precisa_atencao(r)


def test_sem_motores_vai_para_revisao_manual(png):
    r = vid.analisar(png, png, CPF_OK, vid.MotorFacial(), vid.MotorOCR())
    assert r['resultado_facial'] == 'indisponivel'
    assert r['cpf_confere'] is None
    assert not vid.precisa_atencao(r)  # nada divergiu; o admin decide olhando as fotos


def test_ocr_instalado_mas_quebrado_aparece_como_indisponivel(monkeypatch):
    """Ex.: pacote presente mas o opencv falha por falta de libGL no sistema.
    O aviso da tela tem de refletir isso, e a análise segue para revisão manual."""
    import sys
    monkeypatch.setitem(sys.modules, 'rapidocr_onnxruntime', None)  # import levanta ImportError
    motor = vid.MotorRapidOCR()
    assert motor.disponivel() is False
    assert vid.ler_cpf_documento('qualquer.jpg', motor)['status'] == 'indisponivel'


# ── Rotas ───────────────────────────────────────────────────────────────────

@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'DB_PATH', str(tmp_path / 'db' / 'teste.db'))
    monkeypatch.setattr(gc, 'UPLOAD_FOLDER', str(tmp_path / 'up' / 'exames'))
    monkeypatch.setattr(gc, 'IDENTIDADE_FOLDER', str(tmp_path / 'up' / 'identidade'))
    monkeypatch.setattr(gc, '_UPLOAD_FOLDER_ANTIGO', str(tmp_path / 'nao_existe'))
    monkeypatch.setattr(vid, '_motor_facial', MotorFacialFalso(0.3))
    monkeypatch.setattr(vid, '_motor_ocr', OCRFalso('CPF ' + CPF_OK))
    gc.init_db()
    gc.init_db()  # idempotente
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO casas (id, nome, slug) VALUES (1, 'Casa A', 'casa-a'), (2, 'Casa B', 'casa-b')")
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, cpf, casa_id, contato_emergencia_nome, "
                "contato_emergencia_parentesco) VALUES (1, 'Idoso A', '1940-01-01', ?, 1, 'Filha A', 'Filha')",
                (CPF_OK,))
    con.execute("INSERT INTO pacientes (id, nome, data_nascimento, casa_id) VALUES (2, 'Idoso B', '1940-01-01', 2)")
    usuarios = [(1, 'Admin A', 'a@a', 'admin', 1, None, None), (2, 'Enf A', 'e@a', 'enfermeiro', 1, None, None),
                (3, 'Resp A', 'r@a', 'responsavel', 1, 1, CPF_OK), (4, 'Admin B', 'a@b', 'admin', 2, None, None),
                (5, 'Resp A2', 'r2@a', 'responsavel', 1, 1, '111.444.777-35')]
    for u in usuarios:
        con.execute('INSERT INTO usuarios (id, nome, email, senha, perfil, casa_id, paciente_id, cpf) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (u[0], u[1], u[2], 'x', u[3], u[4], u[5], u[6]))
    con.commit()
    con.close()
    gc.app.config['TESTING'] = True
    return gc.app.test_client()


PERFIS = {1: ('admin', 1, None), 2: ('enfermeiro', 1, None), 3: ('responsavel', 1, 1),
          4: ('admin', 2, None), 5: ('responsavel', 1, 1)}


def entrar(c, uid):
    perfil, casa, pac = PERFIS[uid]
    with c.session_transaction() as s:
        s.clear()
        s.update(usuario_id=uid, usuario_nome='U', usuario_perfil=perfil, casa_id=casa,
                 paciente_id=pac, casa_slug='x', csrf_token='tok')


def fotos(**extra):
    d = {'foto_documento': (io.BytesIO(PNG_1PX), 'doc.png'),
         'foto_rosto': (io.BytesIO(PNG_1PX), 'rosto.png'), 'csrf_token': 'tok'}
    d.update(extra)
    return d


def enviar_idoso(c, **extra):
    campos = dict(pessoa='paciente:1', consentido_por_nome='Filha A', relacao='Filha',
                  aceite_termo='sim', rosto_presencial='sim')
    campos.update(extra)
    return c.post('/repositorio/nova', data=fotos(**campos), content_type='multipart/form-data')


def linhas(sql, *p):
    con = sqlite3.connect(gc.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, p).fetchall()
    finally:
        con.close()


def test_admin_envia_e_rosto_aparece_na_ficha(cliente):
    entrar(cliente, 1)
    r = enviar_idoso(cliente)
    assert r.status_code == 302 and '/repositorio/1' in r.headers['Location']
    v = linhas('SELECT * FROM verificacoes_identidade')[0]
    assert v['status'] == 'pendente' and v['resultado_facial'] == 'compativel' and v['cpf_confere'] == 1
    assert v['rosto_presencial'] == 1
    c = linhas('SELECT * FROM consentimentos_biometria')[0]
    assert c['consentido_por_nome'] == 'Filha A' and c['coletado_por'] == 1
    html = cliente.get('/paciente/1').get_data(as_text=True)
    assert '/identidade/arquivo/1/rosto' in html and 'Aguardando validação' in html
    assert os.path.isfile(os.path.join(gc.IDENTIDADE_FOLDER, v['foto_rosto']))
    assert not v['foto_rosto'].startswith('static')


def test_envio_sem_consentimento_e_recusado(cliente):
    entrar(cliente, 1)
    enviar_idoso(cliente, aceite_termo='')
    assert linhas('SELECT COUNT(*) n FROM verificacoes_identidade')[0]['n'] == 0
    assert os.listdir(gc.IDENTIDADE_FOLDER) == []


def test_arquivo_que_nao_e_imagem_e_recusado(cliente):
    entrar(cliente, 1)
    dados = fotos(pessoa='paciente:1', consentido_por_nome='F', relacao='Filha', aceite_termo='sim')
    dados['foto_documento'] = (io.BytesIO(b'%PDF-1.4 falso'), 'doc.png')
    cliente.post('/repositorio/nova', data=dados, content_type='multipart/form-data')
    assert linhas('SELECT COUNT(*) n FROM verificacoes_identidade')[0]['n'] == 0
    assert os.listdir(gc.IDENTIDADE_FOLDER) == []


def test_admin_nao_envia_para_pessoa_de_outra_casa(cliente):
    entrar(cliente, 4)  # admin da casa B
    enviar_idoso(cliente)  # paciente 1 é da casa A
    assert linhas('SELECT COUNT(*) n FROM verificacoes_identidade')[0]['n'] == 0


def test_controle_de_acesso_aos_arquivos(cliente):
    entrar(cliente, 1)
    enviar_idoso(cliente)
    # anônimo
    with cliente.session_transaction() as s:
        s.clear()
    assert cliente.get('/identidade/arquivo/1/rosto').status_code == 302
    # admin da casa: os dois
    entrar(cliente, 1)
    assert cliente.get('/identidade/arquivo/1/documento').status_code == 200
    r = cliente.get('/identidade/arquivo/1/rosto')
    assert r.status_code == 200 and 'no-store' in r.headers['Cache-Control']
    # enfermeiro: rosto do idoso sim, documento não
    entrar(cliente, 2)
    assert cliente.get('/identidade/arquivo/1/rosto').status_code == 200
    assert cliente.get('/identidade/arquivo/1/documento').status_code == 403
    # responsável: não vê fotos do idoso
    entrar(cliente, 3)
    assert cliente.get('/identidade/arquivo/1/rosto').status_code == 403
    # admin de outra casa
    entrar(cliente, 4)
    assert cliente.get('/identidade/arquivo/1/rosto').status_code == 403
    # pasta pública não tem nada
    nome = linhas('SELECT foto_rosto FROM verificacoes_identidade')[0]['foto_rosto']
    assert cliente.get(f'/static/uploads/identidade/{nome}').status_code == 404
    acoes = [a['acao'] for a in linhas('SELECT acao FROM acessos_identidade WHERE verificacao_id = 1')]
    assert 'enviar' in acoes and 'ver_documento' in acoes and acoes.count('negado') >= 2


def test_repositorio_so_para_admin(cliente):
    entrar(cliente, 2)
    assert cliente.get('/repositorio').status_code == 302
    entrar(cliente, 3)
    assert cliente.get('/repositorio').status_code == 302
    entrar(cliente, 1)
    html = cliente.get('/repositorio').get_data(as_text=True)
    assert 'Repositório' in html and 'Idoso A' in html


def test_decisao_do_admin(cliente):
    entrar(cliente, 1)
    enviar_idoso(cliente)
    # recusar exige motivo
    cliente.post('/repositorio/1/decidir', data={'decisao': 'recusado', 'csrf_token': 'tok'})
    assert linhas('SELECT status FROM verificacoes_identidade')[0]['status'] == 'pendente'
    cliente.post('/repositorio/1/decidir', data={'decisao': 'aprovado', 'csrf_token': 'tok'})
    v = linhas('SELECT * FROM verificacoes_identidade')[0]
    assert v['status'] == 'aprovado' and v['revisado_por'] == 1
    html = cliente.get('/paciente/1').get_data(as_text=True)
    assert 'Aguardando validação' not in html
    # outra casa não decide
    entrar(cliente, 4)
    cliente.post('/repositorio/1/decidir', data={'decisao': 'recusado', 'motivo': 'x', 'csrf_token': 'tok'})
    assert linhas('SELECT status FROM verificacoes_identidade')[0]['status'] == 'aprovado'


def test_aprovar_com_divergencia_exige_motivo(cliente, monkeypatch):
    monkeypatch.setattr(vid, '_motor_facial', MotorFacialFalso(0.9))
    entrar(cliente, 1)
    enviar_idoso(cliente)
    cliente.post('/repositorio/1/decidir', data={'decisao': 'aprovado', 'csrf_token': 'tok'})
    assert linhas('SELECT status FROM verificacoes_identidade')[0]['status'] == 'pendente'
    cliente.post('/repositorio/1/decidir', data={'decisao': 'aprovado', 'csrf_token': 'tok',
                                                 'motivo': 'Conferi o RG original; foto do RG é de 1980.'})
    assert linhas('SELECT status FROM verificacoes_identidade')[0]['status'] == 'aprovado'


def test_recusado_nao_aparece_na_ficha(cliente):
    entrar(cliente, 1)
    enviar_idoso(cliente)
    cliente.post('/repositorio/1/decidir', data={'decisao': 'recusado', 'motivo': 'foto de outra pessoa',
                                                 'csrf_token': 'tok'})
    entrar(cliente, 2)
    assert '/identidade/arquivo/1/rosto' not in cliente.get('/paciente/1').get_data(as_text=True)
    assert cliente.get('/identidade/arquivo/1/rosto').status_code == 403


def test_responsavel_envia_as_proprias_fotos(cliente, monkeypatch):
    monkeypatch.setattr(vid, '_motor_ocr', OCRFalso('CPF 111.444.777-35'))
    entrar(cliente, 3)
    r = cliente.post('/responsavel/verificacao', data=fotos(aceite_termo='sim'),
                     content_type='multipart/form-data')
    assert r.status_code == 302
    v = linhas('SELECT * FROM verificacoes_identidade')[0]
    assert v['tipo_pessoa'] == 'responsavel' and v['pessoa_id'] == 3
    assert v['rosto_presencial'] == 0 and v['cpf_confere'] == 0  # CPF do doc difere do cadastro
    assert cliente.get('/identidade/arquivo/1/documento').status_code == 200
    # segundo envio enquanto pendente é bloqueado
    cliente.post('/responsavel/verificacao', data=fotos(aceite_termo='sim'),
                 content_type='multipart/form-data')
    assert linhas('SELECT COUNT(*) n FROM verificacoes_identidade')[0]['n'] == 1
    # outro responsável não vê
    entrar(cliente, 5)
    assert cliente.get('/identidade/arquivo/1/documento').status_code == 403
    # enfermeiro não vê fotos de responsável
    entrar(cliente, 2)
    assert cliente.get('/identidade/arquivo/1/rosto').status_code == 403
    # aparece na ficha do idoso para o admin
    entrar(cliente, 1)
    html = cliente.get('/paciente/1').get_data(as_text=True)
    assert 'Resp A' in html and '/identidade/arquivo/1/documento' in html


def test_excluir_apaga_arquivos(cliente):
    entrar(cliente, 1)
    enviar_idoso(cliente)
    assert len(os.listdir(gc.IDENTIDADE_FOLDER)) == 2
    cliente.post('/repositorio/1/excluir', data={'csrf_token': 'tok'})
    assert os.listdir(gc.IDENTIDADE_FOLDER) == []
    assert linhas('SELECT COUNT(*) n FROM verificacoes_identidade')[0]['n'] == 0
    assert linhas("SELECT COUNT(*) n FROM acessos_identidade WHERE acao = 'excluir'")[0]['n'] == 1


def test_excluir_responsavel_preserva_fotos_e_apagar_definitivo_remove(cliente):
    entrar(cliente, 3)
    cliente.post('/responsavel/verificacao', data=fotos(aceite_termo='sim'),
                 content_type='multipart/form-data')
    entrar(cliente, 1)
    # exclusão comum é reversível: as fotos ficam, mas o responsável sai das listas
    cliente.post('/usuarios/3/excluir', data={'csrf_token': 'tok'})
    assert len(os.listdir(gc.IDENTIDADE_FOLDER)) == 2
    assert linhas('SELECT revogado_em FROM consentimentos_biometria')[0]['revogado_em'] is None
    html = cliente.get('/repositorio').get_data(as_text=True)
    assert 'value="responsavel:3"' not in html  # não dá para enviar fotos para excluído
    # restaurar não perde nada
    cliente.post('/usuarios/3/restaurar', data={'csrf_token': 'tok'})
    assert len(os.listdir(gc.IDENTIDADE_FOLDER)) == 2
    # apagar de vez (LGPD): fotos somem e o consentimento é revogado
    cliente.post('/usuarios/3/excluir', data={'csrf_token': 'tok'})
    cliente.post('/usuarios/3/apagar', data={'csrf_token': 'tok'})
    assert os.listdir(gc.IDENTIDADE_FOLDER) == []
    assert linhas('SELECT COUNT(*) n FROM verificacoes_identidade')[0]['n'] == 0
    assert linhas('SELECT revogado_em FROM consentimentos_biometria')[0]['revogado_em']


def test_responsavel_bloqueado_ate_aprovacao(cliente):
    # exame com arquivo do idoso 1
    os.makedirs(gc.UPLOAD_FOLDER, exist_ok=True)
    with open(os.path.join(gc.UPLOAD_FOLDER, 'exame.pdf'), 'wb') as f:
        f.write(b'%PDF-1.4 teste')
    con = sqlite3.connect(gc.DB_PATH)
    con.execute("INSERT INTO exames (paciente_id, tipo_exame, data_exame, arquivo_nome) "
                "VALUES (1, 'Hemograma', '2026-01-01', 'exame.pdf')")
    con.commit()
    con.close()

    # sem nenhum envio: ficha e PDF bloqueados, vai para a tela de verificação
    entrar(cliente, 3)
    r = cliente.get('/meu-paciente')
    assert r.status_code == 302 and '/responsavel/verificacao' in r.headers['Location']
    assert cliente.get('/exame/arquivo/exame.pdf').status_code == 403
    assert 'bloqueado' in cliente.get('/responsavel/verificacao').get_data(as_text=True)

    # enviou, mas está pendente: continua bloqueado
    cliente.post('/responsavel/verificacao', data=fotos(aceite_termo='sim'),
                 content_type='multipart/form-data')
    assert cliente.get('/meu-paciente').status_code == 302
    assert cliente.get('/exame/arquivo/exame.pdf').status_code == 403
    assert 'sendo analisado' in cliente.get('/responsavel/verificacao').get_data(as_text=True)

    # admin aprova: libera
    entrar(cliente, 1)
    cliente.post('/repositorio/1/decidir', data={'decisao': 'aprovado', 'csrf_token': 'tok',
                                                 'motivo': 'conferi o documento original'})
    entrar(cliente, 3)
    assert cliente.get('/meu-paciente').status_code == 200
    assert cliente.get('/exame/arquivo/exame.pdf').status_code == 200
    assert 'liberado' in cliente.get('/responsavel/verificacao').get_data(as_text=True)

    # novo envio pendente não tira o acesso de quem já foi aprovado
    cliente.post('/responsavel/verificacao', data=fotos(aceite_termo='sim'),
                 content_type='multipart/form-data')
    assert cliente.get('/meu-paciente').status_code == 200

    # admin recusa o envio mais recente: bloqueia de novo
    entrar(cliente, 1)
    cliente.post('/repositorio/2/decidir', data={'decisao': 'recusado', 'csrf_token': 'tok',
                                                 'motivo': 'foto de outra pessoa'})
    entrar(cliente, 3)
    assert cliente.get('/meu-paciente').status_code == 302
    assert cliente.get('/exame/arquivo/exame.pdf').status_code == 403
    html = cliente.get('/responsavel/verificacao').get_data(as_text=True)
    assert 'foto de outra pessoa' in html  # motivo da recusa visível


def test_bloqueio_nao_afeta_equipe(cliente):
    entrar(cliente, 2)
    assert cliente.get('/paciente/1').status_code == 200


# ── Motor real (opcional) ───────────────────────────────────────────────────

def _motores_reais():
    try:
        import skimage  # noqa: F401
    except ImportError:
        return False
    return vid.MotorDlib().disponivel() and vid.MotorRapidOCR().disponivel()


@pytest.mark.skipif(not _motores_reais(), reason='dlib/rapidocr/scikit-image não instalados')
def test_motor_real_documento_ficticio(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from skimage import data
    astro = Image.fromarray(data.astronaut())
    outra = Image.fromarray(data.camera()).convert('RGB').resize((1024, 1024))
    doc = Image.new('RGB', (1000, 640), (225, 235, 220))
    doc.paste(astro.crop((140, 20, 340, 260)).resize((250, 300)), (40, 160))
    try:
        fonte = ImageFont.truetype('DejaVuSans.ttf', 34)
    except OSError:
        try:
            fonte = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', 34)
        except OSError:
            fonte = ImageFont.load_default(size=34)
    d = ImageDraw.Draw(doc)
    d.text((330, 180), 'NOME: MARIA EXEMPLO', fill=(0, 0, 0), font=fonte)
    d.text((330, 260), 'CPF: ' + CPF_OK, fill=(0, 0, 0), font=fonte)
    p_doc, p_eu, p_outra = (str(tmp_path / n) for n in ('doc.jpg', 'eu.jpg', 'outra.jpg'))
    doc.save(p_doc)
    astro.save(p_eu)
    outra.save(p_outra)

    mesma = vid.analisar(p_doc, p_eu, CPF_OK)
    assert mesma['resultado_facial'] == 'compativel'
    assert mesma['cpf_confere'] == 1
    diferente = vid.analisar(p_doc, p_outra, '111.444.777-35')
    assert diferente['resultado_facial'] == 'incompativel'
    assert diferente['cpf_confere'] == 0
