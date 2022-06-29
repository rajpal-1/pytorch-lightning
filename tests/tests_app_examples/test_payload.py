import os
from time import sleep

import pytest
from tests_app import _PROJECT_ROOT

from lightning_app.testing.testing import run_app_in_cloud


@pytest.mark.cloud
def test_payload_example_cloud() -> None:
    with run_app_in_cloud(os.path.join(_PROJECT_ROOT, "examples/app_examples/payload")) as (_, _, fetch_logs):

        has_logs = False
        while not has_logs:
            for log in fetch_logs():
                if "Application End!" in log:
                    has_logs = True
            sleep(1)
