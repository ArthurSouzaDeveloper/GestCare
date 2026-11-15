# Prompt — Redesign de UI/UX do GestCare

> Cole o conteúdo abaixo (da linha `## PAPEL` até o fim) como mensagem para o Claude Code
> dentro do repositório GestCare. Está escrito para ser executado em fases, com critérios
> de aceite verificáveis.

---

## PAPEL

Você é um Product Designer sênior especializado em **software clínico/institucional**, com
experiência em design systems, acessibilidade WCAG 2.2 e front-end (CSS moderno, Jinja2).
Você não é um gerador de telas bonitas: você é responsável por um produto que **enfermeiros
usam em plantão, com pressa, uma mão ocupada e luz ruim**, e por um portal que **familiares
idosos ou pouco letrados digitalmente** acessam pelo celular.

Sua missão é elevar o GestCare do nível "projeto feito com IA" para o nível de **produto SaaS
de saúde profissional, com identidade própria e credibilidade clínica**.

---

## CONTEXTO DO PRODUTO

**GestCare** — sistema de gestão para casas de repouso / instituições de cuidado (ILPI).

- **Stack:** Flask + Jinja2, CSS puro (sem framework), PWA com service worker, ícones Tabler
  via webfont, fonte Plus Jakarta Sans via Google Fonts.
- **Arquitetura de arquivos:**
  - `static/css/style.css` — ~850 linhas, folha única, todo o visual do sistema
  - `templates/base.html` — shell: sidebar (desktop) + header mobile + bottom nav + rodapé
  - `templates/login.html` — página isolada com `<style>` inline próprio
  - `templates/*.html` — 20 telas (maior: `paciente_detalhe.html`, 621 linhas)
  - `static/js/app.js` — busca/filtros client-side, toast, notificações push
- **Perfis de usuário (4), cada um com navegação diferente:**
  1. `enfermeiro` — operação diária: agenda de medicação, evoluções, exames
  2. `admin` — gestão da casa + usuários
  3. `superadmin` — gestão de múltiplas casas
  4. `responsavel` — portal da família, somente leitura (topbar + bottom nav, sem sidebar)
- **Idioma:** tudo em pt-BR, **inclusive nomes de classes CSS** (`.card-paciente`,
  `.botao-primario`, `--cor-primaria`). Essa convenção deve ser **mantida**.
- **Contexto de uso real:** celular na mão durante o plantão, tablet no posto de enfermagem,
  desktop na administração, impressão de fichas e prescrições em papel.

---

## AUDITORIA — PROBLEMAS JÁ IDENTIFICADOS

Trate esta lista como diagnóstico confirmado. Confirme cada item no código antes de corrigir e
**acrescente** o que mais encontrar.

### A. O "aspecto de IA" (prioridade máxima)

1. **Gradiente decorativo + blobs** — `login.html` usa
   `linear-gradient(160deg, primaria, primaria-escura)` com dois círculos
   `rgba(255,255,255,.06)` em `::before`/`::after`. Mesmo padrão na `.topbar-familia`.
   É a assinatura visual mais reconhecível de interface gerada por IA.
2. **Tudo em peso 700/800** — `.titulo-pagina`, `.secao-titulo`, `.card-paciente-nome`,
   `.form-label`, `.badge-*`, `.nav-bottom-item`, `.tab-item`, até labels de 12px. Sem
   hierarquia tipográfica não existe hierarquia de informação.
3. **`letter-spacing` negativo global** no `body` (−0.1px) e mais negativo nos títulos
   (−0.3/−0.4/−0.5px) — tique estético, não decisão de legibilidade.
4. **Sombra teatral** — `--sombra-md: 0 30px 70px -30px rgba(15,76,87,.4)` em hover de card e
   no card de login. Software clínico não levita.
5. **Raios inconsistentes** — tokens 15/11/8px, mas o card de login usa `22px` hardcoded e
   tags usam `4px`/`5px`/`20px` ad-hoc.
6. **Copy de landing page dentro do produto** — "Cuidar de quem um dia cuidou de você." no
   login. Decida se é marca ou enfeite; se for marca, trate como marca (não como frase solta
   sobre gradiente).
7. **Ícone genérico `ti-heartbeat`** fazendo papel de logo, dentro de um quadrado branco
   arredondado. Não é identidade visual.

### B. Design system inexistente

