import asyncio
import fcntl
import json
import logging
import os
import sys
from datetime import datetime, time as dt_time, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from agent import ChatAgent
from drive_client import DriveClient
from models import ProcessedRecording, Recording
from plaud_client import PlaudClient
from processor import Processor, TEMPLATES

load_dotenv()

DAILY_CHECK_TZ = ZoneInfo("America/Sao_Paulo")
DAILY_CHECK_TIME = dt_time(hour=21, minute=0, tzinfo=DAILY_CHECK_TZ)
RECENT_FILES_LIMIT = 20

BASE_DIR = Path(__file__).parent
USERS_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "users"))
LOG_PATH = Path(os.getenv("LOG_PATH", BASE_DIR / "bot.log"))
LOCK_PATH = Path(os.getenv("LOCK_PATH", "/tmp/plaud-drive.lock"))
USERS_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=10_000_000, backupCount=3),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

_lock_fd = None


def acquire_singleton_lock():
    global _lock_fd
    _lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Outra instância já está rodando (lock {LOCK_PATH}). Saindo.")
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()

# States
VALIDATING = 1
CHOOSING_CLIENT = 2
NEW_CLIENT_NAME = 3
CHOOSING_TYPE = 4


async def safe_send(send_func, *args, **kwargs):
    """Wrap reply_text/send_message com retry simples em falhas de rede."""
    last_err = None
    for attempt in range(3):
        try:
            return await send_func(*args, **kwargs)
        except (NetworkError, TimedOut) as e:
            last_err = e
            wait = 2 ** attempt
            logger.warning(f"Telegram send falhou ({type(e).__name__}: {e}), retry em {wait}s")
            await asyncio.sleep(wait)
    raise last_err


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


def state_path(user_name: str) -> Path:
    return USERS_DIR / f"{user_name.lower()}_state.json"


def load_state(user_name: str) -> dict:
    p = state_path(user_name)
    if not p.exists():
        return {"seen_ids": [], "pending": []}
    with open(p) as f:
        data = json.load(f)
    data.setdefault("seen_ids", [])
    data.setdefault("pending", [])
    return data


