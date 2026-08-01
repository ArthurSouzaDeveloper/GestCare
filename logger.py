"""
Logger estruturado para o GestCare.

- Saída JSON em produção (fácil de ingerir por Loki, Datadog, etc.)
- Saída legível no terminal em desenvolvimento
- Data masking: senhas, tokens e CPF nunca aparecem nos logs
- Níveis: DEBUG < INFO < WARNING < ERROR < CRITICAL
- Contexto automático: request_id, user_id, action, ip
"""

import logging
import json
import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from flask import request, session, has_request_context

# ---------------------------------------------------------------------------
# Campos sensíveis — NUNCA gravados nos logs
# ---------------------------------------------------------------------------
_CAMPOS_SENSIVEIS = re.compile(
    r'(senha|password|token|secret|cpf|credit_card|cartao|card_number'
    r'|authorization|cookie|session_key|private_key|vapid)',
    re.IGNORECASE,
)

_CPF_PATTERN = re.compile(r'\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\-\.]?\d{2}')


def _mascarar(valor):
    """Substitui o conteúdo de campos sensíveis por [REDACTED]."""
    if isinstance(valor, dict):
        return {k: '[REDACTED]' if _CAMPOS_SENSIVEIS.search(k) else _mascarar(v)
                for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_mascarar(v) for v in valor]
    if isinstance(valor, str):
        return _CPF_PATTERN.sub('[CPF-REDACTED]', valor)
    return valor


# ---------------------------------------------------------------------------
# Formatter JSON
# ---------------------------------------------------------------------------
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Contexto do request Flask (se disponível)
        ctx: dict = {}
        if has_request_context():
            ctx['request_id'] = getattr(request, '_gestcare_request_id', '-')
            ctx['method']     = request.method
            ctx['path']       = request.path
            ctx['ip']         = request.remote_addr
            ctx['user_id']    = session.get('usuario_id')
            ctx['user_role']  = session.get('usuario_perfil')
            ctx['casa_id']    = session.get('casa_id')

        payload = {
            'ts':      datetime.now(timezone.utc).isoformat(),
            'level':   record.levelname,
            'logger':  record.name,
            'msg':     record.getMessage(),
            **ctx,
        }

        # Campos extras passados via extra={}
        for key, val in record.__dict__.items():
            if key.startswith('gc_'):          # convenção: gc_action, gc_data, etc.
                payload[key[3:]] = _mascarar(val)

        # Stack trace em caso de exceção
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Formatter legível para desenvolvimento
# ---------------------------------------------------------------------------
class DevFormatter(logging.Formatter):
    CORES = {
        'DEBUG':    '\033[36m',   # ciano
        'INFO':     '\033[32m',   # verde
        'WARNING':  '\033[33m',   # amarelo
        'ERROR':    '\033[31m',   # vermelho
        'CRITICAL': '\033[35m',   # magenta
    }
    RESET = '\033[0m'

    def format(self, record: logging.LogRecord) -> str:
        cor    = self.CORES.get(record.levelname, '')
        ts     = datetime.now().strftime('%H:%M:%S')
        prefix = f"{cor}[{record.levelname}]{self.RESET} {ts}"
        msg    = record.getMessage()

        extras = {k[3:]: _mascarar(v)
                  for k, v in record.__dict__.items() if k.startswith('gc_')}
        suffix = f"  {extras}" if extras else ''

        base = f"{prefix} {record.name}: {msg}{suffix}"
        if record.exc_info:
            base += '\n' + self.formatException(record.exc_info)
        return base


# ---------------------------------------------------------------------------
# Fábrica do logger
# ---------------------------------------------------------------------------
def setup_logger(app) -> logging.Logger:
    """
    Configura o logger raiz do GestCare.
    Chame uma vez ao iniciar o servidor.
    """
    nivel_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
    nivel     = getattr(logging, nivel_str, logging.INFO)
    em_prod   = os.environ.get('FLASK_ENV', 'production') != 'development'

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter() if em_prod else DevFormatter())

    logger = logging.getLogger('gestcare')
    logger.setLevel(nivel)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    # Silencia logs verbosos de bibliotecas
    for lib in ('werkzeug', 'apscheduler', 'waitress'):
        logging.getLogger(lib).setLevel(logging.WARNING)

    app.logger.handlers = logger.handlers
    app.logger.setLevel(nivel)

    return logger


# ---------------------------------------------------------------------------
# Helper: injeta request_id em cada requisição
# ---------------------------------------------------------------------------
def registrar_hooks(app):
    @app.before_request
    def _atribuir_request_id():
        request._gestcare_request_id = str(uuid.uuid4())[:8]

    @app.after_request
    def _logar_request(response):
        if has_request_context():
            log = logging.getLogger('gestcare.http')
            log.info(
                '%s %s → %d',
                request.method, request.path, response.status_code,
                extra={
                    'gc_request_id': getattr(request, '_gestcare_request_id', '-'),
                    'gc_status':     response.status_code,
                    'gc_ip':         request.remote_addr,
                    'gc_user_id':    session.get('usuario_id'),
                }
            )
        return response

    @app.teardown_request
    def _logar_excecao(exc):
        if exc is not None:
            log = logging.getLogger('gestcare.error')
            log.error(
                'Exceção não tratada na requisição',
                exc_info=exc,
                extra={
                    'gc_request_id': getattr(request, '_gestcare_request_id', '-'),
                    'gc_path':       request.path if has_request_context() else '-',
                    'gc_user_id':    session.get('usuario_id') if has_request_context() else None,
                }
            )
