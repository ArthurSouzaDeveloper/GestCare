// ── MODAIS ────────────────────────────────────────────────
// Diálogo acessível: foco preso dentro, Esc fecha, foco volta
// para quem abriu. O véu não rola a página atrás.
let _modalAberto = null;
let _disparador  = null;

const FOCAVEIS = 'a[href], button:not([disabled]), input:not([disabled]), ' +
                 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function abrirModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  _disparador = document.activeElement;
  _modalAberto = modal;

  const caixa = modal.querySelector('.modal-conteudo');
  if (caixa) {
    caixa.setAttribute('role', 'dialog');
    caixa.setAttribute('aria-modal', 'true');
    const titulo = caixa.querySelector('.modal-titulo');
    if (titulo) {
      if (!titulo.id) titulo.id = id + '-titulo';
      caixa.setAttribute('aria-labelledby', titulo.id);
    }
  }

  modal.classList.add('ativo');
  document.body.style.overflow = 'hidden';

  const primeiro = modal.querySelector('input, select, textarea') ||
                   modal.querySelector(FOCAVEIS);
  if (primeiro) primeiro.focus({ preventScroll: true });
}

function fecharModal(id) {
  const modal = id ? document.getElementById(id) : _modalAberto;
  if (!modal) return;
  modal.classList.remove('ativo');
  document.body.style.overflow = '';
  if (_disparador && document.contains(_disparador)) _disparador.focus();
  _disparador = null;
  _modalAberto = null;
}

// Fechar clicando no véu
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal')) fecharModal(e.target.id);
});

document.addEventListener('keydown', e => {
  if (!_modalAberto) return;
  if (e.key === 'Escape') { fecharModal(); return; }
  if (e.key !== 'Tab') return;
  // Foco preso: Tab circula só dentro do diálogo.
  const itens = Array.from(_modalAberto.querySelectorAll(FOCAVEIS))
                     .filter(el => el.offsetParent !== null);
  if (!itens.length) return;
  const primeiro = itens[0], ultimo = itens[itens.length - 1];
  if (e.shiftKey && document.activeElement === primeiro) { e.preventDefault(); ultimo.focus(); }
  else if (!e.shiftKey && document.activeElement === ultimo) { e.preventDefault(); primeiro.focus(); }
});

// ── HORÁRIOS DINÂMICOS ───────────────────────────────────
function adicionarHorario(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const item = document.createElement('div');
  item.className = 'horario-item';
  item.innerHTML = `
    <input type="time" name="horarios[]" class="form-input" required>
    <button type="button" class="btn-remover-horario"
            onclick="removerHorario(this)">Remover</button>
  `;
  container.appendChild(item);
  item.querySelector('input').focus();
}

function removerHorario(botao) {
  const lista = botao.parentElement.parentElement;
  if (lista.querySelectorAll('.horario-item').length > 1) {
    botao.parentElement.remove();
  } else {
    mostrarToast('É necessário ter pelo menos um horário.', 'erro');
  }
}

// ── CONFIRMAÇÃO DE EXCLUSÃO ──────────────────────────────
function confirmarExclusao(msg) {
  return confirm(msg || 'Tem certeza? Esta ação não pode ser desfeita.');
}

// ── FORMATAÇÃO DE CPF ────────────────────────────────────
function formatarCPF(input) {
  let v = input.value.replace(/\D/g, '').slice(0, 11);
  if (v.length > 9)      v = v.replace(/(\d{3})(\d{3})(\d{3})(\d{1,2})/, '$1.$2.$3-$4');
  else if (v.length > 6) v = v.replace(/(\d{3})(\d{3})(\d{1,3})/, '$1.$2.$3');
  else if (v.length > 3) v = v.replace(/(\d{3})(\d{1,3})/, '$1.$2');
  input.value = v;
}

// ── FORMATAÇÃO DE TELEFONE ───────────────────────────────
function formatarTelefone(input) {
  let v = input.value.replace(/\D/g, '').slice(0, 11);
  if (v.length > 10)     v = v.replace(/(\d{2})(\d{5})(\d{4})/, '($1) $2-$3');
  else if (v.length > 6) v = v.replace(/(\d{2})(\d{4})(\d{1,4})/, '($1) $2-$3');
  else if (v.length > 2) v = v.replace(/(\d{2})(\d{1,5})/, '($1) $2');
  input.value = v;
}

// ── TOAST / SNACKBAR ─────────────────────────────────────
// Região live: o leitor de tela anuncia sem roubar o foco.
function mostrarToast(mensagem, tipo) {
  let toast = document.getElementById('_toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = '_toast';
    toast.className = 'toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    document.body.appendChild(toast);
  }
  toast.textContent = mensagem;
  toast.classList.toggle('toast-erro', tipo === 'erro');
  toast.classList.add('visivel');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toast.classList.remove('visivel'), 4000);
}