8. **Estilos inline espalhados pelos templates** — `index.html` tem bloco de alerta com
   `style="border-left:4px solid var(--cor-erro);background:#fff5f5"`; `base.html` tem
   `style="display:flex;align-items:center;gap:14px"`, `style="font-size:1.05rem"`,
   `style="background:rgba(255,255,255,.16)"`. Mapeie **todos** e promova a componentes.
9. **Sem escala de espaçamento** — valores ad-hoc: `.85rem`, `.9rem`, `1.1rem`, `1.25rem`,
   `18px`, `14px`. Nenhum grid de 4/8px.
10. **Sem escala tipográfica** — `14.5px`, `12.5px`, `.82rem`, `.72rem`, `.68rem`, `1.35rem`
    convivendo sem sistema.
11. **`--sombra: none`** declarado e aplicado em vários componentes — token morto. Não existe
    escala de elevação.
12. **Cores de estado incompletas** — CSS define `.alerta-sucesso` e `.alerta-erro`, mas o
    `base.html` renderiza `alerta-{{ categoria }}` com a categoria vinda do `flash()`. Audite
    `app.py`: qualquer categoria fora dessas duas (`alerta`, `info`, `warning`) renderiza
    **sem estilo**. Cubra todas.
13. **Inputs sem estado de erro** — não existe `.form-input.erro`; validação só existe no JS de
    CPF. Sem desenho de erro inline, mensagem de campo ou `aria-invalid`.

### C. Arquitetura de navegação quebrada

14. **Bottom nav com até 6 itens** (Pacientes, Usuários/Agenda, Cadastrar, Avisos, Perfil,
    Sair) com labels de `0.68rem`. Limite praticável é 5; alvos ficam estreitos e o texto
    ilegível.
15. **"Sair" como aba de navegação** — ação destrutiva de sessão no mesmo nível de navegação
    primária, no mobile *e* na sidebar. Pertence ao menu de conta.
16. **Sidebar e bottom nav divergem** — para `admin`, a sidebar mostra Agenda *e* Usuários; a
    bottom nav troca Agenda por Usuários. O mesmo usuário tem dois mapas mentais diferentes
    do mesmo app.
17. **FAB redundante e desaparecido** — `.fab` (novo paciente) existe no mobile e é
    `display:none` no desktop, onde a mesma ação vira item de sidebar. A ação primária muda de
    natureza conforme o viewport.
18. **Estado ativo sem semântica** — apenas a classe `.ativo`; falta `aria-current="page"`.

### D. Acessibilidade (bloqueante)

19. **Contraste reprovado** — `--cor-texto-terciario: #8b9a9a` sobre branco ≈ **3:1**
    (reprova para texto); `--cor-texto-suave: #6b7a7a` ≈ **4.5:1** sobre branco, mas cai
    abaixo do mínimo sobre `--cor-fundo`/`--cor-canvas`, onde é usado. Recalcule e corrija
    **todos** os pares cor/fundo.
20. **Sem `:focus-visible`** — foco visível existe só em `.form-input`/`.busca-input`.
    Botões, links, itens de nav, abas, FAB e `.btn-icone` são inoperáveis por teclado com
    segurança.
21. **Sem skip link** para o conteúdo principal.
22. **"Tabelas" que não são tabelas** — `.tabela-cabecalho`/`.tabela-linha` e
    `.usuario-linha` são grids de `div`. Sem semântica de tabela, leitor de tela não relaciona
    célula e cabeçalho.
23. **Toast sem região live** — `.toast` aparece sem `role="status"`/`aria-live`, e
    `white-space: nowrap` trunca mensagem longa no celular.
24. **Sem `prefers-reduced-motion`** — `slideUpModal` e transições não são neutralizadas.
25. **Modal sem foco preso** — sem focus trap, sem `Esc`, sem `aria-modal`, sem devolver o
    foco ao disparador.
26. **Ícones decorativos vs. semânticos** — `aria-hidden="true"` é aplicado de forma
    consistente, mas botões só-ícone (`.btn-icone`, `.cpf-toggle`, `.modal-fechar`) precisam de
    `aria-label` verificado em todos os templates.

### E. Lacunas de UX de produto

27. **Sem modo escuro** — plantão noturno em quarto de paciente com tela branca a 100%.
    Requisito funcional, não preferência estética.
28. **Sem estados de carregamento** — nenhum skeleton, nenhum botão em estado "enviando".
    Formulários longos (`paciente_form.html`, 400 linhas) podem ser submetidos duas vezes.
29. **`erro.html` com 11 linhas** — página de erro sem desenho, sem saída, sem orientação.
30. **Estado vazio único e genérico** — `.vazio` serve a todas as telas com o mesmo texto
    estrutural. Cada lista vazia tem um significado diferente (nunca cadastrou × filtro não
    encontrou nada × sem permissão).
