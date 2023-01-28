import inspect
import os
import tempfile

from tests_cloud import _API_KEY, _PROJECT_ID, _PROJECT_ROOT, _TEST_ROOT, _USERNAME

from lightning.store import download_from_cloud, upload_to_cloud
from lightning.store.save import __STORAGE_DIR_NAME
from pytorch_lightning.demos.boring_classes import BoringModel


def test_source_code_implicit(clean_home, model_name: str = "model_test_source_code_implicit"):
    upload_to_cloud(model_name, model=BoringModel(), api_key=_API_KEY, project_id=_PROJECT_ID)

    download_from_cloud(f"{_USERNAME}/{model_name}")
    assert os.path.isfile(
        os.path.join(
            clean_home,
            __STORAGE_DIR_NAME,
            _USERNAME,
            model_name,
            "latest",
            str(os.path.basename(inspect.getsourcefile(BoringModel))),
        )
    )


def test_source_code_saving_disabled(clean_home, model_name: str = "model_test_source_code_dont_save"):
    upload_to_cloud(model_name, model=BoringModel(), api_key=_API_KEY, project_id=_PROJECT_ID, save_code=False)

    download_from_cloud(f"{_USERNAME}/{model_name}")
    assert not os.path.isfile(
        os.path.join(
            clean_home,
            __STORAGE_DIR_NAME,
            _USERNAME,
            model_name,
            "latest",
            str(os.path.basename(inspect.getsourcefile(BoringModel))),
        )
    )


def test_source_code_explicit_relative_folder(clean_home, model_name: str = "model_test_source_code_explicit_relative"):
    upload_to_cloud(
        model_name, model=BoringModel(), source_code_path=_TEST_ROOT, api_key=_API_KEY, project_id=_PROJECT_ID
    )

    download_from_cloud(f"{_USERNAME}/{model_name}")

    assert os.path.isdir(
        os.path.join(
            clean_home,
            __STORAGE_DIR_NAME,
            _USERNAME,
            model_name,
            "latest",
            os.path.basename(os.path.abspath(_TEST_ROOT)),
        )
    )


def test_source_code_explicit_absolute_folder(
    clean_home, model_name: str = "model_test_source_code_explicit_absolute_path"
):
    # TODO: unify with above `test_source_code_explicit_relative_folder`
    with tempfile.TemporaryDirectory() as tmpdir:
        dir_upload_path = os.path.abspath(tmpdir)
        upload_to_cloud(
            model_name, model=BoringModel(), source_code_path=dir_upload_path, api_key=_API_KEY, project_id=_PROJECT_ID
        )

    download_from_cloud(f"{_USERNAME}/{model_name}")

    assert os.path.isdir(
        os.path.join(
            clean_home,
            __STORAGE_DIR_NAME,
            _USERNAME,
            model_name,
            "latest",
            os.path.basename(os.path.abspath(dir_upload_path)),
        )
    )


def test_source_code_explicit_file(clean_home, model_name: str = "model_test_source_code_explicit_file"):
    file_name = os.path.join(_PROJECT_ROOT, "setup.py")
    upload_to_cloud(
        model_name, model=BoringModel(), source_code_path=file_name, api_key=_API_KEY, project_id=_PROJECT_ID
    )

    download_from_cloud(f"{_USERNAME}/{model_name}")

    assert os.path.isfile(
        os.path.join(
            clean_home,
            __STORAGE_DIR_NAME,
            _USERNAME,
            model_name,
            "latest",
            os.path.basename(file_name),
        )
    )
