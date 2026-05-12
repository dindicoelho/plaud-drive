# Plaud Drive

> **[Versão em português aqui](README.md)**

Telegram bot that pulls your [Plaud](https://plaud.ai) recordings, generates structured summaries with Claude, and organizes everything in Google Drive by client or project.

## What it does

1. You record meetings with Plaud as usual
2. **Every day at 9pm** the bot checks Plaud, generates summaries for new recordings, and sends you a Telegram message: *"🆕 3 meetings ready — /validar"*
3. You send `/validar`, confirm or correct the folder/type of each one with a tap
4. Everything is saved to your Google Drive, organized by client

If you'd rather run it manually instead of waiting for 9pm, send `/processar` any time.

There's also `/evolucao [client]` — it reads all summaries for a client and generates an analysis of how the project evolved over time. Works incrementally: the second time you run it, it reads the previous analysis + only the new notes.

## 4 summary types

Claude automatically detects the recording type and uses the right template:

| Type | For | Summary focuses on |
|---|---|---|
| 🤝 Meeting | Client calls, standups, alignments | Decisions, next steps, attendees |
| 💭 Personal note | Talking to yourself, brainstorming | Key ideas, to-dos, connections |
| 🧠 Therapy | Therapy sessions | Topics, insights, how you felt |
| 🎤 Talk/Event | Talks, lectures, conferences | Key concepts, references, takeaways |

If it gets the type wrong, you correct it on Telegram and the summary is regenerated.

## Google Drive structure

```
📁 plaud-drive/
└── 📁 Reuniões/
    ├── 📁 Client Alpha/
    │   ├── 2026-03-15 - Project kickoff.md
    │   ├── 2026-03-22 - Sprint 1 review.md
    │   └── _evolucao_2026-04-04.md
    ├── 📁 Client Beta/
    │   └── 2026-04-02 - Alignment call.md
    └── 📁 Internal/
        └── 2026-04-03 - Daily standup.md
```

## Multi-user

Each person has their own config with their own Plaud account, Drive, and Telegram chat. The bot identifies who sent the message and uses the right credentials. Two (or more) people can share the same bot, each with fully separate data.

---

## Setup

You'll need to create 3 things: a Telegram bot, an Anthropic API key, and a Google Cloud app. Sounds like a lot, but it takes about 15 minutes total.

### Prerequisites

- Python 3.12+
- A [Plaud](https://plaud.ai) account with automatic transcriptions enabled
- A Plaud device (Plaud Note, NotePin, etc.)

### 1. Clone and install

```bash
git clone https://github.com/YOUR-USERNAME/plaud-drive.git
cd plaud-drive
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create the Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Pick a name (e.g. `Plaud Drive`)
4. Pick a username ending in `bot` (e.g. `my_plaud_drive_bot`)
5. Copy the **token** it gives you (looks like `7123456789:AAH...`)

Now get your **chat_id**:

6. Search for **@userinfobot** on Telegram
7. Send it any message
8. Copy the **Id** it replies with (a number like `123456789`)

### 3. Create an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account (or log in)
3. Go to **Settings** → **Billing** → add credits (US$ 5-10 is enough to start)
4. Go to **API Keys** → **Create Key**
5. Copy the key (starts with `sk-ant-...`)

**Cost:** each processed meeting costs ~US$ 0.01-0.03. For 25 meetings/week that's ~US$ 1-3/month.

### 4. Create a Google Cloud app

This is the longest step, but you only do it once.

**Create the project:**

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Log in with the **same Google account** as the Drive you want to use
3. In the project selector (top of page) → **New Project**
4. Name: `plaud-drive` → **Create**
5. Select the newly created project

**Enable the Drive API:**

6. Side menu → **APIs & Services** → **Library**
7. Search for **"Google Drive API"**
8. Click it → **Enable**

**Configure the consent screen:**

9. Side menu → **APIs & Services** → **OAuth consent screen**
10. User type: **External** → **Create**
11. Fill in app name (`plaud-drive`), support email and developer email (use yours for both)
12. Click **Save and continue** through all screens
13. Under **Publishing status**, click **Publish app** if available (prevents the token from expiring every 7 days)

**Create credentials:**

14. Side menu → **APIs & Services** → **Credentials**
15. **Create Credentials** → **OAuth client ID**
16. Application type: **Desktop app**
17. Name: `plaud-drive`
18. **Create**
19. Copy the **Client ID** and **Client Secret**

### 5. Get the Plaud token

1. Open [web.plaud.ai](https://web.plaud.ai) in Chrome and log in
2. Press **F12** (opens DevTools)
3. Click the **Application** tab
4. In the left sidebar: **Local Storage** → `https://web.plaud.ai`
5. Look for a value starting with `eyJ...` (it's a long string)
6. Copy the entire value

### 6. Configure

Create the `.env` file in the project root:

```bash
cp .env.example .env
```

Open `.env` and fill it in:

```
TELEGRAM_BOT_TOKEN=7123456789:AAH...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxx
```

Create your user file:

```bash
cp users/exemplo.json users/yourname.json
```

Open `users/yourname.json` and fill it in:

```json
{
  "name": "YourName",
  "telegram_chat_id": 123456789,
  "plaud_token": "eyJ...",
  "plaud_origin": "https://api.plaud.ai",
  "clients": [
    "Internal"
  ]
}
```

### 7. Authorize Google Drive

Run once — it opens the browser for you to log in and authorize:

```bash
source .venv/bin/activate
python setup_drive.py yourname
```

### 8. Run the bot

```bash
source .venv/bin/activate
python bot.py
```

Open Telegram, send `/start` to your bot. From there, every day at 9pm it checks for new recordings and pings you to `/validar` — or send `/processar` right now to run on demand.

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Checks if your config is set up correctly |
| `/validar` | Opens meetings queued by the daily check |
| `/processar` | Pulls the last 20 recordings right now (manual) |
| `/processar 14` | Instead of the 20-recording cap, scans the last 14 days |
| `/evolucao` | Lists available clients |
| `/evolucao Client Name` | Generates (or updates) an evolution analysis for the client |
| `/cancel` | Cancels the validation flow (pending queue is preserved — resume with `/validar`) |

## Daily check

Every day at 9pm (America/Sao_Paulo) the bot:

1. Looks at the last 20 recordings on Plaud
2. Filters out the ones it has already seen (state stored in `users/<name>_state.json`)
3. Generates summaries with Claude and queues them in `pending`
4. Sends you **one** grouped Telegram message — only if there's something new; otherwise stays quiet

The pending queue survives a bot restart. `/cancel` mid-validation doesn't lose anything — `/validar` resumes where you left off.

---

## Adding another person

The same bot can serve multiple people. Each person needs to:

1. Get their **chat_id** from @userinfobot on Telegram
2. Get their **Plaud token** from `web.plaud.ai` (their own account)
3. Create `users/theirname.json` with their info
4. Run `python setup_drive.py theirname` (they log in with their own Google account)

Then they just open the chat with the same bot and send `/start`.

---

## Notes

- **Plaud token:** comes from the unofficial web API. It may expire eventually — if the bot gives an auth error, redo step 5.
- **The bot must be running** to respond on Telegram. If you close the terminal, it stops. To run in the background: `nohup python bot.py &`
- **For permanent hosting**, consider using a server (VPS, Raspberry Pi) or a service like Railway/Fly.io.
- Summaries are generated by Claude (Sonnet) via API. Your recording transcripts are sent to Anthropic's API for processing.

---

## License

MIT