31. **Busca e filtros sem feedback de resultado zero** — verifique `app.js`: filtrar até
    esvaziar a lista precisa de um estado dedicado com ação de limpar filtros.
32. **Impressão subdesenvolvida** — o `@media print` só esconde cromo. Casa de repouso
    imprime ficha de paciente e mapa de medicação; isso precisa de layout próprio
    (cabeçalho com nome da casa, paciente, data, quebras de página coerentes).
33. **Hierarquia clínica ausente na ficha do paciente** — `paciente_detalhe.html` (621
    linhas) trata alergia, condição grave, antibiótico vencendo e observação administrativa com
    peso visual semelhante. Informação que evita dano precisa de prioridade visual explícita.
34. **Alerta de antibiótico fragilizado** — é o recurso de segurança mais importante do
    sistema e hoje é um bloco com estilo inline em `index.html`.
35. **Dependências de CDN sem versão fixa** —
    `@tabler/icons-webfont@latest` quebra o visual sem aviso, causa FOUT/CLS e falha offline,
    contrariando a proposta de PWA. Fixe versão e tenha fallback.
36. **Arquivo órfão** — existe `static/js/base.html`. Investigue e remova se for resíduo.

---

## PRINCÍPIOS DE DESIGN A SEGUIR

1. **Clareza acima de estética.** Toda decisão visual precisa responder: isso ajuda alguém a
   não errar medicação?
2. **Densidade de informação alta, mas respirável.** Software profissional mostra mais dados
   por tela que um app de consumo — sem virar planilha.
3. **Identidade sóbria e específica.** Paleta teal/âmbar atual é defensável: refine-a em vez de
   trocar por outra moda. Marca se expressa em tipografia, espaçamento, forma dos componentes
   e microcópia — não em gradiente.
4. **Cor é informação, não decoração.** Reserve saturação para estado e risco. Nunca use cor
   como único portador de significado (adicione ícone ou texto).
5. **Mobile-first de verdade.** O CSS já é mobile-first (`min-width` nos breakpoints):
   preserve isso. Alvo mínimo de toque 44×44px, zona de alcance do polegar.
6. **Acessibilidade é requisito.** WCAG 2.2 AA é o piso, não a meta.
7. **Consistência rende velocidade.** O mesmo conceito tem a mesma aparência em todas as 20
   telas.
8. **Mudança incremental e segura.** Nada de reescrever tudo: o sistema está em produção.

---

## ENTREGA — EXECUTE EM FASES, UMA POR VEZ

Pare ao fim de cada fase, resuma o que mudou e aguarde confirmação antes de seguir.

### Fase 0 — Auditoria e plano
- Leia `static/css/style.css`, `templates/base.html`, `templates/login.html` e todos os
  templates.
- Confirme/refute cada item da auditoria acima, citando `arquivo:linha`.
- Liste o que **mais** encontrou.
- Calcule o contraste real de todos os pares cor-texto/cor-fundo usados e monte uma tabela
  com razão atual, status AA e valor corrigido proposto.
- Inventarie **todos** os estilos inline dos templates e mapeie cada um para o componente CSS
  que vai substituí-lo.
- Entregue um plano de fases com arquivos afetados e risco de regressão por fase.
- **Não altere nenhum arquivo nesta fase.**

### Fase 1 — Fundação de tokens
- Reorganize `:root` em camadas explícitas: **primitivos** (paleta bruta, escalas) →
  **semânticos** (`--superficie`, `--superficie-elevada`, `--borda`, `--texto-primario`,
  `--texto-secundario`, `--acao-primaria`, `--risco-alto`…). Componentes só consomem
  semânticos.
- Escala tipográfica com proporção definida (ex. 12 / 13 / 14 / 16 / 20 / 24 / 32) e uso
  **intencional de pesos** (400 corpo, 500 ênfase, 600 títulos; 700+ só em dado numérico
  destacado). Elimine o 800 generalizado.
- Escala de espaçamento em grid de 4px, tokenizada (`--esp-1` … `--esp-12`).
- Escala de elevação real (3 níveis sutis) ou decisão explícita de usar borda em vez de sombra
  — e então remova `--sombra: none` e `--sombra-md`.
- Escala de raio coerente, aplicada também ao card de login.
- Remova `letter-spacing` negativo do corpo; mantenha ajuste ótico apenas em títulos grandes.
- **Dark mode** via `prefers-color-scheme` + atributo `data-tema` para override manual, com
  persistência e sem FOUC.
