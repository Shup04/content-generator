import pytest

from save_reel.models import ConceptRequest
from save_reel.providers import MockConceptProvider


@pytest.fixture
def request_model():
    return ConceptRequest(theme="quiet adventures")


@pytest.fixture
def concept(request_model):
    return MockConceptProvider().generate_concept(request_model)
