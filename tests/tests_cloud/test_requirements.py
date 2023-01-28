import os

from tests_cloud import _API_KEY, _PROJECT_ID, _USERNAME

from lightning.store import download_from_cloud, upload_to_cloud
from lightning.store.save import __STORAGE_DIR_NAME
from pytorch_lightning.demos.boring_classes import BoringModel


def test_requirements(clean_home, version: str = "1.0.0", model_name: str = "boring_model"):
    requirements_list = ["pytorch_lightning==1.7.7", "lightning"]

    upload_to_cloud(
        model_name,
        version=version,
        model=BoringModel(),
        requirements=requirements_list,
        api_key=_API_KEY,
        project_id=_PROJECT_ID,
    )

    download_from_cloud(f"{_USERNAME}/{model_name}", version=version)

    req_folder_path = os.path.join(clean_home, __STORAGE_DIR_NAME, _USERNAME, model_name, version)
    assert os.path.isdir(req_folder_path), "missing: %s" % req_folder_path
    assert "requirements.txt" in os.listdir(req_folder_path), "among files: %r" % os.listdir(req_folder_path)

    with open(f"{req_folder_path}/requirements.txt") as req_file:
        reqs = req_file.readlines()
        reqs = [req.strip("\n") for req in reqs]

    assert requirements_list == reqs
