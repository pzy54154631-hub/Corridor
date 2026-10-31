"""session-intent-05: intent-eval measures a classifier the deployed session never runs.

Stated rules:
- docs/PREREG-DOMESTIC.md section 2 E2: intent recognition accuracy and clarification
  precision/recall are interaction metrics of the system; R2-16 schedules E2 for the main
  model and a domestic deployment.
- docs/DESIGN-DOMESTIC.md section 4.2 / dialogue.py: user text is PII-tokenized before any
  model context (DomesticSession._classify sends vault.tokenize(text)).

bench/domestic_runner.evaluate_intents (reached by `corridor intent-eval`) instead:
- sends the raw utterance (vault-private names such as "Chen") as user_text;
- uses a different instruction / tool_state than DomesticSession._classify;
- counts the model's raw intent even where the session would never ask the model
  (lexical clarify) or would override it (model != lexical -> clarify);
- scores IntentCandidate.needs_clarification, a flag the session ignores.
So a live E2 "意图 X/155" / clarification precision-recall is not the agent's behaviour.
"""

from types import SimpleNamespace

from corridor.agent.domestic_session import DomesticSession
from corridor.bench.domestic_runner import evaluate_intents
from corridor.crypto.jcs import jcs_bytes
from corridor.ui.controller import DemoController


class StubClient:
    mode = "record"
    model = "stub-model"

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def complete(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        return SimpleNamespace(output=dict(self.answer))


def _source(tmp_path, text, intent, scenario, needs):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(
        jcs_bytes({
            "id": "SI05",
            "text": text,
            "expected": {
                "intent": intent,
                "scenario": scenario,
                "needs_clarification": needs,
                "parameters": {"recipient": None, "amount": None, "date": None, "product": None},
            },
        })
        + b"\n"
    )
    return path


def test_session_intent_05_eval_prompt_is_pii_tokenized_like_deployment(tmp_path):
    client = StubClient({"intent": "transfer", "needs_clarification": False})
    evaluate_intents(
        client=client, source=_source(tmp_path, "给Chen转200元", "transfer", "transfers", False)
    )
    sent = [prompt["user_text"] for prompt in client.prompts]
    assert sent and all("Chen" not in text for text in sent), sent


def test_session_intent_05_eval_intent_score_matches_deployed_session(tmp_path):
    text = "我卡里还有钱吗"
    answer = {"intent": "balance_query", "needs_clarification": False}
    report = evaluate_intents(
        client=StubClient(answer),
        source=_source(tmp_path, text, "balance_query", "billing", False),
    )
    controller = DemoController()
    controller.start_session()
    session = DomesticSession(controller, client=StubClient(answer))
    deployed = session.receive(text)
    deployed_correct = int(deployed["intent"] == "balance_query")
    assert report["intent_correct"] == deployed_correct, (
        report["intent_correct"],
        deployed["intent"],
    )


def test_session_intent_05_clarification_metric_reflects_session_behaviour(tmp_path):
    text = "不要给老妈转800元"
    answer = {"intent": "transfer", "needs_clarification": True}
    report = evaluate_intents(
        client=StubClient(answer),
        source=_source(tmp_path, text, "transfer", "transfers", True),
    )
    controller = DemoController()
    controller.start_session()
    session = DomesticSession(controller, client=StubClient(answer))
    deployed = session.receive(text)
    # The report credits a correct clarification; the deployed agent offers a payment card.
    assert report["clarification_correct"] == int(deployed["needs_clarification"] is True), (
        report["clarification_correct"],
        deployed["status"],
        bool(deployed["card"]),
    )
