import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from drive_client import DriveClient
from models import ProcessedRecording
from plaud_client import PlaudClient
from processor import Processor, TEMPLATES

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# States
VALIDATING = 1
CHOOSING_CLIENT = 2
NEW_CLIENT_NAME = 3
CHOOSING_TYPE = 4

USERS_DIR = Path(__file__).parent / "users"


def load_user_config(chat_id: int) -> dict | None:
    for f in USERS_DIR.glob("*.json"):
        with open(f) as fp:
            config = json.load(fp)
        if config.get("telegram_chat_id") == chat_id:
            return config
    return None


def save_user_config(config: dict):
    name = config.get("name", "user").lower().replace(" ", "-")
    path = USERS_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_user_clients(config: dict) -> list[str]:
    return config.get("clients", ["Interno"])


# --- Command handlers ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if config:
        await update.message.reply_text(
            f"Oi {config['name']}! Manda /processar pra começar."
        )
    else:
        await update.message.reply_text(
            f"Não te conheço ainda. Seu chat_id é {chat_id}.\n\n"
            "Pede pra quem configura o sistema adicionar seu chat_id "
            "no arquivo de config em users/."
        )


async def processar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if not config:
        await update.message.reply_text("Você não está configurada. Manda /start.")
        return ConversationHandler.END

    # Parse período: /processar ou /processar 7 (dias) ou /processar 2026-03-01 2026-03-31
    days = 7
    args = context.args or []
    if len(args) == 1 and args[0].isdigit():
        days = int(args[0])
    # TODO: parse date range

    since = datetime.now(timezone.utc) - timedelta(days=days)

    await update.message.reply_text(
        f"⏳ Buscando gravações dos últimos {days} dias no Plaud..."
    )

    # Busca gravações
    try:
        plaud = PlaudClient(token=config["plaud_token"], origin=config.get("plaud_origin", "https://api.plaud.ai"))
        recordings = plaud.get_recordings(since=since)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao acessar Plaud: {e}")
        return ConversationHandler.END

    if not recordings:
        await update.message.reply_text("Nenhuma gravação com transcrição encontrada nesse período.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"📋 {len(recordings)} reuniões encontradas. Gerando resumos..."
    )

    # Processa com Claude
    try:
        processor = Processor(api_key=os.getenv("ANTHROPIC_API_KEY"))
        clients = get_user_clients(config)

        async def on_progress(i, total, title):
            if i % 5 == 0 or i == total:
                await update.message.reply_text(f"⏳ {i}/{total} processadas...")

        processed = []
        for i, rec in enumerate(recordings):
            result = processor.process(rec, clients)
            processed.append(result)
            if (i + 1) % 5 == 0 or (i + 1) == len(recordings):
                await update.message.reply_text(f"⏳ {i+1}/{len(recordings)} processadas...")

    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao processar: {e}")
        return ConversationHandler.END

    # Salva no contexto pra validação
    context.user_data["processed"] = processed
    context.user_data["current_index"] = 0
    context.user_data["config"] = config

    # Mostra o primeiro pra validar
    await show_recording_for_validation(update, context)
    return VALIDATING


async def show_recording_for_validation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processed: list[ProcessedRecording] = context.user_data["processed"]
    idx = context.user_data["current_index"]

    if idx >= len(processed):
        await finish_validation(update, context)
        return

    rec = processed[idx]
    total = len(processed)
    type_info = TEMPLATES.get(rec.rec_type, TEMPLATES["reuniao"])

    text = (
        f"**[{idx+1}/{total}]** {rec.recording.title}\n"
        f"📅 {rec.recording.date.strftime('%d/%m/%Y')} — {rec.recording.duration_minutes}min\n\n"
        f"{type_info['emoji']} Tipo: **{type_info['label']}**\n"
        f"📁 Pasta: **{rec.suggested_client}**"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Tudo certo", callback_data=f"confirm:{idx}"),
        ],
        [
            InlineKeyboardButton("✏️ Mudar pasta", callback_data=f"change:{idx}"),
            InlineKeyboardButton("🔄 Mudar tipo", callback_data=f"changetype:{idx}"),
        ],
        [InlineKeyboardButton("⏭ Pular", callback_data=f"skip:{idx}")],
    ]

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )


