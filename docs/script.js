// GestCare — site do PI. JavaScript simples, sem bibliotecas.
(function () {
  'use strict';
  var raiz = document.documentElement;

  function guardar(chave, valor) { try { localStorage.setItem(chave, valor); } catch (e) {} }

  // ---------- Tema claro/escuro ----------
  var btnTema = document.getElementById('btn-tema');
  function temaAtual() {
    if (raiz.dataset.theme) return raiz.dataset.theme;
    return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function rotularTema() { btnTema.textContent = temaAtual() === 'dark' ? 'Tema claro' : 'Tema escuro'; }
  btnTema.addEventListener('click', function () {
    var novo = temaAtual() === 'dark' ? 'light' : 'dark';
    raiz.dataset.theme = novo; guardar('gc-tema', novo); rotularTema();
  });
  rotularTema();

  // ---------- Modo projeção (letras maiores) ----------
  var btnProj = document.getElementById('btn-projecao');
  function rotularProj() { btnProj.setAttribute('aria-pressed', raiz.classList.contains('projecao') ? 'true' : 'false'); }
  btnProj.addEventListener('click', function () {
    raiz.classList.toggle('projecao');
    guardar('gc-projecao', raiz.classList.contains('projecao') ? '1' : '0'); rotularProj();
  });
  rotularProj();

  // ---------- Menu: destaca a seção visível ----------
  var links = Array.prototype.slice.call(document.querySelectorAll('.menu a'));
  var secoes = links.map(function (a) { return document.querySelector(a.getAttribute('href')); });
  if ('IntersectionObserver' in window) {
    var obs = new IntersectionObserver(function (entradas) {
      entradas.forEach(function (e) {
        if (!e.isIntersecting) return;
        links.forEach(function (a) { a.classList.toggle('ativo', a.getAttribute('href') === '#' + e.target.id); });
      });
    }, { rootMargin: '-30% 0px -60% 0px' });
    secoes.forEach(function (s) { if (s) obs.observe(s); });
  }

  // Na impressão, abre todos os cenários.
  window.addEventListener('beforeprint', function () {
    document.querySelectorAll('details').forEach(function (d) { d.open = true; });
  });

  // ---------- Simulador de min-heap ----------
  // Chave: [prioridade, horário, nome]. Menor = mais urgente. Mesma lógica do heapq do Python.
  var heap = [];
  var log = document.getElementById('sim-log');
  var arvore = document.getElementById('sim-arvore');
  var vetor = document.getElementById('sim-vetor');
  var btnPop = document.getElementById('sim-pop');
  if (!arvore) return;

  var NOMES = ['Residente A', 'Residente B', 'Residente C', 'Residente D', 'Residente E', 'Residente F', 'Residente G'];
  var REMEDIOS = ['Losartana', 'Metformina', 'Sinvastatina', 'Omeprazol', 'Levotiroxina', 'Dipirona'];
  var ROTULO = { 1: 'crítico', 2: 'atrasada', 3: 'próxima' };
  var MAX = 15;

  function menor(a, b) {
    if (a.p !== b.p) return a.p < b.p;
    if (a.h !== b.h) return a.h < b.h;
    return a.nome < b.nome;
  }
  function trocar(i, j) { var t = heap[i]; heap[i] = heap[j]; heap[j] = t; }

  function subir(i) {
    var passos = 0;
    while (i > 0) {
      var pai = Math.floor((i - 1) / 2);
      if (!menor(heap[i], heap[pai])) break;
      trocar(i, pai); i = pai; passos++;
    }
    return passos;
  }
  function descer(i) {
    var passos = 0, n = heap.length;
    for (;;) {
      var e = 2 * i + 1, d = 2 * i + 2, m = i;
      if (e < n && menor(heap[e], heap[m])) m = e;
      if (d < n && menor(heap[d], heap[m])) m = d;
      if (m === i) break;
      trocar(i, m); i = m; passos++;
    }
    return passos;
  }
  function push(item) {
    heap.push(item);
    var p = subir(heap.length - 1);
    registrar('Entrou <b>' + desc(item) + '</b> na posição ' + (heap.length - 1) + ' e subiu ' + p + ' nível(is). O(log n).');
  }
  function pop() {
    if (!heap.length) return;
    var topo = heap[0], ultimo = heap.pop();
    var p = 0;
    if (heap.length) { heap[0] = ultimo; p = descer(0); }
    registrar('Atendido: <b>' + desc(topo) + '</b>. O último foi para o topo e desceu ' + p + ' nível(is).');
  }
  function desc(x) { return ROTULO[x.p] + ' · ' + x.h + ' · ' + x.nome + ' (' + x.rem + ')'; }
  function aleat(lista) { return lista[Math.floor(Math.random() * lista.length)]; }
  function hora() {
    var h = 6 + Math.floor(Math.random() * 16), m = Math.random() < 0.5 ? '00' : '30';
    return (h < 10 ? '0' : '') + h + ':' + m;
  }
  function registrar(html) {
    var li = document.createElement('li'); li.innerHTML = html;
    log.insertBefore(li, log.firstChild);
    while (log.children.length > 12) log.removeChild(log.lastChild);
  }

  var SVGNS = 'http://www.w3.org/2000/svg';
  function el(nome, attrs, texto) {
    var n = document.createElementNS(SVGNS, nome);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (texto != null) n.textContent = texto;
    return n;
  }
  function desenhar() {
    // Árvore: 4 níveis cabem (até 15 nós)
    arvore.innerHTML = '';
    var W = 520, H = 220, svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Árvore do heap com ' + heap.length + ' alertas' });
    var pos = heap.map(function (_, i) {
      var nivel = Math.floor(Math.log2(i + 1)), primeiro = Math.pow(2, nivel) - 1, qtd = Math.pow(2, nivel);
      return { x: (i - primeiro + 0.5) * W / qtd, y: 26 + nivel * 56 };
    });
    heap.forEach(function (_, i) {
      if (i === 0) return;
      var pai = Math.floor((i - 1) / 2);
      svg.appendChild(el('line', { x1: pos[pai].x, y1: pos[pai].y, x2: pos[i].x, y2: pos[i].y }));
    });
    heap.forEach(function (x, i) {
      svg.appendChild(el('rect', { x: pos[i].x - 27, y: pos[i].y - 16, width: 54, height: 32, rx: 7,
        'class': 'no p' + x.p + (i === 0 ? ' topo-heap' : '') }));
      svg.appendChild(el('text', { x: pos[i].x, y: pos[i].y - 2 }, x.p + '·' + x.h));
      svg.appendChild(el('text', { x: pos[i].x, y: pos[i].y + 11, style: 'font-weight:400;font-size:9px' }, x.nome.replace('Residente ', 'Res. ')));
    });
    if (!heap.length) svg.appendChild(el('text', { x: W / 2, y: H / 2, style: 'font-weight:400' }, 'Heap vazio: adicione um alerta'));
    arvore.appendChild(svg);

    vetor.innerHTML = '';
    heap.forEach(function (x, i) {
      var s = document.createElement('span');
      s.innerHTML = '<i>[' + i + ']</i>' + x.p + '·' + x.h;
      vetor.appendChild(s);
    });
    if (!heap.length) vetor.textContent = '[ ]';
    btnPop.disabled = !heap.length;
    document.getElementById('sim-add').disabled = heap.length >= MAX;
    document.getElementById('sim-crit').disabled = heap.length >= MAX;
  }

  function novo(p) {
    return { p: p, h: p === 1 ? '00:00' : hora(), nome: aleat(NOMES), rem: p === 1 ? 'Amoxicilina terminou' : aleat(REMEDIOS) };
  }
  function iniciar() {
    heap = []; log.innerHTML = '';
    [novo(2), novo(3), novo(2), novo(3), novo(2)].forEach(function (x) { heap.push(x); subir(heap.length - 1); });
    registrar('Heap montado com 5 alertas de exemplo. O topo (posição 0) é sempre o mais urgente.');
    desenhar();
  }

  document.getElementById('sim-add').addEventListener('click', function () {
    push(novo(Math.random() < 0.5 ? 2 : 3)); desenhar();
  });
  document.getElementById('sim-crit').addEventListener('click', function () { push(novo(1)); desenhar(); });
  btnPop.addEventListener('click', function () { pop(); desenhar(); });
  document.getElementById('sim-reset').addEventListener('click', iniciar);
  iniciar();
})();
