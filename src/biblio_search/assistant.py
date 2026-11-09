"""Optional local assistant: drafts a structured question from a sentence with Ollama.

The model runs on the user's own machine (https://ollama.com), so nothing is paid and the
question never leaves it. It only proposes: every MeSH heading it writes goes through the
thesaurus check, and what MeSH does not know is searched as free text. biblio-search works
the same without it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import httpx

from biblio_search.check import QuestionCheck, apply_check, check_question
from biblio_search.models import (
    FORMAT_ROLES,
    Component,
    ComponentRole,
    Condition,
    Discipline,
    QuestionError,
    QuestionFormat,
    StructuredQuestion,
    Term,
    TermKind,
    Vocabulary,
)
from biblio_search.protocols import Thesaurus

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
# Corto a propósito: el modelo comparte la GPU con otras tareas y solo hace falta mientras
# se redacta un borrador (dos llamadas seguidas).
DEFAULT_KEEP_ALIVE = "1m"
OLLAMA_URL = "https://ollama.com"
MODEL_VARIABLE = "BIBLIO_SEARCH_MODEL"
HOST_VARIABLE = "OLLAMA_HOST"
MAX_QUESTION_LENGTH = 1000


class AssistantError(RuntimeError):
    """The local model is not available or answered something unusable."""


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Where Ollama listens and which model drafts the question."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    keep_alive: str = DEFAULT_KEEP_ALIVE
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("the model name must not be empty or contain spaces")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"the Ollama address must start with http:// or https://: {self.base_url}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OllamaConfig:
        """Read ``BIBLIO_SEARCH_MODEL`` and Ollama's own ``OLLAMA_HOST``; both are optional."""
        source = os.environ if env is None else env
        model = source.get(MODEL_VARIABLE, "").strip() or DEFAULT_MODEL
        host = source.get(HOST_VARIABLE, "").strip() or DEFAULT_OLLAMA_URL
        if not host.startswith(("http://", "https://")):
            # Ollama acepta OLLAMA_HOST sin esquema (127.0.0.1:11434).
            host = f"http://{host}"
        return cls(model=model, base_url=host.rstrip("/"))


class LanguageModel(Protocol):
    """What the assistant needs from a model (OllamaClient satisfies it)."""

    @property
    def model(self) -> str:
        """Name of the model, shown next to what it proposed."""

    async def complete_json(
        self, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Answer ``prompt`` with a JSON object that follows ``schema``."""


class OllamaClient:
    """Minimal async client of Ollama's chat API with structured outputs."""

    def __init__(
        self, config: OllamaConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.base_url, timeout=config.timeout_seconds, transport=transport
        )

    @property
    def model(self) -> str:
        return self._config.model

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete_json(
        self, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        body = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "format": schema,
            "stream": False,
            # Razonar en voz alta no mejora estos borradores y los hace varias veces más lentos.
            "think": False,
            "keep_alive": self._config.keep_alive,
            "options": {"temperature": 0},
        }
        try:
            response = await self._http.post("/api/chat", json=body)
        except httpx.ConnectError as exc:
            raise AssistantError(
                f"Ollama is not running at {self._config.base_url}; install it from {OLLAMA_URL} "
                "and start it, or write the question by hand"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AssistantError(
                f"the model {self._config.model} took more than "
                f"{self._config.timeout_seconds:g} s to answer"
            ) from exc
        except httpx.TransportError as exc:
            raise AssistantError(f"cannot reach Ollama: {exc.__class__.__name__}") from exc
        return self._content(response)

    def _content(self, response: httpx.Response) -> Mapping[str, object]:
        if response.status_code == 404:
            raise AssistantError(
                f"the model {self._config.model} is not installed: run "
                f"`ollama pull {self._config.model}` or choose another one in {MODEL_VARIABLE}"
            )
        if response.status_code != 200:
            raise AssistantError(
                f"Ollama answered HTTP {response.status_code}: {_error_of(response)}"
            )
        try:
            payload = response.json()
            content = payload["message"]["content"]
            answer = json.loads(content)
        except (ValueError, KeyError, TypeError) as exc:
            raise AssistantError("Ollama answered something that is not the expected JSON") from exc
        if not isinstance(answer, dict):
            raise AssistantError("the model did not answer a JSON object")
        return answer


def _error_of(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200] or "no details"
    error = payload.get("error") if isinstance(payload, dict) else None
    return str(error) if error else "no details"


# --- Esquemas y prompts ---

_DISCIPLINES = [d.value for d in Discipline]

CLASSIFY_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "discipline": {"type": "string", "enum": _DISCIPLINES},
        "format": {"type": "string", "enum": [f.value for f in QuestionFormat]},
    },
    "required": ["discipline", "format"],
}

