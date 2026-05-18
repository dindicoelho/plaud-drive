# Deploy no Railway

Esse bot tem que rodar 24/7 pra o check diário das 21h (BRT) funcionar e pra você poder conversar com ele a qualquer hora. Railway é o caminho mais simples: deploy via Dockerfile + volume persistente pros arquivos de config/state.

## Pré-requisitos

- Conta Railway (https://railway.app)
- Railway CLI: `brew install railway` (ou `npm i -g @railway/cli`)
- Os tokens já gerados localmente: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`
- Os arquivos `users/dindi.json` e `users/dindi_drive_creds.json` (já existem)

## Setup inicial (uma vez)

```bash
cd /Users/dindicoelho/plaud-drive

railway login                 # abre browser
railway init                  # cria projeto novo (ou link com `railway link`)
railway add --plugin volume   # cria volume (mount em /data, declarado no Dockerfile)
```

Setar variáveis de ambiente:

```bash
railway variables set TELEGRAM_BOT_TOKEN="..."
railway variables set ANTHROPIC_API_KEY="sk-ant-..."
# Opcional, força um modelo específico no agente:
# railway variables set ANTHROPIC_MODEL="claude-sonnet-4-5"
```

## Subir os arquivos de estado pro volume

O volume `/data` no container começa vazio. Você precisa colocar lá:

- `dindi.json` — config do usuário (chat_id, plaud_token, lista de clientes, drive_root_folder_id)
- `dindi_drive_creds.json` — token OAuth do Google Drive

A maneira mais simples é mandar uma vez via `railway run` ou subir os arquivos pra um deploy preliminar e copiá-los. Caminho recomendado:

1. Faz o primeiro deploy:
   ```bash
   railway up
   ```
2. Depois que o serviço subir e atachar o volume, abra um shell:
   ```bash
   railway shell
   # ou: railway run bash
   ```
3. Dentro do container, crie os arquivos com o conteúdo dos locais. Por exemplo:
   ```bash
   cat > /data/dindi.json <<'EOF'
   {
     "name": "Dindi",
     "telegram_chat_id": 8370451563,
     "plaud_token": "...",
     "plaud_origin": "https://api.plaud.ai",
     "clients": ["Interno", "Ininterrupta"],
     "drive_root_folder_id": "..."
   }
   EOF
   cat > /data/dindi_drive_creds.json <<'EOF'
   { ... cole o JSON ... }
   EOF
   ```
4. Reinicie:
   ```bash
   railway restart
   ```

Alternativa (sem shell): use `railway run --service <name>` apontando pra um script que escreve via stdin.

## Deploys subsequentes

```bash
git push  # se vinculou GitHub
# ou
railway up
```

## Logs

```bash
railway logs --tail
```

## Variáveis disponíveis no container

| Variável | Default | Função |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | obrigatória |
| `ANTHROPIC_API_KEY` | — | obrigatória |
| `DATA_DIR` | `/data` | onde ficam users/, state, creds OAuth |
| `LOG_PATH` | `/data/bot.log` | rotated handler ainda funciona |
| `LOCK_PATH` | `/data/plaud-drive.lock` | singleton lock |

## Como verificar que tá funcionando

1. `railway logs --tail` deve mostrar `Bot rodando...` e `daily_check agendado para 21:00:00`
2. No Telegram, manda `/start` — deve responder
3. Manda "o que tem novo?" — deve listar gravações recentes (chama o agente)
4. Espere uma 21h SP — deve aparecer log `daily_check: iniciando varredura`

## Rollback / problemas comuns

- **"TELEGRAM_BOT_TOKEN não encontrado"**: variável não setada — `railway variables`.
- **"Credenciais inválidas em /data/dindi_drive_creds.json"**: arquivo OAuth não foi pro volume, ou expirou. Refaz o upload, ou roda `setup_drive.py` local e copia o novo.
- **Bot reinicia em loop**: olhe `railway logs --tail` — provavelmente erro de import ou variável faltando.
- **Telegram não responde**: pode ter outro processo conectado ao bot (ex.: você reiniciou o local). Cheque com `curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo` — não deve ter webhook ativo.
- **getUpdates 409 Conflict**: dois processos estão pedindo updates simultaneamente. Provavelmente é o launchd local — veja seção abaixo.

## Desligar o bot local (após o Railway estar no ar)

Tem um launchd plist em `~/Library/LaunchAgents/com.plaud-drive.bot.plist` que ressuscita o bot local a cada reboot do Mac. Depois que o Railway estiver estável:

```bash
launchctl unload ~/Library/LaunchAgents/com.plaud-drive.bot.plist
# pra remover de vez:
rm ~/Library/LaunchAgents/com.plaud-drive.bot.plist
```

Se reativar o local algum dia, só **um** dos dois pode estar ligado por token Telegram, senão dá `409 Conflict` no getUpdates.