async def handle_validation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, idx_str = query.data.split(":")
    idx = int(idx_str)
    processed: list[ProcessedRecording] = context.user_data["processed"]

    if action == "confirm":
        processed[idx].validated_client = processed[idx].suggested_client
        context.user_data["current_index"] = idx + 1
        await show_recording_for_validation(update, context)
        return VALIDATING

    elif action == "skip":
        # Remove da lista
        processed.pop(idx)
        # Não incrementa index porque list shifted
        if idx >= len(processed):
            context.user_data["current_index"] = len(processed)
        else:
            context.user_data["current_index"] = idx
        await show_recording_for_validation(update, context)
        return VALIDATING

    elif action == "change":
        context.user_data["changing_index"] = idx
        config = context.user_data["config"]
        clients = get_user_clients(config)

        keyboard = []
        for i, client in enumerate(clients):
            keyboard.append([InlineKeyboardButton(client, callback_data=f"setclient:{i}")])
        keyboard.append([InlineKeyboardButton("➕ Novo cliente", callback_data="setclient:new")])

        await query.message.reply_text(
            "Qual cliente/projeto?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CHOOSING_CLIENT

    elif action == "changetype":
        context.user_data["changing_index"] = idx

        keyboard = []
        for type_key, t in TEMPLATES.items():
            keyboard.append([InlineKeyboardButton(
                f"{t['emoji']} {t['label']}", callback_data=f"settype:{type_key}"
            )])

        await query.message.reply_text(
            "Qual o tipo dessa gravação?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CHOOSING_TYPE

    return VALIDATING


async def handle_choose_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":")

    if value == "new":
        await query.message.reply_text("Manda o nome do novo cliente:")
        return NEW_CLIENT_NAME

    config = context.user_data["config"]
    clients = get_user_clients(config)
    client_idx = int(value)
    client_name = clients[client_idx]

    idx = context.user_data["changing_index"]
    processed: list[ProcessedRecording] = context.user_data["processed"]
    processed[idx].validated_client = client_name

    context.user_data["current_index"] = idx + 1
    await show_recording_for_validation(update, context)
    return VALIDATING


async def handle_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, type_key = query.data.split(":")
    idx = context.user_data["changing_index"]
    processed: list[ProcessedRecording] = context.user_data["processed"]
    processed[idx].validated_type = type_key

    # Reprocessa o resumo com o template correto
    config = context.user_data["config"]
    clients = get_user_clients(config)
    type_info = TEMPLATES[type_key]
    await query.message.reply_text(f"🔄 Reclassificado como {type_info['emoji']} {type_info['label']}. Regenerando resumo...")

    try:
        processor = Processor(api_key=os.getenv("ANTHROPIC_API_KEY"))
        new_result = processor.process(processed[idx].recording, clients)
        processed[idx].summary_md = new_result.summary_md
        processed[idx].rec_type = type_key
    except Exception as e:
        await query.message.reply_text(f"⚠️ Não consegui regenerar, mantendo o resumo anterior: {e}")
        processed[idx].rec_type = type_key

    context.user_data["current_index"] = idx + 1
    await show_recording_for_validation(update, context)
    return VALIDATING


async def handle_new_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()

    # Adiciona à lista de clientes conhecidos
    config = context.user_data["config"]
    if "clients" not in config:
        config["clients"] = ["Interno"]
    if new_name not in config["clients"]:
        config["clients"].append(new_name)
        save_user_config(config)

    idx = context.user_data["changing_index"]
    processed: list[ProcessedRecording] = context.user_data["processed"]
    processed[idx].validated_client = new_name

    context.user_data["current_index"] = idx + 1
    await show_recording_for_validation(update, context)
    return VALIDATING


async def finish_validation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processed: list[ProcessedRecording] = context.user_data["processed"]
    config = context.user_data["config"]

    # Filtra os que foram validados
    to_save = [p for p in processed if p.validated_client]

    if not to_save:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("Nenhuma reunião pra salvar.")
        return ConversationHandler.END

    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(f"💾 Salvando {len(to_save)} reuniões no Google Drive...")

    try:
        creds_path = str(USERS_DIR / f"{config['name'].lower()}_drive_creds.json")
        drive = DriveClient(creds_path)

        # Cria pasta-mãe plaud-drive, depois subpasta Reuniões
        root_id = config.get("drive_root_folder_id")
        if not root_id:
            plaud_drive_id = drive.get_or_create_folder("plaud-drive")
            root_id = drive.get_or_create_folder("Reuniões", parent_id=plaud_drive_id)
            config["drive_root_folder_id"] = root_id
            save_user_config(config)

        saved = 0
        for p in to_save:
            client_folder_id = drive.get_or_create_folder(p.client, parent_id=root_id)
            drive.upload_markdown(p.filename, p.summary_md, client_folder_id)
            saved += 1

        await msg.reply_text(
            f"✅ Pronto! {saved} reuniões salvas no Google Drive.\n\n"
            + "\n".join(f"📁 {p.client} → {p.filename}" for p in to_save)
        )
    except Exception as e:
        await msg.reply_text(f"❌ Erro ao salvar no Drive: {e}")

    return ConversationHandler.END


async def evolucao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if not config:
        await update.message.reply_text("Você não está configurada. Manda /start.")
        return

    args = context.args
    if not args:
        # Lista clientes disponíveis
        try:
            creds_path = str(USERS_DIR / f"{config['name'].lower()}_drive_creds.json")
            drive = DriveClient(creds_path)
            root_id = config.get("drive_root_folder_id")
            if not root_id:
                await update.message.reply_text("Nenhuma reunião salva ainda. Use /processar primeiro.")
                return

            folders = drive.list_client_folders(root_id)
            if not folders:
                await update.message.reply_text("Nenhum cliente encontrado.")
                return

            clients_list = "\n".join(f"• {f['name']}" for f in folders)
            await update.message.reply_text(
                f"Manda: /evolucao Nome do Cliente\n\nClientes disponíveis:\n{clients_list}"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Erro: {e}")
        return

    client_name = " ".join(args)
    await update.message.reply_text(f"⏳ Analisando evolução de {client_name}...")

    try:
        creds_path = str(USERS_DIR / f"{config['name'].lower()}_drive_creds.json")
        drive = DriveClient(creds_path)
        root_id = config.get("drive_root_folder_id")

        client_folder_id = drive.get_or_create_folder(client_name, parent_id=root_id)
        files = drive.list_files_in_folder(client_folder_id)

        # Separa evoluções anteriores e notas de reunião
        evolution_files = sorted(
            [f for f in files if f["name"].startswith("_evolucao_") and f["name"].endswith(".md")],
            key=lambda x: x["name"],
        )
        note_files = sorted(
            [f for f in files if f["name"].endswith(".md") and not f["name"].startswith("_")],
            key=lambda x: x["name"],
        )

        if not note_files:
            await update.message.reply_text(f"Nenhum resumo encontrado para {client_name}.")
            return

        # Se tem evolução anterior, pega a última e filtra só notas novas
        last_evolution = None
        last_evolution_date = None
        new_notes = []

        if evolution_files:
            last_evo = evolution_files[-1]
            last_evolution = drive.read_file(last_evo["id"])
            # Extrai data do nome: _evolucao_2026-04-04.md
            date_part = last_evo["name"].replace("_evolucao_", "").replace(".md", "")
            last_evolution_date = date_part

            # Filtra notas mais recentes que a última evolução
            for f in note_files:
                note_date = f["name"][:10]  # "2026-04-04" do início do nome
                if note_date > date_part:
                    new_notes.append(f)

            if not new_notes:
                await update.message.reply_text(
                    f"Nenhuma nota nova desde a última evolução ({last_evolution_date}). "
                    "Processe mais reuniões antes de pedir evolução de novo."
                )
                return

            await update.message.reply_text(
                f"📊 Última evolução: {last_evolution_date}\n"
                f"📝 {len(new_notes)} notas novas encontradas. Atualizando..."
            )
        else:
            new_notes = note_files
            await update.message.reply_text(
                f"📝 Primeira evolução — analisando {len(new_notes)} notas..."
            )

        # Lê conteúdo das notas novas
        new_summaries = []
        for f in new_notes:
            content = drive.read_file(f["id"])
            new_summaries.append(content)

        processor = Processor(api_key=os.getenv("ANTHROPIC_API_KEY"))
        evolution = processor.generate_evolution(
            client_name, new_summaries, previous_evolution=last_evolution
        )

        # Salva com data no nome
        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"_evolucao_{today}.md"
        drive.upload_markdown(filename, evolution, client_folder_id)

        # Manda no Telegram
        if len(evolution) > 4000:
            for i in range(0, len(evolution), 4000):
                await update.message.reply_text(evolution[i:i+4000])
        else:
            await update.message.reply_text(evolution)

    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN não encontrado no .env")

    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("processar", processar)],
        states={
            VALIDATING: [CallbackQueryHandler(handle_validation, pattern=r"^(confirm|change|changetype|skip):")],
            CHOOSING_CLIENT: [CallbackQueryHandler(handle_choose_client, pattern=r"^setclient:")],
            NEW_CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_client_name)],
            CHOOSING_TYPE: [CallbackQueryHandler(handle_choose_type, pattern=r"^settype:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("evolucao", evolucao))

    logger.info("Bot rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
