"""Verificação de identidade local: CPF, OCR do documento e comparação facial.

Tudo roda nesta máquina. Nenhuma imagem sai do servidor: não há chamada para
serviço externo de biometria, OCR ou consulta à Receita Federal.

O resultado é um INDÍCIO para o administrador, nunca uma prova. A decisão de
aprovar ou recusar é sempre de uma pessoa, na tela Repositório.

Dependências opcionais (requirements-verificacao.txt):
  - dlib-bin + face-recognition-models + numpy  -> comparação facial
  - rapidocr-onnxruntime                          -> leitura do CPF no documento
Se faltarem, a função devolve 'indisponivel' e o envio segue para revisão manual.
"""
import importlib.util
import logging
import os
import re

log = logging.getLogger('gestcare.verificacao')

# Distância euclidiana entre embeddings do dlib (0 = idêntico). O limiar de
# referência do modelo é 0,6. Abaixo de 0,50 tratamos como "compatível"; entre
# 0,50 e 0,60 como "inconclusivo" (comum em foto antiga de documento e em
# idosos); acima de 0,60 como "incompatível". São faixas para orientar a
# revisão, não um veredito.
LIMIAR_COMPATIVEL = 0.50
LIMIAR_INCOMPATIVEL = 0.60
LADO_MAXIMO = 1600  # reduz fotos grandes de celular antes de processar

_RE_CPF = re.compile(r'(?<!\d)(\d{3})[.\s]?(\d{3})[.\s]?(\d{3})[-\s.]?(\d{2})(?!\d)')


def so_digitos(valor):
    return ''.join(c for c in (valor or '') if c.isdigit())


def cpf_digitos_validos(valor):
    """Confere os dois dígitos verificadores. Não diz se o CPF existe nem de quem é."""
    cpf = so_digitos(valor)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(cpf[i]) * (tamanho + 1 - i) for i in range(tamanho))
        if (soma * 10) % 11 % 10 != int(cpf[tamanho]):
            return False
    return True


def extrair_cpfs(texto):
    """Todos os CPFs com dígitos válidos encontrados num texto (ordem de aparição)."""
    vistos = []
    for grupos in _RE_CPF.findall(texto or ''):
        cpf = ''.join(grupos)
        if cpf_digitos_validos(cpf) and cpf not in vistos:
            vistos.append(cpf)
    return vistos


# ---------------------------------------------------------------------------
# Carregamento de imagem
# ---------------------------------------------------------------------------

def _carregar_rgb(caminho):
    """Lê a imagem como array RGB (uint8), reduzindo para LADO_MAXIMO."""
    import numpy as np
    from PIL import Image, ImageOps
    with Image.open(caminho) as img:
        img = ImageOps.exif_transpose(img).convert('RGB')
        img.thumbnail((LADO_MAXIMO, LADO_MAXIMO))
        return np.asarray(img)


# ---------------------------------------------------------------------------
# Comparação facial
# ---------------------------------------------------------------------------

class MotorFacial:
    """Interface: troque a implementação sem mexer nas rotas."""
    nome = 'nenhum'

    def disponivel(self):
        return False

    def descritor(self, rgb):
        """Vetor do maior rosto da imagem, ou None se nenhum rosto for achado."""
        raise NotImplementedError

    def distancia(self, a, b):
        raise NotImplementedError


class MotorDlib(MotorFacial):
    """dlib ResNet (o mesmo modelo da biblioteca face_recognition), via pip:
    dlib-bin traz o dlib já compilado e face-recognition-models traz os pesos."""
    nome = 'dlib-resnet-v1'

    def __init__(self):
        self._pronto = None
        self._detector = self._pose = self._rede = None

    def _pasta_modelos(self):
        spec = importlib.util.find_spec('face_recognition_models')
        if not spec or not spec.submodule_search_locations:
            return None
        return os.path.join(list(spec.submodule_search_locations)[0], 'models')

    def disponivel(self):
        if self._pronto is None:
            try:
                import dlib  # noqa: F401
                import numpy  # noqa: F401
                import PIL  # noqa: F401
                pasta = self._pasta_modelos()
                self._pronto = bool(pasta) and os.path.isfile(
                    os.path.join(pasta, 'dlib_face_recognition_resnet_model_v1.dat'))
            except ImportError:
                self._pronto = False
        return self._pronto

    def _carregar(self):
        if self._rede is None:
            import dlib
            pasta = self._pasta_modelos()
            self._detector = dlib.get_frontal_face_detector()
            self._pose = dlib.shape_predictor(
                os.path.join(pasta, 'shape_predictor_5_face_landmarks.dat'))
            self._rede = dlib.face_recognition_model_v1(
                os.path.join(pasta, 'dlib_face_recognition_resnet_model_v1.dat'))

    def descritor(self, rgb):
        import numpy as np
        self._carregar()
        rostos = self._detector(rgb, 1)
        if not rostos:
            # Rosto pequeno (foto 3x4 dentro do documento): tenta ampliar uma vez.
            rostos = self._detector(rgb, 2)
        if not rostos:
            return None
        maior = max(rostos, key=lambda r: r.width() * r.height())
        forma = self._pose(rgb, maior)
        return np.array(self._rede.compute_face_descriptor(rgb, forma, 1))

    def distancia(self, a, b):
        import numpy as np
        return float(np.linalg.norm(a - b))


