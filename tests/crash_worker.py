"""Subprocess for test_crash_resume: runs the real stage runner against the test DB and blocks
forever inside the rubric evaluator, so the parent can SIGKILL it mid-evaluation."""

import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from judgebench.worker.stages import run_next
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps


def main(db_url: str, flag: str) -> None:
    def hang(s: object) -> dict[str, object]:
        Path(flag).write_text("in evaluator")
        time.sleep(600)  # killed long before this returns
        return {}

    factory = sessionmaker(create_engine(db_url), expire_on_commit=False)
    d = deps(repos.ALPHA_RAG, fake(rubric_evaluator=hang))
    while run_next(factory, d) is not None:
        pass


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