// Valida CPF pelos dois dígitos verificadores (mesma regra do servidor)
function cpfValido(valor) {
  const cpf = (valor || '').replace(/\D/g, '');
  if (cpf.length !== 11 || /^(\d)\1{10}$/.test(cpf)) return false;
  for (let t = 9; t <= 10; t++) {
    let soma = 0;
    for (let i = 0; i < t; i++) soma += parseInt(cpf[i], 10) * (t + 1 - i);
    if ((soma * 10) % 11 % 10 !== parseInt(cpf[t], 10)) return false;
  }
  return true;
}

// ── INIT ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // Aplica formatadores nos campos marcados
  document.querySelectorAll('[data-formato="cpf"]').forEach(el =>
    el.addEventListener('input', () => formatarCPF(el))
  );
  document.querySelectorAll('[data-formato="telefone"]').forEach(el =>
    el.addEventListener('input', () => formatarTelefone(el))
  );

  // Bloqueia envio de CPF inválido antes de ir ao servidor
  document.querySelectorAll('input[name="cpf"]').forEach(campo => {
    const checar = () => {
      const v = campo.value.trim();
      campo.setCustomValidity(!v || cpfValido(v) ? '' : 'CPF inválido. Confira os números digitados.');
    };
    campo.addEventListener('input', checar);
    campo.addEventListener('blur', checar);
  });

  // Mostrar/ocultar CPF nas fichas
  document.querySelectorAll('.cpf-campo').forEach(campo => {
    const btn   = campo.querySelector('.cpf-toggle');
    const valor = campo.querySelector('.cpf-valor');
    if (!btn || !valor) return;
    const oculto = valor.textContent;
    const aberto = campo.dataset.cpfAberto || '';
    let visivel = false;
    btn.addEventListener('click', () => {
      visivel = !visivel;
      valor.textContent = visivel ? aberto : oculto;
      btn.querySelector('i').className = visivel ? 'ti ti-eye-off' : 'ti ti-eye';
      btn.setAttribute('aria-label', visivel ? 'Ocultar CPF' : 'Mostrar CPF');
    });
  });

  // Alertas flash somem depois de 5 segundos
  setTimeout(() => {
    document.querySelectorAll('.alerta').forEach(a => {
      a.style.transition = 'opacity .5s';
      a.style.opacity = '0';
      setTimeout(() => a.remove(), 500);
    });
  }, 5000);

  // Desabilita botão de submit após clique (evita duplo envio)
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', () => {
      const btn = form.querySelector('[type="submit"]');
      if (btn) {
        btn.disabled = true;
        const original = btn.textContent;
        btn.textContent = 'Aguarde...';
        setTimeout(() => {
          btn.disabled = false;
          btn.textContent = original;
        }, 5000);
      }
    });
  });

  // Registra Service Worker para funcionamento offline
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  }

  // Se a permissão de notificação já foi concedida antes, renova a inscrição
  // silenciosamente (ex: depois de reabrir o app em outro dia).
  if (document.body.dataset.notificacoes === 'equipe' &&
      'Notification' in window && Notification.permission === 'granted') {
    ativarNotificacoes(true);
  }
});

// ── NOTIFICAÇÕES PUSH (lembrete de antibiótico no celular) ──
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const dadosBrutos = atob(base64);
  const saida = new Uint8Array(dadosBrutos.length);
  for (let i = 0; i < dadosBrutos.length; ++i) saida[i] = dadosBrutos.charCodeAt(i);
  return saida;
}