_motor_facial = MotorDlib()


def motor_facial():
    return _motor_facial


def comparar_rostos(caminho_documento, caminho_rosto, motor=None):
    """Devolve {'resultado', 'distancia', 'motor'}.
    resultado: compativel | inconclusivo | incompativel | sem_rosto_documento |
               sem_rosto_selfie | indisponivel | erro"""
    motor = motor or _motor_facial
    if not motor.disponivel():
        return {'resultado': 'indisponivel', 'distancia': None, 'motor': motor.nome}
    try:
        d_doc = motor.descritor(_carregar_rgb(caminho_documento))
        if d_doc is None:
            return {'resultado': 'sem_rosto_documento', 'distancia': None, 'motor': motor.nome}
        d_rosto = motor.descritor(_carregar_rgb(caminho_rosto))
        if d_rosto is None:
            return {'resultado': 'sem_rosto_selfie', 'distancia': None, 'motor': motor.nome}
        dist = motor.distancia(d_doc, d_rosto)
    except Exception as exc:  # imagem corrompida, formato estranho etc.
        log.warning('comparacao_facial_falhou: %s', type(exc).__name__)
        return {'resultado': 'erro', 'distancia': None, 'motor': motor.nome}
    if dist < LIMIAR_COMPATIVEL:
        resultado = 'compativel'
    elif dist <= LIMIAR_INCOMPATIVEL:
        resultado = 'inconclusivo'
    else:
        resultado = 'incompativel'
    return {'resultado': resultado, 'distancia': round(dist, 4), 'motor': motor.nome}


# ---------------------------------------------------------------------------
# OCR do documento
# ---------------------------------------------------------------------------

class MotorOCR:
    nome = 'nenhum'

    def disponivel(self):
        return False

    def ler_texto(self, caminho):
        raise NotImplementedError


class MotorRapidOCR(MotorOCR):
    """RapidOCR (modelos PaddleOCR em ONNX, embutidos no pacote pip)."""
    nome = 'rapidocr'

    def __init__(self):
        self._pronto = None
        self._ocr = None

    def disponivel(self):
        # Importa de verdade: o pacote pode estar instalado e ainda assim falhar
        # (ex.: o opencv exige libGL no sistema). Assim o aviso da tela não mente.
        if self._pronto is None:
            try:
                from rapidocr_onnxruntime import RapidOCR  # noqa: F401
                self._pronto = True
            except Exception as exc:
                log.warning('ocr_indisponivel: %s: %s', type(exc).__name__, exc)
                self._pronto = False
        return self._pronto

    def ler_texto(self, caminho):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr = RapidOCR()
        resultado, _ = self._ocr(_carregar_rgb(caminho))
        return '\n'.join(linha[1] for linha in (resultado or []))


_motor_ocr = MotorRapidOCR()


def motor_ocr():
    return _motor_ocr


def ler_cpf_documento(caminho_documento, motor=None):
    """Devolve {'status', 'cpfs', 'motor'}. status: lido | nao_encontrado | indisponivel | erro."""
    motor = motor or _motor_ocr
    if not motor.disponivel():
        return {'status': 'indisponivel', 'cpfs': [], 'motor': motor.nome}
    try:
        texto = motor.ler_texto(caminho_documento)
    except Exception as exc:
        log.warning('ocr_falhou: %s', type(exc).__name__)
        return {'status': 'erro', 'cpfs': [], 'motor': motor.nome}
    cpfs = extrair_cpfs(texto)
    return {'status': 'lido' if cpfs else 'nao_encontrado', 'cpfs': cpfs, 'motor': motor.nome}


# ---------------------------------------------------------------------------
# Análise completa
# ---------------------------------------------------------------------------

def analisar(caminho_documento, caminho_rosto, cpf_cadastro,
             motor_rosto=None, motor_texto=None):
    """Junta as três checagens. Nada aqui aprova ninguém: só sinaliza."""
    cpf = so_digitos(cpf_cadastro)
    face = comparar_rostos(caminho_documento, caminho_rosto, motor_rosto)
    ocr = ler_cpf_documento(caminho_documento, motor_texto)
    if ocr['status'] == 'lido' and cpf:
        cpf_confere = 1 if cpf in ocr['cpfs'] else 0
    else:
        cpf_confere = None  # não deu para conferir
    return {
        'cpf_cadastro_valido': 1 if cpf_digitos_validos(cpf) else 0,
        'cpf_documento': ','.join(ocr['cpfs']) or None,
        'cpf_ocr_status': ocr['status'],
        'cpf_confere': cpf_confere,
        'resultado_facial': face['resultado'],
        'distancia_facial': face['distancia'],
        'motor': f"{face['motor']}+{ocr['motor']}",
    }


def precisa_atencao(analise):
    """True quando algum sinal pede olhar com mais cuidado antes de aprovar."""
    return (not analise.get('cpf_cadastro_valido')
            or analise.get('cpf_confere') == 0
            or analise.get('resultado_facial') in (
                'incompativel', 'inconclusivo', 'sem_rosto_documento', 'sem_rosto_selfie'))
