import bcrypt
import uuid
import json
import base64
import atexit
import re
import logging
import unicodedata
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify
from werkzeug.utils import secure_filename
import sqlite3
import os
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from py_vapid import Vapid01 as Vapid
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from apscheduler.schedulers.background import BackgroundScheduler
import verificacao_identidade as vid

app = Flask(__name__)

_secret = os.environ.get('GESTCARE_SECRET_KEY', '')
if not _secret:
    import warnings
    warnings.warn('GESTCARE_SECRET_KEY não definida — usando chave insegura. Defina a variável de ambiente em produção.')
    _secret = 'casa-idosos-sistema-2026-troque-em-producao'
app.secret_key = _secret

# Cookies de sessão seguros
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('HTTPS', ''))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB máximo por upload

from logger import setup_logger, registrar_hooks
log = setup_logger(app)
registrar_hooks(app)


import hmac
import hashlib

def _gerar_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = uuid.uuid4().hex
    return session['csrf_token']

def _validar_csrf():
    token_sessao = session.get('csrf_token', '')
    token_form   = request.form.get('csrf_token', '')
    if not token_sessao or not hmac.compare_digest(token_sessao, token_form):
        log.warning('csrf_falhou', extra={'gc_action': 'csrf_check',
                    'gc_path': request.path, 'gc_user_id': session.get('usuario_id')})
        return False
    return True

app.jinja_env.globals['csrf_token'] = _gerar_csrf_token

ROTAS_SEM_CSRF = {'/push/inscrever', '/push/desinscrever'}

@app.before_request
def checar_csrf():
    if request.method == 'POST' and request.path not in ROTAS_SEM_CSRF:
        if not _validar_csrf():
            flash('Requisição inválida. Recarregue a página e tente novamente.', 'erro')
            return redirect(url_for('login'))


# Rotas que não dependem de sessão: não faz sentido consultar o banco nelas.
_PREFIXOS_SEM_CHECAGEM_SESSAO = ('/static/', '/login', '/entrar/', '/responsavel/cadastro', '/logout')

@app.before_request
def revalidar_sessao():
    """Derruba a sessão quando a conta foi desativada ou excluída, quando o perfil
    mudou ou quando a casa foi desativada. Antes, desativar um usuário só valia no
    próximo login: quem já estava logado continuava com acesso."""
    usuario_id = session.get('usuario_id')
    if not usuario_id or request.path.startswith(_PREFIXOS_SEM_CHECAGEM_SESSAO):
        return None
    with get_db() as conn:
        usuario = conn.execute(
            'SELECT u.ativo, u.perfil, u.casa_id, u.excluido_em, c.ativo AS casa_ativa '
            'FROM usuarios u LEFT JOIN casas c ON c.id = u.casa_id WHERE u.id = ?',
            (usuario_id,)
        ).fetchone()
    motivo = None
    if not usuario or usuario['excluido_em']:
        motivo = 'conta_excluida'
    elif not usuario['ativo']:
        motivo = 'conta_desativada'
    elif usuario['perfil'] != session.get('usuario_perfil'):
        motivo = 'perfil_alterado'
    elif usuario['casa_id'] != session.get('casa_id'):
        motivo = 'casa_alterada'
    elif usuario['casa_id'] and not usuario['casa_ativa']:
        motivo = 'casa_desativada'
    if motivo is None:
        return None
    log.warning('sessao_encerrada', extra={'gc_action': 'revalidar_sessao',
                'gc_user_id': usuario_id, 'gc_motivo': motivo})
    casa_slug = session.get('casa_slug')
    session.clear()
    flash('Sua sessão foi encerrada. Entre novamente.', 'erro')
    if casa_slug:
        return redirect(url_for('login_casa', slug=casa_slug))
    return redirect(url_for('login'))


@app.errorhandler(413)
def arquivo_muito_grande(e):
    flash('Arquivo muito grande. O limite é 16 MB.', 'erro')
    return redirect(request.referrer or url_for('index')), 302

@app.errorhandler(404)
def pagina_nao_encontrada(e):
    return render_template('erro.html', codigo=404, mensagem='Página não encontrada.'), 404

@app.errorhandler(500)
def erro_interno(e):
    log.error('erro_500', extra={'gc_action': 'error_500', 'gc_path': request.path})
    return render_template('erro.html', codigo=500, mensagem='Erro interno. Tente novamente em instantes.'), 500

@app.after_request
def adicionar_headers_seguranca(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    if not app.debug:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ---------------------------------------------------------------------------
# Rate limiting simples em memória (por IP, sem dependência externa)
# Máximo: 10 tentativas de login por IP a cada 60 segundos
# ---------------------------------------------------------------------------
_login_tentativas: dict = {}  # {ip: [timestamps]}

def _checar_rate_limit(ip: str, limite: int = 10, janela: int = 60) -> bool:
    """Retorna True se o IP ainda está dentro do limite permitido."""
    agora = datetime.now().timestamp()
    historico = [t for t in _login_tentativas.get(ip, []) if agora - t < janela]
    if len(historico) >= limite:
        _login_tentativas[ip] = historico
        return False
    historico.append(agora)
    _login_tentativas[ip] = historico
    # Limpeza periódica: remove IPs com histórico vazio para evitar crescimento infinito
    if len(_login_tentativas) > 5000:
        _login_tentativas.clear()
    return True


@app.context_processor
def injetar_versao_assets():
    """Versão baseada no mtime do CSS, usada como ?v= para invalidar cache do navegador
    sempre que o arquivo mudar (evita que usuários fiquem com estilo antigo em cache)."""
    try:
        versao_assets = str(int(max(
            os.path.getmtime(os.path.join(app.static_folder, 'css', nome))
            for nome in ('style.css', 'direcoes.css', 'portal.css'))))
    except OSError:
        versao_assets = '1'
    return dict(versao_assets=versao_assets)

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'casa_idosos.db')
# Exames ficam FORA de static/: o Flask serve static/ sem login, então um arquivo
# ali abriria para qualquer pessoa com o link. O único caminho de acesso é a rota
# /exame/arquivo/<nome>, que confere sessão, casa e paciente vinculado.
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads', 'exames')
_UPLOAD_FOLDER_ANTIGO = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'exames')


def _migrar_exames_da_pasta_publica():
    """Move exames salvos por versões anteriores em static/uploads/exames/ para a
    pasta protegida. Idempotente: não sobrescreve arquivo que já exista no destino."""
    if not os.path.isdir(_UPLOAD_FOLDER_ANTIGO):
        return
    movidos = 0
    for nome in os.listdir(_UPLOAD_FOLDER_ANTIGO):
        origem = os.path.join(_UPLOAD_FOLDER_ANTIGO, nome)
        destino = os.path.join(UPLOAD_FOLDER, nome)
        if os.path.isfile(origem) and not os.path.exists(destino):
            os.replace(origem, destino)
            movidos += 1
    if movidos:
        logging.getLogger('gestcare').info(
            'exames_migrados_para_pasta_protegida', extra={'gc_action': 'migrar_exames',
                                                           'gc_total': movidos})
EXTENSOES_PERMITIDAS = {'pdf', 'png', 'jpg', 'jpeg', 'webp'}

# Fotos de documento e de rosto: também fora de static/, servidas só pela rota
# /identidade/arquivo/<id>/<tipo>, que confere casa e perfil e grava log de acesso.
IDENTIDADE_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads', 'identidade')
EXTENSOES_FOTO = {'png', 'jpg', 'jpeg', 'webp'}
VERSAO_TERMO_BIOMETRIA = 'v1-2026-11'

# ============================================================================
# NOTIFICAÇÕES PUSH (lembrete de antibiótico no celular das enfermeiras)
# ============================================================================
VAPID_KEY_PATH = os.path.join(os.path.dirname(__file__), 'vapid_private_key.pem')
VAPID_CLAIMS_SUB = 'mailto:contato@gestcare.local'


def _b64url(dados_bytes):
    return base64.urlsafe_b64encode(dados_bytes).rstrip(b'=').decode('ascii')


def _obter_chave_publica_vapid():
    vapid = Vapid.from_file(VAPID_KEY_PATH)
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _b64url(raw)


try:
    VAPID_PUBLIC_KEY = _obter_chave_publica_vapid()
except Exception:
    VAPID_PUBLIC_KEY = None
    logging.warning('VAPID: chave privada não encontrada — notificações push desativadas')


def enviar_push_para_equipe(casa_id, titulo, corpo, url='/'):
    """Envia uma notificação push para a equipe (enfermeiros/admin) de uma casa específica."""
    payload = json.dumps({'title': titulo, 'body': corpo, 'url': url})
    with get_db() as conn:
        inscricoes = conn.execute('''
            SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth
            FROM push_subscriptions ps
            JOIN usuarios u ON ps.usuario_id = u.id
            WHERE u.ativo = 1 AND u.perfil IN ('enfermeiro', 'admin') AND u.casa_id = ?
        ''', (casa_id,)).fetchall()
        for inscricao in inscricoes:
            try:
                webpush(
                    subscription_info={
                        'endpoint': inscricao['endpoint'],
                        'keys': {'p256dh': inscricao['p256dh'], 'auth': inscricao['auth']}
                    },
                    data=payload,
                    vapid_private_key=VAPID_KEY_PATH,
                    vapid_claims={'sub': VAPID_CLAIMS_SUB}
                )
            except WebPushException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (404, 410):
                    conn.execute('DELETE FROM push_subscriptions WHERE id = ?', (inscricao['id'],))
                    logging.getLogger('gestcare.push').info(
                        'push_subscription_removida', extra={'gc_action': 'push_cleanup',
                        'gc_subscription_id': inscricao['id'], 'gc_status': status}
                    )
                else:
                    logging.getLogger('gestcare.push').error(
                        'push_falhou', extra={'gc_action': 'push_send',
                        'gc_subscription_id': inscricao['id'], 'gc_status': status}
                    )


def checar_lembretes_antibiotico():
    """Job periódico: avisa a equipe quando um tratamento com antibiótico
    está terminando (em até 2 dias) ou já terminou, uma vez por dia."""
    hoje = date.today().isoformat()
    with app.app_context(), get_db() as conn:
        medicamentos = conn.execute('''
            SELECT m.id, m.nome, m.data_fim_tratamento, m.ultimo_alerta_enviado,
                   p.id as paciente_id, p.nome as paciente_nome, p.casa_id
            FROM medicamentos m
            JOIN pacientes p ON m.paciente_id = p.id
            WHERE m.ativo = 1 AND p.ativo = 1 AND m.eh_antibiotico = 1
              AND m.data_fim_tratamento IS NOT NULL
              AND date(m.data_fim_tratamento) <= date('now', 'localtime', '+2 days')
        ''').fetchall()
        for m in medicamentos:
            if m['ultimo_alerta_enviado'] == hoje:
                continue
            try:
                fim = datetime.strptime(m['data_fim_tratamento'], '%Y-%m-%d').date()
            except ValueError:
                continue
            dias = (fim - date.today()).days
            if dias < 0:
                corpo = f"Tratamento com {m['nome']} encerrado há {-dias} dia(s)."
            elif dias == 0:
                corpo = f"Tratamento com {m['nome']} termina hoje."
            else:
                corpo = f"Tratamento com {m['nome']} termina em {dias} dia(s)."
            enviar_push_para_equipe(
                m['casa_id'],
                f"💊 Antibiótico — {m['paciente_nome']}",
                corpo,
                f"/paciente/{m['paciente_id']}"
            )
            conn.execute(
                'UPDATE medicamentos SET ultimo_alerta_enviado = ? WHERE id = ?',
                (hoje, m['id'])
            )


_agendador = None


