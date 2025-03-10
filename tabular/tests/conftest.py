import copy
import os
import shutil
import uuid
import pytest

from contextlib import contextmanager
from typing import List

from autogluon.common.utils.resource_utils import ResourceManager
from autogluon.core.utils import download, unzip
from autogluon.core.constants import BINARY, MULTICLASS, REGRESSION
from autogluon.core.data.label_cleaner import LabelCleaner
from autogluon.core.utils import infer_problem_type, generate_train_test_split
from autogluon.features.generators import AutoMLPipelineFeatureGenerator
from autogluon.tabular import TabularDataset, TabularPredictor


def pytest_addoption(parser):
    parser.addoption(
        "--runslow", action="store_true", default=False, help="run slow tests"
    )
    parser.addoption(
        "--runregression", action="store_true", default=False, help="run regression tests"
    )
    parser.addoption(
        "--runpyodide", action="store_true", default=False, help="run Pyodide tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow to run")
    config.addinivalue_line("markers", "regression: mark test as regression test")
    config.addinivalue_line("markers", "pyodide: mark test as pyodide test")


def pytest_collection_modifyitems(config, items):
    skip_slow = pytest.mark.skip(reason="need --runslow option to run")
    skip_regression = pytest.mark.skip(reason="need --runregression option to run")
    skip_pyodide = pytest.mark.skip(reason="need --runpyodide option to run")
    custom_markers = dict(
        slow=skip_slow,
        regression=skip_regression,
        pyodide=skip_pyodide
    )
    if config.getoption("--runslow"):
        # --runslow given in cli: do not skip slow tests
        custom_markers.pop("slow", None)
    if config.getoption("--runregression"):
        # --runregression given in cli: do not skip slow tests
        custom_markers.pop("regression", None)
    if config.getoption("--runpyodide"):
        # --runpyodide given in cli: do not skip pyodide tests
        custom_markers.pop("pyodide", None)

    for item in items:
        for marker in custom_markers:
            if marker in item.keywords:
                item.add_marker(custom_markers[marker])


class DatasetLoaderHelper:
    dataset_info_dict = dict(
        # Binary dataset
        adult={
            'url': 'https://autogluon.s3.amazonaws.com/datasets/AdultIncomeBinaryClassification.zip',
            'name': 'AdultIncomeBinaryClassification',
            'problem_type': BINARY,
            'label': 'class',
        },
        # Multiclass big dataset with 7 classes, all features are numeric. Runs SLOW.
        covertype={
            'url': 'https://autogluon.s3.amazonaws.com/datasets/CoverTypeMulticlassClassification.zip',
            'name': 'CoverTypeMulticlassClassification',
            'problem_type': MULTICLASS,
            'label': 'Cover_Type',
        },
        # Subset of covertype dataset with 3k train/test rows. Ratio of labels is preserved.
        covertype_small={
            'url': 'https://autogluon.s3.amazonaws.com/datasets/CoverTypeMulticlassClassificationSmall.zip',
            'name': 'CoverTypeMulticlassClassificationSmall',
            'problem_type': MULTICLASS,
            'label': 'Cover_Type',
        },
        # Regression with mixed feature-types, skewed Y-values.
        ames={
            'url': 'https://autogluon.s3.amazonaws.com/datasets/AmesHousingPriceRegression.zip',
            'name': 'AmesHousingPriceRegression',
            'problem_type': REGRESSION,
            'label': 'SalePrice',
        },
        # Regression with multiple text field and categorical
        sts={
            'url': 'https://autogluon-text.s3.amazonaws.com/glue_sts.zip',
            'name': 'glue_sts',
            'problem_type': REGRESSION,
            'label': 'score',
        }
    )

    @staticmethod
    def load_dataset(name: str, directory_prefix: str = './datasets/'):
        dataset_info = copy.deepcopy(DatasetLoaderHelper.dataset_info_dict[name])
        train_file = dataset_info.pop('train_file', 'train_data.csv')
        test_file = dataset_info.pop('test_file', 'test_data.csv')
        name_inner = dataset_info.pop('name')
        url = dataset_info.pop('url', None)
        train_data, test_data = DatasetLoaderHelper.load_data(
            directory_prefix=directory_prefix,
            train_file=train_file,
            test_file=test_file,
            name=name_inner,
            url=url,
        )

        return train_data, test_data, dataset_info

    @staticmethod
    def load_data(directory_prefix, train_file, test_file, name, url=None):
        if not os.path.exists(directory_prefix):
            os.mkdir(directory_prefix)
        directory = directory_prefix + name + "/"
        train_file_path = directory + train_file
        test_file_path = directory + test_file
        if (not os.path.exists(train_file_path)) or (not os.path.exists(test_file_path)):
            # fetch files from s3:
            print("%s data not found locally, so fetching from %s" % (name, url))
            zip_name = download(url, directory_prefix)
            unzip(zip_name, directory_prefix)
            os.remove(zip_name)

        train_data = TabularDataset(train_file_path)
        test_data = TabularDataset(test_file_path)
        return train_data, test_data


