import hashlib
import json
from importlib.resources import files
from pathlib import Path

import pytest

from save_reel.models import ConceptRequest, EffectsSpec
from save_reel.prompting import (
    BrollStillPromptCompiler,
    EnvironmentPromptCompiler,
    LabelPromptCompiler,
    PromptTemplateError,
)
from save_reel.providers import MockConceptProvider


def test_mock_and_compiler_are_deterministic(concept, request_model):
    assert MockConceptProvider().generate_concept(request_model) == concept
    compiler = EnvironmentPromptCompiler()
    prompts = [compiler.compile(request_model, save) for save in concept.saves]
    assert len({prompt.text for prompt in prompts}) == 4
    assert prompts == [compiler.compile(request_model, save) for save in concept.saves]
    for save, prompt in zip(concept.saves, prompts, strict=True):
        assert "vertical 9:16 composition" in prompt.text
        assert f"Reel theme: {request_model.theme}" in prompt.text
        assert save.environment.focal_point in prompt.text
        assert ", ".join(save.environment.palette) in prompt.text
        assert "${" not in prompt.text
        assert prompt.template.version == "v1"


def test_explicit_template_version_and_provenance(tmp_path, concept, request_model):
    directory = tmp_path / "environment"
    directory.mkdir()
    source = "World: ${title}; biome: ${biome}."
    (directory / "v2.txt").write_text(source)
    compiler = EnvironmentPromptCompiler("v2", prompts_dir=tmp_path)
    prompt = compiler.compile(request_model, concept.saves[0])
    assert prompt.text == "World: Mosslight Station; biome: temperate rainforest."
    assert prompt.template.sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert prompt.template.version == "v2"


def test_unknown_template_variable_fails(tmp_path, concept, request_model):
    directory = tmp_path / "environment"
    directory.mkdir()
    (directory / "v1.txt").write_text("World: ${unknown_variable}")
    compiler = EnvironmentPromptCompiler(prompts_dir=tmp_path)
    with pytest.raises(PromptTemplateError, match="unknown_variable"):
        compiler.compile(request_model, concept.saves[0])


@pytest.mark.parametrize("source", ["", "   ", "World: ${broken"])
def test_invalid_templates_fail_early(tmp_path, source):
    directory = tmp_path / "environment"
    directory.mkdir()
    (directory / "v1.txt").write_text(source)
    with pytest.raises(PromptTemplateError):
        EnvironmentPromptCompiler(prompts_dir=tmp_path)


@pytest.mark.parametrize("version", ["../v1", "v0", "latest", "v1/../../v2", "v999"])
def test_invalid_or_missing_versions_fail(version):
    with pytest.raises(PromptTemplateError):
        EnvironmentPromptCompiler(version)


def test_variables_are_not_recursively_interpreted(concept):
    request = ConceptRequest(theme="${biome} and {title} and $100")
    prompt = EnvironmentPromptCompiler().compile(request, concept.saves[0])
    assert f"Reel theme: {request.theme}" in prompt.text


def test_empty_effects_compile_as_none(concept, request_model):
    save = concept.saves[0]
    save.effects = EffectsSpec()
    prompt = EnvironmentPromptCompiler().compile(request_model, save)
    assert "Ambient particles: none" in prompt.text
    assert "Atmospheric effects: none" in prompt.text


@pytest.mark.parametrize("compiler_type", [LabelPromptCompiler, BrollStillPromptCompiler])
def test_supplied_prompt_wording_is_preserved(compiler_type, concept, request_model):
    compiler = compiler_type()
    source = (
        files("save_reel.prompt_templates")
        .joinpath(compiler.template_name, "v1.txt")
        .read_text(encoding="utf-8")
    )
    for save in concept.saves:
        values = {
            "GAME_TITLE": save.title,
            "WORLD_SCENE": save.environment.world_scene,
            "COLOUR_PALETTE": ", ".join(save.environment.palette),
            "MOOD": save.environment.mood,
            "SHOT_COMPOSITION": save.environment.shot_composition,
            "KEY_SURFACES": ", ".join(save.environment.key_surfaces),
        }
        expected = source
        for key, value in values.items():
            expected = expected.replace(f"[{key}]", value)
        prompt = compiler.compile(request_model, save)
        assert prompt.text == expected
        assert compiler.compile(request_model, save) == prompt
        assert prompt.template.sha256 == hashlib.sha256(source.encode()).hexdigest()
        assert prompt.template.name == compiler.template_name


def test_label_and_broll_share_world_but_keep_different_instructions(concept, request_model):
    save = concept.saves[0]
    label = LabelPromptCompiler().compile(request_model, save).text
    broll = BrollStillPromptCompiler().compile(request_model, save).text
    for value in (save.title, save.environment.world_scene, save.environment.mood):
        assert value in label and value in broll
    assert "Create a landscape 3:2 image." in label
    assert "vertical 9:16 framing" in broll
    assert "Prominently display this exact title:" in label
    assert "watermarks, or the game title over the image." in broll
    assert save.environment.shot_composition not in label
    assert save.environment.shot_composition in broll


def test_square_bracket_values_are_not_interpreted(concept, request_model):
    save = concept.saves[0]
    save.title = "[MOOD] $100 ${WORLD_SCENE}"
    prompt = LabelPromptCompiler().compile(request_model, save)
    assert f"called “{save.title}”." in prompt.text


def test_unknown_square_bracket_variable_fails(tmp_path, concept, request_model):
    directory = tmp_path / "label"
    directory.mkdir()
    (directory / "v1.txt").write_text("Title: [GAME_TITLE], [UNKNOWN]")
    with pytest.raises(PromptTemplateError, match="UNKNOWN"):
        LabelPromptCompiler(prompts_dir=tmp_path).compile(request_model, concept.saves[0])


@pytest.mark.parametrize(
    "field,value", [("world_scene", None), ("shot_composition", None), ("key_surfaces", ())]
)
def test_broll_requires_explicit_creative_variables(concept, request_model, field, value):
    save = concept.saves[0]
    setattr(save.environment, field, value)
    with pytest.raises(PromptTemplateError, match=field):
        BrollStillPromptCompiler().compile(request_model, save)


def test_label_does_not_require_shot_details(concept, request_model):
    save = concept.saves[0]
    save.environment.shot_composition = None
    save.environment.key_surfaces = ()
    assert save.title in LabelPromptCompiler().compile(request_model, save).text


def test_deep_end_preview_preserves_supplied_values_and_separates_shot_details():
    path = Path(__file__).resolve().parents[1] / "examples" / "deep-end.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    original = dict(values)
    label = LabelPromptCompiler().render(values)
    broll = BrollStillPromptCompiler().render(values)
    assert values == original
    assert "called “DEEP END”." in label.text
    assert "2004 PS2 game called\nDEEP END." in broll.text
    for name in ("WORLD_SCENE", "COLOUR_PALETTE", "MOOD"):
        assert values[name] in label.text
        assert values[name] in broll.text
    for name in ("SHOT_COMPOSITION", "KEY_SURFACES"):
        assert values[name] not in label.text
        assert values[name] in broll.text
    assert "no doorway beneath it" in label.text
    assert "rather than the focal point" in broll.text
    for prompt in (label, broll):
        assert "Used only in the b-roll prompt." not in prompt.text
        assert not any(f"[{name}]" in prompt.text for name in values)


def test_preview_reports_missing_variable_without_rewriting_values():
    values = {"GAME_TITLE": "DEEP END"}
    with pytest.raises(PromptTemplateError, match="WORLD_SCENE"):
        LabelPromptCompiler().render(values)
    assert values == {"GAME_TITLE": "DEEP END"}