def iniciar_agendador():
    """Inicia o agendador em thread de fundo (a cada 30 min).
    No PythonAnywhere isso é desnecessário/instável — lá usamos o recurso
    'Scheduled Tasks' deles chamando tarefa_lembretes.py (veja esse arquivo)."""
    global _agendador
    if _agendador is not None:
        return
    if os.environ.get('PYTHONANYWHERE_DOMAIN'):
        return
    _agendador = BackgroundScheduler(daemon=True)
    _agendador.add_job(checar_lembretes_antibiotico, 'interval', minutes=30,
                        next_run_time=datetime.now())
    _agendador.start()
    atexit.register(lambda: _agendador.shutdown(wait=False))

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logging.getLogger('gestcare.db').error(
            'db_rollback: %s', type(exc).__name__,
            exc_info=exc,
            extra={'gc_action': 'db_rollback'}
        )
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(IDENTIDADE_FOLDER, exist_ok=True)
    _migrar_exames_da_pasta_publica()
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS casas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                slug TEXT,
                ativo INTEGER DEFAULT 1,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Migration: add slug column if it doesn't exist yet (without UNIQUE — SQLite limitation)
        colunas = [r[1] for r in cursor.execute("PRAGMA table_info(casas)").fetchall()]
        if 'slug' not in colunas:
            cursor.execute('ALTER TABLE casas ADD COLUMN slug TEXT')
        # Populate slugs for existing casas that have none
        sem_slug = conn.execute('SELECT id, nome FROM casas WHERE slug IS NULL OR slug = ""').fetchall()
        for row in sem_slug:
            base = gerar_slug(row['nome'])
            slug = slug_unico(conn, base)
            conn.execute('UPDATE casas SET slug = ? WHERE id = ?', (slug, row['id']))

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pacientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                data_nascimento DATE NOT NULL,
                sexo TEXT, cpf TEXT, rg TEXT, telefone TEXT, quarto TEXT,
                contato_emergencia_nome TEXT, contato_emergencia_telefone TEXT,
                contato_emergencia_parentesco TEXT, observacoes TEXT,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ativo INTEGER DEFAULT 1
            )
        ''')

        try:
            cursor.execute('ALTER TABLE pacientes ADD COLUMN casa_id INTEGER REFERENCES casas(id)')
        except Exception:
            pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS condicoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL, nome TEXT NOT NULL,
                descricao TEXT, gravidade TEXT, data_diagnostico DATE,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS medicamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL, nome TEXT NOT NULL,
                dosagem TEXT, via_administracao TEXT, instrucoes TEXT,
                ativo INTEGER DEFAULT 1,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        try:
            cursor.execute('ALTER TABLE medicamentos ADD COLUMN eh_antibiotico INTEGER DEFAULT 0')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE medicamentos ADD COLUMN data_fim_tratamento DATE')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE medicamentos ADD COLUMN ultimo_alerta_enviado DATE')
        except Exception:
            pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS horarios_medicamento (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicamento_id INTEGER NOT NULL, horario TIME NOT NULL,
                FOREIGN KEY (medicamento_id) REFERENCES medicamentos(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS doses_administradas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicamento_id INTEGER NOT NULL,
                horario_previsto TIMESTAMP NOT NULL,
                horario_administrado TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                administrado_por TEXT, observacoes TEXT,
                FOREIGN KEY (medicamento_id) REFERENCES medicamentos(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                senha TEXT NOT NULL, perfil TEXT NOT NULL DEFAULT 'enfermeiro',
                paciente_id INTEGER,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id)
            )
        ''')

        # Adiciona coluna paciente_id se a tabela já existia sem ela
        try:
            cursor.execute('ALTER TABLE usuarios ADD COLUMN paciente_id INTEGER')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE usuarios ADD COLUMN cpf TEXT')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE usuarios ADD COLUMN casa_id INTEGER REFERENCES casas(id)')
        except Exception:
            pass
        # Exclusão reversível: em vez de apagar a linha, marca quando e por quem
        # foi excluído. O usuário fica fora da lista, sem acesso, e pode ser restaurado.
        for coluna in ('excluido_em TEXT', 'excluido_por INTEGER', 'atualizado_em TEXT'):
            try:
                cursor.execute(f'ALTER TABLE usuarios ADD COLUMN {coluna}')
            except Exception:
                pass

        # Migração: se já existem pacientes/usuários sem casa (banco anterior ao
        # multi-tenant), cria uma "Casa Padrão" e vincula todos os registros a ela.
        sem_casa = cursor.execute(
            "SELECT COUNT(*) as total FROM pacientes WHERE casa_id IS NULL"
        ).fetchone()['total']
        sem_casa += cursor.execute(
            "SELECT COUNT(*) as total FROM usuarios WHERE casa_id IS NULL AND perfil != 'superadmin'"
        ).fetchone()['total']
        if sem_casa > 0:
            casa_padrao = cursor.execute(
                "SELECT id FROM casas ORDER BY id LIMIT 1"
            ).fetchone()
            if casa_padrao:
                casa_padrao_id = casa_padrao['id']
            else:
                casa_padrao_id = cursor.execute(
                    "INSERT INTO casas (nome) VALUES ('Casa Padrão')"
                ).lastrowid
            cursor.execute(
                'UPDATE pacientes SET casa_id = ? WHERE casa_id IS NULL', (casa_padrao_id,)
            )
            cursor.execute(
                "UPDATE usuarios SET casa_id = ? WHERE casa_id IS NULL AND perfil != 'superadmin'",
                (casa_padrao_id,)
            )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ficha_psicologica (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL UNIQUE,
                antecedentes TEXT, diagnosticos_mentais TEXT,
                medicacao_psiquiatrica TEXT, comportamento TEXT, observacoes TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS profissionais_consultados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL, nome TEXT NOT NULL,
                especialidade TEXT, data_consulta DATE, observacoes TEXT,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comorbidades_dieta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL UNIQUE,
                peso REAL, altura REAL, comorbidades TEXT,
                restricoes_alimentares TEXT, dieta_especifica TEXT,
                consistencia_alimento TEXT, alergias_alimentares TEXT,
                observacoes_nutricao TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        try:
            cursor.execute('ALTER TABLE comorbidades_dieta ADD COLUMN peso REAL')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE comorbidades_dieta ADD COLUMN altura REAL')
        except Exception:
            pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS visitas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL, visitante_nome TEXT NOT NULL,
                visitante_parentesco TEXT, data_visita DATE NOT NULL,
                hora_entrada TEXT, hora_saida TEXT, observacoes TEXT,
                registrado_por TEXT,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS evolucoes_medicas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL,
                data_evolucao DATE NOT NULL,
                profissional TEXT, especialidade TEXT,
                descricao TEXT NOT NULL,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS exames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id INTEGER NOT NULL, tipo_exame TEXT NOT NULL,
                data_exame DATE NOT NULL, laboratorio TEXT,
                medico_solicitante TEXT, resultado TEXT,
                valores_referencia TEXT, arquivo_nome TEXT, observacoes TEXT,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paciente_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        ''')

        # Recado da casa para a família: único texto do exame que o portal do
        # responsável mostra. 'observacoes' continua sendo nota interna da equipe.
        try:
            cursor.execute('ALTER TABLE exames ADD COLUMN observacao_familia TEXT')
        except Exception:
            pass

        # ── Repositório de identidade (documento + rosto) ──────────────────
        # tipo_pessoa + pessoa_id apontam para pacientes.id ou usuarios.id
        # (responsável). Não há FK porque a coluna aponta para duas tabelas.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS consentimentos_biometria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                casa_id INTEGER NOT NULL REFERENCES casas(id),
                tipo_pessoa TEXT NOT NULL CHECK (tipo_pessoa IN ('paciente', 'responsavel')),
                pessoa_id INTEGER NOT NULL,
                consentido_por_nome TEXT NOT NULL,
                relacao TEXT NOT NULL,
                versao_termo TEXT NOT NULL,
                coletado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                coletado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                revogado_em TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS verificacoes_identidade (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                casa_id INTEGER NOT NULL REFERENCES casas(id),
                tipo_pessoa TEXT NOT NULL CHECK (tipo_pessoa IN ('paciente', 'responsavel')),
                pessoa_id INTEGER NOT NULL,
                consentimento_id INTEGER REFERENCES consentimentos_biometria(id),
                foto_documento TEXT NOT NULL,
                foto_rosto TEXT NOT NULL,
                rosto_presencial INTEGER NOT NULL DEFAULT 0,
                cpf_cadastro TEXT,
                cpf_cadastro_valido INTEGER,
                cpf_documento TEXT,
                cpf_ocr_status TEXT,
                cpf_confere INTEGER,
                resultado_facial TEXT,
                distancia_facial REAL,
                motor TEXT,
                status TEXT NOT NULL DEFAULT 'pendente'
                    CHECK (status IN ('pendente', 'aprovado', 'recusado')),
                motivo TEXT,
                enviado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                enviado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                revisado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                revisado_em TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_verif_pessoa '
                       'ON verificacoes_identidade (casa_id, tipo_pessoa, pessoa_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_verif_status '
                       'ON verificacoes_identidade (casa_id, status)')
        # Log de acesso: sem FK para a verificação, para o registro sobreviver à exclusão.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS acessos_identidade (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                casa_id INTEGER,
                usuario_id INTEGER,
                verificacao_id INTEGER,
                acao TEXT NOT NULL,
                detalhe TEXT,
                ip TEXT,
                em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_acessos_verif '
                       'ON acessos_identidade (verificacao_id)')

        # Índices para a lista, a busca e as contagens continuarem rápidas com
        # milhares de pacientes. Só aceleram consultas: não mudam nenhum dado.
        for nome_idx, tabela, colunas in [
            ('idx_pacientes_casa', 'pacientes', 'casa_id, ativo, nome'),
            ('idx_condicoes_paciente', 'condicoes', 'paciente_id'),
            ('idx_medicamentos_paciente', 'medicamentos', 'paciente_id, ativo'),
            ('idx_horarios_medicamento', 'horarios_medicamento', 'medicamento_id'),
            ('idx_doses_medicamento', 'doses_administradas', 'medicamento_id, horario_previsto'),
            ('idx_exames_paciente', 'exames', 'paciente_id, data_exame'),
            ('idx_visitas_paciente', 'visitas', 'paciente_id, data_visita'),
            ('idx_evolucoes_paciente', 'evolucoes_medicas', 'paciente_id, data_evolucao'),
            ('idx_usuarios_casa', 'usuarios', 'casa_id, perfil'),
        ]:
            cursor.execute(f'CREATE INDEX IF NOT EXISTS {nome_idx} ON {tabela} ({colunas})')

        conn.commit()
    # WAL: quem lê não trava quem grava. A configuração fica gravada no arquivo do banco.
    try:
        conexao = sqlite3.connect(DB_PATH)
        conexao.execute('PRAGMA journal_mode = WAL')
        conexao.close()
    except sqlite3.Error:
        logging.getLogger('gestcare.db').warning('wal_indisponivel')


def calcular_idade(data_nasc_str):
    if not data_nasc_str:
        return None
    try:
        data_nasc = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()
        hoje = date.today()
        return hoje.year - data_nasc.year - (
            (hoje.month, hoje.day) < (data_nasc.month, data_nasc.day)
        )
    except ValueError:
        return None


def _normalizar_documento(valor):
    return ''.join(c for c in (valor or '') if c.isdigit())


def cpf_valido(valor):
    """Valida CPF pelos dois dígitos verificadores (algoritmo da Receita Federal)."""
    cpf = _normalizar_documento(valor)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(cpf[i]) * (tamanho + 1 - i) for i in range(tamanho))
        digito = (soma * 10) % 11 % 10
        if digito != int(cpf[tamanho]):
            return False
    return True


def formatar_cpf(valor):
    """Formata como 000.000.000-00; devolve o valor original se não tiver 11 dígitos."""
    cpf = _normalizar_documento(valor)
    if len(cpf) != 11:
        return valor or ''
    return f'{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}'


def mascarar_cpf(valor):
    """Oculta o CPF deixando apenas os 3 últimos dígitos: •••.•••.789-00."""
    cpf = _normalizar_documento(valor)
    if len(cpf) != 11:
        return '•••' if valor else ''
    return f'•••.•••.{cpf[6:9]}-{cpf[9:]}'


app.jinja_env.filters['formatar_cpf'] = formatar_cpf
app.jinja_env.filters['mascarar_cpf'] = mascarar_cpf


def extensao_permitida(nome_arquivo):
    return '.' in nome_arquivo and nome_arquivo.rsplit('.', 1)[1].lower() in EXTENSOES_PERMITIDAS


_MIME_PERMITIDOS = {
    b'\x25\x50\x44\x46': 'pdf',          # %PDF
    b'\x89\x50\x4e\x47': 'png',          # PNG
    b'\xff\xd8\xff':      'jpg',          # JPEG
    b'\x52\x49\x46\x46': 'webp',         # WEBP (RIFF)
}

def _mime_valido(arquivo) -> bool:
    cabecalho = arquivo.read(8)
    arquivo.seek(0)
    for assinatura in _MIME_PERMITIDOS:
        if cabecalho[:len(assinatura)] == assinatura:
            return True
    return False

def salvar_arquivo_exame(arquivo):
    """Salva o arquivo anexado a um exame (se houver) e retorna o nome salvo."""
    if not arquivo or not arquivo.filename:
        return None
    if not extensao_permitida(arquivo.filename):
        return None
    if not _mime_valido(arquivo):
        log.warning('upload_mime_invalido', extra={'gc_action': 'upload_exame',
                    'gc_user_id': session.get('usuario_id')})
        return None
    ext = arquivo.filename.rsplit('.', 1)[1].lower()
    if ext not in EXTENSOES_PERMITIDAS:
        return None
    arquivo_nome = f"{uuid.uuid4().hex}.{ext}"
    arquivo.save(os.path.join(UPLOAD_FOLDER, arquivo_nome))
    return arquivo_nome


def hash_senha(senha):
    return bcrypt.hashpw(senha.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verificar_senha(senha, hash_armazenado):
    return bcrypt.checkpw(senha.encode('utf-8'), hash_armazenado.encode('utf-8'))


def login_obrigatorio(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'usuario_id' not in session:
            flash('Faça login para acessar esta página.', 'erro')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def apenas_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('usuario_perfil') != 'admin':
            flash('Acesso restrito a administradores.', 'erro')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

def apenas_equipe(f):
    """Bloqueia responsáveis e o superadmin de acessar páginas de uma casa."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('usuario_perfil') == 'responsavel':
            return redirect(url_for('painel_responsavel'))
        if session.get('usuario_perfil') == 'superadmin':
            return redirect(url_for('listar_casas'))
        return f(*args, **kwargs)
    return decorated

def apenas_superadmin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('usuario_perfil') != 'superadmin':
            flash('Acesso restrito.', 'erro')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def casa_id_atual():
    """Casa (instituição) do usuário logado. None para superadmin."""
    return session.get('casa_id')


def buscar_paciente_da_casa(conn, paciente_id):
    """Busca um paciente garantindo que ele pertence à casa do usuário logado."""
    return conn.execute(
        'SELECT * FROM pacientes WHERE id = ? AND casa_id = ?',
        (paciente_id, casa_id_atual())
    ).fetchone()


def gerar_slug(nome):
    """Gera slug URL-seguro a partir do nome da casa."""
    nome_norm = unicodedata.normalize('NFD', nome)
    nome_ascii = nome_norm.encode('ascii', 'ignore').decode('ascii')
    slug = nome_ascii.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug or 'casa'


def slug_unico(conn, base_slug, excluir_id=None):
    """Garante unicidade do slug adicionando sufixo numérico se necessário."""
    slug = base_slug
    contador = 1
    while True:
        query = 'SELECT id FROM casas WHERE slug = ?'
        params = [slug]
        if excluir_id:
            query += ' AND id != ?'
            params.append(excluir_id)
        if not conn.execute(query, params).fetchone():
            return slug
        slug = f'{base_slug}-{contador}'
        contador += 1


# ============================================================================
# GESTÃO DE CASAS (multi-instituição — restrito ao superadmin)
# ============================================================================

@app.route('/admin/casas')
@login_obrigatorio
@apenas_superadmin
def listar_casas():
    with get_db() as conn:
        casas = conn.execute('''
            SELECT c.*,
                   (SELECT COUNT(*) FROM pacientes WHERE casa_id = c.id AND ativo = 1) as total_pacientes,
                   (SELECT COUNT(*) FROM usuarios WHERE casa_id = c.id AND ativo = 1) as total_usuarios
            FROM casas c ORDER BY c.nome
        ''').fetchall()
    return render_template('casas.html', casas=casas)


@app.route('/admin/casas/nova', methods=['GET', 'POST'])
@login_obrigatorio
@apenas_superadmin
def nova_casa():
    if request.method == 'POST':
        dados = request.form
        nome_casa     = dados.get('nome_casa', '').strip()
        admin_nome    = dados.get('admin_nome', '').strip()
        admin_email   = dados.get('admin_email', '').strip().lower()
        admin_senha   = dados.get('admin_senha', '')

        if not nome_casa or not admin_nome or not admin_email or not admin_senha:
            flash('Preencha todos os campos obrigatórios.', 'erro')
            return render_template('casa_form.html')
        if len(admin_senha) < 6:
            flash('A senha do administrador deve ter pelo menos 6 caracteres.', 'erro')
            return render_template('casa_form.html')

        try:
            with get_db() as conn:
                base = gerar_slug(nome_casa)
                slug = slug_unico(conn, base)
                casa_id = conn.execute(
                    'INSERT INTO casas (nome, slug) VALUES (?, ?)', (nome_casa, slug)
                ).lastrowid
                conn.execute('''
                    INSERT INTO usuarios (nome, email, senha, perfil, casa_id)
                    VALUES (?, ?, ?, 'admin', ?)
                ''', (admin_nome, admin_email, hash_senha(admin_senha), casa_id))
        except sqlite3.IntegrityError:
            flash('Este e-mail já está cadastrado para outro usuário.', 'erro')
            return render_template('casa_form.html')

        flash(f'Casa "{nome_casa}" criada! Administrador {admin_nome} pode logar com {admin_email}.', 'sucesso')
        return redirect(url_for('listar_casas'))

    return render_template('casa_form.html')


@app.route('/admin/casas/<int:casa_id>/editar', methods=['GET', 'POST'])
@login_obrigatorio
@apenas_superadmin
def editar_casa(casa_id):
    with get_db() as conn:
        casa = conn.execute('SELECT * FROM casas WHERE id = ?', (casa_id,)).fetchone()
        if not casa:
            flash('Casa não encontrada.', 'erro')
            return redirect(url_for('listar_casas'))

        if request.method == 'POST':
            nome = request.form.get('nome', '').strip()
            if not nome:
                flash('Informe o nome da casa.', 'erro')
                return render_template('casa_editar.html', casa=casa)
            base = gerar_slug(nome)
            slug = slug_unico(conn, base, excluir_id=casa_id)
            conn.execute('UPDATE casas SET nome = ?, slug = ? WHERE id = ?', (nome, slug, casa_id))
            flash('Nome da casa atualizado!', 'sucesso')
            return redirect(url_for('listar_casas'))

    return render_template('casa_editar.html', casa=casa)


@app.route('/admin/casas/<int:casa_id>/toggle', methods=['POST'])
@login_obrigatorio
@apenas_superadmin
def toggle_casa(casa_id):
    with get_db() as conn:
        casa = conn.execute('SELECT ativo, nome FROM casas WHERE id = ?', (casa_id,)).fetchone()
        if casa:
            novo_status = 0 if casa['ativo'] else 1
            conn.execute('UPDATE casas SET ativo = ? WHERE id = ?', (novo_status, casa_id))
            acao = 'ativada' if novo_status else 'desativada'
            flash(f'Casa "{casa["nome"]}" {acao}.', 'sucesso')
    return redirect(url_for('listar_casas'))


# ============================================================================
# AUTENTICAÇÃO
# ============================================================================

@app.route('/entrar/<slug>', methods=['GET', 'POST'])
def login_casa(slug):
    if 'usuario_id' in session:
        if session.get('usuario_perfil') == 'responsavel':
            return redirect(url_for('painel_responsavel'))
        if session.get('usuario_perfil') == 'superadmin':
            return redirect(url_for('listar_casas'))
        return redirect(url_for('index'))

    with get_db() as conn:
        casa_pre = conn.execute('SELECT * FROM casas WHERE slug = ? AND ativo = 1', (slug,)).fetchone()
    if not casa_pre:
        flash('Link inválido ou instituição desativada.', 'erro')
        return redirect(url_for('login'))

    if request.method == 'POST':
        ip = request.remote_addr
        if not _checar_rate_limit(ip):
            log.warning('rate_limit_login', extra={'gc_action': 'login_bloqueado', 'gc_ip': ip})
            flash('Muitas tentativas. Aguarde 1 minuto antes de tentar novamente.', 'erro')
            return render_template('login.html', casa_preselecionar=casa_pre)

        email = request.form.get('email', '').strip()
        senha = request.form.get('senha', '')
        casa_id_escolhida = str(casa_pre['id'])

        with get_db() as conn:
            usuario = conn.execute(
                'SELECT * FROM usuarios WHERE LOWER(email) = LOWER(?) AND ativo = 1 AND excluido_em IS NULL',
                (email,)
            ).fetchone()
            casa = None
            if usuario and usuario['casa_id']:
                casa = conn.execute('SELECT * FROM casas WHERE id = ?', (usuario['casa_id'],)).fetchone()

        if (usuario and usuario['casa_id']
                and str(usuario['casa_id']) != casa_id_escolhida):
            flash('Este e-mail não pertence a esta instituição.', 'erro')
            return render_template('login.html', casa_preselecionar=casa_pre)

        if usuario and usuario['casa_id'] and (not casa or not casa['ativo']):
            flash('O acesso da sua instituição está temporariamente desativado. Contate o suporte.', 'erro')
            return render_template('login.html', casa_preselecionar=casa_pre)

        if usuario and verificar_senha(senha, usuario['senha']):
            session.permanent = False
            session['usuario_id']     = usuario['id']
            session['usuario_nome']   = usuario['nome']
            session['usuario_perfil'] = usuario['perfil']
            session['paciente_id']    = usuario['paciente_id']
            session['casa_id']        = usuario['casa_id']
            session['casa_nome']      = casa['nome'] if casa else None
            session['casa_slug']      = casa_pre['slug']
            log.info('login_sucesso', extra={'gc_action': 'login', 'gc_user_id': usuario['id'],
                     'gc_perfil': usuario['perfil'], 'gc_casa_id': usuario['casa_id']})
            flash(f'Bem-vindo(a), {usuario["nome"]}!', 'sucesso')
            if usuario['perfil'] == 'superadmin':
                return redirect(url_for('listar_casas'))
            if usuario['perfil'] == 'responsavel':
                if not usuario['paciente_id']:
                    return redirect(url_for('vincular_idoso'))
                return redirect(url_for('painel_responsavel'))
            return redirect(url_for('hoje'))
        else:
            log.warning('login_falhou', extra={'gc_action': 'login', 'gc_email_hash': hash(email),
                        'gc_casa_slug': casa_pre['slug']})
            flash('E-mail ou senha incorretos.', 'erro')
            return render_template('login.html', casa_preselecionar=casa_pre)

    return render_template('login.html', casa_preselecionar=casa_pre)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'usuario_id' in session:
        if session.get('usuario_perfil') == 'responsavel':
            return redirect(url_for('painel_responsavel'))
        if session.get('usuario_perfil') == 'superadmin':
            return redirect(url_for('listar_casas'))
        return redirect(url_for('index'))

    if request.method == 'POST':
        ip = request.remote_addr
        if not _checar_rate_limit(ip):
            log.warning('rate_limit_login', extra={'gc_action': 'login_bloqueado', 'gc_ip': ip})
            flash('Muitas tentativas. Aguarde 1 minuto antes de tentar novamente.', 'erro')
            return render_template('login.html')

        email = request.form.get('email', '').strip()
        senha = request.form.get('senha', '')

        with get_db() as conn:
            usuario = conn.execute(
                'SELECT * FROM usuarios WHERE LOWER(email) = LOWER(?) AND ativo = 1 AND excluido_em IS NULL',
                (email,)
            ).fetchone()
            casa = None
            if usuario and usuario['casa_id']:
                casa = conn.execute(
                    'SELECT * FROM casas WHERE id = ?', (usuario['casa_id'],)
                ).fetchone()

        if usuario and usuario['casa_id'] and (not casa or not casa['ativo']):
            flash('O acesso da sua instituição está temporariamente desativado. Contate o suporte.', 'erro')
            return render_template('login.html')

        if usuario and verificar_senha(senha, usuario['senha']):
            session.permanent = False
            session['usuario_id']     = usuario['id']
            session['usuario_nome']   = usuario['nome']
            session['usuario_perfil'] = usuario['perfil']
            session['paciente_id']    = usuario['paciente_id']
            session['casa_id']        = usuario['casa_id']
            session['casa_nome']      = casa['nome'] if casa else None
            session['casa_slug']      = casa['slug'] if casa else None
            log.info('login_sucesso', extra={'gc_action': 'login', 'gc_user_id': usuario['id'],
                     'gc_perfil': usuario['perfil'], 'gc_casa_id': usuario['casa_id']})
            flash(f'Bem-vindo(a), {usuario["nome"]}!', 'sucesso')
            if usuario['perfil'] == 'superadmin':
                return redirect(url_for('listar_casas'))
            if usuario['perfil'] == 'responsavel':
                if not usuario['paciente_id']:
                    return redirect(url_for('vincular_idoso'))
                return redirect(url_for('painel_responsavel'))
            return redirect(url_for('hoje'))
        else:
            log.warning('login_falhou', extra={'gc_action': 'login', 'gc_email_hash': hash(email)})
            flash('E-mail ou senha incorretos.', 'erro')
            return render_template('login.html')
    return render_template('login.html')


@app.route('/entrar/<slug>/cadastro', methods=['GET', 'POST'])
def cadastro_responsavel_casa(slug):
    if 'usuario_id' in session:
        return redirect(url_for('index'))
    with get_db() as conn:
        casa_pre = conn.execute('SELECT * FROM casas WHERE slug = ? AND ativo = 1', (slug,)).fetchone()
    if not casa_pre:
        flash('Link inválido ou instituição desativada.', 'erro')
        return redirect(url_for('login'))
    return _processar_cadastro_responsavel(casa_preselecionar=casa_pre)


@app.route('/responsavel/cadastro', methods=['GET', 'POST'])
def cadastro_responsavel():
    if 'usuario_id' in session:
        return redirect(url_for('index'))
    with get_db() as conn:
        casas = conn.execute('SELECT id, nome FROM casas WHERE ativo = 1 ORDER BY nome').fetchall()
    return _processar_cadastro_responsavel(casas=casas)


def _processar_cadastro_responsavel(casas=None, casa_preselecionar=None):
    if request.method == 'POST':
        dados = request.form
        nome    = dados.get('nome', '').strip()
        email   = dados.get('email', '').strip().lower()
        cpf     = dados.get('cpf', '').strip()
        senha   = dados.get('senha', '')
        senha2  = dados.get('confirmar_senha', '')
        casa_id = str(casa_preselecionar['id']) if casa_preselecionar else (dados.get('casa_id') or None)

        if not nome or not email or not cpf or not senha or not casa_id:
            flash('Preencha todos os campos obrigatórios.', 'erro')
            return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)
        if not cpf_valido(cpf):
            flash('CPF inválido. Confira os números digitados.', 'erro')
            return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)
        cpf = formatar_cpf(cpf)
        if len(senha) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'erro')
            return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)
        if senha != senha2:
            flash('As senhas não coincidem.', 'erro')
            return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)

        with get_db() as conn:
            casa = conn.execute(
                'SELECT * FROM casas WHERE id = ? AND ativo = 1', (casa_id,)
            ).fetchone()
        if not casa:
            flash('Instituição inválida. Selecione novamente.', 'erro')
            return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)

        try:
            with get_db() as conn:
                cursor = conn.execute('''
                    INSERT INTO usuarios (nome, email, senha, perfil, cpf, casa_id)
                    VALUES (?, ?, ?, 'responsavel', ?, ?)
                ''', (nome, email, hash_senha(senha), cpf, casa_id))
                usuario_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            log.warning('cadastro_email_duplicado', extra={'gc_action': 'cadastro_responsavel',
                        'gc_casa_id': casa_id})
            flash('Este e-mail já está cadastrado.', 'erro')
            return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)

        log.info('cadastro_responsavel_ok', extra={'gc_action': 'cadastro_responsavel',
                 'gc_user_id': usuario_id, 'gc_casa_id': casa['id']})
        session.permanent = False
        session['usuario_id']     = usuario_id
        session['usuario_nome']   = nome
        session['usuario_perfil'] = 'responsavel'
        session['paciente_id']    = None
        session['casa_id']        = casa['id']
        session['casa_nome']      = casa['nome']
        session['casa_slug']      = casa['slug']
        flash(f'Conta criada com sucesso, {nome}! Agora informe o CPF/RG do idoso para continuar.', 'sucesso')
        return redirect(url_for('vincular_idoso'))

    return render_template('responsavel_cadastro.html', casas=casas, casa_preselecionar=casa_preselecionar)


@app.route('/responsavel/vincular-idoso', methods=['GET', 'POST'])
@login_obrigatorio
def vincular_idoso():
    if session.get('usuario_perfil') != 'responsavel':
        flash('Acesso não autorizado.', 'erro')
        return redirect(url_for('index'))

    if request.method == 'POST':
        documento = _normalizar_documento(request.form.get('cpf_rg', ''))
        if not documento:
            flash('Informe o CPF ou RG do idoso.', 'erro')
            return render_template('responsavel_vincular.html')

        with get_db() as conn:
            paciente = conn.execute('''
                SELECT * FROM pacientes WHERE ativo = 1 AND casa_id = ?
                  AND (REPLACE(REPLACE(REPLACE(cpf,'.',''),'-',''),' ','') = ?
                       OR REPLACE(REPLACE(REPLACE(rg,'.',''),'-',''),' ','') = ?)
            ''', (casa_id_atual(), documento, documento)).fetchone()

            if not paciente:
                flash('Nenhum idoso encontrado com este CPF/RG. Confira o documento e tente novamente.', 'erro')
                return render_template('responsavel_vincular.html')

            conn.execute(
                'UPDATE usuarios SET paciente_id = ? WHERE id = ?',
                (paciente['id'], session['usuario_id'])
            )
        session['paciente_id'] = paciente['id']
        flash(f'Vinculado(a) com sucesso a {paciente["nome"]}!', 'sucesso')
        return redirect(url_for('painel_responsavel'))

    return render_template('responsavel_vincular.html')


@app.route('/responsavel/desvincular', methods=['POST'])
@login_obrigatorio
def desvincular_idoso():
    if session.get('usuario_perfil') != 'responsavel':
        flash('Acesso não autorizado.', 'erro')
        return redirect(url_for('index'))
    with get_db() as conn:
        conn.execute(
            'UPDATE usuarios SET paciente_id = NULL WHERE id = ?',
            (session['usuario_id'],)
        )
    session['paciente_id'] = None
    flash('Você pode vincular outro idoso agora.', 'sucesso')
    return redirect(url_for('vincular_idoso'))


@app.route('/meu-perfil', methods=['GET', 'POST'])
@login_obrigatorio
def meu_perfil():
    usuario_id = session['usuario_id']
    eh_admin = session.get('usuario_perfil') == 'admin'
    with get_db() as conn:
        usuario = conn.execute('SELECT * FROM usuarios WHERE id = ?', (usuario_id,)).fetchone()
        casa = None
        if eh_admin:
            casa = conn.execute('SELECT * FROM casas WHERE id = ?', (casa_id_atual(),)).fetchone()

        if request.method == 'POST':
            dados = request.form
            nome        = dados.get('nome', '').strip()
            email       = dados.get('email', '').strip().lower()
            senha_atual = dados.get('senha_atual', '')
            senha_nova  = dados.get('senha_nova', '')
            senha_nova2 = dados.get('confirmar_senha_nova', '')
            nome_casa   = dados.get('nome_casa', '').strip()

            if not nome or not email:
                flash('Nome e e-mail são obrigatórios.', 'erro')
                return render_template('meu_perfil.html', usuario=usuario, casa=casa)
            if eh_admin and not nome_casa:
                flash('Informe o nome da casa.', 'erro')
                return render_template('meu_perfil.html', usuario=usuario, casa=casa)

            quer_trocar_senha = bool(senha_nova or senha_nova2)
            email_mudou = email.lower() != usuario['email'].lower()

            if (quer_trocar_senha or email_mudou) and not senha_atual:
                flash('Informe sua senha atual para confirmar a alteração de e-mail ou senha.', 'erro')
                return render_template('meu_perfil.html', usuario=usuario, casa=casa)

            if (quer_trocar_senha or email_mudou) and not verificar_senha(senha_atual, usuario['senha']):
                flash('Senha atual incorreta.', 'erro')
                return render_template('meu_perfil.html', usuario=usuario, casa=casa)

            if quer_trocar_senha:
                if len(senha_nova) < 6:
                    flash('A nova senha deve ter pelo menos 6 caracteres.', 'erro')
                    return render_template('meu_perfil.html', usuario=usuario, casa=casa)
                if senha_nova != senha_nova2:
                    flash('As senhas novas não coincidem.', 'erro')
                    return render_template('meu_perfil.html', usuario=usuario, casa=casa)

            try:
                if quer_trocar_senha:
                    conn.execute(
                        'UPDATE usuarios SET nome = ?, email = ?, senha = ? WHERE id = ?',
                        (nome, email, hash_senha(senha_nova), usuario_id)
                    )
                else:
                    conn.execute(
                        'UPDATE usuarios SET nome = ?, email = ? WHERE id = ?',
                        (nome, email, usuario_id)
                    )
            except sqlite3.IntegrityError:
                flash('Este e-mail já está em uso por outra conta.', 'erro')
                return render_template('meu_perfil.html', usuario=usuario, casa=casa)

            if eh_admin:
                conn.execute('UPDATE casas SET nome = ? WHERE id = ?', (nome_casa, casa_id_atual()))
                session['casa_nome'] = nome_casa

            session['usuario_nome'] = nome
            flash('Perfil atualizado com sucesso!', 'sucesso')
            return redirect(url_for('meu_perfil'))

    return render_template('meu_perfil.html', usuario=usuario, casa=casa)


@app.route('/logout')
def logout():
    nome = session.get('usuario_nome', '')
    casa_slug = session.get('casa_slug')
    session.clear()
    flash(f'Até logo, {nome}!', 'sucesso')
    if casa_slug:
        return redirect(url_for('login_casa', slug=casa_slug))
    return redirect(url_for('login'))


# ============================================================================
# PORTAL DO RESPONSÁVEL (somente leitura)
# Início, Saúde, Exames e Visitas. A Conta fica no menu do avatar (meu_perfil).
# ============================================================================

_MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
          'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']


def data_br(valor):
    """'2026-11-12' (ou '2026-11-12T08:00') -> '12/11/2026'. Valor vazio ou fora do padrão volta como veio."""
    if not valor:
        return ''
    texto = str(valor)[:10]
    try:
        return datetime.strptime(texto, '%Y-%m-%d').strftime('%d/%m/%Y')
    except ValueError:
        return str(valor)


def data_curta(valor):
    """'2026-11-12' -> '12/11'."""
    completa = data_br(valor)
    return completa[:5] if len(completa) == 10 else completa


app.jinja_env.filters['data_br'] = data_br
app.jinja_env.filters['data_curta'] = data_curta


def _identidade_liberada(conn):
    """Libera o portal só para quem a casa aprovou, quando o repositório de identidade existir.
    Enquanto ele não existir neste código, não há aprovação a exigir."""
    verificar = globals().get('responsavel_verificado')
    return True if verificar is None else bool(verificar(conn, session['usuario_id']))


def _abrir_portal(conn):
    """Confere perfil, idoso vinculado e aprovação de identidade.
    Devolve (paciente, None) quando pode seguir, ou (None, resposta) com o redirecionamento."""
    if session.get('usuario_perfil') != 'responsavel':
        flash('Acesso não autorizado.', 'erro')
        return None, redirect(url_for('login'))
    paciente_id = session.get('paciente_id')
    if not paciente_id:
        return None, redirect(url_for('vincular_idoso'))
    if not _identidade_liberada(conn):
        if 'verificacao_responsavel' in app.view_functions:
            flash('Seu acesso à ficha será liberado depois que a casa aprovar sua identidade.', 'erro')
            return None, redirect(url_for('verificacao_responsavel'))
        return None, redirect(url_for('login'))
    paciente = buscar_paciente_da_casa(conn, paciente_id)
    if not paciente:
        flash('Paciente não encontrado.', 'erro')
        return None, redirect(url_for('login'))
    dados = dict(paciente)
    dados['idade'] = calcular_idade(dados['data_nascimento'])
    return dados, None


def _remedios_do_paciente(conn, paciente_id, agora=None):
    """Remédios ativos com horários, situação das doses de hoje e fim do antibiótico.

    As doses do dia são casadas por ordem: a 1ª dose registrada hoje cobre o 1º horário do remédio,
    a 2ª cobre o 2º, e assim por diante. Horário que já passou sem registro aparece como
    'sem_registro' (o sistema só guarda doses dadas, então não afirma que a dose foi pulada)."""
    agora = agora or datetime.now()
    hoje = agora.strftime('%Y-%m-%d')
    hora_agora = agora.strftime('%H:%M')
    remedios = []
    for m in conn.execute(
            'SELECT * FROM medicamentos WHERE paciente_id = ? AND ativo = 1 ORDER BY nome',
            (paciente_id,)).fetchall():
        med = dict(m)
        horarios = [h['horario'][:5] for h in conn.execute(
            'SELECT horario FROM horarios_medicamento WHERE medicamento_id = ? ORDER BY horario',
            (m['id'],)).fetchall()]
        registradas = conn.execute(
            'SELECT administrado_por FROM doses_administradas '
            'WHERE medicamento_id = ? AND substr(horario_previsto, 1, 10) = ? '
            'ORDER BY horario_previsto, id', (m['id'], hoje)).fetchall()
        doses = []
        for i, h in enumerate(horarios):
            if i < len(registradas):
                doses.append({'horario': h, 'situacao': 'dado',
                              'por': registradas[i]['administrado_por'] or ''})
            elif h > hora_agora:
                doses.append({'horario': h, 'situacao': 'mais_tarde', 'por': ''})
            else:
                doses.append({'horario': h, 'situacao': 'sem_registro', 'por': ''})
        med['horarios'] = horarios
        med['doses_hoje'] = doses
        med['dias_restantes'] = None
        if med.get('eh_antibiotico') and med.get('data_fim_tratamento'):
            try:
                fim = datetime.strptime(str(med['data_fim_tratamento'])[:10], '%Y-%m-%d').date()
                med['dias_restantes'] = (fim - agora.date()).days
            except ValueError:
                pass
        remedios.append(med)
    return remedios


@app.route('/meu-paciente')
@login_obrigatorio
def painel_responsavel():
    """Início: como foi o dia."""
    with get_db() as conn:
        paciente, resposta = _abrir_portal(conn)
        if resposta:
            return resposta
        pid = paciente['id']
        remedios = _remedios_do_paciente(conn, pid)
        doses_hoje = sorted(
            ({'nome': r['nome'], 'dosagem': r['dosagem'], **d} for r in remedios for d in r['doses_hoje']),
            key=lambda d: (d['horario'], d['nome']))
        anotacoes = conn.execute(
            'SELECT * FROM evolucoes_medicas WHERE paciente_id = ? ORDER BY data_evolucao DESC, id DESC LIMIT 2',
            (pid,)).fetchall()
        ultima_visita = conn.execute(
            'SELECT * FROM visitas WHERE paciente_id = ? ORDER BY data_visita DESC, id DESC LIMIT 1',
            (pid,)).fetchone()
        limite = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        exame_recente = conn.execute(
            'SELECT * FROM exames WHERE paciente_id = ? AND data_exame >= ? '
            'ORDER BY data_exame DESC, id DESC LIMIT 1', (pid, limite)).fetchone()
    return render_template('familia/inicio.html', paciente=paciente, doses_hoje=doses_hoje,
                           anotacoes=anotacoes, ultima_visita=ultima_visita,
                           exame_recente=exame_recente)


@app.route('/meu-paciente/saude')
@login_obrigatorio
def familia_saude():
    """Saúde: remédios, condições, alimentação, acompanhamento e anotações da equipe."""
    with get_db() as conn:
        paciente, resposta = _abrir_portal(conn)
        if resposta:
            return resposta
        pid = paciente['id']
        remedios = _remedios_do_paciente(conn, pid)
        condicoes = conn.execute(
            'SELECT * FROM condicoes WHERE paciente_id = ? ORDER BY data_diagnostico DESC', (pid,)).fetchall()
        dieta = conn.execute('SELECT * FROM comorbidades_dieta WHERE paciente_id = ?', (pid,)).fetchone()
        ficha_psico = conn.execute('SELECT * FROM ficha_psicologica WHERE paciente_id = ?', (pid,)).fetchone()
        profissionais = conn.execute(
            'SELECT * FROM profissionais_consultados WHERE paciente_id = ? ORDER BY data_consulta DESC',
            (pid,)).fetchall()
        anotacoes = conn.execute(
            'SELECT * FROM evolucoes_medicas WHERE paciente_id = ? ORDER BY data_evolucao DESC, id DESC',
            (pid,)).fetchall()
    return render_template('familia/saude.html', paciente=paciente, remedios=remedios,
                           condicoes=condicoes, dieta=dieta, ficha_psico=ficha_psico,
                           profissionais=profissionais, anotacoes=anotacoes)


@app.route('/meu-paciente/exames')
@login_obrigatorio
def familia_exames():
    """Exames com PDF e recado da casa, agrupados por mês."""
    with get_db() as conn:
        paciente, resposta = _abrir_portal(conn)
        if resposta:
            return resposta
        linhas = conn.execute(
            'SELECT * FROM exames WHERE paciente_id = ? ORDER BY data_exame DESC, id DESC',
            (paciente['id'],)).fetchall()
    limite = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    meses, ordem = {}, []
    for linha in linhas:
        exame = dict(linha)
        exame['recente'] = str(exame['data_exame'])[:10] >= limite
        chave = str(exame['data_exame'])[:7]
        if chave not in meses:
            meses[chave] = []
            ordem.append(chave)
        meses[chave].append(exame)

    def titulo_mes(chave):
        try:
            ano, mes = chave.split('-')
            return f'{_MESES[int(mes) - 1].capitalize()} de {ano}'
        except (ValueError, IndexError):
            return chave

    grupos = [(titulo_mes(c), meses[c]) for c in ordem]
    return render_template('familia/exames.html', paciente=paciente, grupos=grupos,
                           total=len(linhas),
                           com_recado=sum(1 for l in linhas if l['observacao_familia']),
                           com_pdf=sum(1 for l in linhas if l['arquivo_nome']))


@app.route('/meu-paciente/visitas')
@login_obrigatorio
def familia_visitas():
    with get_db() as conn:
        paciente, resposta = _abrir_portal(conn)
        if resposta:
            return resposta
        visitas = conn.execute(
            'SELECT * FROM visitas WHERE paciente_id = ? ORDER BY data_visita DESC, id DESC LIMIT 30',
            (paciente['id'],)).fetchall()
    return render_template('familia/visitas.html', paciente=paciente, visitas=visitas)


# ============================================================================
# USUÁRIOS
# ============================================================================

# Perfis que o admin da casa pode atribuir. 'superadmin' nunca entra aqui.
PERFIS_DA_CASA = ('enfermeiro', 'admin', 'responsavel')
SENHA_MINIMA = 6


def _pacientes_da_casa(conn):
    return conn.execute(
        'SELECT id, nome, cpf, rg FROM pacientes WHERE ativo = 1 AND casa_id = ? ORDER BY nome',
        (casa_id_atual(),)
    ).fetchall()


def _buscar_usuario_da_casa(conn, usuario_id, excluido=False):
    """Usuário da casa logada. Por padrão só os não excluídos; excluido=True
    busca apenas os que estão na lixeira."""
    filtro = 'excluido_em IS NOT NULL' if excluido else 'excluido_em IS NULL'
    return conn.execute(
        f"SELECT * FROM usuarios WHERE id = ? AND casa_id = ? AND perfil != 'superadmin' AND {filtro}",
        (usuario_id, casa_id_atual())
    ).fetchone()


def _email_de_usuario_excluido(conn, email):
    """True quando o e-mail pertence a um usuário excluído desta casa."""
    return conn.execute(
        'SELECT 1 FROM usuarios WHERE LOWER(email) = LOWER(?) AND casa_id = ? AND excluido_em IS NOT NULL',
        (email, casa_id_atual())
    ).fetchone() is not None


def _ler_form_usuario(conn, exigir_senha):
    """Lê e valida o formulário de usuário (novo ou edição).
    Retorna (dados, erro). A senha só é validada quando informada, ou quando
    exigir_senha=True (cadastro novo)."""
    f = request.form
    dados = {
        'nome':   f.get('nome', '').strip(),
        'email':  f.get('email', '').strip().lower(),
        'perfil': f.get('perfil', 'enfermeiro'),
        'paciente_id': f.get('paciente_id') or None,
        'cpf':    f.get('cpf', '').strip(),
        'senha':  f.get('senha', ''),
        'senha2': f.get('confirmar_senha', ''),
    }
    if not dados['nome'] or not dados['email']:
        return dados, 'Preencha todos os campos obrigatórios.'
    if '@' not in dados['email']:
        return dados, 'Informe um e-mail válido.'
    if dados['perfil'] not in PERFIS_DA_CASA:
        return dados, 'Perfil inválido.'

    if dados['perfil'] == 'responsavel':
        if dados['paciente_id']:
            try:
                dados['paciente_id'] = int(dados['paciente_id'])
            except ValueError:
                return dados, 'Paciente inválido.'
            if not buscar_paciente_da_casa(conn, dados['paciente_id']):
                return dados, 'Paciente inválido.'
        if dados['cpf']:
            if not cpf_valido(dados['cpf']):
                return dados, 'CPF inválido. Confira os números digitados.'
            dados['cpf'] = formatar_cpf(dados['cpf'])
    else:
        # Vínculo com paciente e CPF de responsável só fazem sentido nesse perfil.
        dados['paciente_id'] = None

    if exigir_senha and not dados['senha']:
        return dados, 'Preencha todos os campos obrigatórios.'
    if dados['senha'] or dados['senha2']:
        if len(dados['senha']) < SENHA_MINIMA:
            return dados, f'A senha deve ter pelo menos {SENHA_MINIMA} caracteres.'
        if dados['senha2'] and dados['senha'] != dados['senha2']:
            return dados, 'As senhas não coincidem.'
        if not exigir_senha and dados['senha'] != dados['senha2']:
            return dados, 'As senhas não coincidem.'
    return dados, None


@app.route('/usuarios')
@login_obrigatorio
@apenas_admin
def listar_usuarios():
    ver_excluidos = request.args.get('ver') == 'excluidos'
    with get_db() as conn:
        usuarios = conn.execute(f'''
            SELECT u.id, u.nome, u.email, u.perfil, u.ativo, u.criado_em,
                   u.paciente_id, p.nome as paciente_nome,
                   strftime('%d/%m/%Y às %H:%M', u.excluido_em, 'localtime') as excluido_em_fmt,
                   quem.nome as excluido_por_nome
            FROM usuarios u
            LEFT JOIN pacientes p ON u.paciente_id = p.id
            LEFT JOIN usuarios quem ON quem.id = u.excluido_por
            WHERE u.casa_id = ? AND u.perfil != 'superadmin'
              AND u.excluido_em IS {'NOT NULL' if ver_excluidos else 'NULL'}
            ORDER BY {'u.excluido_em DESC' if ver_excluidos else 'u.nome'}
        ''', (casa_id_atual(),)).fetchall()
        total_excluidos = conn.execute(
            'SELECT COUNT(*) FROM usuarios WHERE casa_id = ? AND excluido_em IS NOT NULL',
            (casa_id_atual(),)
        ).fetchone()[0]
    return render_template('usuarios.html', usuarios=usuarios,
                           ver_excluidos=ver_excluidos, total_excluidos=total_excluidos)


@app.route('/usuarios/novo', methods=['GET', 'POST'])
@login_obrigatorio
@apenas_admin
def novo_usuario():
    with get_db() as conn:
        pacientes = _pacientes_da_casa(conn)
        if request.method == 'POST':
            dados, erro = _ler_form_usuario(conn, exigir_senha=True)
            if erro:
                flash(erro, 'erro')
                return render_template('usuario_form.html', pacientes=pacientes, usuario=None, form=dados)
            try:
                cursor = conn.execute(
                    'INSERT INTO usuarios (nome, email, senha, perfil, paciente_id, cpf, casa_id) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (dados['nome'], dados['email'], hash_senha(dados['senha']), dados['perfil'],
                     dados['paciente_id'], (dados['cpf'] or None) if dados['perfil'] == 'responsavel' else None,
                     casa_id_atual())
                )
            except sqlite3.IntegrityError:
                if _email_de_usuario_excluido(conn, dados['email']):
                    flash('Este e-mail pertence a um usuário excluído. Restaure-o na aba "Excluídos".', 'erro')
                else:
                    flash('Erro: este e-mail já está cadastrado.', 'erro')
                return render_template('usuario_form.html', pacientes=pacientes, usuario=None, form=dados)
            log.info('usuario_criado', extra={'gc_action': 'novo_usuario', 'gc_user_id': session.get('usuario_id'),
                     'gc_alvo_id': cursor.lastrowid, 'gc_perfil': dados['perfil'], 'gc_casa_id': casa_id_atual()})
            flash(f'Usuário {dados["nome"]} criado com sucesso!', 'sucesso')
            return redirect(url_for('listar_usuarios'))
    return render_template('usuario_form.html', pacientes=pacientes, usuario=None, form=None)


@app.route('/usuarios/<int:usuario_id>/editar', methods=['GET', 'POST'])
@login_obrigatorio
@apenas_admin
def editar_usuario(usuario_id):
    # A própria conta é editada em "Meu perfil", que pede a senha atual para
    # trocar e-mail ou senha e não deixa o admin rebaixar o próprio perfil.
    if usuario_id == session.get('usuario_id'):
        return redirect(url_for('meu_perfil'))
    with get_db() as conn:
        usuario = _buscar_usuario_da_casa(conn, usuario_id)
        if not usuario:
            flash('Usuário não encontrado.', 'erro')
            return redirect(url_for('listar_usuarios'))
        pacientes = _pacientes_da_casa(conn)

        if request.method == 'POST':
            dados, erro = _ler_form_usuario(conn, exigir_senha=False)
            if erro:
                flash(erro, 'erro')
                return render_template('usuario_form.html', pacientes=pacientes, usuario=usuario, form=dados)

            cpf = (dados['cpf'] or None) if dados['perfil'] == 'responsavel' else usuario['cpf']
            novos = {'nome': dados['nome'], 'email': dados['email'], 'perfil': dados['perfil'],
                     'paciente_id': dados['paciente_id'], 'cpf': cpf}
            alterados = [campo for campo, valor in novos.items() if usuario[campo] != valor]
            if dados['senha']:
                novos['senha'] = hash_senha(dados['senha'])
                alterados.append('senha')
            if not alterados:
                flash('Nenhuma alteração para salvar.', 'sucesso')
                return redirect(url_for('listar_usuarios'))

            sets = ', '.join(f'{campo} = ?' for campo in novos)
            try:
                conn.execute(
                    f"UPDATE usuarios SET {sets}, atualizado_em = datetime('now') WHERE id = ? AND casa_id = ?",
                    (*novos.values(), usuario_id, casa_id_atual())
                )
            except sqlite3.IntegrityError:
                flash('Este e-mail já está em uso por outra conta.', 'erro')
                return render_template('usuario_form.html', pacientes=pacientes, usuario=usuario, form=dados)

            # Só os nomes dos campos vão para o log, nunca os valores.
            log.info('usuario_editado', extra={'gc_action': 'editar_usuario', 'gc_user_id': session.get('usuario_id'),
                     'gc_alvo_id': usuario_id, 'gc_campos': ','.join(alterados), 'gc_casa_id': casa_id_atual()})
            msg = f'Usuário {dados["nome"]} atualizado.'
            if 'perfil' in alterados:
                msg += ' A sessão dele foi encerrada e o novo perfil vale no próximo login.'
            flash(msg, 'sucesso')
            return redirect(url_for('listar_usuarios'))

    return render_template('usuario_form.html', pacientes=pacientes, usuario=usuario, form=None)


@app.route('/usuarios/<int:usuario_id>/toggle', methods=['POST'])
@login_obrigatorio
@apenas_admin
def toggle_usuario(usuario_id):
    if usuario_id == session.get('usuario_id'):
        flash('Você não pode desativar sua própria conta.', 'erro')
        return redirect(url_for('listar_usuarios'))
    with get_db() as conn:
        usuario = _buscar_usuario_da_casa(conn, usuario_id)
        if usuario:
            novo_status = 0 if usuario['ativo'] else 1
            conn.execute("UPDATE usuarios SET ativo = ?, atualizado_em = datetime('now') WHERE id = ?",
                         (novo_status, usuario_id))
            acao = 'ativado' if novo_status else 'desativado'
            log.info(f'usuario_{acao}', extra={'gc_action': 'toggle_usuario', 'gc_user_id': session.get('usuario_id'),
                     'gc_alvo_id': usuario_id, 'gc_casa_id': casa_id_atual()})
            flash(f'Usuário {usuario["nome"]} {acao}.', 'sucesso')
        else:
            flash('Usuário não encontrado.', 'erro')
    return redirect(url_for('listar_usuarios'))


@app.route('/usuarios/<int:usuario_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_admin
def excluir_usuario(usuario_id):
    """Exclusão reversível: o usuário perde o acesso na hora (a sessão dele cai
    no próximo clique) e vai para a aba "Excluídos", de onde pode ser restaurado."""
    if usuario_id == session.get('usuario_id'):
        flash('Você não pode excluir sua própria conta.', 'erro')
        return redirect(url_for('listar_usuarios'))
    with get_db() as conn:
        usuario = _buscar_usuario_da_casa(conn, usuario_id)
        if not usuario:
            flash('Usuário não encontrado.', 'erro')
            return redirect(url_for('listar_usuarios'))
        if usuario['perfil'] == 'admin':
            outros_admins = conn.execute(
                'SELECT COUNT(*) as total FROM usuarios WHERE casa_id = ? AND perfil = ? AND ativo = 1 '
                'AND excluido_em IS NULL AND id != ?',
                (casa_id_atual(), 'admin', usuario_id)
            ).fetchone()['total']
            if outros_admins == 0:
                flash('Não é possível excluir o único administrador da casa.', 'erro')
                return redirect(url_for('listar_usuarios'))
        conn.execute(
            "UPDATE usuarios SET ativo = 0, excluido_em = datetime('now'), excluido_por = ? "
            'WHERE id = ? AND casa_id = ?',
            (session.get('usuario_id'), usuario_id, casa_id_atual())
        )
        # Para de mandar notificações para o aparelho de quem foi excluído.
        conn.execute('DELETE FROM push_subscriptions WHERE usuario_id = ?', (usuario_id,))
        log.info('usuario_excluido', extra={'gc_action': 'excluir_usuario', 'gc_user_id': session.get('usuario_id'),
                 'gc_alvo_id': usuario_id, 'gc_casa_id': casa_id_atual()})
        flash(f'Usuário {usuario["nome"]} excluído. Se foi engano, restaure na aba "Excluídos".', 'sucesso')
    return redirect(url_for('listar_usuarios'))


@app.route('/usuarios/<int:usuario_id>/restaurar', methods=['POST'])
@login_obrigatorio
@apenas_admin
def restaurar_usuario(usuario_id):
    """Traz de volta um usuário excluído, com os mesmos dados e senha. Não cria
    sessão para ele: a pessoa precisa entrar de novo com e-mail e senha."""
    with get_db() as conn:
        usuario = _buscar_usuario_da_casa(conn, usuario_id, excluido=True)
        if not usuario:
            flash('Usuário não encontrado na lista de excluídos.', 'erro')
            return redirect(url_for('listar_usuarios', ver='excluidos'))
        conn.execute(
            "UPDATE usuarios SET ativo = 1, excluido_em = NULL, excluido_por = NULL, "
            "atualizado_em = datetime('now') WHERE id = ? AND casa_id = ?",
            (usuario_id, casa_id_atual())
        )
        log.info('usuario_restaurado', extra={'gc_action': 'restaurar_usuario', 'gc_user_id': session.get('usuario_id'),
                 'gc_alvo_id': usuario_id, 'gc_casa_id': casa_id_atual()})
        flash(f'Usuário {usuario["nome"]} restaurado e ativo de novo. Ele entra com o mesmo e-mail e senha.', 'sucesso')
    return redirect(url_for('listar_usuarios'))


@app.route('/usuarios/<int:usuario_id>/apagar', methods=['POST'])
@login_obrigatorio
@apenas_admin
def apagar_usuario_definitivo(usuario_id):
    """Apaga de vez um usuário que já está em "Excluídos" (ex.: pedido de
    eliminação de dados pela LGPD). Não tem volta."""
    with get_db() as conn:
        usuario = _buscar_usuario_da_casa(conn, usuario_id, excluido=True)
        if not usuario:
            flash('Só é possível apagar de vez um usuário que já está em "Excluídos".', 'erro')
            return redirect(url_for('listar_usuarios', ver='excluidos'))
        # Eliminação de dados (LGPD): leva junto as fotos de documento e de rosto.
        if usuario['perfil'] == 'responsavel':
            apagar_verificacoes_da_pessoa(conn, 'responsavel', usuario_id)
        conn.execute('DELETE FROM usuarios WHERE id = ? AND casa_id = ?', (usuario_id, casa_id_atual()))
        log.warning('usuario_apagado_definitivo', extra={'gc_action': 'apagar_usuario',
                    'gc_user_id': session.get('usuario_id'), 'gc_alvo_id': usuario_id,
                    'gc_casa_id': casa_id_atual()})
        flash(f'Usuário {usuario["nome"]} apagado definitivamente.', 'sucesso')
    return redirect(url_for('listar_usuarios', ver='excluidos'))


# ============================================================================
# PACIENTES
# ============================================================================

POR_PAGINA = 25
FAIXAS_IDADE = {'0-69': (0, 69), '70-79': (70, 79), '80-89': (80, 89), '90-200': (90, 200)}


def _norm(valor):
    """Minúsculas e sem acento, para a busca achar 'jose' em 'José'."""
    if valor is None:
        return ''
    txt = unicodedata.normalize('NFD', str(valor).lower())
    return ''.join(c for c in txt if unicodedata.category(c) != 'Mn')


def _padrao_like(texto):
    """Texto digitado vira padrão de busca 'contém', sem deixar % e _ virarem curinga."""
    t = _norm(texto).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return '%' + t + '%'


def _aniversario(ano, hoje):
    try:
        return date(ano, hoje.month, hoje.day)
    except ValueError:  # 29/02 em ano não bissexto
        return date(ano, hoje.month, 28)


def _filtros_pacientes(args):
    """Lê e valida os filtros da lista (GET). Devolve (filtros, SQL, parâmetros)."""
    f = {
        'q': (args.get('q') or '').strip()[:80],
        'quarto': (args.get('quarto') or '').strip()[:40],
        'condicao': (args.get('condicao') or '').strip()[:80],
        'idade': args.get('idade') if args.get('idade') in FAIXAS_IDADE else '',
    }
    where, params = ['p.ativo = 1', 'p.casa_id = ?'], [casa_id_atual()]
    if f['q']:
        where.append("norm(p.nome) LIKE ? ESCAPE '\\'")
        params.append(_padrao_like(f['q']))
    if f['quarto']:
        where.append("norm(COALESCE(p.quarto, '')) = ?")
        params.append(_norm(f['quarto']))
    if f['condicao']:
        where.append('EXISTS (SELECT 1 FROM condicoes c WHERE c.paciente_id = p.id AND norm(c.nome) = ?)')
        params.append(_norm(f['condicao']))
    if f['idade']:
        minimo, maximo = FAIXAS_IDADE[f['idade']]
        hoje = date.today()
        # idade entre minimo e maximo = nascido depois de (hoje - maximo - 1 anos) e até (hoje - minimo anos)
        where.append('p.data_nascimento <= ? AND p.data_nascimento > ?')
        params.append(_aniversario(hoje.year - minimo, hoje).isoformat())
        params.append(_aniversario(hoje.year - maximo - 1, hoje).isoformat())
    return f, ' AND '.join(where), params


@app.route('/')
@login_obrigatorio
@apenas_equipe
def index():
    casa = casa_id_atual()
    with get_db() as conn:
        conn.create_function('norm', 1, _norm, deterministic=True)
        filtros, where, params = _filtros_pacientes(request.args)

        total_casa = conn.execute(
            'SELECT COUNT(*) FROM pacientes WHERE ativo = 1 AND casa_id = ?', (casa,)
        ).fetchone()[0]
        total = conn.execute(
            f'SELECT COUNT(*) FROM pacientes p WHERE {where}', params
        ).fetchone()[0]

        paginas = max(1, -(-total // POR_PAGINA))
        try:
            pagina = int(request.args.get('pagina', 1))
        except ValueError:
            pagina = 1
        pagina = min(max(pagina, 1), paginas)

        pacientes = conn.execute(f'''
            SELECT p.*,
                   (SELECT COUNT(*) FROM medicamentos WHERE paciente_id = p.id AND ativo = 1) AS total_medicamentos,
                   (SELECT COUNT(*) FROM condicoes WHERE paciente_id = p.id) AS total_condicoes
            FROM pacientes p WHERE {where}
            ORDER BY p.nome COLLATE NOCASE, p.id
            LIMIT ? OFFSET ?
        ''', params + [POR_PAGINA, (pagina - 1) * POR_PAGINA]).fetchall()
        pacientes_list = []
        for p in pacientes:
            p_dict = dict(p)
            p_dict['idade'] = calcular_idade(p_dict['data_nascimento'])
            pacientes_list.append(p_dict)

        quartos = [r[0] for r in conn.execute(
            "SELECT DISTINCT TRIM(quarto) FROM pacientes "
            "WHERE ativo = 1 AND casa_id = ? AND TRIM(COALESCE(quarto, '')) != ''", (casa,)).fetchall()]
        quartos_lista = sorted(quartos, key=lambda q: (len(q), q))
        condicoes_lista = sorted({r[0].strip() for r in conn.execute(
            'SELECT DISTINCT c.nome FROM condicoes c JOIN pacientes p ON p.id = c.paciente_id '
            'WHERE p.ativo = 1 AND p.casa_id = ?', (casa,)).fetchall() if r[0] and r[0].strip()},
            key=str.lower)

        antibioticos_raw = conn.execute('''
            SELECT m.id, m.nome, m.data_fim_tratamento, p.id as paciente_id, p.nome as paciente_nome
            FROM medicamentos m
            JOIN pacientes p ON m.paciente_id = p.id
            WHERE m.ativo = 1 AND p.ativo = 1 AND m.eh_antibiotico = 1 AND p.casa_id = ?
              AND m.data_fim_tratamento IS NOT NULL
              AND date(m.data_fim_tratamento) <= date('now', 'localtime', '+2 days')
            ORDER BY m.data_fim_tratamento
        ''', (casa,)).fetchall()
        alertas_antibiotico = []
        for a in antibioticos_raw:
            a_dict = dict(a)
            try:
                fim = datetime.strptime(a_dict['data_fim_tratamento'], '%Y-%m-%d').date()
                a_dict['dias_restantes'] = (fim - date.today()).days
            except ValueError:
                a_dict['dias_restantes'] = None
            alertas_antibiotico.append(a_dict)

        agenda_hoje_total = conn.execute('''
            SELECT COUNT(*) FROM horarios_medicamento h
            JOIN medicamentos m ON h.medicamento_id = m.id
            JOIN pacientes p ON m.paciente_id = p.id
            WHERE m.ativo = 1 AND p.ativo = 1 AND p.casa_id = ?
        ''', (casa,)).fetchone()[0]

        equipe_ativa_total = conn.execute('''
            SELECT COUNT(*) FROM usuarios
            WHERE casa_id = ? AND ativo = 1 AND perfil != 'responsavel' AND excluido_em IS NULL
        ''', (casa,)).fetchone()[0]
    filtros_ativos = {k: v for k, v in filtros.items() if v}
    return render_template('index.html', pacientes=pacientes_list,
                           alertas_antibiotico=alertas_antibiotico,
                           agenda_hoje_total=agenda_hoje_total,
                           equipe_ativa_total=equipe_ativa_total,
                           quartos_lista=quartos_lista,
                           condicoes_lista=condicoes_lista,
                           filtros=filtros, filtros_ativos=filtros_ativos,
                           total=total, total_casa=total_casa,
                           pagina=pagina, paginas=paginas, por_pagina=POR_PAGINA)


RECENTES_MAX = 6


def _registrar_recente(paciente_id):
    lista = [i for i in session.get('recentes', []) if i != paciente_id]
    session['recentes'] = ([paciente_id] + lista)[:RECENTES_MAX]


@app.route('/busca')
@login_obrigatorio
@apenas_equipe
def busca_pacientes():
    """Busca rápida (JSON) para o campo do topo e a tecla '/'. Sem texto, devolve os
    últimos pacientes abertos por esta pessoa."""
    q = (request.args.get('q') or '').strip()[:80]
    casa = casa_id_atual()
    with get_db() as conn:
        conn.create_function('norm', 1, _norm, deterministic=True)
        if len(q) >= 2:
            linhas = conn.execute(
                'SELECT id, nome, quarto FROM pacientes '
                "WHERE ativo = 1 AND casa_id = ? AND norm(nome) LIKE ? ESCAPE '\\' "
                'ORDER BY nome COLLATE NOCASE LIMIT 8',
                (casa, _padrao_like(q))).fetchall()
            recentes = False
        elif q:
            linhas, recentes = [], False
        else:
            ids = [i for i in session.get('recentes', []) if isinstance(i, int)]
            linhas = []
            if ids:
                marcas = ','.join('?' * len(ids))
                achados = {r['id']: r for r in conn.execute(
                    f'SELECT id, nome, quarto FROM pacientes '
                    f'WHERE ativo = 1 AND casa_id = ? AND id IN ({marcas})', [casa] + ids).fetchall()}
                linhas = [achados[i] for i in ids if i in achados]
            recentes = True
    return jsonify(recentes=recentes, resultados=[
        {'id': r['id'], 'nome': r['nome'], 'quarto': r['quarto'] or '',
         'url': url_for('detalhe_paciente', paciente_id=r['id'])} for r in linhas])


@app.route('/paciente/novo', methods=['GET', 'POST'])
@login_obrigatorio
@apenas_equipe
def novo_paciente():
    if request.method == 'POST':
        dados = request.form
        cpf = (dados.get('cpf') or '').strip()
        if cpf and not cpf_valido(cpf):
            flash('CPF inválido. Confira os números digitados.', 'erro')
            return redirect(url_for('novo_paciente'))
        cpf = formatar_cpf(cpf) if cpf else None
        with get_db() as conn:
            cursor = conn.execute('''
                INSERT INTO pacientes (
                    nome, data_nascimento, sexo, cpf, rg, telefone, quarto,
                    contato_emergencia_nome, contato_emergencia_telefone,
                    contato_emergencia_parentesco, observacoes, casa_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                dados.get('nome'), dados.get('data_nascimento'), dados.get('sexo'),
                cpf, dados.get('rg'), dados.get('telefone'),
                dados.get('quarto'), dados.get('contato_emergencia_nome'),
                dados.get('contato_emergencia_telefone'),
                dados.get('contato_emergencia_parentesco'), dados.get('observacoes'),
                casa_id_atual()
            ))
            paciente_id = cursor.lastrowid

            # Dieta
            dieta_fields = ['peso', 'altura', 'comorbidades', 'dieta_especifica',
                            'consistencia_alimento', 'restricoes_alimentares',
                            'alergias_alimentares', 'observacoes_nutricao']
            if any(dados.get(f) for f in dieta_fields):
                conn.execute('''
                    INSERT INTO comorbidades_dieta
                        (paciente_id, peso, altura, comorbidades, dieta_especifica,
                         consistencia_alimento, restricoes_alimentares,
                         alergias_alimentares, observacoes_nutricao)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    paciente_id, dados.get('peso') or None, dados.get('altura') or None,
                    dados.get('comorbidades'), dados.get('dieta_especifica'),
                    dados.get('consistencia_alimento'), dados.get('restricoes_alimentares'),
                    dados.get('alergias_alimentares'), dados.get('observacoes_nutricao'),
                ))

            # Medicamentos
            idx = 0
            while dados.get(f'med_nome_{idx}'):
                nome       = dados.get(f'med_nome_{idx}', '').strip()
                dosagem    = dados.get(f'med_dosagem_{idx}', '')
                via        = dados.get(f'med_via_{idx}', '')
                instrucoes = dados.get(f'med_instrucoes_{idx}', '')
                freq_tipo  = dados.get(f'med_freq_tipo_{idx}', 'diaria')
                doses_dia  = dados.get(f'med_doses_dia_{idx}', '1')
                intervalo  = dados.get(f'med_intervalo_{idx}', '')
                dias_sem   = request.form.getlist(f'med_dias_{idx}[]')
                horarios   = request.form.getlist(f'med_horario_{idx}[]')
                info_freq  = _montar_info_frequencia(freq_tipo, doses_dia, intervalo, dias_sem)
                instrucoes_completas = f"{info_freq}\n{instrucoes}".strip() if instrucoes else info_freq
                if nome:
                    cur2 = conn.execute('''
                        INSERT INTO medicamentos
                            (paciente_id, nome, dosagem, via_administracao, instrucoes)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (paciente_id, nome, dosagem, via, instrucoes_completas))
                    med_id = cur2.lastrowid
                    for h in horarios:
                        h = h.strip()
                        if h:
                            conn.execute(
                                'INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (?, ?)',
                                (med_id, h)
                            )
                idx += 1

            # Exames
            exame_idx = 0
            while dados.get(f'exame_tipo_{exame_idx}'):
                tipo_exame = dados.get(f'exame_tipo_{exame_idx}', '').strip()
                if tipo_exame:
                    arquivo_exame = request.files.get(f'exame_arquivo_{exame_idx}')
                    arquivo_nome_exame = salvar_arquivo_exame(arquivo_exame)
                    conn.execute('''
                        INSERT INTO exames
                            (paciente_id, tipo_exame, data_exame, laboratorio,
                             medico_solicitante, resultado, valores_referencia, arquivo_nome, observacoes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        paciente_id, tipo_exame,
                        dados.get(f'exame_data_{exame_idx}') or date.today().isoformat(),
                        dados.get(f'exame_laboratorio_{exame_idx}', ''),
                        dados.get(f'exame_medico_{exame_idx}', ''),
                        dados.get(f'exame_resultado_{exame_idx}', ''),
                        dados.get(f'exame_valores_{exame_idx}', ''),
                        arquivo_nome_exame,
                        dados.get(f'exame_obs_{exame_idx}', '')
                    ))
                exame_idx += 1

            # Ficha psicológica
            psico_fields = ['antecedentes', 'diagnosticos_mentais', 'medicacao_psiquiatrica',
                            'comportamento', 'observacoes_psico']
            if any(dados.get(f) for f in psico_fields):
                conn.execute('''
                    INSERT INTO ficha_psicologica
                        (paciente_id, antecedentes, diagnosticos_mentais,
                         medicacao_psiquiatrica, comportamento, observacoes)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    paciente_id, dados.get('antecedentes'), dados.get('diagnosticos_mentais'),
                    dados.get('medicacao_psiquiatrica'), dados.get('comportamento'),
                    dados.get('observacoes_psico')
                ))

            # Profissionais
            prof_idx = 0
            while dados.get(f'prof_nome_{prof_idx}'):
                prof_nome = dados.get(f'prof_nome_{prof_idx}', '').strip()
                if prof_nome:
                    conn.execute('''
                        INSERT INTO profissionais_consultados
                            (paciente_id, nome, especialidade, data_consulta, observacoes)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        paciente_id, prof_nome,
                        dados.get(f'prof_especialidade_{prof_idx}', ''),
                        dados.get(f'prof_data_{prof_idx}') or None,
                        dados.get(f'prof_obs_{prof_idx}', '')
                    ))
                prof_idx += 1

        log.info('paciente_criado', extra={'gc_action': 'novo_paciente',
                 'gc_paciente_id': paciente_id, 'gc_casa_id': casa_id_atual(),
                 'gc_user_id': session.get('usuario_id')})
        flash('Paciente cadastrado com sucesso!', 'sucesso')
        return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))
    return render_template('paciente_form.html', paciente=None, medicamentos=[],
                           dieta=None, ficha_psico=None, profissionais=[])


def _montar_info_frequencia(tipo, doses_dia, intervalo, dias_semana):
    dias_nomes = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb']
    if tipo == 'condicional':
        return 'Frequência: Uso condicional (SOS) — administrar apenas se necessário'
    if tipo == 'semanal' and dias_semana:
        nomes = [dias_nomes[int(d)] for d in dias_semana if d.isdigit()]
        return f"Frequência: Semanal ({', '.join(nomes)})"
    if tipo == 'intervalo' and intervalo:
        return f"Frequência: A cada {intervalo} dias"
    doses_txt = {
        '1': '1 vez ao dia', '2': '2 vezes ao dia',
        '3': '3 vezes ao dia', '4': '4 vezes ao dia',
    }.get(doses_dia, f'{doses_dia}x ao dia')
    return f"Frequência: Diária — {doses_txt}"


@app.route('/paciente/<int:paciente_id>')
@login_obrigatorio
@apenas_equipe
def detalhe_paciente(paciente_id):
    with get_db() as conn:
        paciente = buscar_paciente_da_casa(conn, paciente_id)
        if not paciente:
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        _registrar_recente(paciente_id)
        paciente_dict = dict(paciente)
        paciente_dict['idade'] = calcular_idade(paciente_dict['data_nascimento'])

        condicoes = conn.execute(
            'SELECT * FROM condicoes WHERE paciente_id = ? ORDER BY data_diagnostico DESC',
            (paciente_id,)
        ).fetchall()

        medicamentos_raw = conn.execute(
            'SELECT * FROM medicamentos WHERE paciente_id = ? AND ativo = 1 ORDER BY nome',
            (paciente_id,)
        ).fetchall()
        medicamentos = []
        for m in medicamentos_raw:
            med_dict = dict(m)
            horarios = conn.execute(
                'SELECT horario FROM horarios_medicamento WHERE medicamento_id = ? ORDER BY horario',
                (m['id'],)
            ).fetchall()
            med_dict['horarios'] = [h['horario'] for h in horarios]

            ultimas_doses = conn.execute(
                'SELECT * FROM doses_administradas WHERE medicamento_id = ? ORDER BY horario_administrado DESC LIMIT 5',
                (m['id'],)
            ).fetchall()
            med_dict['ultimas_doses'] = [dict(d) for d in ultimas_doses]

            doses_hoje = conn.execute('''
                SELECT COUNT(*) as total FROM doses_administradas
                WHERE medicamento_id = ? AND date(horario_administrado) = date('now', 'localtime')
            ''', (m['id'],)).fetchone()['total']
            med_dict['doses_hoje'] = doses_hoje

            if med_dict.get('eh_antibiotico') and med_dict.get('data_fim_tratamento'):
                try:
                    fim = datetime.strptime(med_dict['data_fim_tratamento'], '%Y-%m-%d').date()
                    med_dict['dias_restantes_antibiotico'] = (fim - date.today()).days
                except ValueError:
                    med_dict['dias_restantes_antibiotico'] = None
            else:
                med_dict['dias_restantes_antibiotico'] = None

            medicamentos.append(med_dict)

        ficha_psico = conn.execute(
            'SELECT * FROM ficha_psicologica WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()

        profissionais = conn.execute(
            'SELECT * FROM profissionais_consultados WHERE paciente_id = ? ORDER BY data_consulta DESC',
            (paciente_id,)
        ).fetchall()

        dieta = conn.execute(
            'SELECT * FROM comorbidades_dieta WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()

        total_visitas = conn.execute(
            'SELECT COUNT(*) as total FROM visitas WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()['total']

        total_exames = conn.execute(
            'SELECT COUNT(*) as total FROM exames WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()['total']

        total_evolucoes = conn.execute(
            'SELECT COUNT(*) as total FROM evolucoes_medicas WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()['total']

        # Documentos e verificação: do idoso e dos responsáveis vinculados a ele.
        foto_rosto = foto_rosto_paciente(conn, paciente_id)
        verificacoes_idoso = _verificacoes_da_pessoa(conn, 'paciente', paciente_id)
        responsaveis = []
        for r in conn.execute(
                "SELECT id, nome FROM usuarios WHERE casa_id = ? AND perfil = 'responsavel' "
                "AND paciente_id = ? AND excluido_em IS NULL ORDER BY nome",
                (casa_id_atual(), paciente_id)).fetchall():
            ultima = conn.execute(
                "SELECT * FROM verificacoes_identidade WHERE casa_id = ? AND "
                "tipo_pessoa = 'responsavel' AND pessoa_id = ? ORDER BY id DESC LIMIT 1",
                (casa_id_atual(), r['id'])).fetchone()
            responsaveis.append({'id': r['id'], 'nome': r['nome'], 'verificacao': ultima})

    return render_template(
        'paciente_detalhe.html',
        paciente=paciente_dict, condicoes=condicoes, medicamentos=medicamentos,
        ficha_psico=ficha_psico, profissionais=profissionais, dieta=dieta,
        total_visitas=total_visitas, total_exames=total_exames,
        total_evolucoes=total_evolucoes,
        foto_rosto=foto_rosto, verificacoes_idoso=verificacoes_idoso,
        responsaveis_vinculados=responsaveis,
    )


@app.route('/paciente/<int:paciente_id>/editar', methods=['GET', 'POST'])
@login_obrigatorio
@apenas_equipe
def editar_paciente(paciente_id):
    with get_db() as conn:
        paciente_existente = buscar_paciente_da_casa(conn, paciente_id)
        if not paciente_existente:
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))

        if request.method == 'POST':
            dados = request.form
            cpf = (dados.get('cpf') or '').strip()
            if cpf and not cpf_valido(cpf):
                flash('CPF inválido. Confira os números digitados.', 'erro')
                return redirect(url_for('editar_paciente', paciente_id=paciente_id))
            cpf = formatar_cpf(cpf) if cpf else None
            conn.execute('''
                UPDATE pacientes SET
                    nome = ?, data_nascimento = ?, sexo = ?, cpf = ?, rg = ?,
                    telefone = ?, quarto = ?, contato_emergencia_nome = ?,
                    contato_emergencia_telefone = ?, contato_emergencia_parentesco = ?,
                    observacoes = ?
                WHERE id = ?
            ''', (
                dados.get('nome'), dados.get('data_nascimento'), dados.get('sexo'),
                cpf, dados.get('rg'), dados.get('telefone'),
                dados.get('quarto'), dados.get('contato_emergencia_nome'),
                dados.get('contato_emergencia_telefone'),
                dados.get('contato_emergencia_parentesco'),
                dados.get('observacoes'), paciente_id
            ))

            # Dieta
            existente = conn.execute(
                'SELECT id FROM comorbidades_dieta WHERE paciente_id = ?', (paciente_id,)
            ).fetchone()
            if existente:
                conn.execute('''
                    UPDATE comorbidades_dieta SET
                        peso = ?, altura = ?, comorbidades = ?,
                        dieta_especifica = ?, consistencia_alimento = ?,
                        restricoes_alimentares = ?, alergias_alimentares = ?,
                        observacoes_nutricao = ?, atualizado_em = datetime('now')
                    WHERE paciente_id = ?
                ''', (
                    dados.get('peso') or None, dados.get('altura') or None,
                    dados.get('comorbidades'), dados.get('dieta_especifica'),
                    dados.get('consistencia_alimento'), dados.get('restricoes_alimentares'),
                    dados.get('alergias_alimentares'), dados.get('observacoes_nutricao'),
                    paciente_id
                ))
            else:
                dieta_fields = ['peso', 'altura', 'comorbidades', 'dieta_especifica',
                                'consistencia_alimento', 'restricoes_alimentares',
                                'alergias_alimentares', 'observacoes_nutricao']
                if any(dados.get(f) for f in dieta_fields):
                    conn.execute('''
                        INSERT INTO comorbidades_dieta
                            (paciente_id, peso, altura, comorbidades, dieta_especifica,
                             consistencia_alimento, restricoes_alimentares,
                             alergias_alimentares, observacoes_nutricao)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        paciente_id, dados.get('peso') or None, dados.get('altura') or None,
                        dados.get('comorbidades'), dados.get('dieta_especifica'),
                        dados.get('consistencia_alimento'), dados.get('restricoes_alimentares'),
                        dados.get('alergias_alimentares'), dados.get('observacoes_nutricao'),
                    ))

            # Ficha psicológica
            psico_existente = conn.execute(
                'SELECT id FROM ficha_psicologica WHERE paciente_id = ?', (paciente_id,)
            ).fetchone()
            if psico_existente:
                conn.execute('''
                    UPDATE ficha_psicologica SET
                        antecedentes = ?, diagnosticos_mentais = ?,
                        medicacao_psiquiatrica = ?, comportamento = ?,
                        observacoes = ?, atualizado_em = datetime('now')
                    WHERE paciente_id = ?
                ''', (
                    dados.get('antecedentes'), dados.get('diagnosticos_mentais'),
                    dados.get('medicacao_psiquiatrica'), dados.get('comportamento'),
                    dados.get('observacoes_psico'), paciente_id
                ))
            else:
                psico_fields = ['antecedentes', 'diagnosticos_mentais', 'medicacao_psiquiatrica',
                                'comportamento', 'observacoes_psico']
                if any(dados.get(f) for f in psico_fields):
                    conn.execute('''
                        INSERT INTO ficha_psicologica
                            (paciente_id, antecedentes, diagnosticos_mentais,
                             medicacao_psiquiatrica, comportamento, observacoes)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        paciente_id, dados.get('antecedentes'), dados.get('diagnosticos_mentais'),
                        dados.get('medicacao_psiquiatrica'), dados.get('comportamento'),
                        dados.get('observacoes_psico')
                    ))

            # Profissionais — remove e reinsere
            conn.execute(
                'DELETE FROM profissionais_consultados WHERE paciente_id = ?', (paciente_id,)
            )
            prof_idx = 0
            while dados.get(f'prof_nome_{prof_idx}'):
                prof_nome = dados.get(f'prof_nome_{prof_idx}', '').strip()
                if prof_nome:
                    conn.execute('''
                        INSERT INTO profissionais_consultados
                            (paciente_id, nome, especialidade, data_consulta, observacoes)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        paciente_id, prof_nome,
                        dados.get(f'prof_especialidade_{prof_idx}', ''),
                        dados.get(f'prof_data_{prof_idx}') or None,
                        dados.get(f'prof_obs_{prof_idx}', '')
                    ))
                prof_idx += 1

            # Exames novos (os já existentes continuam gerenciáveis na aba Exames)
            exame_idx = 0
            while dados.get(f'exame_tipo_{exame_idx}'):
                tipo_exame = dados.get(f'exame_tipo_{exame_idx}', '').strip()
                if tipo_exame:
                    arquivo_exame = request.files.get(f'exame_arquivo_{exame_idx}')
                    arquivo_nome_exame = salvar_arquivo_exame(arquivo_exame)
                    conn.execute('''
                        INSERT INTO exames
                            (paciente_id, tipo_exame, data_exame, laboratorio,
                             medico_solicitante, resultado, valores_referencia, arquivo_nome, observacoes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        paciente_id, tipo_exame,
                        dados.get(f'exame_data_{exame_idx}') or date.today().isoformat(),
                        dados.get(f'exame_laboratorio_{exame_idx}', ''),
                        dados.get(f'exame_medico_{exame_idx}', ''),
                        dados.get(f'exame_resultado_{exame_idx}', ''),
                        dados.get(f'exame_valores_{exame_idx}', ''),
                        arquivo_nome_exame,
                        dados.get(f'exame_obs_{exame_idx}', '')
                    ))
                exame_idx += 1

            flash('Dados atualizados com sucesso!', 'sucesso')
            return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))

        paciente = paciente_existente

        medicamentos_raw = conn.execute(
            'SELECT * FROM medicamentos WHERE paciente_id = ? AND ativo = 1 ORDER BY nome',
            (paciente_id,)
        ).fetchall()
        medicamentos = []
        for m in medicamentos_raw:
            med_dict = dict(m)
            horarios = conn.execute(
                'SELECT horario FROM horarios_medicamento WHERE medicamento_id = ? ORDER BY horario',
                (m['id'],)
            ).fetchall()
            med_dict['horarios'] = [h['horario'] for h in horarios]
            medicamentos.append(med_dict)

        dieta = conn.execute(
            'SELECT * FROM comorbidades_dieta WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()
        ficha_psico = conn.execute(
            'SELECT * FROM ficha_psicologica WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()
        profissionais = conn.execute(
            'SELECT * FROM profissionais_consultados WHERE paciente_id = ? ORDER BY data_consulta DESC',
            (paciente_id,)
        ).fetchall()

    return render_template('paciente_form.html', paciente=dict(paciente),
                           medicamentos=medicamentos, dieta=dieta,
                           ficha_psico=ficha_psico, profissionais=profissionais)


@app.route('/paciente/<int:paciente_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_paciente(paciente_id):
    with get_db() as conn:
        conn.execute(
            'UPDATE pacientes SET ativo = 0 WHERE id = ? AND casa_id = ?',
            (paciente_id, casa_id_atual())
        )
    flash('Paciente removido com sucesso.', 'sucesso')
    return redirect(url_for('index'))


# ============================================================================
# CONDIÇÕES
# ============================================================================

@app.route('/paciente/<int:paciente_id>/condicao/nova', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def nova_condicao(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        conn.execute('''
            INSERT INTO condicoes (paciente_id, nome, descricao, gravidade, data_diagnostico)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            paciente_id, dados.get('nome'), dados.get('descricao'),
            dados.get('gravidade'), dados.get('data_diagnostico') or None
        ))
    flash('Condição adicionada com sucesso!', 'sucesso')
    return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))


