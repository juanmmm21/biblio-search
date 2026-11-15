from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx
import pytest
from conftest import load_fixture

from biblio_search.arxiv_categories import ARXIV_CATEGORIES
from biblio_search.assistant import (
    CLASSIFY_SCHEMA,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    AssistantError,
    OllamaClient,
    OllamaConfig,
    draft_prompt,
    draft_question,
    draft_schema,
    parse_classification,
    parse_draft,
    parse_exclusions,
)
from biblio_search.models import (
    ComponentRole,
    Discipline,
    QuestionError,
    QuestionFormat,
    TermKind,
    ThesaurusEntry,
    Vocabulary,
)

QUESTION = (
    "¿El ejercicio en el agua mejora el dolor en la fibromialgia comparado con el ejercicio "
    "en tierra?"
)


def _content(fixture: str) -> Mapping[str, object]:
    message = load_fixture(fixture)["message"]
    assert isinstance(message, dict)
    answer: Mapping[str, object] = json.loads(message["content"])
    return answer


class FakeModel:
    """Answers with the recorded Ollama replies, the classification first."""

    model = "fake:1b"

    def __init__(self, answers: Sequence[Mapping[str, object]]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    async def complete_json(
        self, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((system, prompt, schema))
        return self.answers.pop(0)


class FakeMesh:
    name = Vocabulary.MESH.value

    def __init__(self, known: Mapping[str, str]) -> None:
        self.known = known
        self.asked: list[str] = []

    async def lookup(self, texts: Sequence[str]) -> Mapping[str, ThesaurusEntry | None]:
        self.asked += texts
        return {
            text: ThesaurusEntry("mesh", self.known[text], text) if text in self.known else None
            for text in texts
        }


def test_config_reads_model_and_host_from_the_environment() -> None:
    default = OllamaConfig.from_env({})
    assert (default.model, default.base_url) == (DEFAULT_MODEL, DEFAULT_OLLAMA_URL)
    config = OllamaConfig.from_env(
        {"BIBLIO_SEARCH_MODEL": " gemma3:4b ", "OLLAMA_HOST": "192.168.1.5:11434/"}
    )
    assert (config.model, config.base_url) == ("gemma3:4b", "http://192.168.1.5:11434")
    with pytest.raises(ValueError, match="model"):
        OllamaConfig(model="two words")
    with pytest.raises(ValueError, match="http"):
        OllamaConfig(base_url="ftp://host")
    with pytest.raises(ValueError, match="positive"):
        OllamaConfig(timeout_seconds=0)


Handler = Callable[[httpx.Request], httpx.Response]


def _run_client(handler: Handler, call: Callable[[OllamaClient], Awaitable[object]]) -> object:
    async def go() -> object:
        async with OllamaClient(
            OllamaConfig(model="qwen3:8b"), transport=httpx.MockTransport(handler)
        ) as client:
            return await call(client)

    return asyncio.run(go())


def test_client_asks_for_structured_output_without_thinking() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("ollama_classify_fibromyalgia.json"))

    answer = _run_client(handler, lambda c: c.complete_json("system", QUESTION, CLASSIFY_SCHEMA))
    assert answer == {"discipline": "health", "format": "pico"}
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/chat"
    assert body["model"] == "qwen3:8b"
    assert body["format"] == CLASSIFY_SCHEMA
    assert (body["stream"], body["think"], body["keep_alive"]) == (False, False, "1m")
    assert body["options"] == {"temperature": 0}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_missing_model_says_how_to_pull_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=load_fixture("ollama_error_model_not_found.json"))

    with pytest.raises(AssistantError, match="ollama pull qwen3:8b"):
        _run_client(handler, lambda c: c.complete_json("s", "p", CLASSIFY_SCHEMA))


def test_ollama_not_running_says_how_to_go_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AssistantError, match=r"not running.*by hand"):
        _run_client(handler, lambda c: c.complete_json("s", "p", CLASSIFY_SCHEMA))


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(500, json={"error": "out of memory"}), "HTTP 500: out of memory"),
        (httpx.Response(200, json={"message": {"content": "not json"}}), "expected JSON"),
        (httpx.Response(200, json={"message": {"content": "[1, 2]"}}), "JSON object"),
        (httpx.Response(200, json={"done": True}), "expected JSON"),
    ],
)
def test_unusable_answers_are_errors(response: httpx.Response, message: str) -> None:
    with pytest.raises(AssistantError, match=message):
        _run_client(lambda _: response, lambda c: c.complete_json("s", "p", CLASSIFY_SCHEMA))


