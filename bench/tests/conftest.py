import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
MOCK_MAPS = BENCH_DIR / "mockmaps"
MOCK_DATA = BENCH_DIR / "mockdata"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def mock_maps_dir() -> Path:
    return MOCK_MAPS


@pytest.fixture(scope="session")
def mock_data_dir() -> Path:
    return MOCK_DATA


@pytest.fixture()
def maps(mock_maps_dir):
    from bench.mapio import MapSet

    return MapSet.load(mock_maps_dir)


@pytest.fixture()
def knobs():
    from bench.engine import Knobs

    return Knobs()


@pytest.fixture()
def engine(maps, knobs):
    from bench.engine import Engine

    return Engine(maps, knobs)


@pytest.fixture()
def patch_text(mock_data_dir):
    def read(name: str) -> str:
        return (mock_data_dir / "commits" / name).read_text("utf-8")

    return read