@app.route('/condicao/<int:condicao_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_condicao(condicao_id):
    with get_db() as conn:
        cond = conn.execute('''
            SELECT c.paciente_id FROM condicoes c
            JOIN pacientes p ON c.paciente_id = p.id
            WHERE c.id = ? AND p.casa_id = ?
        ''', (condicao_id, casa_id_atual())).fetchone()
        if cond:
            paciente_id = cond['paciente_id']
            conn.execute('DELETE FROM condicoes WHERE id = ?', (condicao_id,))
            flash('Condição removida.', 'sucesso')
            return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))
    return redirect(url_for('index'))


# ============================================================================
# MEDICAMENTOS
# ============================================================================

@app.route('/paciente/<int:paciente_id>/medicamento/novo', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def novo_medicamento(paciente_id):
    dados = request.form
    horarios = request.form.getlist('horarios[]')
    eh_antibiotico = 1 if dados.get('eh_antibiotico') else 0
    data_fim_tratamento = dados.get('data_fim_tratamento') or None
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        cursor = conn.execute('''
            INSERT INTO medicamentos
                (paciente_id, nome, dosagem, via_administracao, instrucoes,
                 eh_antibiotico, data_fim_tratamento)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            paciente_id, dados.get('nome'), dados.get('dosagem'),
            dados.get('via_administracao'), dados.get('instrucoes'),
            eh_antibiotico, data_fim_tratamento if eh_antibiotico else None
        ))
        medicamento_id = cursor.lastrowid
        for h in horarios:
            if h.strip():
                conn.execute(
                    'INSERT INTO horarios_medicamento (medicamento_id, horario) VALUES (?, ?)',
                    (medicamento_id, h)
                )
    flash('Medicamento cadastrado com sucesso!', 'sucesso')
    return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))


@app.route('/medicamento/<int:medicamento_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_medicamento(medicamento_id):
    with get_db() as conn:
        med = conn.execute('''
            SELECT m.paciente_id FROM medicamentos m
            JOIN pacientes p ON m.paciente_id = p.id
            WHERE m.id = ? AND p.casa_id = ?
        ''', (medicamento_id, casa_id_atual())).fetchone()
        if med:
            paciente_id = med['paciente_id']
            conn.execute('UPDATE medicamentos SET ativo = 0 WHERE id = ?', (medicamento_id,))
            flash('Medicamento removido.', 'sucesso')
            return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))
    return redirect(url_for('index'))


@app.route('/medicamento/<int:medicamento_id>/administrar', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def administrar_dose(medicamento_id):
    dados = request.form
    administrado_por = session.get('usuario_nome', dados.get('administrado_por', ''))
    with get_db() as conn:
        med = conn.execute('''
            SELECT m.paciente_id FROM medicamentos m
            JOIN pacientes p ON m.paciente_id = p.id
            WHERE m.id = ? AND p.casa_id = ?
        ''', (medicamento_id, casa_id_atual())).fetchone()
        if not med:
            flash('Medicamento não encontrado.', 'erro')
            return redirect(url_for('index'))
        conn.execute('''
            INSERT INTO doses_administradas
                (medicamento_id, horario_previsto, administrado_por, observacoes)
            VALUES (?, ?, ?, ?)
        ''', (
            medicamento_id,
            dados.get('horario_previsto') or datetime.now().isoformat(),
            administrado_por, dados.get('observacoes')
        ))
    flash(f'Dose registrada por {administrado_por}!', 'sucesso')
    return redirect(url_for('detalhe_paciente', paciente_id=med['paciente_id']))


# ============================================================================
# FICHA PSICOLÓGICA
# ============================================================================

@app.route('/paciente/<int:paciente_id>/psicologico/salvar', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def salvar_psicologico(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        existente = conn.execute(
            'SELECT id FROM ficha_psicologica WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()
        if existente:
            conn.execute('''
                UPDATE ficha_psicologica SET
                    antecedentes = ?, diagnosticos_mentais = ?,
                    medicacao_psiquiatrica = ?, comportamento = ?,
                    observacoes = ?, atualizado_em = datetime('now')
                WHERE paciente_id = ?
            ''', (
                dados.get('antecedentes'), dados.get('diagnosticos_mentais'),
                dados.get('medicacao_psiquiatrica'), dados.get('comportamento'),
                dados.get('observacoes'), paciente_id
            ))
        else:
            conn.execute('''
                INSERT INTO ficha_psicologica
                    (paciente_id, antecedentes, diagnosticos_mentais,
                     medicacao_psiquiatrica, comportamento, observacoes)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                paciente_id, dados.get('antecedentes'), dados.get('diagnosticos_mentais'),
                dados.get('medicacao_psiquiatrica'), dados.get('comportamento'),
                dados.get('observacoes')
            ))
    flash('Ficha psicológica salva com sucesso!', 'sucesso')
    return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))