def test_classification_keeps_formats_inside_their_discipline() -> None:
    assert parse_classification({"discipline": "health", "format": "pico"}) == (
        Discipline.HEALTH,
        QuestionFormat.PICO,
    )
    assert parse_classification({"discipline": "health", "format": "concepts"}) == (
        Discipline.HEALTH,
        QuestionFormat.SPC,
    )
    assert parse_classification({"discipline": "education", "format": "spc"}) == (
        Discipline.EDUCATION,
        QuestionFormat.CONCEPTS,
    )
    with pytest.raises(AssistantError, match="classify"):
        parse_classification({"discipline": "astrology", "format": "spc"})


def test_draft_schema_has_one_key_per_component() -> None:
    pico = draft_schema(QuestionFormat.PICO)
    assert pico["required"] == [
        "english",
        "population",
        "intervention",
        "comparison",
        "outcome",
        "exclusions",
    ]
    concepts = draft_schema(QuestionFormat.CONCEPTS)
    assert concepts["required"] == ["english", "concepts", "exclusions"]


def test_parse_draft_from_the_recorded_answer() -> None:
    payload = _content("ollama_draft_fibromyalgia.json")
    question, english, notes = parse_draft(
        payload, QUESTION, QuestionFormat.PICO, Discipline.HEALTH
    )
    assert english == "Does water exercise improve pain in fibromyalgia compared to land exercise?"
    assert question.text == QUESTION
    assert question.discipline is Discipline.HEALTH
    roles = [c.role for c in question.components]
    assert roles == [
        ComponentRole.POPULATION,
        ComponentRole.INTERVENTION,
        ComponentRole.COMPARISON,
        ComponentRole.OUTCOME,
    ]
    comparison = question.components[2]
    assert comparison.description == "Land exercise" and comparison.conditions == ()
    assert any("comparison" in note for note in notes)
    fibromyalgia = question.components[0].conditions[0]
    assert [(t.text, t.kind) for t in fibromyalgia.terms] == [
        ("Fibromyalgia", TermKind.CONTROLLED),
        ("fibromyalgia", TermKind.FREE),
        ("fibro*", TermKind.FREE),
    ]


def test_parse_draft_leaves_out_bad_terms_and_repeated_labels() -> None:
    payload = {
        "english": "Peer tutoring and reading",
        "concepts": [
            {
                "description": "Peer tutoring",
                "conditions": [
                    {
                        "label": "tutoring",
                        "mesh": ["Tutoring"],
                        "free": ["peer tutor*", "*tutor*", 'the "best" tutors', "peer tutor*"],
                    }
                ],
            },
            {
                "description": "Tutoring again",
                "conditions": [{"label": "Tutoring", "mesh": [], "free": ["tutorial*"]}],
            },
            {"description": "", "conditions": [{"label": "empty", "mesh": [], "free": [" "]}]},
            "not an object",
        ],
    }
    question, _, notes = parse_draft(
        payload, "¿La tutoría mejora la lectura?", QuestionFormat.CONCEPTS, Discipline.EDUCATION
    )
    labels = [c.label for c in question.conditions]
    assert labels == ["tutoring", "Tutoring (2)"]
    # Fuera de salud no hay MeSH: el modelo no debería proponerlo y si lo hace se ignora.
    assert [t.text for t in question.conditions[0].terms] == ["peer tutor*"]
    assert len(notes) == 2 and all(note.startswith("left out") for note in notes)


def test_parse_draft_without_terms_is_an_error() -> None:
    payload = {"english": "x", "concepts": [{"description": "x", "conditions": []}]}
    with pytest.raises(AssistantError, match="any term"):
        parse_draft(payload, "x", QuestionFormat.CONCEPTS, Discipline.PHYSICS)


def test_draft_question_classifies_and_checks_the_headings() -> None:
    model = FakeModel(
        [
            _content("ollama_classify_fibromyalgia.json"),
            _content("ollama_draft_fibromyalgia.json"),
        ]
    )
    mesh = FakeMesh({"Fibromyalgia": "D005356", "Exercise": "D015444"})
    draft = asyncio.run(draft_question(f"  {QUESTION} ", model, thesaurus=mesh))
    assert draft.model == "fake:1b"
    assert draft.question.format is QuestionFormat.PICO
    assert [call[2] for call in model.calls] == [CLASSIFY_SCHEMA, draft_schema(QuestionFormat.PICO)]
    assert model.calls[1][1] == QUESTION
    assert sorted(mesh.asked) == ["Exercise", "Fibromyalgia", "Pain"]
    assert draft.check is not None
    # Pain no está en este MeSH falso: pasa a texto libre, como hace la comprobación.
    pain = draft.question.components[3].conditions[0]
    assert (pain.terms[0].text, pain.terms[0].kind) == ("Pain", TermKind.FREE)


