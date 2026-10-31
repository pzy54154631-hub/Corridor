"""Test doubles for the model suggestion desk (agent/domestic_suggest.py).

``FakeLive`` is a live-mode client that never opens a socket: an intent step agrees with
the lexical router (its own scripted answer) unless the test sets ``intent`` (an object to
answer, or an exception to raise), and a suggestion step answers what the test gives (an
object, a function of the prompt, or an exception to raise).
"""

from copy import deepcopy
from types import SimpleNamespace

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController, demo_state

# Plain clarification for the lexical rules (no transfer verb), with one trusted contact
# and one whole amount in the user's words.
UNCLEAR = "帮我给妈妈弄500块过去"
SENTENCE = "给妈妈转500元"
TRANSFER = {"intent": "transfer", "slots": {"contact_ref": "CONTACT_CHEN", "amount": "500"}}
FREEZE = {"intent": "card_freeze", "slots": {"contact_ref": None, "amount": None}}
SUGGEST_PREFIX = "suggest:"


def batch(*items):
    return {"suggestions": [deepcopy(item) for item in items]}


class FakeLive:
    """A live-mode client double; it records every step, prompt and tool state it was given."""

    mode = "record"
    model = "fake-suggest-model"

    def __init__(self, answer=None, *, source="live-recorded", intent=None):
        self.answer, self.source, self.intent = answer, source, intent
        self.calls, self.steps, self.prompts, self.tool_states = [], [], [], []

    def complete(self, *, task_id, step, prompt, tool_state, scripted, sample_idx=0):
        self.steps.append(step)
        self.calls.append({"step": step, "source": self.source})
        self.prompts.append(deepcopy(prompt))
        self.tool_states.append(deepcopy(tool_state))
        if not step.startswith(SUGGEST_PREFIX):
            if isinstance(self.intent, BaseException):
                raise self.intent
            output = scripted() if self.intent is None else deepcopy(self.intent)
            return SimpleNamespace(output=output, source=self.source)
        answer = self.answer
        if isinstance(answer, BaseException):
            raise answer
        output = answer(prompt) if callable(answer) else answer
        return SimpleNamespace(output=deepcopy(output), source=self.source)

    def suggest_steps(self):
        return [step for step in self.steps if step.startswith(SUGGEST_PREFIX)]


def controller(*, showcase=False):
    built = DemoController(initial_state=demo_state(showcase=showcase))
    built.start_session()
    return built


def live_session(answer=None, *, suggest=True, showcase=False, source="live-recorded", intent=None):
    client = FakeLive(answer, source=source, intent=intent)
    return DomesticSession(controller(showcase=showcase), client=client, suggest=suggest), client


def adopt(session, response_id, index):
    """Choose one suggestion as a page does: the desk checks it, the session receives its
    sentence as typed. The reply, or None when the check failed and nothing was sent."""
    sent, reply = session.suggestions.adopt(session, response_id, index, session.receive)
    return reply if sent else None


def suggest_rule(*items):
    """A ``tests.unit.test_ui_live_mode.FakeModel`` rule: suggestion steps answer ``items``,
    every other step keeps the client's own (scripted) answer."""

    def rule(step, output):
        return batch(*items) if str(step).startswith(SUGGEST_PREFIX) else output

    return rule