@app.route('/paciente/<int:paciente_id>/profissional/novo', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def novo_profissional(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        conn.execute('''
            INSERT INTO profissionais_consultados
                (paciente_id, nome, especialidade, data_consulta, observacoes)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            paciente_id, dados.get('nome'), dados.get('especialidade'),
            dados.get('data_consulta') or None, dados.get('observacoes')
        ))
    flash('Profissional adicionado!', 'sucesso')
    return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))


@app.route('/profissional/<int:prof_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_profissional(prof_id):
    with get_db() as conn:
        prof = conn.execute('''
            SELECT pc.paciente_id FROM profissionais_consultados pc
            JOIN pacientes p ON pc.paciente_id = p.id
            WHERE pc.id = ? AND p.casa_id = ?
        ''', (prof_id, casa_id_atual())).fetchone()
        if prof:
            conn.execute('DELETE FROM profissionais_consultados WHERE id = ?', (prof_id,))
            flash('Profissional removido.', 'sucesso')
            return redirect(url_for('detalhe_paciente', paciente_id=prof['paciente_id']))
    return redirect(url_for('index'))


# ============================================================================
# COMORBIDADES E DIETA
# ============================================================================

@app.route('/paciente/<int:paciente_id>/dieta/salvar', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def salvar_dieta(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        existente = conn.execute(
            'SELECT id FROM comorbidades_dieta WHERE paciente_id = ?', (paciente_id,)
        ).fetchone()
        if existente:
            conn.execute('''
                UPDATE comorbidades_dieta SET
                    peso = ?, altura = ?, comorbidades = ?,
                    restricoes_alimentares = ?, dieta_especifica = ?,
                    consistencia_alimento = ?, alergias_alimentares = ?,
                    observacoes_nutricao = ?, atualizado_em = datetime('now')
                WHERE paciente_id = ?
            ''', (
                dados.get('peso') or None, dados.get('altura') or None,
                dados.get('comorbidades'), dados.get('restricoes_alimentares'),
                dados.get('dieta_especifica'), dados.get('consistencia_alimento'),
                dados.get('alergias_alimentares'), dados.get('observacoes_nutricao'),
                paciente_id
            ))
        else:
            conn.execute('''
                INSERT INTO comorbidades_dieta
                    (paciente_id, peso, altura, comorbidades, restricoes_alimentares,
                     dieta_especifica, consistencia_alimento,
                     alergias_alimentares, observacoes_nutricao)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                paciente_id, dados.get('peso') or None, dados.get('altura') or None,
                dados.get('comorbidades'), dados.get('restricoes_alimentares'),
                dados.get('dieta_especifica'), dados.get('consistencia_alimento'),
                dados.get('alergias_alimentares'), dados.get('observacoes_nutricao')
            ))
    flash('Informações de dieta salvas com sucesso!', 'sucesso')
    return redirect(url_for('detalhe_paciente', paciente_id=paciente_id))


# ============================================================================
# EVOLUÇÃO MÉDICA
# ============================================================================

@app.route('/paciente/<int:paciente_id>/evolucoes')
@login_obrigatorio
@apenas_equipe
def listar_evolucoes(paciente_id):
    with get_db() as conn:
        paciente = buscar_paciente_da_casa(conn, paciente_id)
        if not paciente:
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        evolucoes = conn.execute(
            'SELECT * FROM evolucoes_medicas WHERE paciente_id = ? ORDER BY data_evolucao DESC, id DESC',
            (paciente_id,)
        ).fetchall()
    return render_template('evolucoes.html', paciente=dict(paciente), evolucoes=evolucoes,
                           hoje=date.today().isoformat())


@app.route('/paciente/<int:paciente_id>/evolucao/nova', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def nova_evolucao(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        conn.execute('''
            INSERT INTO evolucoes_medicas
                (paciente_id, data_evolucao, profissional, especialidade, descricao)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            paciente_id, dados.get('data_evolucao') or date.today().isoformat(),
            dados.get('profissional') or session.get('usuario_nome', ''),
            dados.get('especialidade'), dados.get('descricao')
        ))
    flash('Evolução médica registrada com sucesso!', 'sucesso')
    return redirect(url_for('listar_evolucoes', paciente_id=paciente_id))


@app.route('/evolucao/<int:evolucao_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_evolucao(evolucao_id):
    with get_db() as conn:
        evolucao = conn.execute('''
            SELECT e.paciente_id FROM evolucoes_medicas e
            JOIN pacientes p ON e.paciente_id = p.id
            WHERE e.id = ? AND p.casa_id = ?
        ''', (evolucao_id, casa_id_atual())).fetchone()
        if evolucao:
            conn.execute('DELETE FROM evolucoes_medicas WHERE id = ?', (evolucao_id,))
            flash('Registro de evolução removido.', 'sucesso')
            return redirect(url_for('listar_evolucoes', paciente_id=evolucao['paciente_id']))
    return redirect(url_for('index'))


# ============================================================================
# VISITAS
# ============================================================================

@app.route('/paciente/<int:paciente_id>/visitas')
@login_obrigatorio
@apenas_equipe
def listar_visitas(paciente_id):
    with get_db() as conn:
        paciente = buscar_paciente_da_casa(conn, paciente_id)
        if not paciente:
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        visitas = conn.execute(
            'SELECT * FROM visitas WHERE paciente_id = ? ORDER BY data_visita DESC, hora_entrada DESC',
            (paciente_id,)
        ).fetchall()
    return render_template('visitas.html', paciente=dict(paciente), visitas=visitas,
                           hoje=date.today().isoformat())


@app.route('/paciente/<int:paciente_id>/visita/nova', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def nova_visita(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        conn.execute('''
            INSERT INTO visitas
                (paciente_id, visitante_nome, visitante_parentesco,
                 data_visita, hora_entrada, hora_saida, observacoes, registrado_por)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            paciente_id, dados.get('visitante_nome'), dados.get('visitante_parentesco'),
            dados.get('data_visita'), dados.get('hora_entrada') or None,
            dados.get('hora_saida') or None, dados.get('observacoes'),
            session.get('usuario_nome', '')
        ))
    flash('Visita registrada com sucesso!', 'sucesso')
    return redirect(url_for('listar_visitas', paciente_id=paciente_id))


@app.route('/visita/<int:visita_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_visita(visita_id):
    with get_db() as conn:
        visita = conn.execute('''
            SELECT v.paciente_id FROM visitas v
            JOIN pacientes p ON v.paciente_id = p.id
            WHERE v.id = ? AND p.casa_id = ?
        ''', (visita_id, casa_id_atual())).fetchone()
        if visita:
            conn.execute('DELETE FROM visitas WHERE id = ?', (visita_id,))
            flash('Visita removida.', 'sucesso')
            return redirect(url_for('listar_visitas', paciente_id=visita['paciente_id']))
    return redirect(url_for('index'))


# ============================================================================
# EXAMES
# ============================================================================

@app.route('/paciente/<int:paciente_id>/exames')
@login_obrigatorio
@apenas_equipe
def listar_exames(paciente_id):
    with get_db() as conn:
        paciente = buscar_paciente_da_casa(conn, paciente_id)
        if not paciente:
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))
        exames = conn.execute(
            'SELECT * FROM exames WHERE paciente_id = ? ORDER BY data_exame DESC',
            (paciente_id,)
        ).fetchall()
    return render_template('exames.html', paciente=dict(paciente), exames=exames)


@app.route('/paciente/<int:paciente_id>/exame/novo', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def novo_exame(paciente_id):
    dados = request.form
    with get_db() as conn:
        if not buscar_paciente_da_casa(conn, paciente_id):
            flash('Paciente não encontrado.', 'erro')
            return redirect(url_for('index'))

    arquivo = request.files.get('arquivo')
    if arquivo and arquivo.filename and not extensao_permitida(arquivo.filename):
        flash('Formato de arquivo não permitido. Use PDF, JPG, PNG ou WEBP.', 'erro')
        return redirect(url_for('listar_exames', paciente_id=paciente_id))
    arquivo_nome = salvar_arquivo_exame(arquivo)

    with get_db() as conn:
        conn.execute('''
            INSERT INTO exames
                (paciente_id, tipo_exame, data_exame, laboratorio,
                 medico_solicitante, resultado, valores_referencia, arquivo_nome, observacoes,
                 observacao_familia)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            paciente_id, dados.get('tipo_exame'), dados.get('data_exame'),
            dados.get('laboratorio'), dados.get('medico_solicitante'),
            dados.get('resultado'), dados.get('valores_referencia'),
            arquivo_nome, dados.get('observacoes'),
            (dados.get('observacao_familia') or '').strip() or None
        ))
    flash('Exame registrado com sucesso!', 'sucesso')
    return redirect(url_for('listar_exames', paciente_id=paciente_id))


@app.route('/exame/<int:exame_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def excluir_exame(exame_id):
    with get_db() as conn:
        exame = conn.execute('''
            SELECT e.paciente_id, e.arquivo_nome FROM exames e
            JOIN pacientes p ON e.paciente_id = p.id
            WHERE e.id = ? AND p.casa_id = ?
        ''', (exame_id, casa_id_atual())).fetchone()
        if exame:
            conn.execute('DELETE FROM exames WHERE id = ?', (exame_id,))
            if exame['arquivo_nome']:
                caminho = os.path.join(UPLOAD_FOLDER, exame['arquivo_nome'])
                if os.path.exists(caminho):
                    os.remove(caminho)
            flash('Exame removido.', 'sucesso')
            return redirect(url_for('listar_exames', paciente_id=exame['paciente_id']))
    return redirect(url_for('index'))


@app.route('/exame/arquivo/<nome_arquivo>')
@login_obrigatorio
def servir_exame(nome_arquivo):
    """Serve arquivos de exame apenas para usuários autenticados com acesso ao paciente."""
    perfil = session.get('usuario_perfil')
    with get_db() as conn:
        if perfil == 'responsavel':
            # Responsável só acessa exames do seu próprio paciente vinculado
            paciente_id = session.get('paciente_id')
            exame = conn.execute(
                'SELECT e.id FROM exames e WHERE e.arquivo_nome = ? AND e.paciente_id = ?',
                (nome_arquivo, paciente_id)
            ).fetchone() if responsavel_verificado(conn, session.get('usuario_id')) else None
        else:
            # Equipe e superadmin: restringe à casa
            exame = conn.execute('''
                SELECT e.id FROM exames e
                JOIN pacientes p ON e.paciente_id = p.id
                WHERE e.arquivo_nome = ? AND (p.casa_id = ? OR ? IS NULL)
            ''', (nome_arquivo, casa_id_atual(), casa_id_atual())).fetchone()

    if not exame:
        log.warning('acesso_negado_exame', extra={'gc_action': 'servir_exame',
                    'gc_arquivo': nome_arquivo, 'gc_user_id': session.get('usuario_id')})
        return 'Acesso negado.', 403

    return send_from_directory(UPLOAD_FOLDER, nome_arquivo)


# ============================================================================
# REPOSITÓRIO DE IDENTIDADE (foto do documento + foto do rosto)
# ============================================================================
# A análise automática (CPF, OCR, comparação facial) roda localmente e só
# SINALIZA. Quem aprova ou recusa é sempre o administrador da casa.

TERMO_BIOMETRIA = (
    'Autorizo a instituição a guardar uma foto do meu documento de identidade e uma '
    'foto do meu rosto, e a compará-las de forma automática neste servidor, apenas para '
    'confirmar minha identidade no cadastro. As fotos não são enviadas a outras empresas, '
    'só a administração da casa vê o documento, e posso pedir a exclusão a qualquer momento.'
)

ROTULOS_RESULTADO_FACIAL = {
    'compativel': 'Rostos compatíveis',
    'inconclusivo': 'Inconclusivo',
    'incompativel': 'Rostos não conferem',
    'sem_rosto_documento': 'Nenhum rosto no documento',
    'sem_rosto_selfie': 'Nenhum rosto na foto',
    'indisponivel': 'Comparação indisponível',
    'erro': 'Falha ao analisar',
}
app.jinja_env.globals['ROTULOS_RESULTADO_FACIAL'] = ROTULOS_RESULTADO_FACIAL
app.jinja_env.globals['TERMO_BIOMETRIA'] = TERMO_BIOMETRIA
app.jinja_env.tests['atencao_identidade'] = lambda v: vid.precisa_atencao(dict(v))


def salvar_foto_identidade(arquivo):
    """Salva uma foto (JPG, PNG ou WEBP) com nome aleatório. Devolve o nome ou None."""
    if not arquivo or not arquivo.filename or '.' not in arquivo.filename:
        return None
    ext = arquivo.filename.rsplit('.', 1)[1].lower()
    if ext not in EXTENSOES_FOTO:
        return None
    cabecalho = arquivo.read(12)
    arquivo.seek(0)
    eh_imagem = (cabecalho.startswith(b'\x89PNG') or cabecalho.startswith(b'\xff\xd8\xff')
                 or (cabecalho.startswith(b'RIFF') and cabecalho[8:12] == b'WEBP'))
    if not eh_imagem:
        log.warning('upload_identidade_invalido', extra={'gc_action': 'upload_identidade',
                    'gc_user_id': session.get('usuario_id')})
        return None
    nome = f'{uuid.uuid4().hex}.{"jpg" if ext == "jpeg" else ext}'
    arquivo.save(os.path.join(IDENTIDADE_FOLDER, nome))
    return nome


def _apagar_arquivos_identidade(*nomes):
    for nome in nomes:
        if not nome:
            continue
        caminho = os.path.join(IDENTIDADE_FOLDER, os.path.basename(nome))
        if os.path.isfile(caminho):
            os.remove(caminho)


def _registrar_acesso_identidade(conn, verificacao_id, acao, detalhe=None):
    conn.execute(
        'INSERT INTO acessos_identidade (casa_id, usuario_id, verificacao_id, acao, detalhe, ip) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (casa_id_atual(), session.get('usuario_id'), verificacao_id, acao, detalhe,
         request.remote_addr)
    )


def _pessoa_da_casa(conn, tipo_pessoa, pessoa_id):
    """Nome e CPF de um idoso ou responsável da casa atual (None se não for da casa)."""
    if tipo_pessoa == 'paciente':
        row = conn.execute(
            'SELECT id, nome, cpf, contato_emergencia_nome, contato_emergencia_parentesco '
            'FROM pacientes WHERE id = ? AND casa_id = ? AND ativo = 1',
            (pessoa_id, casa_id_atual())).fetchone()
    elif tipo_pessoa == 'responsavel':
        row = conn.execute(
            "SELECT id, nome, cpf, paciente_id FROM usuarios "
            "WHERE id = ? AND casa_id = ? AND perfil = 'responsavel' AND excluido_em IS NULL",
            (pessoa_id, casa_id_atual())).fetchone()
    else:
        return None
    return dict(row) if row else None


def _consentimento_ativo(conn, tipo_pessoa, pessoa_id):
    return conn.execute(
        'SELECT * FROM consentimentos_biometria WHERE casa_id = ? AND tipo_pessoa = ? '
        'AND pessoa_id = ? AND revogado_em IS NULL ORDER BY id DESC LIMIT 1',
        (casa_id_atual(), tipo_pessoa, pessoa_id)).fetchone()


def _criar_verificacao(conn, tipo_pessoa, pessoa, consentimento_id,
                       foto_documento, foto_rosto, presencial):
    """Roda a análise local e grava a verificação como 'pendente'."""
    analise = vid.analisar(os.path.join(IDENTIDADE_FOLDER, foto_documento),
                           os.path.join(IDENTIDADE_FOLDER, foto_rosto),
                           pessoa.get('cpf'))
    cur = conn.execute('''
        INSERT INTO verificacoes_identidade
            (casa_id, tipo_pessoa, pessoa_id, consentimento_id, foto_documento, foto_rosto,
             rosto_presencial, cpf_cadastro, cpf_cadastro_valido, cpf_documento,
             cpf_ocr_status, cpf_confere, resultado_facial, distancia_facial, motor, enviado_por)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (casa_id_atual(), tipo_pessoa, pessoa['id'], consentimento_id, foto_documento,
          foto_rosto, 1 if presencial else 0, pessoa.get('cpf'),
          analise['cpf_cadastro_valido'], analise['cpf_documento'], analise['cpf_ocr_status'],
          analise['cpf_confere'], analise['resultado_facial'], analise['distancia_facial'],
          analise['motor'], session.get('usuario_id')))
    verificacao_id = cur.lastrowid
    _registrar_acesso_identidade(conn, verificacao_id, 'enviar',
                                 f"{tipo_pessoa}:{pessoa['id']} facial={analise['resultado_facial']}")
    log.info('verificacao_identidade_enviada', extra={'gc_action': 'verificacao_enviar',
             'gc_user_id': session.get('usuario_id'), 'gc_casa_id': casa_id_atual()})
    return verificacao_id


def _receber_envio(conn, tipo_pessoa, pessoa, consentido_por_nome, relacao, presencial):
    """Valida e salva as duas fotos, registra o consentimento e cria a verificação.
    Devolve (verificacao_id, None) ou (None, mensagem_de_erro)."""
    arq_doc = request.files.get('foto_documento')
    arq_rosto = request.files.get('foto_rosto')
    if not arq_doc or not arq_doc.filename or not arq_rosto or not arq_rosto.filename:
        return None, 'Envie a foto do documento e a foto do rosto.'
    if request.form.get('aceite_termo') != 'sim':
        return None, 'É preciso registrar o consentimento antes de enviar as fotos.'
    if not consentido_por_nome or not relacao:
        return None, 'Informe quem deu o consentimento e a relação com a pessoa.'
    foto_documento = salvar_foto_identidade(arq_doc)
    foto_rosto = salvar_foto_identidade(arq_rosto)
    if not foto_documento or not foto_rosto:
        _apagar_arquivos_identidade(foto_documento, foto_rosto)
        return None, 'As fotos precisam ser imagens JPG, PNG ou WEBP.'

    consentimento = _consentimento_ativo(conn, tipo_pessoa, pessoa['id'])
    if consentimento:
        consentimento_id = consentimento['id']
    else:
        consentimento_id = conn.execute('''
            INSERT INTO consentimentos_biometria
                (casa_id, tipo_pessoa, pessoa_id, consentido_por_nome, relacao,
                 versao_termo, coletado_por)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (casa_id_atual(), tipo_pessoa, pessoa['id'], consentido_por_nome[:120],
              relacao[:60], VERSAO_TERMO_BIOMETRIA, session.get('usuario_id'))).lastrowid
    try:
        verificacao_id = _criar_verificacao(conn, tipo_pessoa, pessoa, consentimento_id,
                                            foto_documento, foto_rosto, presencial)
    except Exception:
        _apagar_arquivos_identidade(foto_documento, foto_rosto)
        raise
    return verificacao_id, None


def responsavel_verificado(conn, usuario_id):
    """True se a decisão mais recente da casa sobre este responsável foi 'aprovado'.
    Envio novo pendente não tira o acesso de quem já foi aprovado; uma recusa tira."""
    ultima = conn.execute(
        "SELECT status FROM verificacoes_identidade WHERE casa_id = ? AND "
        "tipo_pessoa = 'responsavel' AND pessoa_id = ? AND status != 'pendente' "
        "ORDER BY revisado_em DESC, id DESC LIMIT 1",
        (casa_id_atual(), usuario_id)).fetchone()
    return bool(ultima and ultima['status'] == 'aprovado')


def _verificacoes_da_pessoa(conn, tipo_pessoa, pessoa_id):
    return conn.execute(
        'SELECT * FROM verificacoes_identidade WHERE casa_id = ? AND tipo_pessoa = ? '
        'AND pessoa_id = ? ORDER BY id DESC',
        (casa_id_atual(), tipo_pessoa, pessoa_id)).fetchall()


def foto_rosto_paciente(conn, paciente_id):
    """Verificação cujo rosto aparece no topo da ficha: a aprovada mais recente,
    ou, se não houver, a pendente mais recente. Recusadas nunca aparecem."""
    return conn.execute('''
        SELECT id, status FROM verificacoes_identidade
        WHERE casa_id = ? AND tipo_pessoa = 'paciente' AND pessoa_id = ?
          AND status IN ('aprovado', 'pendente')
        ORDER BY (status = 'aprovado') DESC, id DESC LIMIT 1
    ''', (casa_id_atual(), paciente_id)).fetchone()


def apagar_verificacoes_da_pessoa(conn, tipo_pessoa, pessoa_id):
    for v in _verificacoes_da_pessoa(conn, tipo_pessoa, pessoa_id):
        _apagar_arquivos_identidade(v['foto_documento'], v['foto_rosto'])
        _registrar_acesso_identidade(conn, v['id'], 'excluir', 'exclusao_da_pessoa')
    conn.execute('DELETE FROM verificacoes_identidade WHERE casa_id = ? AND tipo_pessoa = ? '
                 'AND pessoa_id = ?', (casa_id_atual(), tipo_pessoa, pessoa_id))
    conn.execute('UPDATE consentimentos_biometria SET revogado_em = datetime(\'now\', \'localtime\') '
                 'WHERE casa_id = ? AND tipo_pessoa = ? AND pessoa_id = ? AND revogado_em IS NULL',
                 (casa_id_atual(), tipo_pessoa, pessoa_id))


@app.route('/repositorio')
@login_obrigatorio
@apenas_admin
def repositorio():
    filtro_status = request.args.get('status', 'pendente')
    if filtro_status not in ('pendente', 'aprovado', 'recusado', 'todos'):
        filtro_status = 'pendente'
    filtro_tipo = request.args.get('tipo', '')
    if filtro_tipo not in ('paciente', 'responsavel'):
        filtro_tipo = ''
    sql = '''
        SELECT v.*, COALESCE(p.nome, u.nome) AS pessoa_nome,
               pv.nome AS idoso_do_responsavel
        FROM verificacoes_identidade v
        LEFT JOIN pacientes p ON v.tipo_pessoa = 'paciente' AND p.id = v.pessoa_id
        LEFT JOIN usuarios u ON v.tipo_pessoa = 'responsavel' AND u.id = v.pessoa_id
        LEFT JOIN pacientes pv ON pv.id = u.paciente_id
        WHERE v.casa_id = ?
    '''
    params = [casa_id_atual()]
    if filtro_status != 'todos':
        sql += ' AND v.status = ?'
        params.append(filtro_status)
    if filtro_tipo:
        sql += ' AND v.tipo_pessoa = ?'
        params.append(filtro_tipo)
    sql += ' ORDER BY v.id DESC'
    with get_db() as conn:
        verificacoes = conn.execute(sql, params).fetchall()
        contagem = {r['status']: r['total'] for r in conn.execute(
            'SELECT status, COUNT(*) AS total FROM verificacoes_identidade '
            'WHERE casa_id = ? GROUP BY status', (casa_id_atual(),))}
        pacientes = conn.execute(
            'SELECT id, nome, contato_emergencia_nome, contato_emergencia_parentesco '
            'FROM pacientes WHERE casa_id = ? AND ativo = 1 ORDER BY nome',
            (casa_id_atual(),)).fetchall()
        responsaveis = conn.execute(
            "SELECT u.id, u.nome, p.nome AS paciente_nome FROM usuarios u "
            "LEFT JOIN pacientes p ON p.id = u.paciente_id "
            "WHERE u.casa_id = ? AND u.perfil = 'responsavel' AND u.excluido_em IS NULL "
            "ORDER BY u.nome",
            (casa_id_atual(),)).fetchall()
    return render_template(
        'repositorio.html', verificacoes=verificacoes, contagem=contagem,
        filtro_status=filtro_status, filtro_tipo=filtro_tipo,
        pacientes=pacientes, responsaveis=responsaveis,
        motor_facial_ok=vid.motor_facial().disponivel(),
        motor_ocr_ok=vid.motor_ocr().disponivel(),
        pre_tipo=request.args.get('novo_tipo', ''), pre_id=request.args.get('novo_id', ''))


@app.route('/repositorio/nova', methods=['POST'])
@login_obrigatorio
@apenas_admin
def repositorio_nova():
    # Campo único "pessoa" no formato "paciente:12" ou "responsavel:7".
    tipo_pessoa, _, id_texto = (request.form.get('pessoa') or '').partition(':')
    pessoa_id = int(id_texto) if id_texto.isdigit() else None
    with get_db() as conn:
        pessoa = _pessoa_da_casa(conn, tipo_pessoa, pessoa_id) if pessoa_id else None
        if not pessoa:
            flash('Escolha um idoso ou responsável desta casa.', 'erro')
            return redirect(url_for('repositorio'))
        verificacao_id, erro = _receber_envio(
            conn, tipo_pessoa, pessoa,
            (request.form.get('consentido_por_nome') or '').strip(),
            (request.form.get('relacao') or '').strip(),
            presencial=request.form.get('rosto_presencial') == 'sim')
    if erro:
        flash(erro, 'erro')
        return redirect(url_for('repositorio'))
    flash('Fotos recebidas. Confira a análise e decida.', 'sucesso')
    return redirect(url_for('repositorio_detalhe', verificacao_id=verificacao_id))


@app.route('/repositorio/<int:verificacao_id>')
@login_obrigatorio
@apenas_admin
def repositorio_detalhe(verificacao_id):
    with get_db() as conn:
        v = conn.execute('SELECT * FROM verificacoes_identidade WHERE id = ? AND casa_id = ?',
                         (verificacao_id, casa_id_atual())).fetchone()
        if not v:
            flash('Verificação não encontrada.', 'erro')
            return redirect(url_for('repositorio'))
        pessoa = _pessoa_da_casa(conn, v['tipo_pessoa'], v['pessoa_id']) or {}
        consentimento = conn.execute(
            'SELECT c.*, u.nome AS coletado_por_nome FROM consentimentos_biometria c '
            'LEFT JOIN usuarios u ON u.id = c.coletado_por WHERE c.id = ?',
            (v['consentimento_id'],)).fetchone()
        nomes = {r['id']: r['nome'] for r in conn.execute(
            'SELECT id, nome FROM usuarios WHERE id IN (?, ?)',
            (v['enviado_por'], v['revisado_por']))}
        acessos = conn.execute(
            'SELECT a.*, u.nome AS usuario_nome FROM acessos_identidade a '
            'LEFT JOIN usuarios u ON u.id = a.usuario_id '
            'WHERE a.verificacao_id = ? AND a.casa_id = ? ORDER BY a.id DESC LIMIT 30',
            (verificacao_id, casa_id_atual())).fetchall()
        _registrar_acesso_identidade(conn, verificacao_id, 'abrir_detalhe')
    return render_template('repositorio_detalhe.html', v=v, pessoa=pessoa,
                           consentimento=consentimento, nomes=nomes, acessos=acessos,
                           atencao=vid.precisa_atencao(dict(v)))


@app.route('/repositorio/<int:verificacao_id>/decidir', methods=['POST'])
@login_obrigatorio
@apenas_admin
def repositorio_decidir(verificacao_id):
    decisao = request.form.get('decisao')
    motivo = (request.form.get('motivo') or '').strip()[:500]
    if decisao not in ('aprovado', 'recusado'):
        flash('Escolha aprovar ou recusar.', 'erro')
        return redirect(url_for('repositorio_detalhe', verificacao_id=verificacao_id))
    with get_db() as conn:
        v = conn.execute('SELECT * FROM verificacoes_identidade WHERE id = ? AND casa_id = ?',
                         (verificacao_id, casa_id_atual())).fetchone()
        if not v:
            flash('Verificação não encontrada.', 'erro')
            return redirect(url_for('repositorio'))
        if not motivo and (decisao == 'recusado' or vid.precisa_atencao(dict(v))):
            flash('Escreva o motivo: ele é obrigatório para recusar e para aprovar '
                  'quando a análise automática apontou divergência.', 'erro')
            return redirect(url_for('repositorio_detalhe', verificacao_id=verificacao_id))
        conn.execute('''
            UPDATE verificacoes_identidade
            SET status = ?, motivo = ?, revisado_por = ?, revisado_em = datetime('now', 'localtime')
            WHERE id = ?
        ''', (decisao, motivo or None, session.get('usuario_id'), verificacao_id))
        _registrar_acesso_identidade(conn, verificacao_id, decisao, motivo or None)
    log.info('verificacao_identidade_decidida', extra={'gc_action': 'verificacao_decidir',
             'gc_user_id': session.get('usuario_id'), 'gc_decisao': decisao})
    flash('Verificação aprovada.' if decisao == 'aprovado' else 'Verificação recusada.', 'sucesso')
    return redirect(url_for('repositorio'))


@app.route('/repositorio/<int:verificacao_id>/excluir', methods=['POST'])
@login_obrigatorio
@apenas_admin
def repositorio_excluir(verificacao_id):
    with get_db() as conn:
        v = conn.execute('SELECT * FROM verificacoes_identidade WHERE id = ? AND casa_id = ?',
                         (verificacao_id, casa_id_atual())).fetchone()
        if v:
            conn.execute('DELETE FROM verificacoes_identidade WHERE id = ?', (verificacao_id,))
            _apagar_arquivos_identidade(v['foto_documento'], v['foto_rosto'])
            _registrar_acesso_identidade(conn, verificacao_id, 'excluir')
            flash('Fotos e verificação excluídas.', 'sucesso')
    return redirect(url_for('repositorio'))


@app.route('/identidade/arquivo/<int:verificacao_id>/<tipo>')
@login_obrigatorio
def servir_identidade(verificacao_id, tipo):
    """Única porta de saída das fotos. Regras:
    - admin da casa: documento e rosto de qualquer pessoa da casa;
    - enfermeiro da casa: só o rosto de idosos (para a ficha);
    - responsável: só as próprias fotos.
    Toda abertura fica registrada em acessos_identidade."""
    if tipo not in ('documento', 'rosto'):
        return 'Não encontrado.', 404
    perfil = session.get('usuario_perfil')
    with get_db() as conn:
        v = conn.execute('SELECT * FROM verificacoes_identidade WHERE id = ? AND casa_id = ?',
                         (verificacao_id, casa_id_atual())).fetchone()
        permitido = False
        if v:
            if perfil == 'admin':
                permitido = True
            elif perfil == 'enfermeiro':
                permitido = tipo == 'rosto' and v['tipo_pessoa'] == 'paciente' \
                    and v['status'] != 'recusado'
            elif perfil == 'responsavel':
                permitido = v['tipo_pessoa'] == 'responsavel' \
                    and v['pessoa_id'] == session.get('usuario_id')
        if not permitido:
            log.warning('acesso_negado_identidade', extra={'gc_action': 'servir_identidade',
                        'gc_user_id': session.get('usuario_id')})
            if v:
                _registrar_acesso_identidade(conn, verificacao_id, 'negado', tipo)
            return 'Acesso negado.', 403
        _registrar_acesso_identidade(conn, verificacao_id, 'ver_' + tipo)
        nome = v['foto_documento'] if tipo == 'documento' else v['foto_rosto']
    resposta = send_from_directory(IDENTIDADE_FOLDER, nome)
    resposta.headers['Cache-Control'] = 'private, no-store'
    return resposta


@app.route('/responsavel/verificacao', methods=['GET', 'POST'])
@login_obrigatorio
def verificacao_responsavel():
    """O responsável envia o próprio documento e uma selfie. Fica pendente até o admin decidir."""
    if session.get('usuario_perfil') != 'responsavel':
        flash('Acesso não autorizado.', 'erro')
        return redirect(url_for('index'))
    with get_db() as conn:
        pessoa = _pessoa_da_casa(conn, 'responsavel', session['usuario_id'])
        if not pessoa:
            return redirect(url_for('logout'))
        if request.method == 'POST':
            ja_pendente = conn.execute(
                "SELECT 1 FROM verificacoes_identidade WHERE casa_id = ? AND "
                "tipo_pessoa = 'responsavel' AND pessoa_id = ? AND status = 'pendente'",
                (casa_id_atual(), pessoa['id'])).fetchone()
            if ja_pendente:
                flash('Você já tem um envio aguardando a análise da casa.', 'erro')
                return redirect(url_for('verificacao_responsavel'))
            _, erro = _receber_envio(conn, 'responsavel', pessoa, pessoa['nome'], 'próprio',
                                     presencial=False)
            if erro:
                flash(erro, 'erro')
            else:
                flash('Fotos enviadas. A casa vai conferir e avisar o resultado aqui.', 'sucesso')
            return redirect(url_for('verificacao_responsavel'))
        verificacoes = _verificacoes_da_pessoa(conn, 'responsavel', pessoa['id'])
        liberado = responsavel_verificado(conn, pessoa['id'])
    return render_template('responsavel_verificacao.html', verificacoes=verificacoes,
                           liberado=liberado)


# ============================================================================
# NOTIFICAÇÕES PUSH
# ============================================================================

@app.route('/push/chave-publica')
@login_obrigatorio
def push_chave_publica():
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})


@app.route('/push/inscrever', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def push_inscrever():
    dados = request.get_json(silent=True) or {}
    endpoint = dados.get('endpoint')
    keys = dados.get('keys') or {}
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')
    if not endpoint or not p256dh or not auth:
        return jsonify({'erro': 'Dados de inscrição inválidos.'}), 400
    with get_db() as conn:
        conn.execute('''
            INSERT INTO push_subscriptions (usuario_id, endpoint, p256dh, auth)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                usuario_id = excluded.usuario_id,
                p256dh = excluded.p256dh,
                auth = excluded.auth
        ''', (session['usuario_id'], endpoint, p256dh, auth))
    return jsonify({'ok': True})


@app.route('/push/desinscrever', methods=['POST'])
@login_obrigatorio
@apenas_equipe
def push_desinscrever():
    dados = request.get_json(silent=True) or {}
    endpoint = dados.get('endpoint')
    if endpoint:
        with get_db() as conn:
            conn.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (endpoint,))
    return jsonify({'ok': True})


# ============================================================================
# AGENDA
# ============================================================================

@app.route('/agenda')
@login_obrigatorio
@apenas_equipe
def agenda_diaria():
    with get_db() as conn:
        dados = conn.execute('''
            SELECT h.horario, m.id as medicamento_id, m.nome as medicamento,
                   m.dosagem, m.via_administracao, m.instrucoes,
                   p.id as paciente_id, p.nome as paciente, p.quarto
            FROM horarios_medicamento h
            JOIN medicamentos m ON h.medicamento_id = m.id
            JOIN pacientes p ON m.paciente_id = p.id
            WHERE m.ativo = 1 AND p.ativo = 1 AND p.casa_id = ?
            ORDER BY h.horario, p.nome
        ''', (casa_id_atual(),)).fetchall()
        agenda = {}
        quartos = set()
        for d in dados:
            item = dict(d)
            if item.get('quarto'):
                quartos.add(str(item['quarto']).strip())
            h = item['horario']
            if h not in agenda:
                agenda[h] = []
            agenda[h].append(item)
    quartos_lista = sorted(quartos, key=lambda q: (len(q), q))
    return render_template('agenda.html', agenda=agenda, quartos_lista=quartos_lista)


# ============================================================================
# HOJE (início por exceção) e VISÕES GERAIS (somente leitura)
# ============================================================================

TOLERANCIA_DOSE_MIN = 30


def _doses_sem_registro(conn, casa, agora=None, janela_proximas_h=2):
    """Doses de hoje da casa que passaram do horário sem registro e as próximas.

    Mesma regra do portal da família: as doses registradas hoje cobrem os horários
    do remédio em ordem. O sistema só guarda doses dadas, então 'sem registro' não
    afirma que a dose foi pulada."""
    agora = agora or datetime.now()
    hoje = agora.strftime('%Y-%m-%d')
    limite_atraso = (agora - timedelta(minutes=TOLERANCIA_DOSE_MIN)).strftime('%H:%M')
    limite_proximas = (agora + timedelta(hours=janela_proximas_h)).strftime('%H:%M')
    # Virada do dia: nos primeiros 30 min nada está atrasado; nas últimas 2 h a janela vai até 23:59.
    if (agora - timedelta(minutes=TOLERANCIA_DOSE_MIN)).date() != agora.date():
        limite_atraso = ''
    if (agora + timedelta(hours=janela_proximas_h)).date() != agora.date():
        limite_proximas = '23:59'
    feitas = {r[0]: r[1] for r in conn.execute(
        'SELECT d.medicamento_id, COUNT(*) FROM doses_administradas d '
        'JOIN medicamentos m ON m.id = d.medicamento_id JOIN pacientes p ON p.id = m.paciente_id '
        'WHERE p.casa_id = ? AND substr(d.horario_previsto, 1, 10) = ? GROUP BY d.medicamento_id',
        (casa, hoje)).fetchall()}
    linhas = conn.execute(
        'SELECT m.id AS med_id, m.nome AS medicamento, m.dosagem, h.horario, '
        'p.id AS paciente_id, p.nome AS paciente, p.quarto '
        'FROM horarios_medicamento h JOIN medicamentos m ON m.id = h.medicamento_id '
        'JOIN pacientes p ON p.id = m.paciente_id '
        'WHERE m.ativo = 1 AND p.ativo = 1 AND p.casa_id = ? '
        'ORDER BY m.id, h.horario', (casa,)).fetchall()
    atrasadas, proximas, posicao = [], [], {}
    for r in linhas:
        i = posicao.get(r['med_id'], 0)
        posicao[r['med_id']] = i + 1
        if i < feitas.get(r['med_id'], 0):
            continue  # já coberta por uma dose registrada
        h = (r['horario'] or '')[:5]
        item = dict(r)
        item['horario'] = h
        if h <= limite_atraso:
            atrasadas.append(item)
        elif h <= limite_proximas:  # dentro da tolerância ou nas próximas horas
            proximas.append(item)
    atrasadas.sort(key=lambda x: (x['horario'], _norm(x['paciente'])))
    proximas.sort(key=lambda x: (x['horario'], _norm(x['paciente'])))
    return atrasadas, proximas


@app.route('/hoje')
@login_obrigatorio
@apenas_equipe
def hoje():
    casa = casa_id_atual()
    agora = datetime.now()
    hoje_str = agora.strftime('%Y-%m-%d')
    with get_db() as conn:
        atrasadas, proximas = _doses_sem_registro(conn, casa, agora)
        antibioticos = conn.execute("""
            SELECT m.nome, m.data_fim_tratamento, p.id AS paciente_id, p.nome AS paciente, p.quarto
            FROM medicamentos m JOIN pacientes p ON p.id = m.paciente_id
            WHERE m.ativo = 1 AND p.ativo = 1 AND m.eh_antibiotico = 1 AND p.casa_id = ?
              AND m.data_fim_tratamento IS NOT NULL
              AND date(m.data_fim_tratamento) <= date('now', 'localtime', '+2 days')
            ORDER BY m.data_fim_tratamento LIMIT 10""", (casa,)).fetchall()
        total_antibioticos = conn.execute("""
            SELECT COUNT(*) FROM medicamentos m JOIN pacientes p ON p.id = m.paciente_id
            WHERE m.ativo = 1 AND p.ativo = 1 AND m.eh_antibiotico = 1 AND p.casa_id = ?
              AND m.data_fim_tratamento IS NOT NULL
              AND date(m.data_fim_tratamento) <= date('now', 'localtime', '+2 days')""", (casa,)).fetchone()[0]
        exames_pendentes = conn.execute("""
            SELECT COUNT(*) FROM exames e JOIN pacientes p ON p.id = e.paciente_id
            WHERE p.ativo = 1 AND p.casa_id = ? AND TRIM(COALESCE(e.resultado, '')) = ''
              AND TRIM(COALESCE(e.arquivo_nome, '')) = ''""", (casa,)).fetchone()[0]
        visitas_hoje = conn.execute("""
            SELECT COUNT(*) FROM visitas v JOIN pacientes p ON p.id = v.paciente_id
            WHERE p.ativo = 1 AND p.casa_id = ? AND v.data_visita = ?""", (casa, hoje_str)).fetchone()[0]
        total_pacientes = conn.execute(
            'SELECT COUNT(*) FROM pacientes WHERE ativo = 1 AND casa_id = ?', (casa,)).fetchone()[0]
        ids = [i for i in session.get('recentes', []) if isinstance(i, int)]
        recentes = []
        if ids:
            achados = {r['id']: r for r in conn.execute(
                'SELECT id, nome, quarto FROM pacientes WHERE ativo = 1 AND casa_id = ? AND id IN (%s)'
                % ','.join('?' * len(ids)), [casa] + ids).fetchall()}
            recentes = [achados[i] for i in ids if i in achados]
    ant = []
    for a in antibioticos:
        d = dict(a)
        try:
            d['dias'] = (datetime.strptime(d['data_fim_tratamento'][:10], '%Y-%m-%d').date() - agora.date()).days
        except ValueError:
            d['dias'] = None
        ant.append(d)
    return render_template('hoje.html', atrasadas=atrasadas[:15], total_atrasadas=len(atrasadas),
                           proximas=proximas[:10], total_proximas=len(proximas),
                           antibioticos=ant, total_antibioticos=total_antibioticos,
                           exames_pendentes=exames_pendentes, visitas_hoje=visitas_hoje,
                           total_pacientes=total_pacientes, recentes=recentes,
                           hora=agora.strftime('%H:%M'), tolerancia=TOLERANCIA_DOSE_MIN)


def _visao(titulo, icone, subtitulo, filtros, linhas, total, pagina, por_pagina, vazio, endpoint, extra=None):
    paginas = max(1, -(-total // por_pagina))
    return render_template('visao_geral.html', titulo=titulo, icone=icone, subtitulo=subtitulo,
                           filtros=filtros, linhas=linhas, total=total, pagina=pagina, paginas=paginas,
                           vazio=vazio, endpoint=endpoint,
                           q=(request.args.get('q') or '').strip()[:80],
                           args={k: v for k, v in request.args.items() if k != 'pagina' and v}, extra=extra)


def _pagina_atual():
    try:
        return max(1, int(request.args.get('pagina', 1)))
    except ValueError:
        return 1


def _opcao(nome, padrao, validas):
    valor = request.args.get(nome, padrao)
    return valor if valor in validas else padrao


def _visao_sql(conn, select, tabela, where, params, ordem):
    """Contagem + página (25) de uma visão geral. Sempre limitada à casa por 'where'."""
    conn.create_function('norm', 1, _norm, deterministic=True)
    total = conn.execute(f'SELECT COUNT(*) FROM {tabela} WHERE {where}', params).fetchone()[0]
    pagina = min(_pagina_atual(), max(1, -(-total // POR_PAGINA)))
    linhas = conn.execute(
        f'SELECT {select} FROM {tabela} WHERE {where} ORDER BY {ordem} LIMIT ? OFFSET ?',
        params + [POR_PAGINA, (pagina - 1) * POR_PAGINA]).fetchall()
    return [dict(r) for r in linhas], total, pagina


def _base_visao():
    where, params = ['p.ativo = 1', 'p.casa_id = ?'], [casa_id_atual()]
    q = (request.args.get('q') or '').strip()[:80]
    if q:
        where.append("norm(p.nome) LIKE ? ESCAPE '\\'")
        params.append(_padrao_like(q))
    return where, params


@app.route('/exames')
@login_obrigatorio
@apenas_equipe
def visao_exames():
    where, params = _base_visao()
    situacao = _opcao('situacao', 'pendente', ('pendente', 'todos'))
    if situacao == 'pendente':
        where.append("TRIM(COALESCE(e.resultado, '')) = '' AND TRIM(COALESCE(e.arquivo_nome, '')) = ''")
    with get_db() as conn:
        linhas, total, pagina = _visao_sql(
            conn,
            'e.id, e.tipo_exame, e.data_exame, e.laboratorio, e.resultado, e.arquivo_nome, '
            'p.id AS paciente_id, p.nome AS paciente, p.quarto',
            'exames e JOIN pacientes p ON p.id = e.paciente_id', ' AND '.join(where), params,
            'e.data_exame DESC, e.id DESC')
    itens = [{'paciente_id': r['paciente_id'], 'paciente': r['paciente'], 'quarto': r['quarto'],
              'titulo': r['tipo_exame'], 'detalhe': r['laboratorio'] or '', 'data': r['data_exame'],
              'selo': 'Sem resultado' if not (r['resultado'] or '').strip() and not (r['arquivo_nome'] or '').strip() else '',
              'url': url_for('listar_exames', paciente_id=r['paciente_id'])} for r in linhas]
    return _visao('Exames', 'microscope',
                  'Comece pelo nome do paciente ou veja os exames que ainda estão sem resultado.',
                  [{'nome': 'situacao', 'rotulo': 'Mostrar', 'valor': situacao,
                    'opcoes': [('pendente', 'Sem resultado'), ('todos', 'Todos os exames')]}],
                  itens, total, pagina, POR_PAGINA, 'Nenhum exame com esse recorte.', 'visao_exames')


@app.route('/visitas')
@login_obrigatorio
@apenas_equipe
def visao_visitas():
    where, params = _base_visao()
    periodo = _opcao('periodo', 'hoje', ('hoje', '7d', 'todas'))
    hoje_d = date.today()
    if periodo == 'hoje':
        where.append('v.data_visita = ?'); params.append(hoje_d.isoformat())
    elif periodo == '7d':
        where.append('v.data_visita >= ?'); params.append((hoje_d - timedelta(days=6)).isoformat())
    with get_db() as conn:
        linhas, total, pagina = _visao_sql(
            conn,
            'v.id, v.visitante_nome, v.visitante_parentesco, v.data_visita, v.hora_entrada, v.hora_saida, '
            'p.id AS paciente_id, p.nome AS paciente, p.quarto',
            'visitas v JOIN pacientes p ON p.id = v.paciente_id', ' AND '.join(where), params,
            'v.data_visita DESC, v.hora_entrada DESC, v.id DESC')
    itens = [{'paciente_id': r['paciente_id'], 'paciente': r['paciente'], 'quarto': r['quarto'],
              'titulo': r['visitante_nome'] + (f" ({r['visitante_parentesco']})" if r['visitante_parentesco'] else ''),
              'detalhe': ' às '.join(x for x in [r['hora_entrada'], r['hora_saida']] if x) if r['hora_entrada'] else '',
              'data': r['data_visita'], 'selo': '',
              'url': url_for('listar_visitas', paciente_id=r['paciente_id'])} for r in linhas]
    return _visao('Visitas', 'users', 'Quem visitou e quando. Abre mostrando as visitas de hoje.',
                  [{'nome': 'periodo', 'rotulo': 'Período', 'valor': periodo,
                    'opcoes': [('hoje', 'Hoje'), ('7d', 'Últimos 7 dias'), ('todas', 'Todas')]}],
                  itens, total, pagina, POR_PAGINA, 'Nenhuma visita nesse período.', 'visao_visitas')


@app.route('/evolucoes')
@login_obrigatorio
@apenas_equipe
def visao_evolucoes():
    where, params = _base_visao()
    periodo = _opcao('periodo', '24h', ('24h', '7d', 'todas'))
    hoje_d = date.today()
    if periodo == '24h':
        where.append('ev.data_evolucao >= ?'); params.append((hoje_d - timedelta(days=1)).isoformat())
    elif periodo == '7d':
        where.append('ev.data_evolucao >= ?'); params.append((hoje_d - timedelta(days=6)).isoformat())
    with get_db() as conn:
        linhas, total, pagina = _visao_sql(
            conn,
            'ev.id, ev.data_evolucao, ev.profissional, ev.especialidade, ev.descricao, '
            'p.id AS paciente_id, p.nome AS paciente, p.quarto',
            'evolucoes_medicas ev JOIN pacientes p ON p.id = ev.paciente_id', ' AND '.join(where), params,
            'ev.data_evolucao DESC, ev.id DESC')
    itens = [{'paciente_id': r['paciente_id'], 'paciente': r['paciente'], 'quarto': r['quarto'],
              'titulo': ' · '.join(x for x in [r['profissional'], r['especialidade']] if x) or 'Evolução',
              'detalhe': (r['descricao'] or '')[:160] + ('…' if len(r['descricao'] or '') > 160 else ''),
              'data': r['data_evolucao'], 'selo': '',
              'url': url_for('listar_evolucoes', paciente_id=r['paciente_id'])} for r in linhas]
    return _visao('Evoluções', 'clipboard-list', 'Registros clínicos recentes de toda a casa.',
                  [{'nome': 'periodo', 'rotulo': 'Período', 'valor': periodo,
                    'opcoes': [('24h', 'Últimas 24 horas'), ('7d', 'Últimos 7 dias'), ('todas', 'Todas')]}],
                  itens, total, pagina, POR_PAGINA, 'Nenhuma evolução nesse período.', 'visao_evolucoes')


@app.route('/medicamentos')
@login_obrigatorio
@apenas_equipe
def visao_medicamentos():
    visao = _opcao('visao', 'antibioticos', ('antibioticos', 'atrasadas', 'todos'))
    q = (request.args.get('q') or '').strip()[:80]
    filtros = [{'nome': 'visao', 'rotulo': 'Mostrar', 'valor': visao,
                'opcoes': [('antibioticos', 'Antibióticos em uso'), ('atrasadas', 'Doses sem registro hoje'),
                           ('todos', 'Todos os medicamentos ativos')]}]
    if visao == 'atrasadas':
        with get_db() as conn:
            atrasadas, _ = _doses_sem_registro(conn, casa_id_atual())
        if q:
            atrasadas = [a for a in atrasadas if _norm(q) in _norm(a['paciente'])]
        total = len(atrasadas)
        pagina = min(_pagina_atual(), max(1, -(-total // POR_PAGINA)))
        parte = atrasadas[(pagina - 1) * POR_PAGINA: pagina * POR_PAGINA]
        itens = [{'paciente_id': a['paciente_id'], 'paciente': a['paciente'], 'quarto': a['quarto'],
                  'titulo': a['medicamento'] + (f" {a['dosagem']}" if a['dosagem'] else ''),
                  'detalhe': 'Horário previsto ' + a['horario'], 'data': 'hoje', 'selo': 'Sem registro',
                  'url': url_for('detalhe_paciente', paciente_id=a['paciente_id'])} for a in parte]
        return _visao('Medicamentos', 'pill', 'Visão geral de remédios da casa. Só leitura: registre a dose na ficha do paciente.',
                      filtros, itens, total, pagina, POR_PAGINA, 'Nenhuma dose pendente agora.', 'visao_medicamentos')
    where, params = _base_visao()
    where.append('m.ativo = 1')
    if visao == 'antibioticos':
        where.append('m.eh_antibiotico = 1')
    with get_db() as conn:
        linhas, total, pagina = _visao_sql(
            conn,
            'm.id, m.nome, m.dosagem, m.via_administracao, m.eh_antibiotico, m.data_fim_tratamento, '
            'p.id AS paciente_id, p.nome AS paciente, p.quarto',
            'medicamentos m JOIN pacientes p ON p.id = m.paciente_id', ' AND '.join(where), params,
            'p.nome COLLATE NOCASE, m.nome COLLATE NOCASE')
    itens = [{'paciente_id': r['paciente_id'], 'paciente': r['paciente'], 'quarto': r['quarto'],
              'titulo': r['nome'] + (f" {r['dosagem']}" if r['dosagem'] else ''),
              'detalhe': r['via_administracao'] or '',
              'data': ('até ' + r['data_fim_tratamento']) if r['data_fim_tratamento'] else '',
              'selo': 'Antibiótico' if r['eh_antibiotico'] else '',
              'url': url_for('detalhe_paciente', paciente_id=r['paciente_id'])} for r in linhas]
    return _visao('Medicamentos', 'pill', 'Visão geral de remédios da casa. Só leitura: registre a dose na ficha do paciente.',
                  filtros, itens, total, pagina, POR_PAGINA, 'Nenhum medicamento com esse recorte.', 'visao_medicamentos')


@app.route('/registros')
@login_obrigatorio
@apenas_equipe
def registros():
    """Índice das visões gerais (a barra do celular só tem 4 destinos)."""
    casa = casa_id_atual()
    hoje_d = date.today()
    with get_db() as conn:
        def n(sql, *p):
            return conn.execute(sql, (casa, *p)).fetchone()[0]
        contagens = {
            'visao_exames': n("SELECT COUNT(*) FROM exames e JOIN pacientes p ON p.id = e.paciente_id "
                              "WHERE p.ativo = 1 AND p.casa_id = ? AND TRIM(COALESCE(e.resultado, '')) = '' "
                              "AND TRIM(COALESCE(e.arquivo_nome, '')) = ''"),
            'visao_visitas': n('SELECT COUNT(*) FROM visitas v JOIN pacientes p ON p.id = v.paciente_id '
                               'WHERE p.ativo = 1 AND p.casa_id = ? AND v.data_visita = ?', hoje_d.isoformat()),
            'visao_evolucoes': n('SELECT COUNT(*) FROM evolucoes_medicas ev JOIN pacientes p ON p.id = ev.paciente_id '
                                 'WHERE p.ativo = 1 AND p.casa_id = ? AND ev.data_evolucao >= ?',
                                 (hoje_d - timedelta(days=1)).isoformat()),
            'visao_medicamentos': n('SELECT COUNT(*) FROM medicamentos m JOIN pacientes p ON p.id = m.paciente_id '
                                    'WHERE p.ativo = 1 AND p.casa_id = ? AND m.ativo = 1 AND m.eh_antibiotico = 1'),
        }
    return render_template('registros.html', contagens=contagens)


# ============================================================================
# INICIALIZAÇÃO
# ============================================================================

if __name__ == '__main__':
    init_db()
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        iniciar_agendador()
    print("=" * 60)
    print("Sistema de Gestão - GestCare")
    print("=" * 60)
    print(f"Banco de dados: {DB_PATH}")
    print("Acesse: http://localhost:5000")
    print("Para parar: Ctrl+C")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=5000)