def test_draft_question_keeps_what_the_user_chose() -> None:
    model = FakeModel([_content("ollama_draft_fibromyalgia.json")])
    draft = asyncio.run(
        draft_question(
            QUESTION, model, discipline=Discipline.HEALTH, question_format=QuestionFormat.PICO
        )
    )
    assert len(model.calls) == 1
    assert "PICO" in model.calls[0][0] and "MeSH descriptors" in model.calls[0][0]
    assert draft.check is None


def test_draft_question_rejects_empty_and_huge_questions() -> None:
    model = FakeModel([])
    with pytest.raises(QuestionError, match="write the question"):
        asyncio.run(draft_question("   ", model))
    with pytest.raises(QuestionError, match="longer"):
        asyncio.run(draft_question("a" * 1001, model))
    assert model.calls == []


MEMORY = (
    "¿El ejercicio aeróbico mejora la memoria en personas mayores con deterioro cognitivo "
    "leve? No me interesan los estudios en animales."
)


def test_the_prompt_asks_for_narrow_exclusions_only() -> None:
    prompt = draft_prompt(QuestionFormat.PICO, discipline=Discipline.HEALTH)
    assert '"exclusions"' in prompt and "usually an empty list" in prompt
    assert "Never exclude what is merely not mentioned" in prompt


def test_parse_exclusions_from_the_recorded_answer() -> None:
    payload = _content("ollama_draft_memory.json")
    question, _, _ = parse_draft(payload, MEMORY, QuestionFormat.PICO, Discipline.HEALTH)
    assert question.exclusions == ()
    notes: list[str] = []
    (animals,) = parse_exclusions(payload, question, notes)
    assert animals.condition.label == "animal studies"
    assert animals.reason == "the question specifically excludes studies involving animals"
    assert [(t.text, t.kind) for t in animals.condition.terms] == [
        ("Animals", TermKind.CONTROLLED),
        ("rat*", TermKind.FREE),
        ("mice", TermKind.FREE),
        ("mouse*", TermKind.FREE),
        ("animal study*", TermKind.FREE),
    ]
    assert notes == []


def test_parse_exclusions_needs_terms_and_a_reason_and_renames_labels() -> None:
    payload = _content("ollama_draft_memory.json")
    question, _, _ = parse_draft(payload, MEMORY, QuestionFormat.PICO, Discipline.HEALTH)
    raw = {
        "exclusions": [
            {"label": "older people", "reason": "clash", "mesh": [], "free": ["x*"]},
            {"label": "no reason", "reason": "  ", "mesh": [], "free": ["y*"]},
            {"label": "no terms", "reason": "why", "mesh": [], "free": []},
            {"label": "bad", "reason": "why", "mesh": [], "free": ["a[tiab]"]},
            "not an object",
        ]
    }
    notes: list[str] = []
    (renamed,) = parse_exclusions(raw, question, notes)
    assert renamed.condition.label == "older people (2)"
    assert any("a[tiab]" in note for note in notes)
    assert parse_exclusions({"exclusions": "nothing"}, question, []) == ()
    assert parse_exclusions({}, question, []) == ()


def test_draft_question_checks_the_suggested_exclusions_apart() -> None:
    model = FakeModel(
        [_content("ollama_classify_memory.json"), _content("ollama_draft_memory.json")]
    )
    mesh = FakeMesh({"Aged": "D000368", "Exercise": "D015444", "Memory": "D008568"})
    draft = asyncio.run(draft_question(MEMORY, model, thesaurus=mesh))
    assert "Animals" in mesh.asked
    # La sugerencia no entra en la pregunta: el usuario decide.
    assert draft.question.exclusions == ()
    (animals,) = draft.exclusions
    # Animals no está en este MeSH falso: pasa a texto libre, como el resto del borrador.
    assert (animals.condition.terms[0].text, animals.condition.terms[0].kind) == (
        "Animals",
        TermKind.FREE,
    )
    assert draft.check is not None
    assert any(c.condition == "animal studies" for c in draft.check.checks)


def test_draft_without_exclusions_suggests_none() -> None:
    model = FakeModel([_content("ollama_draft_fibromyalgia.json")])
    draft = asyncio.run(
        draft_question(
            QUESTION, model, discipline=Discipline.HEALTH, question_format=QuestionFormat.PICO
        )
    )
    assert draft.exclusions == ()


TUTORING = "¿La tutoría entre iguales mejora la comprensión lectora en primaria?"
DECODERS = (
    "¿Pueden las redes neuronales decodificar códigos de corrección de errores cuánticos mejor "
    "que los decodificadores clásicos?"
)


def _properties(schema: Mapping[str, object]) -> Mapping[str, object]:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    return properties