def save_state(user_name: str, state: dict):
    with open(state_path(user_name), "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def serialize_processed(p: ProcessedRecording) -> dict:
    return {
        "recording": {
            "id": p.recording.id,
            "title": p.recording.title,
            "date": p.recording.date.isoformat(),
            "duration_minutes": p.recording.duration_minutes,
            "transcript": p.recording.transcript,
            "has_summary": p.recording.has_summary,
            "plaud_summary": p.recording.plaud_summary,
        },
        "summary_md": p.summary_md,
        "suggested_client": p.suggested_client,
        "rec_type": p.rec_type,
        "validated_client": p.validated_client,
        "validated_type": p.validated_type,
    }


def deserialize_processed(d: dict) -> ProcessedRecording:
    r = d["recording"]
    rec = Recording(
        id=r["id"],
        title=r["title"],
        date=datetime.fromisoformat(r["date"]),
        duration_minutes=r["duration_minutes"],
        transcript=r["transcript"],
        has_summary=r.get("has_summary", False),
        plaud_summary=r.get("plaud_summary", ""),
    )
    return ProcessedRecording(
        recording=rec,
        summary_md=d["summary_md"],
        suggested_client=d["suggested_client"],
        rec_type=d.get("rec_type", "reuniao"),
        validated_client=d.get("validated_client"),
        validated_type=d.get("validated_type"),
    )


def iter_user_configs():
    for f in USERS_DIR.glob("*.json"):
        stem = f.stem
        if stem == "exemplo" or stem.endswith("_state") or stem.endswith("_drive_creds"):
            continue
        try:
            with open(f) as fp:
                yield json.load(fp)
        except Exception as e:
            logger.warning(f"Falha ao ler {f.name}: {e}")


# --- Command handlers ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if config:
        await update.message.reply_text(
            f"Oi {config['name']}! Pode me mandar mensagem natural tipo:\n"
            "• 'o que tem novo?'\n"
            "• 'processa a reunião de ontem e salva no Ininterrupta'\n"
            "• 'faz a evolução do Interno'\n\n"
            "Ou usa os comandos: /processar, /validar, /evolucao, /reset (limpa histórico do chat)."
        )
    else:
        await update.message.reply_text(
            f"Não te conheço ainda. Seu chat_id é {chat_id}.\n\n"
            "Pede pra quem configura o sistema adicionar seu chat_id "
            "no arquivo de config em users/."
        )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop("history", None)
    context.chat_data.pop("drafts", None)
    await update.message.reply_text("Histórico zerado. 🧹")


async def processar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if not config:
        await update.message.reply_text("Você não está configurada. Manda /start.")
        return ConversationHandler.END

    # Parse período: /processar (tudo que ainda não foi visto) ou /processar 7 (dias)
    args = context.args or []
    days: int | None = None
    if len(args) == 1 and args[0].isdigit():
        days = int(args[0])

    state = load_state(config["name"])
    seen = set(state["seen_ids"])

    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        await update.message.reply_text(
            f"⏳ Buscando gravações dos últimos {days} dias no Plaud..."
        )
        max_files = None
    else:
        since = None
        await update.message.reply_text(
            f"⏳ Buscando as últimas {RECENT_FILES_LIMIT} gravações no Plaud..."
        )
        max_files = RECENT_FILES_LIMIT

    # Busca gravações
    try:
        plaud = PlaudClient(token=config["plaud_token"], origin=config.get("plaud_origin", "https://api.plaud.ai"))
        recordings = plaud.get_recordings(since=since, seen_ids=seen, max_files=max_files)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao acessar Plaud: {e}")
        return ConversationHandler.END

    if not recordings:
        await update.message.reply_text("Nenhuma gravação com transcrição encontrada.")
        return ConversationHandler.END

    new_recs = [r for r in recordings if r.id not in seen]
    skipped = len(recordings) - len(new_recs)

    if not new_recs:
        await update.message.reply_text(
            f"Todas as {len(recordings)} já foram vistas. Use /validar pra ver as engatilhadas."
        )
        return ConversationHandler.END

    extra = f" ({skipped} já vistas, ignoradas)" if skipped else ""
    await update.message.reply_text(
        f"📋 {len(new_recs)} reuniões novas{extra}. Gerando resumos..."
    )
    recordings = new_recs

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

    seen.update(p.recording.id for p in processed)
    state["seen_ids"] = sorted(seen)
    save_state(config["name"], state)

    # Salva no contexto pra validação
    context.user_data["processed"] = processed
    context.user_data["current_index"] = 0
    context.user_data["config"] = config
    context.user_data["from_pending"] = False

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
    from_pending = context.user_data.get("from_pending", False)

    if from_pending:
        state = load_state(config["name"])
        state["pending"] = []
        save_state(config["name"], state)

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


async def validar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if not config:
        await update.message.reply_text("Você não está configurada. Manda /start.")
        return ConversationHandler.END

    state = load_state(config["name"])
    if not state["pending"]:
        await update.message.reply_text(
            "Nada engatilhado. O check diário roda às 21h, ou use /processar pra rodar manual."
        )
        return ConversationHandler.END

    processed = [deserialize_processed(d) for d in state["pending"]]
    context.user_data["processed"] = processed
    context.user_data["current_index"] = 0
    context.user_data["config"] = config
    context.user_data["from_pending"] = True

    await update.message.reply_text(f"📋 {len(processed)} reuniões pendentes.")
    await show_recording_for_validation(update, context)
    return VALIDATING


async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    logger.info("daily_check: iniciando varredura")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("daily_check: ANTHROPIC_API_KEY ausente, pulando")
        return

    for config in iter_user_configs():
        chat_id = config.get("telegram_chat_id")
        plaud_token = config.get("plaud_token")
        user_name = config.get("name")
        if not (chat_id and plaud_token and user_name):
            continue

        state = load_state(user_name)
        seen = set(state["seen_ids"])

        try:
            plaud = PlaudClient(
                token=plaud_token,
                origin=config.get("plaud_origin", "https://api.plaud.ai"),
            )
            recordings = plaud.get_recordings(seen_ids=seen, max_files=RECENT_FILES_LIMIT)
        except Exception as e:
            logger.warning(f"daily_check Plaud falhou para {user_name}: {e}")
            continue

        new_recs = [r for r in recordings if r.id not in seen]
        if not new_recs:
            logger.info(f"daily_check: {user_name} sem gravações novas")
            continue

        try:
            processor = Processor(api_key=api_key)
            clients = get_user_clients(config)
            new_processed = [processor.process(r, clients) for r in new_recs]
        except Exception as e:
            logger.warning(f"daily_check Claude falhou para {user_name}: {e}")
            continue

        seen.update(r.id for r in new_recs)
        state["seen_ids"] = sorted(seen)
        state["pending"].extend(serialize_processed(p) for p in new_processed)
        save_state(user_name, state)

        count = len(new_processed)
        total = len(state["pending"])
        suffix = f" (total {total} pendentes)" if total != count else ""
        try:
            await context.bot.send_message(
                chat_id,
                f"🆕 {count} reunião{'ões' if count != 1 else ''} pronta{'s' if count != 1 else ''} pra validar{suffix} — /validar",
            )
        except Exception as e:
            logger.warning(f"daily_check Telegram falhou para {user_name}: {e}")


def _build_recording_from_detail(plaud: PlaudClient, file_id: str) -> Recording:
    detail = plaud.get_file_detail(file_id)
    transcript = plaud.get_transcript(file_id)
    if not transcript:
        raise ValueError(f"Gravação {file_id} sem transcrição disponível")
    ts = detail.get("start_time")
    if ts:
        ts = int(ts)
        if ts >= 1_000_000_000_000:
            ts = ts / 1000
        date = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        date = datetime.now(timezone.utc)
    duration_ms = detail.get("duration", 0)
    duration_min = duration_ms // 60_000 if duration_ms >= 60_000 else max(duration_ms // 1000 // 60, 1)
    return Recording(
        id=file_id,
        title=detail.get("filename") or detail.get("title") or "Sem título",
        date=date,
        duration_minutes=max(duration_min, 1),
        transcript=transcript,
        has_summary=detail.get("is_summary", False),
    )


def _ensure_drive_root(config: dict, drive: DriveClient) -> str:
    root_id = config.get("drive_root_folder_id")
    if not root_id:
        plaud_drive_id = drive.get_or_create_folder("plaud-drive")
        root_id = drive.get_or_create_folder("Reuniões", parent_id=plaud_drive_id)
        config["drive_root_folder_id"] = root_id
        save_user_config(config)
    return root_id


def make_tool_runner(config: dict, drafts: dict, api_key: str):
    """Cria o runner síncrono que o ChatAgent vai chamar."""
    plaud = PlaudClient(
        token=config["plaud_token"],
        origin=config.get("plaud_origin", "https://api.plaud.ai"),
    )
    processor = Processor(api_key=api_key)
    user_name = config["name"]

    def _drive() -> DriveClient:
        return DriveClient(str(USERS_DIR / f"{user_name.lower()}_drive_creds.json"))

    def run(name: str, args: dict):
        if name == "list_recent_recordings":
            limit = args.get("limit", RECENT_FILES_LIMIT)
            days = args.get("days")
            include_seen = args.get("include_seen", False)
            since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
            state = load_state(user_name)
            seen = set() if include_seen else set(state["seen_ids"])
            recs = plaud.get_recordings(since=since, seen_ids=seen, max_files=limit)
            return {
                "count": len(recs),
                "recordings": [
                    {
                        "id": r.id,
                        "title": r.title,
                        "date": r.date.strftime("%Y-%m-%d %H:%M"),
                        "duration_minutes": r.duration_minutes,
                    }
                    for r in recs
                ],
            }

        if name == "process_recording":
            rec_id = args["recording_id"]
            rec = _build_recording_from_detail(plaud, rec_id)
            clients = get_user_clients(config)
            processed = processor.process(rec, clients)
            drafts[rec_id] = processed
            type_info = TEMPLATES.get(processed.rec_type, TEMPLATES["reuniao"])
            return {
                "recording_id": rec_id,
                "title": rec.title,
                "date": rec.date.strftime("%Y-%m-%d"),
                "duration_minutes": rec.duration_minutes,
                "suggested_type": processed.rec_type,
                "type_label": type_info["label"],
                "suggested_client": processed.suggested_client,
                "summary_preview": processed.summary_md[:400] + ("..." if len(processed.summary_md) > 400 else ""),
            }

        if name == "save_to_drive":
            rec_id = args["recording_id"]
            client_name = args["client"]
            rec_type = args.get("rec_type")
            draft = drafts.get(rec_id)
            if not draft:
                return {"error": f"Draft {rec_id} não está em memória. Processe primeiro com process_recording."}
            draft.validated_client = client_name
            if rec_type:
                draft.validated_type = rec_type
            if client_name not in get_user_clients(config):
                config.setdefault("clients", ["Interno"]).append(client_name)
                save_user_config(config)
            drive = _drive()
            root_id = _ensure_drive_root(config, drive)
            client_folder_id = drive.get_or_create_folder(client_name, parent_id=root_id)
            file_id = drive.upload_markdown(draft.filename, draft.summary_md, client_folder_id)
            state = load_state(user_name)
            seen = set(state["seen_ids"])
            seen.add(rec_id)
            state["seen_ids"] = sorted(seen)
            save_state(user_name, state)
            return {
                "saved": True,
                "drive_file_id": file_id,
                "filename": draft.filename,
                "client": client_name,
            }

        if name == "list_clients":
            return {"clients": get_user_clients(config)}

        if name == "register_client":
            new_name = args["name"].strip()
            clients = config.setdefault("clients", ["Interno"])
            if new_name in clients:
                return {"already_exists": True, "client": new_name}
            clients.append(new_name)
            save_user_config(config)
            return {"registered": True, "client": new_name}

        if name == "list_pending":
            state = load_state(user_name)
            pending = state.get("pending", [])
            return {
                "count": len(pending),
                "pending": [
                    {
                        "index": i,
                        "recording_id": p["recording"]["id"],
                        "title": p["recording"]["title"],
                        "date": p["recording"]["date"][:10],
                        "suggested_type": p.get("rec_type", "reuniao"),
                        "suggested_client": p["suggested_client"],
                    }
                    for i, p in enumerate(pending)
                ],
            }

        if name == "save_pending":
            idx = args["pending_index"]
            client_name = args["client"]
            rec_type = args.get("rec_type")
            state = load_state(user_name)
            pending = state.get("pending", [])
            if not 0 <= idx < len(pending):
                return {"error": f"pending_index {idx} fora do intervalo (0..{len(pending)-1})"}
            draft = deserialize_processed(pending[idx])
            draft.validated_client = client_name
            if rec_type:
                draft.validated_type = rec_type
            if client_name not in get_user_clients(config):
                config.setdefault("clients", ["Interno"]).append(client_name)
                save_user_config(config)
            drive = _drive()
            root_id = _ensure_drive_root(config, drive)
            client_folder_id = drive.get_or_create_folder(client_name, parent_id=root_id)
            file_id = drive.upload_markdown(draft.filename, draft.summary_md, client_folder_id)
            pending.pop(idx)
            state["pending"] = pending
            save_state(user_name, state)
            return {
                "saved": True,
                "drive_file_id": file_id,
                "filename": draft.filename,
                "client": client_name,
                "remaining_pending": len(pending),
            }

        if name == "generate_evolution":
            client_name = args["client"]
            drive = _drive()
            root_id = _ensure_drive_root(config, drive)
            client_folder_id = drive.get_or_create_folder(client_name, parent_id=root_id)
            files = drive.list_files_in_folder(client_folder_id)
            evolution_files = sorted(
                [f for f in files if f["name"].startswith("_evolucao_") and f["name"].endswith(".md")],
                key=lambda x: x["name"],
            )
            note_files = sorted(
                [f for f in files if f["name"].endswith(".md") and not f["name"].startswith("_")],
                key=lambda x: x["name"],
            )
            if not note_files:
                return {"error": f"Nenhuma nota encontrada para {client_name}."}
            last_evolution = None
            new_notes = note_files
            if evolution_files:
                last_evo = evolution_files[-1]
                last_evolution = drive.read_file(last_evo["id"])
                date_part = last_evo["name"].replace("_evolucao_", "").replace(".md", "")
                new_notes = [f for f in note_files if f["name"][:10] > date_part]
                if not new_notes:
                    return {"info": f"Nenhuma nota nova desde a última evolução ({date_part})."}
            summaries = [drive.read_file(f["id"]) for f in new_notes]
            evolution = processor.generate_evolution(client_name, summaries, previous_evolution=last_evolution)
            today = datetime.now().strftime("%Y-%m-%d")
            drive.upload_markdown(f"_evolucao_{today}.md", evolution, client_folder_id)
            return {
                "client": client_name,
                "notes_analyzed": len(new_notes),
                "saved_as": f"_evolucao_{today}.md",
                "evolution": evolution,
            }

        return {"error": f"Tool desconhecida: {name}"}

    return run


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id)
    if not config:
        await update.message.reply_text("Você não está configurada. Manda /start.")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        await update.message.reply_text("⚠️ ANTHROPIC_API_KEY não configurada no servidor.")
        return

    history = context.chat_data.setdefault("history", [])
    drafts = context.chat_data.setdefault("drafts", {})
    user_msg = update.message.text

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    runner = make_tool_runner(config, drafts, api_key)
    agent = ChatAgent(api_key=api_key, runner=runner)

    try:
        reply, new_history = await asyncio.to_thread(agent.respond, user_msg, history)
    except Exception as e:
        logger.exception("agent error")
        await update.message.reply_text(f"⚠️ Erro no agente: {type(e).__name__}: {e}")
        return

    if len(new_history) > 40:
        new_history = new_history[-30:]
    context.chat_data["history"] = new_history

    for i in range(0, len(reply), 4000):
        try:
            await safe_send(update.message.reply_text, reply[i:i+4000], parse_mode="Markdown")
        except Exception:
            await safe_send(update.message.reply_text, reply[i:i+4000])


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    logger.exception("Exceção no handler:", exc_info=err)

    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id

    if not chat_id:
        return

    err_name = type(err).__name__
    err_msg = str(err) or err_name
    if len(err_msg) > 300:
        err_msg = err_msg[:300] + "..."

    if isinstance(err, (NetworkError, TimedOut)):
        text = f"⚠️ Rede instável agora ({err_name}). Tenta de novo em alguns segundos."
    else:
        text = f"⚠️ Deu erro: `{err_name}: {err_msg}`\n\nTenta de novo, ou /cancel se travou."

    try:
        await safe_send(context.bot.send_message, chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Não consegui avisar o usuário sobre o erro: {e}")


BOT_COMMANDS = [
    BotCommand("start", "Verifica se sua config tá ok"),
    BotCommand("validar", "Abre as reuniões engatilhadas pelo check diário"),
    BotCommand("processar", "Roda agora nas últimas 20 gravações (ou /processar N pra olhar N dias)"),
    BotCommand("evolucao", "Análise de evolução de um cliente"),
    BotCommand("reset", "Limpa o histórico do chat com o agente"),
    BotCommand("cancel", "Cancela o fluxo de validação"),
]


async def post_init(app: Application):
    await app.bot.set_my_commands(BOT_COMMANDS)
    logger.info(f"Comandos registrados no Telegram: {[c.command for c in BOT_COMMANDS]}")


def main():
    acquire_singleton_lock()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN não encontrado no .env")

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=20.0,
        pool_timeout=10.0,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=40.0,
        write_timeout=20.0,
        pool_timeout=10.0,
    )

    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("processar", processar),
            CommandHandler("validar", validar),
        ],
        states={
            VALIDATING: [CallbackQueryHandler(handle_validation, pattern=r"^(confirm|change|changetype|skip):")],
            CHOOSING_CLIENT: [CallbackQueryHandler(handle_choose_client, pattern=r"^setclient:")],
            NEW_CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_client_name)],
            CHOOSING_TYPE: [CallbackQueryHandler(handle_choose_type, pattern=r"^settype:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("evolucao", evolucao))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)

    app.job_queue.run_daily(
        daily_check,
        time=DAILY_CHECK_TIME,
        name="daily_plaud_check",
        job_kwargs={"misfire_grace_time": 6 * 3600, "coalesce": True},
    )
    logger.info(f"daily_check agendado para {DAILY_CHECK_TIME} (misfire_grace=6h, coalesce=True)")

    logger.info("Bot rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