- Mantenha **todos** os nomes de token atuais como aliases até a migração terminar, para não
  quebrar nenhuma tela.

### Fase 2 — Acessibilidade e fundamentos
- `:focus-visible` consistente em todo elemento interativo (anel de 2px com offset, visível em
  ambos os temas).
- Corrija todos os contrastes reprovados da tabela da Fase 0.
- Skip link em `base.html`; `<main id="conteudo">` como destino.
- `aria-current="page"` nos itens de nav ativos.
- `prefers-reduced-motion: reduce` neutralizando animações e transições.
- Converta as pseudo-tabelas em `<table>` semântica (ou justifique `role="table"` com
  `role="row"`/`role="cell"` se o grid responsivo exigir) — comece por
  `templates/usuarios.html`.
- `aria-label` verificado em todo botão só-ícone.
- Toast com `role="status"`, `aria-live="polite"` e quebra de linha permitida.
- Modal: `role="dialog"`, `aria-modal="true"`, focus trap, fechar com `Esc`, foco devolvido ao
  disparador.
- Hierarquia de headings sem salto em todas as telas.

### Fase 3 — Biblioteca de componentes
Refatore em componentes consistentes, **sem estilo inline nos templates**:
botão (primário / secundário / terciário / perigo, com estados hover, active, focus, disabled,
loading) · campo de formulário (label, ajuda, erro, obrigatório, disabled) · select · textarea ·
busca · card · seção · tabela/lista densa · badge e tag de status · alerta/banner (4 tons) ·
toast · modal/drawer · abas · estado vazio (3 variantes) · skeleton · stat card · avatar ·
paginação se houver.

Para cada um: variantes, estados e tokens que consome. Documente em
`docs/design-system.md` + uma página `/design-system` protegida por login para inspeção visual.

### Fase 4 — Navegação e shell
- Reduza a bottom nav a **no máximo 5 itens**, iguais em significado aos da sidebar.
- Tire "Sair" da navegação: crie **menu de conta** no avatar (perfil, notificações, tema, sair),
  presente nos dois viewports.
- Resolva a divergência sidebar × bottom nav por perfil; documente a matriz
  **perfil × destino** final.
- Decida o destino do FAB: ação primária consistente entre viewports (ou FAB em ambos, ou
  botão de ação no cabeçalho de página em ambos).
- Cabeçalho de página padronizado: título, subtítulo/contagem, ações, breadcrumb onde houver
  profundidade.
- Sidebar: trate densidade, ordem por frequência de uso e separação visual entre navegação e
  rodapé.

### Fase 5 — Telas prioritárias, nesta ordem
1. **`login.html`** — reconstrua sem gradiente decorativo e sem blobs. Credibilidade vem de
   tipografia, espaçamento e hierarquia. Mova o CSS para a folha principal ou para um parcial
   próprio.
2. **`index.html` (lista de pacientes + dashboard)** — hierarquia entre alerta de antibiótico,
   stat cards, busca, filtros e lista. Promova o alerta de antibiótico a componente de
   primeira classe. Trate resultado zero de filtro.
3. **`paciente_detalhe.html`** — triagem visual por risco: alergias e condições graves acima de
   tudo, medicação com antibiótico e data de término em evidência, demais dados subordinados.
   Reavalie se abas são a estrutura certa.
4. **`agenda.html`** — é a tela de plantão: escaneável a distância, por horário, com estado de
   "administrado / atrasado / pendente" inequívoco (cor + ícone + texto).
5. **`paciente_form.html` / `paciente_cadastro`** — formulário longo: progresso, agrupamento,
   validação inline, proteção contra duplo submit, salvar sem perder dados.
6. **Portal do responsável** (`pacienteresponsavel.html`) — público idoso: tipografia maior,
   linguagem sem jargão clínico, menos densidade.
7. **`erro.html`** — página de erro com desenho, explicação e saída.
8. Demais telas por consistência.

### Fase 6 — Polimento e performance
- Fixe a versão do webfont de ícones; avalie subset ou SVG inline para os ícones usados;
  garanta funcionamento offline no PWA.
- Revise carregamento de fonte: só os pesos realmente usados, `font-display: swap`, fallback
  métrico para evitar CLS.
- Micro-interações contidas, sempre com respeito a `reduced-motion`.
- Folha de impressão real para ficha de paciente e mapa de medicação.
- Remova CSS morto e o arquivo órfão `static/js/base.html` se confirmado resíduo.
- Atualize `theme-color` para ambos os temas e revise `manifest.json`.