def _condition_keys(schema: Mapping[str, object]) -> list[str]:
    concepts = _properties(schema)["concepts"]
    assert isinstance(concepts, dict)
    component = concepts["items"]
    conditions = _properties(component)["conditions"]
    assert isinstance(conditions, dict)
    return list(_properties(conditions["items"]))


def test_each_discipline_gets_its_own_descriptor_key_and_arxiv_categories() -> None:
    education = draft_schema(QuestionFormat.CONCEPTS, Discipline.EDUCATION)
    physics = draft_schema(QuestionFormat.CONCEPTS, Discipline.PHYSICS)
    assert _condition_keys(education) == ["label", "eric", "free"]
    assert _condition_keys(physics) == ["label", "free"]
    assert "arxiv_categories" not in _properties(education)
    categories = _properties(physics)["arxiv_categories"]
    assert isinstance(categories, dict)
    assert categories["items"] == {"type": "string", "enum": list(ARXIV_CATEGORIES)}
    assert "arxiv_categories" in physics["required"]  # type: ignore[operator]
    prompt = draft_prompt(QuestionFormat.CONCEPTS, discipline=Discipline.EDUCATION)
    assert "ERIC Thesaurus descriptors" in prompt and "arxiv_categories" not in prompt
    prompt = draft_prompt(QuestionFormat.CONCEPTS, discipline=Discipline.MATHEMATICS)
    assert '"arxiv_categories"' in prompt and '"mesh"' not in prompt


def test_parse_draft_reads_eric_descriptors_for_education() -> None:
    payload = _content("ollama_draft_tutoring.json")
    question, english, notes = parse_draft(
        payload, TUTORING, QuestionFormat.CONCEPTS, Discipline.EDUCATION
    )
    assert english == "Does peer tutoring improve reading comprehension in primary school?"
    assert [(t.text, t.vocabulary) for t in question.controlled_terms] == [
        ("Peer Teaching", Vocabulary.ERIC),
        ("Reading Comprehension", Vocabulary.ERIC),
        ("Elementary School Students", Vocabulary.ERIC),
    ]
    assert question.arxiv_categories == () and notes == ()


def test_parse_draft_keeps_only_real_arxiv_categories() -> None:
    payload = dict(_content("ollama_draft_decoders.json"))
    question, _, notes = parse_draft(
        payload, DECODERS, QuestionFormat.CONCEPTS, Discipline.COMPUTER_SCIENCE
    )
    assert question.arxiv_categories == ("quant-ph", "cs.IT", "cs.LG")
    assert question.controlled_terms == ()
    payload["arxiv_categories"] = ["quant-ph", "cs.QML", "quant-ph", "physics.*", "cs.LG", "cs.AI"]
    question, _, notes = parse_draft(payload, DECODERS, QuestionFormat.CONCEPTS, Discipline.PHYSICS)
    assert question.arxiv_categories == ("quant-ph", "physics.*", "cs.LG", "cs.AI")
    assert notes == ("left out 'cs.QML': it is not an arXiv category",)
    # Fuera de las disciplinas de arXiv no se proponen categorías.
    question, _, _ = parse_draft(payload, DECODERS, QuestionFormat.CONCEPTS, Discipline.EDUCATION)
    assert question.arxiv_categories == ()


class FakeEric(FakeMesh):
    name = Vocabulary.ERIC.value


def test_draft_question_checks_eric_descriptors_with_the_eric_thesaurus() -> None:
    model = FakeModel(
        [_content("ollama_classify_tutoring.json"), _content("ollama_draft_tutoring.json")]
    )
    mesh = FakeMesh({})
    eric = FakeEric({"Peer Teaching": "", "Reading Comprehension": ""})
    draft = asyncio.run(draft_question(TUTORING, model, thesaurus=mesh, eric_thesaurus=eric))
    assert mesh.asked == []
    assert sorted(eric.asked) == [
        "Elementary School Students",
        "Peer Teaching",
        "Reading Comprehension",
    ]
    assert draft.check is not None and draft.check.vocabulary == Vocabulary.ERIC.value
    pupils = draft.question.conditions[2].terms[0]
    assert (pupils.text, pupils.kind) == ("Elementary School Students", TermKind.FREE)
    assert model.calls[1][2] == draft_schema(QuestionFormat.CONCEPTS, Discipline.EDUCATION)


def test_draft_question_proposes_arxiv_categories() -> None:
    model = FakeModel(
        [_content("ollama_classify_decoders.json"), _content("ollama_draft_decoders.json")]
    )
    draft = asyncio.run(draft_question(DECODERS, model, thesaurus=FakeMesh({})))
    assert draft.question.discipline is Discipline.COMPUTER_SCIENCE
    assert draft.question.arxiv_categories == ("quant-ph", "cs.IT", "cs.LG")
    assert draft.check is None
