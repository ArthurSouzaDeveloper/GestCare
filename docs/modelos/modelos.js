// Simuladores da página de Modelos Computacionais e Complexidade.
// JavaScript simples, sem bibliotecas. (O tema e o modo projeção vêm de ../script.js.)
(function () {
  'use strict';

  // =======================================================================
  // 1) AFD do CPF no formato 000.000.000-00
  // Estados q0..q14; q14 é o único de aceitação; "erro" é poço.
  // Cada posição espera um dígito, ou o separador fixo (. . -).
  // =======================================================================
  var FORMATO = ['d','d','d','.','d','d','d','.','d','d','d','-','d','d']; // 14 símbolos
  var entrada = document.getElementById('dfa-entrada');
  var fita = document.getElementById('dfa-fita');
  var estados = document.getElementById('dfa-estados');
  var veredito = document.getElementById('dfa-veredito');

  function ehDigito(c) { return c >= '0' && c <= '9'; }

  function rodarDFA(texto) {
    // Devolve {estado, pos, erroEm} — pos = quantos símbolos foram aceitos.
    var q = 0;
    for (var i = 0; i < texto.length; i++) {
      if (q >= FORMATO.length) return { q: 'erro', pos: i, erroEm: i };  // sobrou caractere
      var esperado = FORMATO[q], c = texto[i];
      var ok = esperado === 'd' ? ehDigito(c) : c === esperado;
      if (!ok) return { q: 'erro', pos: i, erroEm: i };
      q++;
    }
    return { q: q, pos: texto.length, erroEm: -1 };
  }

  function digitosVerificadoresOk(texto) {
    var cpf = texto.replace(/\D/g, '');
    if (cpf.length !== 11 || /^(\d)\1{10}$/.test(cpf)) return false;
    for (var t = 9; t <= 10; t++) {
      var soma = 0;
      for (var i = 0; i < t; i++) soma += parseInt(cpf[i], 10) * (t + 1 - i);
      if ((soma * 10) % 11 % 10 !== parseInt(cpf[t], 10)) return false;
    }
    return true;
  }

  function desenharDFA() {
    var texto = entrada.value;
    var r = rodarDFA(texto);

    fita.innerHTML = '';
    for (var i = 0; i < texto.length; i++) {
      var s = document.createElement('span');
      s.textContent = texto[i] === ' ' ? '␣' : texto[i];
      if (r.erroEm === i) s.className = 'ruim';
      else if (r.erroEm === -1 || i < r.erroEm) s.className = 'ok';
      else s.className = 'resto';
      fita.appendChild(s);
    }
    if (!texto.length) fita.innerHTML = '<span class="resto">(vazio)</span>';

    estados.innerHTML = '';
    var ate = r.q === 'erro' ? r.erroEm : r.q;
    for (var k = 0; k <= FORMATO.length; k++) {
      var b = document.createElement('b');
      b.innerHTML = 'q' + k;
      if (k <= ate) b.classList.add('visitado');
      if (k === FORMATO.length) b.classList.add('final');
      estados.appendChild(b);
    }
    if (r.q === 'erro') {
      var e = document.createElement('b');
      e.textContent = '∅ erro';
      e.className = 'erro';
      estados.appendChild(e);
    }

    if (r.q === FORMATO.length) {
      var digitos = digitosVerificadoresOk(texto);
      veredito.className = 'dfa-veredito ' + (digitos ? 'aceito' : 'rejeitado');
      veredito.innerHTML = digitos
        ? 'Estado q14: <b>o autômato aceita o formato</b>, e a conferência aritmética dos dígitos verificadores também passa.'
        : 'Estado q14: <b>o autômato aceita o formato</b>, mas a conferência aritmética reprova os dígitos verificadores. É exatamente o que a regex sozinha não consegue ver.';
    } else if (r.q === 'erro') {
      veredito.className = 'dfa-veredito rejeitado';
      veredito.textContent = 'Rejeitado no caractere ' + (r.erroEm + 1) + ': o autômato foi para o estado de erro e não sai mais dele.';
    } else {
      veredito.className = 'dfa-veredito rejeitado';
      veredito.textContent = 'Parou no estado q' + r.q + ': o texto acabou antes do formato completo (faltam ' + (FORMATO.length - r.q) + ' símbolo(s)).';
    }
  }

  if (entrada) {
    entrada.addEventListener('input', desenharDFA);
    document.getElementById('dfa-rodar').addEventListener('click', desenharDFA);
    document.getElementById('dfa-erro').addEventListener('click', function () {
      entrada.value = '529.982.247-2X';
      desenharDFA();
    });
    desenharDFA();
  }

  // =======================================================================
  // 2) Autômato de pilha: { } e [ ]
  // =======================================================================
  var pEntrada = document.getElementById('pda-entrada');
  var pFita = document.getElementById('pda-fita');
  var pPilha = document.getElementById('pda-pilha');
  var pVer = document.getElementById('pda-veredito');
  var pInfo = document.getElementById('pda-info');
  if (!pEntrada) return;

  var pilha = [], pos = 0, falhou = -1;
  var PARES = { '}': '{', ']': '[' };

  function reiniciar() {
    pilha = []; pos = 0; falhou = -1;
    pInfo.textContent = 'Nada lido ainda.';
    desenharPDA();
  }

  function passo() {
    if (falhou >= 0 || pos >= pEntrada.value.length) return false;
    var c = pEntrada.value[pos];
    if (c === '{' || c === '[') {
      pilha.push(c);
      pInfo.textContent = 'Leu "' + c + '": empilhou. Profundidade agora: ' + pilha.length + '.';
    } else if (c === '}' || c === ']') {
      if (!pilha.length) {
        falhou = pos;
        pInfo.textContent = 'Leu "' + c + '" com a pilha vazia: fechou algo que nunca foi aberto.';
      } else if (pilha[pilha.length - 1] !== PARES[c]) {
        falhou = pos;
        pInfo.textContent = 'Leu "' + c + '", mas o topo da pilha é "' + pilha[pilha.length - 1] + '": fechamento trocado.';
      } else {
        pilha.pop();
        pInfo.textContent = 'Leu "' + c + '": desempilhou o par. Profundidade agora: ' + pilha.length + '.';
      }
    } else {
      pInfo.textContent = 'Leu "' + c + '": símbolo ignorado (só olhamos chaves e colchetes).';
    }
    pos++;
    desenharPDA();
    return falhou < 0;
  }

  function desenharPDA() {
    var texto = pEntrada.value;
    pFita.innerHTML = '';
    for (var i = 0; i < texto.length; i++) {
      var it = document.createElement('i');
      it.textContent = texto[i];
      if (i === falhou) it.className = 'falhou';
      else if (i === pos && falhou < 0) it.className = 'atual';
      else if (i < pos) it.className = 'lido';
      pFita.appendChild(it);
    }
    if (!texto.length) pFita.textContent = '(vazio)';

    pPilha.innerHTML = '';
    if (!pilha.length) {
      var v = document.createElement('span');
      v.className = 'vazia';
      v.textContent = 'pilha vazia';
      pPilha.appendChild(v);
    } else {
      pilha.forEach(function (c) {
        var s = document.createElement('span');
        s.textContent = c;
        pPilha.appendChild(s);
      });
    }

    if (falhou >= 0) {
      pVer.className = 'dfa-veredito rejeitado';
      pVer.textContent = 'Rejeitado: estrutura desbalanceada no caractere ' + (falhou + 1) + '.';
    } else if (pos >= texto.length) {
      if (pilha.length) {
        pVer.className = 'dfa-veredito rejeitado';
        pVer.textContent = 'Rejeitado: o texto acabou com ' + pilha.length + ' símbolo(s) ainda na pilha (falta fechar).';
      } else {
        pVer.className = 'dfa-veredito aceito';
        pVer.textContent = 'Aceito: tudo que abriu fechou, na ordem certa, e a pilha terminou vazia.';
      }
    } else {
      pVer.className = 'dfa-veredito';
      pVer.textContent = 'Lendo… ' + pos + ' de ' + texto.length + ' caracteres.';
    }
  }

  document.getElementById('pda-passo').addEventListener('click', passo);
  document.getElementById('pda-tudo').addEventListener('click', function () {
    var limite = 5000;
    while (pos < pEntrada.value.length && falhou < 0 && limite-- > 0) passo();
  });
  document.getElementById('pda-reset').addEventListener('click', reiniciar);
  document.getElementById('pda-erro1').addEventListener('click', function () {
    pEntrada.value = '{"keys":{"p256dh":"x","auth":"y"}';
    reiniciar();
  });
  document.getElementById('pda-erro2').addEventListener('click', function () {
    pEntrada.value = '{"horarios":["08:00","12:00"}]';
    reiniciar();
  });
  pEntrada.addEventListener('input', reiniciar);
  reiniciar();

  // =======================================================================
  // 3) Calculadora de crescimento
  // =======================================================================
  var slider = document.getElementById('cres-n');
  var corpo = document.getElementById('cres-corpo');
  var rotulo = document.getElementById('cres-n-v');
  if (!slider) return;

  function formatarNumero(x) {
    if (x < 1e6) return Math.round(x).toLocaleString('pt-BR');
    var exp = Math.floor(Math.log10(x));
    return (x / Math.pow(10, exp)).toFixed(1).replace('.', ',') + ' × 10^' + exp;
  }

  function formatarTempo(ns) {
    var unidades = [
      [1, 'nanossegundos'], [1e3, 'microssegundos'], [1e6, 'milissegundos'],
      [1e9, 'segundos'], [6e10, 'minutos'], [3.6e12, 'horas'],
      [8.64e13, 'dias'], [3.15e16, 'anos']
    ];
    for (var i = unidades.length - 1; i >= 0; i--) {
      if (ns >= unidades[i][0]) {
        var v = ns / unidades[i][0];
        if (i === unidades.length - 1 && v > 1e6) {
          return formatarNumero(v) + ' anos (mais que a idade do Universo)';
        }
        return (v < 1000 ? v.toFixed(1).replace('.', ',') : formatarNumero(v)) + ' ' + unidades[i][1];
      }
    }
    return 'menos de 1 ns';
  }

  function fatorial(n) {
    var r = 1;
    for (var i = 2; i <= n; i++) { r *= i; if (!isFinite(r)) return Infinity; }
    return r;
  }

  function desenharCrescimento() {
    var n = parseInt(slider.value, 10);
    rotulo.textContent = n;
    var linhas = [
      ['n log n', 'ordenar os quartos', n * Math.log2(n)],
      ['n²', 'vizinho mais próximo (ronda)', n * n],
      ['2ⁿ', 'testar todos os subconjuntos', Math.pow(2, n)],
      ['n!', 'testar todas as ordens (TSP por força bruta)', fatorial(n)]
    ];
    corpo.innerHTML = '';
    linhas.forEach(function (l) {
      var tr = document.createElement('tr');
      var ops = l[2];
      tr.innerHTML = '<td class="mono">' + l[0] + '</td><td>' + l[1] + '</td>' +
        '<td class="mono">' + (isFinite(ops) ? formatarNumero(ops) : '∞') + '</td>' +
        '<td>' + (isFinite(ops) ? formatarTempo(ops) : 'incalculável') + '</td>';
      corpo.appendChild(tr);
    });
  }

  slider.addEventListener('input', desenharCrescimento);
  desenharCrescimento();
})();