async function ativarNotificacoes(silencioso) {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    if (!silencioso) mostrarToast('Seu navegador não suporta notificações push.', 'erro');
    return;
  }
  try {
    const permissao = await Notification.requestPermission();
    if (permissao !== 'granted') {
      if (!silencioso) mostrarToast('Permissão de notificação negada.', 'erro');
      return;
    }
    const registro = await navigator.serviceWorker.ready;
    const resposta = await fetch('/push/chave-publica');
    const { publicKey } = await resposta.json();

    let inscricao = await registro.pushManager.getSubscription();
    if (!inscricao) {
      inscricao = await registro.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey)
      });
    }

    await fetch('/push/inscrever', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(inscricao.toJSON())
    });

    if (!silencioso) mostrarToast('Notificações ativadas! Você será avisado(a) sobre antibióticos.');
    const botao = document.getElementById('btn-notificacoes');
    if (botao) botao.classList.add('notificacoes-ativas');
  } catch (erro) {
    if (!silencioso) mostrarToast('Não foi possível ativar as notificações.', 'erro');
  }
}
// ══════════════════════════════════════════════════════════
// MENU DE CONTA
// Perfil, lembretes, tema e sair. Um por viewport; abre com
// clique, fecha com Esc, clique fora ou troca de foco.
// ══════════════════════════════════════════════════════════
(function () {
  const pares = [
    ['conta-topo',    'menu-conta-topo'],
    ['conta-sidebar', 'menu-conta-sidebar'],
    ['conta-mobile',  'menu-conta-mobile'],
  ];

  function fechar(botao, menu) {
    menu.hidden = true;
    botao.setAttribute('aria-expanded', 'false');
  }

  function fecharTodos(exceto) {
    pares.forEach(([bid, mid]) => {
      const b = document.getElementById(bid), m = document.getElementById(mid);
      if (b && m && m !== exceto) fechar(b, m);
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    pares.forEach(([bid, mid]) => {
      const botao = document.getElementById(bid);
      const menu  = document.getElementById(mid);
      if (!botao || !menu) return;

      botao.addEventListener('click', e => {
        e.stopPropagation();
        const abrindo = menu.hidden;
        fecharTodos(menu);
        menu.hidden = !abrindo;
        botao.setAttribute('aria-expanded', String(abrindo));
        if (abrindo) {
          const primeiro = menu.querySelector('a, button');
          if (primeiro) primeiro.focus({ preventScroll: true });
        }
      });

      menu.addEventListener('click', e => e.stopPropagation());

      menu.addEventListener('keydown', e => {
        if (e.key === 'Escape') { fechar(botao, menu); botao.focus(); }
      });
    });

    document.addEventListener('click', () => fecharTodos(null));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') fecharTodos(null); });
  });
})();

// ══════════════════════════════════════════════════════════
// TEMA — claro, escuro ou o do sistema, com persistência.
// A escolha é aplicada antes da primeira pintura pelo script
// de partials/_cabeca.html; aqui só tratamos a troca.
// ══════════════════════════════════════════════════════════
(function () {
  const CHAVE = 'gestcare-tema';

  function lerPreferencia() {
    try { return localStorage.getItem(CHAVE) || 'sistema'; }
    catch (e) { return 'sistema'; }
  }

  function aplicar(valor) {
    if (valor === 'claro' || valor === 'escuro') {
      document.documentElement.setAttribute('data-tema', valor);
    } else {
      document.documentElement.removeAttribute('data-tema');
    }
    try {
      if (valor === 'sistema') localStorage.removeItem(CHAVE);
      else localStorage.setItem(CHAVE, valor);
    } catch (e) { /* armazenamento bloqueado: vale só nesta aba */ }
    marcar(valor);
  }

  function marcar(valor) {
    document.querySelectorAll('[data-seletor-tema] .seletor-tema-opcao').forEach(b => {
      b.setAttribute('aria-pressed', String(b.dataset.temaValor === valor));
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    marcar(lerPreferencia());
    document.querySelectorAll('[data-seletor-tema] .seletor-tema-opcao').forEach(botao => {
      botao.addEventListener('click', () => {
        aplicar(botao.dataset.temaValor);
        mostrarToast('Tema ' + ({
          claro: 'claro', escuro: 'escuro', sistema: 'do sistema'
        })[botao.dataset.temaValor] + ' aplicado.');
      });
    });
  });
})();

// ══════════════════════════════════════════════════════════
// ENVIO DE FORMULÁRIO
// Um formulário longo não pode ser enviado duas vezes: o botão
// entra em estado de envio e só volta se o navegador restaurar
// a página do cache.
// ══════════════════════════════════════════════════════════
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[method="POST"], form[method="post"]').forEach(form => {
      form.addEventListener('submit', () => {
        if (form.dataset.enviando === '1') return;
        if (typeof form.checkValidity === 'function' && !form.checkValidity()) return;
        form.dataset.enviando = '1';
        form.querySelectorAll('button[type="submit"], button:not([type])').forEach(botao => {
          botao.classList.add('botao-enviando');
          botao.setAttribute('aria-busy', 'true');
          botao.disabled = true;
        });
      });
    });
  });

  // Voltar pelo histórico devolve o formulário utilizável.
  window.addEventListener('pageshow', e => {
    if (!e.persisted) return;
    document.querySelectorAll('form[data-enviando="1"]').forEach(form => {
      form.dataset.enviando = '0';
      form.querySelectorAll('button[aria-busy="true"]').forEach(botao => {
        botao.classList.remove('botao-enviando');
        botao.removeAttribute('aria-busy');
        botao.disabled = false;
      });
    });
  });
})();