class FitHelper:
    @staticmethod
    def fit_and_validate_dataset(dataset_name,
                                 fit_args,
                                 init_args=None,
                                 sample_size=1000,
                                 refit_full=True,
                                 delete_directory=True,
                                 extra_metrics=None,
                                 expected_model_count=2,
                                 compile_models=False,
                                 compiler_configs=None):
        if compiler_configs is None:
            compiler_configs = {}
        directory_prefix = './datasets/'
        train_data, test_data, dataset_info = DatasetLoaderHelper.load_dataset(name=dataset_name, directory_prefix=directory_prefix)
        label = dataset_info['label']
        save_path = os.path.join(directory_prefix, dataset_name, f'AutogluonOutput_{uuid.uuid4()}')
        _init_args = dict(
            label=label,
            path=save_path,
        )
        if init_args is None:
            init_args = _init_args
        else:
            _init_args.update(init_args)
            init_args = _init_args
        predictor = FitHelper.fit_dataset(train_data=train_data, init_args=init_args, fit_args=fit_args, sample_size=sample_size)
        if compile_models:
            predictor.compile_models(models="all", compiler_configs=compiler_configs)
            predictor.persist_models(models="all")
        if sample_size is not None and sample_size < len(test_data):
            test_data = test_data.sample(n=sample_size, random_state=0)
        predictor.predict(test_data)
        pred_proba = predictor.predict_proba(test_data)
        predictor.evaluate(test_data)
        predictor.evaluate_predictions(y_true=test_data[label], y_pred=pred_proba)

        model_names = predictor.get_model_names()
        model_name = model_names[0]
        assert len(model_names) == expected_model_count
        if refit_full:
            refit_model_names = predictor.refit_full()
            assert len(refit_model_names) == expected_model_count
            refit_model_name = refit_model_names[model_name]
            assert '_FULL' in refit_model_name
            predictor.predict(test_data, model=refit_model_name)
            predictor.predict_proba(test_data, model=refit_model_name)
        predictor.info()
        predictor.leaderboard(test_data, extra_info=True, extra_metrics=extra_metrics)

        assert os.path.realpath(save_path) == os.path.realpath(predictor.path)
        if delete_directory:
            shutil.rmtree(save_path, ignore_errors=True)  # Delete AutoGluon output directory to ensure runs' information has been removed.
        return predictor

    @staticmethod
    def fit_and_validate_dataset_with_cascade(dataset_name,
                                              fit_args,
                                              cascade: List[str],
                                              sample_size=1000,
                                              refit_full=True,
                                              delete_directory=True,
                                              expected_model_count=2):
        predictor = FitHelper.fit_and_validate_dataset(
            dataset_name=dataset_name,
            fit_args=fit_args,
            sample_size=sample_size,
            refit_full=refit_full,
            expected_model_count=expected_model_count,
            delete_directory=False,
        )
        directory_prefix = './datasets/'
        train_data, test_data, dataset_info = DatasetLoaderHelper.load_dataset(name=dataset_name, directory_prefix=directory_prefix)

        predictor.predict(test_data, model=cascade)
        predictor.predict_proba(test_data, model=cascade)

        if delete_directory:
            shutil.rmtree(predictor.path, ignore_errors=True)  # Delete AutoGluon output directory to ensure runs' information has been removed.

    @staticmethod
    def fit_dataset(train_data, init_args, fit_args, sample_size=None):
        if sample_size is not None and sample_size < len(train_data):
            train_data = train_data.sample(n=sample_size, random_state=0)
        return TabularPredictor(**init_args).fit(train_data, **fit_args)


# Helper functions for training models outside of predictors
class ModelFitHelper:
    @staticmethod
    def fit_and_validate_dataset(dataset_name, model, fit_args, sample_size=1000):
        directory_prefix = './datasets/'
        train_data, test_data, dataset_info = DatasetLoaderHelper.load_dataset(name=dataset_name, directory_prefix=directory_prefix)
        label = dataset_info['label']
        model, label_cleaner, feature_generator = ModelFitHelper.fit_dataset(train_data=train_data, model=model, label=label, fit_args=fit_args, sample_size=sample_size)
        if sample_size is not None and sample_size < len(test_data):
            test_data = test_data.sample(n=sample_size, random_state=0)

        X_test = test_data.drop(columns=[label])
        X_test = feature_generator.transform(X_test)

        model.predict(X_test)
        model.predict_proba(X_test)
        model.get_info()
        return model

    @staticmethod
    def fit_dataset(train_data, model, label, fit_args, sample_size=None):
        if sample_size is not None and sample_size < len(train_data):
            train_data = train_data.sample(n=sample_size, random_state=0)
        X = train_data.drop(columns=[label])
        y = train_data[label]

        problem_type = infer_problem_type(y)
        label_cleaner = LabelCleaner.construct(problem_type=problem_type, y=y)
        y = label_cleaner.transform(y)
        feature_generator = AutoMLPipelineFeatureGenerator()
        X = feature_generator.fit_transform(X, y)

        X, X_val, y, y_val = generate_train_test_split(X, y, problem_type=problem_type, test_size=0.2, random_state=0)

        model.fit(X=X, y=y, X_val=X_val, y_val=y_val, **fit_args)
        return model, label_cleaner, feature_generator
    
    
@contextmanager
def mock_system_resourcses(num_cpus=None, num_gpus=None):
    original_get_cpu_count = ResourceManager.get_cpu_count
    original_get_gpu_count_all = ResourceManager.get_gpu_count_all
    if num_cpus is not None:
        ResourceManager.get_cpu_count = lambda: num_cpus
    if num_gpus is not None:
        ResourceManager.get_gpu_count_all = lambda: num_gpus
    yield
    ResourceManager.get_cpu_count = original_get_cpu_count
    ResourceManager.get_gpu_count_all = original_get_gpu_count_all


@pytest.fixture
def dataset_loader_helper():
    return DatasetLoaderHelper


@pytest.fixture
def fit_helper():
    return FitHelper


@pytest.fixture
def model_fit_helper():
    return ModelFitHelper


@pytest.fixture
def mock_system_resources_ctx_mgr():
    return mock_system_resourcses


@pytest.fixture
def mock_num_cpus():
    return 16


@pytest.fixture
def mock_num_gpus():
    return 2

@pytest.fixture
def k_fold():
    return 2
