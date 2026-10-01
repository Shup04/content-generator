"""Strict, deterministic rendering of versioned prompt resources."""

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from string import Template

from save_reel.models import CompiledPrompt, ConceptRequest, SaveGameConcept, TemplateReference


class PromptTemplateError(ValueError):
    pass


class PromptCompiler(ABC):
    template_name: str
    bracket_placeholders = False

    def __init__(self, version: str = "v1", *, prompts_dir: Path | None = None) -> None:
        if not re.fullmatch(r"v[1-9][0-9]*", version):
            raise PromptTemplateError("template version must have the form v1, v2, ...")
        root = prompts_dir if prompts_dir is not None else files("save_reel.prompt_templates")
        template_path = f"{self.template_name}/{version}.txt"
        path = root.joinpath(self.template_name, f"{version}.txt")
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PromptTemplateError(f"cannot read {template_path}: {exc}") from exc
        render_source = source
        if self.bracket_placeholders:
            # Preserve the supplied [VARIABLE] notation and all literal dollar signs.
            render_source = re.sub(r"\[([A-Z][A-Z0-9_]*)\]", r"${\1}", source.replace("$", "$$"))
        self._template = Template(render_source)
        if not source.strip() or not self._template.is_valid():
            raise PromptTemplateError(f"{template_path} is empty or has invalid placeholders")
        self.reference = TemplateReference(
            name=self.template_name,
            version=version,
            sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

    def compile(self, request: ConceptRequest, save: SaveGameConcept) -> CompiledPrompt:
        return self.render(self._variables(request, save))

    def render(self, variables: Mapping[str, str | tuple[str, ...]]) -> CompiledPrompt:
        """Preview supplied variables without constructing a four-save reel.

        Keys match the template placeholders. Only placeholders in the selected
        template are substituted; the caller's values are not modified.
        """
        variables = {
            key: (", ".join(value) if value else "none") if isinstance(value, tuple) else value
            for key, value in variables.items()
        }
        try:
            text = self._template.substitute(variables)
        except KeyError as exc:
            raise PromptTemplateError(
                f"unknown {self.template_name} template variable: {exc.args[0]}"
            ) from exc
        return CompiledPrompt(text=text, template=self.reference)

    @abstractmethod
    def _variables(
        self, request: ConceptRequest, save: SaveGameConcept
    ) -> dict[str, str | tuple[str, ...]]: ...


class EnvironmentPromptCompiler(PromptCompiler):
    """Original environment template, retained for existing workflows."""

    template_name = "environment"

    def _variables(
        self, request: ConceptRequest, save: SaveGameConcept
    ) -> dict[str, str | tuple[str, ...]]:
        return {
            "theme": request.theme,
            "title": save.title,
            "summary": save.summary,
            **save.environment.model_dump(exclude_none=True),
            **save.effects.model_dump(),
        }


class LabelPromptCompiler(PromptCompiler):
    template_name = "label"
    bracket_placeholders = True

    def _variables(
        self, request: ConceptRequest, save: SaveGameConcept
    ) -> dict[str, str | tuple[str, ...]]:
        if save.environment.world_scene is None:
            raise PromptTemplateError(f"{save.title}: environment.world_scene is required")
        return {
            "GAME_TITLE": save.title,
            "WORLD_SCENE": save.environment.world_scene,
            "COLOUR_PALETTE": save.environment.palette,
            "MOOD": save.environment.mood,
        }


class BrollStillPromptCompiler(LabelPromptCompiler):
    template_name = "broll_still"

    def _variables(
        self, request: ConceptRequest, save: SaveGameConcept
    ) -> dict[str, str | tuple[str, ...]]:
        values = super()._variables(request, save)
        environment = save.environment
        if environment.shot_composition is None or not environment.key_surfaces:
            raise PromptTemplateError(
                f"{save.title}: environment.shot_composition and key_surfaces are required"
            )
        return {
            **values,
            "SHOT_COMPOSITION": environment.shot_composition,
            "KEY_SURFACES": environment.key_surfaces,
        }


class BrollVideoPromptCompiler(PromptCompiler):
    template_name = "broll_video"
    bracket_placeholders = True

    def _variables(
        self, request: ConceptRequest, save: SaveGameConcept
    ) -> dict[str, str | tuple[str, ...]]:
        raise PromptTemplateError("video prompts require explicit motion variables; use render()")


class CartridgePromptCompiler(PromptCompiler):
    template_name = "cartridge"
    bracket_placeholders = True

    def _variables(
        self, request: ConceptRequest, save: SaveGameConcept
    ) -> dict[str, str | tuple[str, ...]]:
        raise PromptTemplateError(
            "cartridge prompts require explicit shell variables; use render()"
        )
