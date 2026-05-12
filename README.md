# Plaud Drive

> **[English version here](README.en.md)**

Bot no Telegram que pega suas gravações do [Plaud](https://plaud.ai), gera resumos padronizados com Claude, e organiza tudo no Google Drive por cliente ou projeto.

## O que faz

1. Você grava reuniões no Plaud normalmente
2. **Todo dia às 21h** o bot checa o Plaud, gera resumos das gravações novas, e te manda no Telegram: *"🆕 3 reuniões prontas — /validar"*
3. Você manda `/validar`, confirma ou corrige a pasta/tipo de cada uma com um toque
4. Tudo é salvo no seu Google Drive, organizado por cliente

Se preferir rodar manualmente em vez de esperar 21h, manda `/processar` a qualquer hora.

Também tem o `/evolucao [cliente]` — ele lê todos os resumos de um cliente e gera uma análise de como o projeto evoluiu ao longo do tempo. Funciona de forma incremental: na segunda vez que você pede, ele lê a análise anterior + só as notas novas.

## 4 tipos de resumo

O Claude detecta automaticamente o tipo da gravação e usa o template certo:

| Tipo | Pra quê | O que o resumo foca |
|---|---|---|
| 🤝 Reunião | Calls com clientes, alinhamentos | Decisões, próximos passos, participantes |
| 💭 Nota pessoal | Você falando sozinha, brainstorm | Ideias-chave, to-dos, conexões |
| 🧠 Terapia | Sessões terapêuticas | Temas, insights, como se sentiu |
| 🎤 Palestra | Talks, aulas, eventos | Conceitos-chave, referências, takeaways |

Se errar o tipo, você corrige no Telegram e o resumo é regenerado.

## Estrutura no Google Drive

```
📁 plaud-drive/
└── 📁 Reuniões/
    ├── 📁 Cliente Alpha/
    │   ├── 2026-03-15 - Kickoff do projeto.md
    │   ├── 2026-03-22 - Review sprint 1.md
    │   └── _evolucao_2026-04-04.md
    ├── 📁 Cliente Beta/
    │   └── 2026-04-02 - Alinhamento.md
    └── 📁 Interno/
        └── 2026-04-03 - Daily.md
```

## Multi-usuário

Cada pessoa tem sua própria config com seu Plaud, seu Drive e seu Telegram. O bot reconhece quem mandou a mensagem e usa as credenciais certas. Duas (ou mais) pessoas podem usar o mesmo bot, cada uma com seus dados separados.

---

## Setup

Você vai precisar criar 3 coisas: um bot no Telegram, uma API key na Anthropic, e um app no Google Cloud. Parece muito, mas leva uns 15 minutos no total.

### Pré-requisitos

- Python 3.12+
- Uma conta no [Plaud](https://plaud.ai) com transcrições automáticas ativas
- Um dispositivo Plaud (Plaud Note, NotePin, etc.)

### 1. Clonar e instalar

```bash
git clone https://github.com/SEU-USUARIO/plaud-drive.git
cd plaud-drive
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Criar o bot no Telegram

1. Abre o Telegram e busca **@BotFather**
2. Manda `/newbot`
3. Escolhe um nome (ex: `Plaud Drive`)
4. Escolhe um username que termine em `bot` (ex: `meu_plaud_drive_bot`)
5. Copia o **token** que ele te dá (tipo `7123456789:AAH...`)

Agora pega teu **chat_id**:

6. Busca **@userinfobot** no Telegram
7. Manda qualquer mensagem
8. Copia o **Id** que ele responde (tipo `123456789`)

### 3. Criar API Key na Anthropic

1. Abre [console.anthropic.com](https://console.anthropic.com)
2. Cria conta (ou faz login)
3. Vai em **Settings** → **Billing** → adiciona créditos (US$ 5-10 é suficiente pra começar)
4. Vai em **API Keys** → **Create Key**
5. Copia a key (começa com `sk-ant-...`)

**Custo:** cada reunião processada gasta ~US$ 0,01-0,03. Pra 25 reuniões/semana fica ~US$ 1-3/mês.

### 4. Criar app no Google Cloud

Esse é o passo mais longo, mas é só uma vez.

**Criar o projeto:**

1. Abre [console.cloud.google.com](https://console.cloud.google.com)
2. Faz login com a **mesma conta Google do Drive** que você quer usar
3. No seletor de projeto (topo da página) → **Novo Projeto**
4. Nome: `plaud-drive` → **Criar**
5. Seleciona o projeto criado

**Ativar a API do Drive:**

6. Menu lateral → **APIs e Serviços** → **Biblioteca**
7. Busca **"Google Drive API"**
8. Clica nela → **Ativar**

**Configurar tela de consentimento:**

9. Menu lateral → **APIs e Serviços** → **Tela de permissão OAuth**
10. Tipo de usuário: **Externo** → **Criar**
11. Preenche nome do app (`plaud-drive`), e-mail de suporte e e-mail do desenvolvedor (use o seu nos dois)
12. Clica **Salvar e continuar** em todas as telas até o fim
13. Em **Publicação**, clica **Publicar app** se disponível (evita que o token expire a cada 7 dias)

**Criar credenciais:**

14. Menu lateral → **APIs e Serviços** → **Credenciais**
15. **Criar Credenciais** → **ID do cliente OAuth**
16. Tipo de aplicativo: **App para computador**
17. Nome: `plaud-drive`
18. **Criar**
19. Copia o **Client ID** e o **Client Secret**

### 5. Pegar o token do Plaud

1. Abre [web.plaud.ai](https://web.plaud.ai) no Chrome e faz login
2. Aperta **F12** (abre o DevTools)
3. Clica na aba **Application**
4. No menu esquerdo: **Local Storage** → `https://web.plaud.ai`
5. Procura um valor que começa com `eyJ...` (é um texto longo)
6. Copia o valor inteiro

### 6. Configurar

Cria o arquivo `.env` na raiz do projeto:

```bash
cp .env.example .env
```

Abre o `.env` e preenche:

```
TELEGRAM_BOT_TOKEN=7123456789:AAH...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxx
```

Cria teu arquivo de usuário:

```bash
cp users/exemplo.json users/seunome.json
```

Abre `users/seunome.json` e preenche:

```json
{
  "name": "SeuNome",
  "telegram_chat_id": 123456789,
  "plaud_token": "eyJ...",
  "plaud_origin": "https://api.plaud.ai",
  "clients": [
    "Interno"
  ]
}
```

### 7. Autorizar o Google Drive

Roda uma vez — abre o browser pra você fazer login e autorizar:

```bash
source .venv/bin/activate
python setup_drive.py seunome
```

### 8. Rodar o bot

```bash
source .venv/bin/activate
python bot.py
```

Abre o Telegram, manda `/start` pro seu bot. A partir daí, todo dia às 21h ele checa as gravações novas e te avisa pra `/validar` — ou manda `/processar` agora pra rodar na hora.

---

## Comandos

| Comando | O que faz |
|---|---|
| `/start` | Verifica se sua config tá ok |
| `/validar` | Abre as reuniões engatilhadas pelo check diário |
| `/processar` | Puxa as últimas 20 gravações agora (manual) |
| `/processar 14` | Em vez do limite de 20, varre os últimos 14 dias |
| `/evolucao` | Lista os clientes disponíveis |
| `/evolucao Nome do Cliente` | Gera (ou atualiza) análise de evolução do cliente |
| `/cancel` | Cancela o fluxo de validação (o pending fica preservado pra retomar com `/validar`) |

## Check diário

Todo dia às 21h (America/Sao_Paulo) o bot:

1. Olha as últimas 20 gravações no Plaud
2. Filtra as que ele ainda não viu (estado guardado em `users/<nome>_state.json`)
3. Gera resumos com Claude e enfileira em `pending`
4. Te manda **uma** mensagem agrupada — só se houver gravação nova; senão fica quieto

A fila pendente sobrevive a restart do bot. `/cancel` no meio da validação não perde nada — `/validar` retoma de onde parou.

---

## Adicionar outra pessoa

O mesmo bot pode atender múltiplas pessoas. Cada uma precisa:

1. Pegar o **chat_id** dela com @userinfobot no Telegram
2. Pegar o **token do Plaud** dela em `web.plaud.ai` (conta dela)
3. Criar `users/nomedela.json` com os dados
4. Rodar `python setup_drive.py nomedela` (ela faz login no Google dela)

Depois é só ela abrir o chat com o mesmo bot e mandar `/start`.

---

## Notas

- **Token do Plaud:** vem da API web não-oficial. Pode expirar eventualmente — se o bot der erro de autenticação, refaça o passo 5.
- **O bot precisa estar rodando** pra responder no Telegram. Se você fechar o terminal, ele para. Pra rodar em background: `nohup python bot.py &`
- **Pra rodar permanente**, considere usar um servidor (VPS, Raspberry Pi) ou um serviço como Railway/Fly.io.
- Os resumos são gerados pelo Claude (Sonnet) via API. O conteúdo das suas gravações é enviado pra API da Anthropic para processamento.

---

## Licença

MIT
