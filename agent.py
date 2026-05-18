"""Agente conversacional do Plaud-Drive: Claude com tool use."""
import json
import logging
from typing import Any, Callable

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Você é o assistente do Plaud-Drive — ajuda a transformar gravações do Plaud em notas organizadas no Google Drive.

Estilo: direto, conversacional, em português. Não narre o que está fazendo — só faça e reporte o resultado. Use bullets curtos quando listar coisas.

Fluxo típico:
1. Usuário pergunta "o que tem novo?" → você chama list_recent_recordings.
2. Usuário pede pra processar X → você chama process_recording (gera draft com resumo+tipo+pasta sugerida).
3. Você confirma a pasta com o usuário (se tiver dúvida) e chama save_to_drive.
4. Usuário pode pedir evolução de um cliente → generate_evolution.

REGRAS:
- Antes de save_to_drive, confirme a pasta com o usuário se houver ambiguidade. Se ele já disse claramente ("salva no Ininterrupta"), apenas execute.
- Use list_clients pra ver pastas existentes antes de criar uma nova. Só registre cliente novo (register_client) se o usuário pediu explicitamente ou disse um nome novo.
- Pra IDs de gravação, use apenas IDs que vieram de list_recent_recordings ou list_pending.
- Quando processar várias gravações de uma vez, agrupe na resposta pro usuário confirmar tudo junto.
- Se uma tool retornar erro, explique pro usuário em linguagem natural — não jogue o stack trace.
- Pra perguntas sobre o sistema ou "o que você faz", responda direto sem chamar tool.
"""

TOOLS = [
    {
        "name": "list_recent_recordings",
        "description": (
            "Lista as gravações mais recentes do Plaud do usuário. Por padrão retorna as "
            "20 mais recentes ainda não processadas. Cada item tem id, title, date, "
            "duration_minutes — NÃO inclui transcrição. Use 'days' pra filtrar por período."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Limita a N dias atrás. Opcional."},
                "limit": {"type": "integer", "description": "Máximo de gravações (default 20)."},
                "include_seen": {
                    "type": "boolean",
                    "description": "Se true, inclui gravações já processadas. Default false.",
                },
            },
        },
    },
    {
        "name": "process_recording",
        "description": (
            "Busca a transcrição de UMA gravação no Plaud, gera resumo Markdown, "
            "classifica tipo (reuniao/nota_pessoal/terapia/palestra) e sugere pasta. "
            "NÃO salva no Drive — devolve um draft. Use save_to_drive em seguida."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"recording_id": {"type": "string"}},
            "required": ["recording_id"],
        },
    },
    {
        "name": "save_to_drive",
        "description": (
            "Salva no Google Drive o draft de uma gravação processada, na pasta do cliente. "
            "Marca a gravação como vista. Use só depois que o usuário confirmou a pasta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "client": {"type": "string", "description": "Nome do cliente/pasta."},
                "rec_type": {
                    "type": "string",
                    "enum": ["reuniao", "nota_pessoal", "terapia", "palestra"],
                    "description": "Opcional, sobrescreve o tipo classificado.",
                },
            },
            "required": ["recording_id", "client"],
        },
    },
    {
        "name": "list_clients",
        "description": "Lista clientes/pastas conhecidos do usuário (da config).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "register_client",
        "description": "Adiciona um cliente/pasta novo à lista do usuário.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_pending",
        "description": (
            "Lista drafts pendentes — gravações processadas pelo check diário mas que ainda "
            "não foram salvas no Drive. Retorna lista com índice, título, data, tipo sugerido."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_pending",
        "description": "Salva um draft pendente no Drive. pending_index vem de list_pending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pending_index": {"type": "integer"},
                "client": {"type": "string"},
                "rec_type": {
                    "type": "string",
                    "enum": ["reuniao", "nota_pessoal", "terapia", "palestra"],
                },
            },
            "required": ["pending_index", "client"],
        },
    },
    {
        "name": "generate_evolution",
        "description": (
            "Gera ou atualiza a análise de evolução de um cliente com base nas notas já "
            "salvas no Drive. Salva o relatório no Drive e devolve o conteúdo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"client": {"type": "string"}},
            "required": ["client"],
        },
    },
]


def _serialize_content_block(block) -> dict:
    """Anthropic SDK block → dict serializável pra reuso no histórico."""
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return dict(block)


class ChatAgent:
    def __init__(
        self,
        api_key: str,
        runner: Callable[[str, dict[str, Any]], Any],
        model: str = "claude-sonnet-4-5",
        max_iterations: int = 10,
    ):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.runner = runner
        self.model = model
        self.max_iterations = max_iterations

    def respond(self, user_message: str, history: list[dict]) -> tuple[str, list[dict]]:
        """Roda um turno do agente. Retorna (texto_final, history_atualizado)."""
        history = list(history)
        history.append({"role": "user", "content": user_message})

        for iteration in range(self.max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            )

            assistant_content = [_serialize_content_block(b) for b in response.content]
            history.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason != "tool_use":
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                return text or "(sem resposta)", history

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info(f"agent tool_use: {block.name}({block.input})")
                try:
                    result = self.runner(block.name, block.input)
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False, default=str)
                    is_error = False
                except Exception as e:
                    logger.exception(f"tool {block.name} falhou")
                    result = f"{type(e).__name__}: {e}"
                    is_error = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                })

            history.append({"role": "user", "content": tool_results})

        return "⚠️ Loop muito longo, parei. Tenta de novo com pedido mais específico.", history
