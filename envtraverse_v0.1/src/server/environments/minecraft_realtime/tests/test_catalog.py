"""Task metadata stays optional while invalid definitions fail clearly."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from .. import catalog


def use_catalog(monkeypatch, tmp_path, tasks):
    (tmp_path / "tasks.json").write_text(json.dumps({"schema_version": catalog.CATALOG_SCHEMA_VERSION, "tasks": tasks}))
    monkeypatch.setattr(catalog, "__file__", str(tmp_path / "catalog.py"))


def test_catalog_accepts_tasks_with_and_without_rank(monkeypatch, tmp_path):
    task = catalog.get_task("kill_zombie")
    task.pop("rank", None)
    ranked = deepcopy(task)
    ranked.update(id="ranked_zombie", rank=12)
    use_catalog(monkeypatch, tmp_path, [task, ranked])
    assert catalog.list_tasks() == [task, ranked]


@pytest.mark.parametrize("rank", [None, True, False, 0, -1, 1.5, "1"])
def test_optional_rank_must_be_positive_integer(monkeypatch, tmp_path, rank):
    task = catalog.get_task("kill_zombie")
    task["rank"] = rank
    use_catalog(monkeypatch, tmp_path, [task])
    with pytest.raises(ValueError):
        catalog.list_tasks()


def test_catalog_listing_works_without_ranks():
    script = Path(catalog.__file__)
    result = subprocess.run([sys.executable, str(script)], check=True, capture_output=True, text=True)
    lines = result.stdout.splitlines()
    tasks = catalog.list_tasks()
    assert len(lines) == len(tasks)
    assert lines == [f'{task["id"]}: {task["instruction"]}' for task in tasks]


def test_ghast_instruction_identifies_creature_and_dimension():
    instruction = catalog.get_task("kill_ghast")["instruction"].lower()
    assert "flying" in instruction and "nether" in instruction