_CONDITION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "mesh": {"type": "array", "items": {"type": "string"}},
        "free": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["label", "mesh", "free"],
}

_COMPONENT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "conditions": {"type": "array", "items": _CONDITION_SCHEMA},
    },
    "required": ["description", "conditions"],
}


def draft_schema(question_format: QuestionFormat) -> Mapping[str, object]:
    """JSON schema of the decomposition, with one fixed key per component of the format.

    Fixed keys make small models fill every component; a free list of roles let them stop
    after the first one.
    """
    if question_format is QuestionFormat.CONCEPTS:
        components: dict[str, object] = {
            "concepts": {"type": "array", "items": _COMPONENT_SCHEMA, "minItems": 1}
        }
    else:
        components = {role.value: _COMPONENT_SCHEMA for role in FORMAT_ROLES[question_format]}
    return {
        "type": "object",
        "properties": {"english": {"type": "string"}, **components},
        "required": ["english", *components],
    }


CLASSIFY_PROMPT = """\
Classify a research question (it may be in Spanish) to plan a bibliographic search.
- discipline: health (medicine, nursing, psychology of patients, public health, \
caregivers...), education (teaching, learning, schools, pupils, students), physics, \
mathematics, computer_science or economics.
- format: pico when a health question asks about the effect of an intervention, treatment \
or exposure; spc for other health questions (experiences, relationships, prevalence, \
burden); concepts for every other discipline."""

_FORMAT_PROMPTS: Mapping[QuestionFormat, str] = {
    QuestionFormat.SPC: """\
You help to plan a health bibliographic search with the SPC format (Del Pino, 2017). \
Decompose the user's question (it may be in Spanish) and propose terms in English.
- situation: the health problem, condition or phenomenon studied (e.g. anxiety or \
depression; type 1 diabetes).
- population: who is studied (e.g. family caregivers; older people; adolescents).
- question: what is asked about it (burden, experiences, quality of life...). Leave it \
without conditions when it is vague ("relationship", "how they live it").
Example: "¿Qué relación hay entre la sobrecarga y la ansiedad o la depresión en cuidadores \
familiares de personas mayores?" ->
situation: [{"label": "anxiety or depression", "mesh": ["Anxiety", "Depression"], \
"free": ["anxiet*", "depress*"]}]
population: [{"label": "caregivers", "mesh": ["Caregivers"], "free": ["caregiv*", \
"carer*"]}, {"label": "older people", "mesh": ["Aged"], "free": ["elder*", "older people"]}]
question: [{"label": "burden", "mesh": [], "free": ["burden*", "overload*", "strain*"]}]""",
    QuestionFormat.PICO: """\
You help to plan a health bibliographic search with the PICO format (Del Pino, 2017). \
Decompose the user's question (it may be in Spanish) and propose terms in English.
- population: who is studied and their condition.
- intervention: the treatment, programme or exposure.
- comparison: what it is compared with.
- outcome: the effect measured.
Example: "¿El ejercicio físico reduce las caídas en mayores que viven en residencias?" ->
population: [{"label": "older people", "mesh": ["Aged"], "free": ["elder*", \
"older adult*"]}, {"label": "nursing homes", "mesh": ["Nursing Homes"], \
"free": ["nursing home*", "care home*"]}]
intervention: [{"label": "exercise", "mesh": ["Exercise"], "free": ["exercis*", \
"physical activit*"]}]
comparison: []
outcome: [{"label": "falls", "mesh": ["Accidental Falls"], "free": ["fall*"]}]""",
    QuestionFormat.CONCEPTS: """\
You help to plan a bibliographic search with free concepts. Decompose the user's question \
(it may be in Spanish) into its key concepts, one entry per concept, and propose terms in \
English. Leave out vague words such as "improve", "effect" or "relationship".
Example: "¿La tutoría entre iguales mejora la comprensión lectora en primaria?" ->
concepts: [{"description": "Peer tutoring", "conditions": [{"label": "peer tutoring", \
"mesh": [], "free": ["peer tutor*", "cross-age tutor*", "peer-assisted learning"]}]},
 {"description": "Reading comprehension", "conditions": [{"label": "reading comprehension", \
"mesh": [], "free": ["reading comprehension", "reading skill*"]}]},
 {"description": "Primary school pupils", "conditions": [{"label": "primary school", \
"mesh": [], "free": ["primary school*", "elementary school*", "pupil*"]}]}]""",
}

