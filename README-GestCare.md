# GestCare

Sistema completo de gestão para instituições de cuidado a idosos (lares de idosos, casas de repouso, clínicas de longa permanência), desenvolvido para digitalizar e organizar toda a rotina de cuidado, administração e comunicação com as famílias.

## Sobre o projeto

O GestCare nasceu para resolver um problema real: instituições de cuidado a idosos, em geral, ainda controlam informações críticas (medicação, prontuário, rotina) em papel ou planilhas soltas. O sistema centraliza essas informações em um só lugar, com controle de acesso por perfil de usuário e portal exclusivo para familiares acompanharem a situação do idoso.

Projeto desenvolvido originalmente como Projeto Integrador (UNISAL), evoluído para um sistema pronto para uso real em instituições.

## ✨ Funcionalidades

-Cadastro e prontuário do residente** — histórico médico completo
-Controle de medicação** — agendamento e registro de administração de medicamentos
-Registros psicológicos** — acompanhamento de saúde mental e comportamental
-Controle nutricional** — acompanhamento dietético individualizado
-Portal da família** — acesso somente leitura para familiares acompanharem o residente
-Registro de visitas familiares**
-Autenticação e controle de acesso (RBAC)** — permissões por tipo de usuário (administração, cuidadores, família)
-PWA mobile-first** — interface otimizada para uso em celular/tablet no dia a dia da instituição

## Tecnologias utilizadas

- **Backend:** Python, Flask (arquitetura MVC / Blueprints)
- **Banco de dados:** SQLite
- **Frontend:** HTML5, CSS3, JavaScript (PWA)
- **Infraestrutura:** roda como serviço Windows (via NSSM), permitindo operação contínua em servidor local da instituição

## Como rodar o projeto

```bash
# Clone o repositório
git clone https://github.com/ArthurSouzaDeveloper/GestCare.git
cd GestCare

# Crie e ative um ambiente virtual
python -m venv venv
venv\Scripts\activate      # Windows
source venv/bin/activate   # Linux/Mac

# Instale as dependências
pip install -r requirements.txt

# Configure as variáveis de ambiente (se aplicável)
# copie .env.example para .env e ajuste os valores

# Rode as migrações / inicialize o banco
python init_db.py

# Inicie o servidor
python app.py
```

Acesse em `http://localhost:5000`

## Screenshots

> _Adicionar prints do sistema aqui (dashboard, prontuário, portal da família)_

## Roadmap

- [ ] Publicação como app mobile (Android/iOS)
- [ ] Relatórios exportáveis em PDF
- [ ] Notificações automáticas para família

## Licença

Este projeto está sob a licença MIT — veja o arquivo [LICENSE](LICENSE) para mais detalhes.

## Contato

Desenvolvido por **Arthur Souza** — [LinkedIn](#) · [GitHub](https://github.com/ArthurSouzaDeveloper)
