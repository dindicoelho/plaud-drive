import json

import anthropic

from models import ProcessedRecording, Recording

TEMPLATES = {
    "reuniao": {
        "emoji": "🤝",
        "label": "Reunião",
        "prompt": """\
# [Título descritivo da reunião]
**Data:** {date}
**Duração:** {duration}min
**Participantes:** (liste os que conseguir identificar)
**Cliente/Projeto:** (a ser preenchido)

## Resumo executivo
[3-5 frases do que foi discutido e decidido]

## Decisões tomadas
- ...

## Próximos passos
- [ ] ...

## Observações
[Qualquer contexto relevante]""",
    },
    "nota_pessoal": {
        "emoji": "💭",
        "label": "Nota pessoal",
        "prompt": """\
# [Título que capture a essência do que foi dito]
**Data:** {date}
**Duração:** {duration}min

## O que estava pensando
[Resuma o raciocínio principal em 3-5 frases, mantendo a voz e intenção de quem falou]

## Ideias-chave
- ...

## To-dos pessoais
- [ ] ...

## Conexões
[Referências a outros assuntos, projetos ou pessoas mencionadas]""",
    },
    "terapia": {
        "emoji": "🧠",
        "label": "Terapia",
        "prompt": """\
# Sessão — {date}
**Duração:** {duration}min

## Temas abordados
- ...

## Insights
[O que ficou claro ou surgiu de novo durante a sessão]

## Como me senti
[Resumo emocional — o que estava presente antes, durante e depois]

## Pontos pra próxima sessão
- ...

## Frases que marcaram
[Citações ou formulações que vale guardar — da terapeuta ou próprias]""",
    },
    "palestra": {
        "emoji": "🎤",
        "label": "Palestra/Evento",
        "prompt": """\
# [Título da palestra ou evento]
**Data:** {date}
**Duração:** {duration}min
**Palestrante/Contexto:** (identifique se possível)

## Sobre o que foi
[3-5 frases resumindo o tema central]

## Conceitos-chave
- ...

## Referências mencionadas
[Livros, artigos, ferramentas, pessoas citadas]

## Meus takeaways
[O que mais valeu pra mim — o que muda ou inspira algo]

## Citações
[Frases marcantes, se houver]""",
    },
}

CLASSIFICATION_PROMPT = """\
Você é um assistente que organiza gravações de áudio. Analise a transcrição abaixo e produza um JSON com 3 campos:

1. "type": classifique o tipo da gravação. Opções:
   - "reuniao" — conversa entre 2+ pessoas com pauta de trabalho/projeto
   - "nota_pessoal" — pessoa falando sozinha, organizando pensamentos, brainstorm
   - "terapia" — sessão terapêutica (psicóloga/psiquiatra + paciente)
   - "palestra" — apresentação, aula, talk, evento com audiência

2. "suggested_client": nome do cliente, projeto ou contexto identificado. Use um dos clientes conhecidos se possível. Se não identificar, use "A classificar".

3. "summary": resumo em markdown seguindo EXATAMENTE o template abaixo para o tipo que você classificou.

{template}

---

REGRAS:
- Responda APENAS com o JSON, sem texto antes ou depois
- O título deve ser descritivo do conteúdo, não genérico
- Seja conciso mas não perca informação importante
- Para terapia: seja sensível e respeite a privacidade. Não julgue.
- Para nota pessoal: mantenha a voz e intenção de quem falou

Clientes conhecidos:
{known_clients}

Transcrição:
{transcript}
"""


class Processor:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def process(self, recording: Recording, known_clients: list[str]) -> ProcessedRecording:
        clients_str = "\n".join(f"- {c}" for c in known_clients) if known_clients else "- (nenhum cadastrado ainda)"

        transcript = recording.transcript
        if len(transcript) > 50_000:
            transcript = transcript[:50_000] + "\n\n[...transcrição truncada...]"

        # Monta todos os templates pra o Claude escolher
        all_templates = ""
        for type_key, t in TEMPLATES.items():
            filled = t["prompt"].format(date=recording.date.strftime("%Y-%m-%d"), duration=recording.duration_minutes)
            all_templates += f"\n### Se tipo = \"{type_key}\" ({t['label']}):\n{filled}\n"

        prompt = CLASSIFICATION_PROMPT.format(
            template=all_templates,
            known_clients=clients_str,
            transcript=transcript,
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text

        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1]
                clean = clean.rsplit("```", 1)[0]
            data = json.loads(clean)
            summary = data.get("summary", text)
            suggested = data.get("suggested_client", "A classificar")
            rec_type = data.get("type", "reuniao")
            if rec_type not in TEMPLATES:
                rec_type = "reuniao"
        except (json.JSONDecodeError, IndexError):
            summary = text
            suggested = "A classificar"
            rec_type = "reuniao"

        return ProcessedRecording(
            recording=recording,
            summary_md=summary,
            suggested_client=suggested,
            rec_type=rec_type,
        )

    def process_batch(self, recordings: list[Recording], known_clients: list[str],
                      on_progress=None) -> list[ProcessedRecording]:
        results = []
        for i, rec in enumerate(recordings):
            result = self.process(rec, known_clients)
            results.append(result)
            if on_progress:
                on_progress(i + 1, len(recordings), rec.title)
        return results

    def generate_evolution(self, client_name: str, summaries: list[str],
                           previous_evolution: str | None = None) -> str:
        all_summaries = "\n\n---\n\n".join(summaries)

        if previous_evolution:
            prompt = f"""\
Você é um analista de projetos. Abaixo está a última análise de evolução do cliente/projeto "{client_name}", seguida de novas notas de reunião que aconteceram DEPOIS dessa análise.

Seu trabalho é ATUALIZAR a evolução, incorporando as novas informações. Não repita o que já está na análise anterior — foque no que mudou, avançou ou surgiu de novo.

Produza o relatório atualizado contendo:

1. **Timeline** — adicione os novos marcos à timeline existente
2. **Evolução** — como o projeto avançou desde a última análise
3. **Decisões-chave** — novas decisões tomadas (mantenha as anteriores importantes)
4. **Pontos de atenção** — riscos novos ou atualizados
5. **Status atual** — onde as coisas estão AGORA, baseado nas notas mais recentes

Seja direto e objetivo. Use bullet points.

## Última análise de evolução:
{previous_evolution}

## Novas notas desde a última análise:
{all_summaries}
"""
        else:
            prompt = f"""\
Você é um analista de projetos. Abaixo estão todos os resumos de reuniões com o cliente/projeto "{client_name}", em ordem cronológica.

Analise a evolução e produza um relatório conciso contendo:

1. **Timeline** — os marcos principais em ordem cronológica
2. **Evolução** — como o projeto/relacionamento evoluiu
3. **Decisões-chave** — as decisões mais importantes tomadas
4. **Pontos de atenção** — riscos, insatisfações, ou mudanças de direção detectadas
5. **Status atual** — onde as coisas estão baseado na reunião mais recente

Seja direto e objetivo. Use bullet points.

Resumos:
{all_summaries}
"""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text