---

## RESTRIÇÕES — NÃO NEGOCIÁVEIS

- **Não altere comportamento de back-end.** Rotas, modelos, queries, autenticação, CSRF,
  permissões e `app.py` ficam intactos, salvo se o visual exigir um dado novo — e nesse caso
  pergunte antes.
- **Não quebre nenhum template Jinja.** Preserve `{% block %}`, `{% extends %}`, `url_for`,
  `csrf_token()`, `get_flashed_messages`, `session.get(...)` e `versao_assets`.
- **Mantenha classes e tokens em pt-BR.** Ao renomear, mantenha o alias antigo funcionando
  durante a migração.
- **Sem framework CSS novo** (Tailwind, Bootstrap), **sem build step**, **sem dependência JS
  nova**. CSS puro + JS vanilla.
- **Preserve toda funcionalidade existente:** busca e filtros client-side, máscara e
  mascaramento de CPF, notificações push, service worker, PWA instalável, safe-areas de notch,
  `font-size: 1rem` em inputs (evita zoom no iOS).
- **Mobile-first preservado** — breakpoints por `min-width`.
- **Incremental:** o app deve ficar funcional e coerente ao fim de **cada** fase.

---

## PROIBIÇÕES ESTÉTICAS (o "não parecer IA")

Não use, em nenhuma tela:
- gradientes decorativos em cabeçalhos, cards ou botões (gradiente só se tiver função, ex.
  máscara de leitura sobre imagem);
- blobs, círculos translúcidos ou formas abstratas de enfeite em `::before`/`::after`;
- glassmorphism, `backdrop-filter` decorativo, neumorfismo;
- sombras longas e difusas que fazem elementos flutuarem;
- emojis como ícones de interface;
- peso 700/800 como padrão de texto;
- roxo-para-azul, teal-para-verde ou qualquer par "de template";
- `border-radius` exagerado em tudo (pílulas por padrão);
- texto centralizado em blocos longos;
- três cards idênticos lado a lado só para preencher espaço;
- microcópia inflada ("Gerencie tudo em um só lugar", "Simplifique sua rotina",
  "✨ Novidade!");
- ilustrações genéricas de banco de imagens em estados vazios.

**Em vez disso:** espaçamento rigoroso, hierarquia tipográfica resolvida, alinhamento óptico,
bordas de 1px bem escolhidas, cor contida e carregada de significado, densidade pensada, e
microcópia específica do domínio ("Nenhum paciente no quarto 3", não "Nada por aqui").

---

## CRITÉRIOS DE ACEITE

Ao fim de cada fase, demonstre:

1. **Zero** `style="..."` de apresentação nos templates (exceto valor dinâmico vindo do Jinja,
   justificado caso a caso).
2. **Zero** par cor/fundo abaixo de 4.5:1 para texto normal e 3:1 para texto grande e
   componentes — com a tabela de contraste atualizada.
3. Navegação completa por teclado, com foco sempre visível, em: login, lista de pacientes,
   ficha, agenda, formulário de cadastro, modal.
4. Dark mode funcional em todas as telas, sem contraste quebrado e sem flash ao carregar.
5. Bottom nav com no máximo 5 itens, coerente com a sidebar, matriz perfil × destino
   documentada.
6. Nenhum valor mágico de espaçamento, tamanho de fonte ou raio fora dos tokens.
7. `docs/design-system.md` e a página `/design-system` cobrindo todos os componentes.
8. Todas as telas verificadas em 360px, 768px, 1024px e 1440px, nos dois temas — com
   screenshots.
9. Nenhuma funcionalidade perdida: checklist explícito de busca, filtros, CPF, push, PWA,
   impressão, flash messages, CSRF.
10. Relatório final de antes/depois por item da auditoria, com o que ficou pendente e por quê.

---

## COMO TRABALHAR

- Comece pela **Fase 0** e **não escreva código** antes de eu aprovar o plano.
- Em decisões de design com trade-off real, apresente **2 opções com recomendação**, não um
  catálogo.
- Use o Playwright já instalado no ambiente para subir o app e **tirar screenshots reais** de
  antes e depois, nos dois temas e nos quatro viewports.
- Commits pequenos e temáticos na branch de desenvolvimento, com mensagem descrevendo a fase.
- Se encontrar bug funcional ou falha de segurança durante o redesign, **relate e não conserte
  junto** — fora do escopo desta tarefa.