_MESH_RULES = {
    True: '- "mesh": exact English names of MeSH descriptors you are confident exist '
    "(Caregivers, Anxiety, Aged, Accidental Falls). Never invent descriptors.",
    False: '- "mesh": always empty.',
}

_CONDITION_RULES = """\
Rules for every condition:
- A condition is one idea that every retrieved record must meet (conditions are joined \
with AND); its terms are alternatives for that single idea (joined with OR).
- Ideas joined with "or" in the question (anxiety or depression) are alternatives: they go \
in the same condition.
- Never put two different ideas in one condition (a relative and an age group): make two \
conditions.
- "label": two or three words naming the idea.
- "free": 2 to 5 English words or short phrases authors write in titles and abstracts: \
synonyms, variants, and roots with a trailing * for word families (caregiv*, depress*, \
fall*). One * at most, at the end; never quotes, brackets or AND/OR/NOT.
- "english": the whole question rewritten in English in one sentence."""


def draft_prompt(question_format: QuestionFormat, *, use_mesh: bool) -> str:
    """System prompt that decomposes a question in ``question_format``."""
    return "\n".join([_FORMAT_PROMPTS[question_format], _MESH_RULES[use_mesh], _CONDITION_RULES])


# --- Del JSON del modelo a una pregunta estructurada ---


@dataclass(frozen=True, slots=True)
class Draft:
    """A question proposed by the local model, already checked against the thesaurus.

    ``notes`` say what was changed or left out of the model's answer, and ``check`` is the
    thesaurus check (None when the question has no MeSH headings or no thesaurus was given).
    """

    question: StructuredQuestion
    english: str
    model: str
    notes: tuple[str, ...] = ()
    check: QuestionCheck | None = None


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def parse_classification(
    payload: Mapping[str, object],
) -> tuple[Discipline, QuestionFormat]:
    """Discipline and format proposed by the model, made consistent with each other.

    SPC and PICO are health formats; outside health biblio-search searches free concepts.
    """
    try:
        discipline = Discipline(_text(payload.get("discipline")))
        question_format = QuestionFormat(_text(payload.get("format")))
    except ValueError as exc:
        raise AssistantError("the model did not classify the question") from exc
    return discipline, _format_for(discipline, question_format)


def _format_for(discipline: Discipline, question_format: QuestionFormat) -> QuestionFormat:
    if discipline is not Discipline.HEALTH:
        return QuestionFormat.CONCEPTS
    if question_format is QuestionFormat.CONCEPTS:
        return QuestionFormat.SPC
    return question_format


def _terms(raw: Mapping[str, object], *, use_mesh: bool, notes: list[str]) -> list[Term]:
    terms: list[Term] = []
    seen: set[tuple[str, TermKind]] = set()
    candidates = [(text, TermKind.CONTROLLED) for text in _texts(raw.get("mesh")) if use_mesh]
    candidates += [(text, TermKind.FREE) for text in _texts(raw.get("free"))]
    for text, kind in candidates:
        key = (text.casefold(), kind)
        if key in seen:
            continue
        seen.add(key)
        try:
            term = Term.controlled(text) if kind is TermKind.CONTROLLED else Term.free(text)
        except QuestionError as exc:
            notes.append(f"left out {text!r}: {exc}")
            continue
        terms.append(term)
    return terms


