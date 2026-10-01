import pytest
from pydantic import ValidationError

from save_reel.models import Artifact, Reel, ReelConcept, SaveGame


@pytest.mark.parametrize("count", [0, 3, 5])
def test_concept_requires_exactly_four_saves(concept, count):
    data = concept.model_dump()
    data["saves"] = [data["saves"][0]] * count
    with pytest.raises(ValidationError):
        ReelConcept.model_validate(data)


def test_creative_models_reject_final_prompt_fields(concept):
    data = concept.model_dump()
    data["saves"][0]["environment"]["final_prompt"] = "A provider-written prompt"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ReelConcept.model_validate(data)


def test_required_creative_variables_cannot_be_blank(concept):
    data = concept.model_dump()
    data["saves"][0]["environment"]["biome"] = "  "
    with pytest.raises(ValidationError):
        ReelConcept.model_validate(data)


def test_reel_requires_four_unique_save_ids(concept, request_model):
    saves = tuple(
        SaveGame(save_id=f"save_{index:02d}", **save.model_dump())
        for index, save in enumerate(concept.saves, 1)
    )
    kwargs = dict(
        run_id="demo", request=request_model, concept_provider="mock", title=concept.title
    )
    reel = Reel(saves=saves, **kwargs)
    assert Reel.model_validate_json(reel.model_dump_json()) == reel
    with pytest.raises(ValidationError):
        Reel(saves=saves[:3], **kwargs)
    with pytest.raises(ValidationError, match="distinct"):
        Reel(saves=(saves[0],) * 4, **kwargs)
    with pytest.raises(ValidationError):
        reel.saves = saves[:3]


@pytest.mark.parametrize(
    "path", ["/tmp/prompt.txt", "../prompt.txt", "saves/../../x", r"..\x", "."]
)
def test_artifact_paths_must_stay_under_run_directory(path):
    with pytest.raises(ValidationError):
        Artifact(path=path, media_type="text/plain", sha256="a" * 64)
