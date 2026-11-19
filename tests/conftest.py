"""Configuração comum dos testes.

O aceite do termo LGPD é exigido de responsáveis e administradores em todas as
telas. Os testes de outras áreas entram direto pela sessão, sem passar pelo
aceite, então a exigência fica desligada para eles. Os testes do termo
(test_termo_lgpd.py) ligam de novo.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gc  # noqa: E402


@pytest.fixture(autouse=True)
def _termo_desligado_por_padrao(monkeypatch):
    monkeypatch.setitem(gc.app.config, 'TERMO_OBRIGATORIO', False)