def _conditions(
    raw_conditions: object, *, use_mesh: bool, notes: list[str]
) -> tuple[Condition, ...]:
    conditions: list[Condition] = []
    for raw in raw_conditions if isinstance(raw_conditions, list) else []:
        if not isinstance(raw, Mapping):
            continue
        terms = _terms(raw, use_mesh=use_mesh, notes=notes)
        if terms:
            label = _text(raw.get("label")) or terms[-1].text
            conditions.append(Condition(label=label, terms=tuple(terms)))
    return tuple(conditions)


def _raw_components(
    payload: Mapping[str, object], question_format: QuestionFormat
) -> list[tuple[ComponentRole, Mapping[str, object]]]:
    if question_format is QuestionFormat.CONCEPTS:
        concepts = payload.get("concepts")
        items = concepts if isinstance(concepts, list) else []
        return [(ComponentRole.CONCEPT, item) for item in items if isinstance(item, Mapping)]
    found: list[tuple[ComponentRole, Mapping[str, object]]] = []
    for role in FORMAT_ROLES[question_format]:
        item = payload.get(role.value)
        if isinstance(item, Mapping):
            found.append((role, item))
    return found


def parse_draft(
    payload: Mapping[str, object],
    text: str,
    question_format: QuestionFormat,
    discipline: Discipline,
) -> tuple[StructuredQuestion, str, tuple[str, ...]]:
    """Turn the model's decomposition into a valid question, its English text and notes.

    Terms the question format would reject are left out with a note instead of failing the
    whole draft. PICO's comparison is described but not searched (Cochrane advises against
    searching it); the user can add it by hand.
    """
    use_mesh = discipline is Discipline.HEALTH
    notes: list[str] = []
    labels_used: set[str] = set()
    components: list[Component] = []
    for role, raw in _raw_components(payload, question_format):
        conditions = _conditions(raw.get("conditions"), use_mesh=use_mesh, notes=notes)
        # Las etiquetas identifican cada condición en la comprobación y en la rejilla.
        renamed: list[Condition] = []
        for condition in conditions:
            label = condition.label
            number = 2
            while label.casefold() in labels_used:
                label = f"{condition.label} ({number})"
                number += 1
            labels_used.add(label.casefold())
            renamed.append(Condition(label=label, terms=condition.terms))
        if role is ComponentRole.COMPARISON and renamed:
            notes.append(
                "the comparison is described but left out of the search, as Cochrane advises; "
                "add its terms by hand if you need them"
            )
            renamed = []
        description = _text(raw.get("description")) or (renamed[0].label if renamed else "")
        if description:
            components.append(Component(role, description, tuple(renamed)))
    if not any(component.conditions for component in components):
        raise AssistantError("the model did not propose any term to search for")
    question = StructuredQuestion(
        text=text, format=question_format, components=tuple(components), discipline=discipline
    )
    return question, _text(payload.get("english")), tuple(dict.fromkeys(notes))


async def draft_question(
    text: str,
    model: LanguageModel,
    *,
    discipline: Discipline | None = None,
    question_format: QuestionFormat | None = None,
    thesaurus: Thesaurus | None = None,
) -> Draft:
    """Ask ``model`` to decompose ``text`` and check its MeSH headings with ``thesaurus``.

    The discipline and format the user already chose are kept; the model only classifies
    the question when one of them is missing. Headings the thesaurus does not know become
    free text and entry terms are renamed to their heading, exactly as the check does.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        raise QuestionError("write the question first")
    if len(cleaned) > MAX_QUESTION_LENGTH:
        raise QuestionError(f"the question is longer than {MAX_QUESTION_LENGTH} characters")
    if discipline is None or question_format is None:
        guessed = parse_classification(
            await model.complete_json(CLASSIFY_PROMPT, cleaned, CLASSIFY_SCHEMA)
        )
        discipline = discipline or guessed[0]
        question_format = question_format or guessed[1]
    use_mesh = discipline is Discipline.HEALTH
    payload = await model.complete_json(
        draft_prompt(question_format, use_mesh=use_mesh), cleaned, draft_schema(question_format)
    )
    question, english, notes = parse_draft(payload, cleaned, question_format, discipline)
    check: QuestionCheck | None = None
    if thesaurus is not None and any(
        t.vocabulary is Vocabulary.MESH for t in question.controlled_terms
    ):
        check = await check_question(question, thesaurus)
        question = apply_check(question, check)
    return Draft(question, english, model.model, notes, check)
