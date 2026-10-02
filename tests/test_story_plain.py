"""The user-supplied plain-English prompt, with independently cached drafts."""

import copy
import json
from types import SimpleNamespace

import pytest

from save_reel.models import ConceptRequest
from save_reel.providers.mock_story import MockStoryProvider
from save_reel.story_models import StorySettings, word_count
from save_reel.story_pipeline import StoryWorkflow
from save_reel.story_prose_models import ProseParagraph


class Spy(MockStoryProvider):
    def __init__(self):
        super().__init__()
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return super().generate(**kwargs)


def test_plain_prompt_and_three_independent_drafts(tmp_path):
    provider = Spy()
    workflow = StoryWorkflow(provider)
    run = workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="plain", random_seed=41)
    assert run.settings.travelogue.prompt_version == "v3"
    calls = [c for c in provider.calls if c["stage"] == "narration_prose_candidate"]
    assert len(calls) == 12
    for call in calls:
        assert "Write 45–70 words." in call["prompt"]
        assert "Aim for roughly a grade 5–6 reading level." in call["prompt"]
        assert "Return only the narration." in call["prompt"]
        assert "The tram stops running when the sun goes down." in call["prompt"]
        assert "approved_facts" not in call["prompt"]
        assert "distinct_fact_count" not in call["prompt"]
        assert call["response_type"] is ProseParagraph
        assert set(call["context"]) == {
            "title", "world_description", "word_range", "candidate_number", "narration_round"
        }
    for slot in run.saves:
        assert len(slot.travelogue.drafts) == 3
        assert 45 <= word_count(slot.final.brief.narration) <= 70
        assert slot.travelogue.selected_id == "narration_3"
    provider.calls.clear()
    assert workflow.resume(tmp_path / "plain") == run
    assert not provider.calls


def test_interruption_reuses_each_saved_draft(tmp_path):
    class Interrupt(Spy):
        interrupted = False

        def generate(self, **kwargs):
            if (kwargs["stage"] == "narration_prose_candidate"
                    and kwargs["context"]["candidate_number"] == 2 and not self.interrupted):
                self.interrupted = True
                raise RuntimeError("interrupted draft 2")
            return super().generate(**kwargs)

    p = Interrupt()
    workflow = StoryWorkflow(p)
    with pytest.raises(RuntimeError, match="interrupted"):
        workflow.create(ConceptRequest(), runs_dir=tmp_path, run_id="resume")
    original = workflow.load(tmp_path / "resume").saves[0].travelogue.drafts
    assert len(original) == 1
    p.calls.clear()
    run = workflow.resume(tmp_path / "resume")
    assert run.saves[0].travelogue.drafts[0] == original[0]
    assert p.calls[0]["stage"] == "narration_prose_candidate"
    assert p.calls[0]["context"]["candidate_number"] == 2
    assert not any(c["stage"] == "world_simulation" and
                   c["context"]["selected"]["title"] == run.saves[0].selected.title
                   for c in p.calls)


def test_duplicate_draft_is_retried_before_selection(tmp_path):
    class DuplicateOnce(Spy):
        first = None
        duplicated = False

        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs["stage"] == "narration_prose_candidate":
                if self.first is None:
                    self.first = result.model_copy(deep=True)
                elif not self.duplicated:
                    self.duplicated = True
                    return self.first
            return result

    p = DuplicateOnce()
    run = StoryWorkflow(p).create(ConceptRequest(), runs_dir=tmp_path, run_id="duplicate")
    assert len({c.paragraph for c in run.saves[0].travelogue.drafts}) == 3
    requests = list((tmp_path / "duplicate/story_requests/save_01/narration_prose_candidate")
                    .glob("*.json"))
    assert any(json.loads(path.read_text())["status"] == "invalid" for path in requests)


def test_plain_writer_uses_sol_medium_and_only_paragraph_schema():
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    requests = []
    value = ProseParagraph(paragraph="You spend your day here.")

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 0,
            "status": "completed", "model": "gpt-6.1-sol",
            "output": [{"id": "msg_offline", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text",
                        "text": value.model_dump_json(), "annotations": []}]}],
        })

    with openai.OpenAI(api_key="offline-test", max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        result = OpenAIStoryProvider(StorySettings(), client=client).generate(
            stage="narration_prose_candidate", prompt="Offline", context={},
            response_type=ProseParagraph)
    assert result == value
    assert requests[0]["model"] == "gpt-6.1-sol"
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert set(requests[0]["text"]["format"]["schema"]["properties"]) == {"paragraph"}


def test_incomplete_response_reports_token_limit_without_response_body():
    pytest.importorskip("openai")
    from save_reel.providers.openai_story import OpenAIStoryProvider

    response = SimpleNamespace(
        output_parsed=None, status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(output_tokens=20000),
    )
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: response))
    with pytest.raises(ValueError, match="reason=max_output_tokens, output_tokens=20000"):
        OpenAIStoryProvider(StorySettings(), client=client).generate(
            stage="narration_prose_candidate", prompt="Offline", context={},
            response_type=ProseParagraph)
