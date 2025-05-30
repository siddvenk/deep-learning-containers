# Copyright 2019-2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
from __future__ import absolute_import

import json
import logging
import os
import platform
import shutil
import sys
import tempfile

import boto3
import pytest

from botocore.exceptions import ClientError
from sagemaker import LocalSession, Session
from sagemaker.pytorch import PyTorch

from .utils import image_utils, get_ecr_registry
from .. import NO_P4_REGIONS, NO_G5_REGIONS

logger = logging.getLogger(__name__)
logging.getLogger("boto").setLevel(logging.INFO)
logging.getLogger("boto3").setLevel(logging.INFO)
logging.getLogger("botocore").setLevel(logging.INFO)
logging.getLogger("factory.py").setLevel(logging.INFO)
logging.getLogger("auth.py").setLevel(logging.INFO)
logging.getLogger("connectionpool.py").setLevel(logging.INFO)


dir_path = os.path.dirname(os.path.realpath(__file__))


def pytest_addoption(parser):
    parser.addoption("--build-image", "-D", action="store_true")
    parser.addoption("--build-base-image", "-B", action="store_true")
    parser.addoption("--aws-id")
    parser.addoption("--instance-type")
    parser.addoption("--accelerator-type", default=None)
    parser.addoption("--docker-base-name", default="pytorch")
    parser.addoption("--region", default="us-west-2")
    parser.addoption("--framework-version", default="")
    parser.addoption(
        "--py-version",
        choices=["2", "3", "37", "38", "39", "310", "311", "312"],
        default=str(sys.version_info.major),
    )
    # Processor is still "cpu" for EIA tests
    parser.addoption(
        "--processor", choices=["gpu", "cpu", "eia", "neuron", "neuronx"], default="cpu"
    )
    # If not specified, will default to {framework-version}-{processor}-py{py-version}
    parser.addoption("--tag", default=None)
    parser.addoption(
        "--generate-coverage-doc",
        default=False,
        action="store_true",
        help="use this option to generate test coverage doc",
    )
    parser.addoption(
        "--efa",
        action="store_true",
        default=False,
        help="Run only efa tests",
    )
    parser.addoption("--sagemaker-regions", default="us-west-2")


def pytest_configure(config):
    config.addinivalue_line("markers", "efa(): explicitly mark to run efa tests")


def pytest_runtest_setup(item):
    if item.config.getoption("--efa"):
        efa_tests = [mark for mark in item.iter_markers(name="efa")]
        if not efa_tests:
            pytest.skip("Skipping non-efa tests")


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        print(f"item {item}")
        for marker in item.iter_markers(name="team"):
            print(f"item {marker}")
            team_name = marker.args[0]
            item.user_properties.append(("team_marker", team_name))
            print(f"item.user_properties {item.user_properties}")

    if config.getoption("--generate-coverage-doc"):
        from test.test_utils.test_reporting import TestReportGenerator

        report_generator = TestReportGenerator(items, is_sagemaker=True)
        report_generator.generate_coverage_doc(framework="pytorch", job_type="inference")


# Nightly fixtures
@pytest.fixture(scope="session")
def feature_aws_framework_present():
    pass


@pytest.fixture(scope="session", name="docker_base_name")
def fixture_docker_base_name(request):
    return request.config.getoption("--docker-base-name")


@pytest.fixture(scope="session", name="region")
def fixture_region(request):
    return request.config.getoption("--region")


@pytest.fixture(scope="session", name="framework_version")
def fixture_framework_version(request):
    return request.config.getoption("--framework-version")


@pytest.fixture(scope="session", name="sagemaker_regions")
def fixture_sagemaker_region(request):
    sagemaker_regions = request.config.getoption("--sagemaker-regions")
    return sagemaker_regions.split(",")


@pytest.fixture(scope="session", name="py_version")
def fixture_py_version(request):
    return "py{}".format(int(request.config.getoption("--py-version")))


@pytest.fixture(scope="session", name="processor")
def fixture_processor(request):
    return request.config.getoption("--processor")


@pytest.fixture(scope="session", name="tag")
def fixture_tag(request, framework_version, processor, py_version):
    provided_tag = request.config.getoption("--tag")
    default_tag = "{}-{}-{}".format(framework_version, processor, py_version)
    return provided_tag if provided_tag else default_tag


@pytest.fixture(scope="session", name="docker_image")
def fixture_docker_image(docker_base_name, tag):
    return "{}:{}".format(docker_base_name, tag)


@pytest.fixture
def opt_ml():
    tmp = tempfile.mkdtemp()
    os.mkdir(os.path.join(tmp, "output"))

    # Docker cannot mount Mac OS /var folder properly see
    # https://forums.docker.com/t/var-folders-isnt-mounted-properly/9600
    opt_ml_dir = "/private{}".format(tmp) if platform.system() == "Darwin" else tmp
    yield opt_ml_dir

    shutil.rmtree(tmp, True)


@pytest.fixture(scope="session", name="use_gpu")
def fixture_use_gpu(processor):
    return processor == "gpu"


@pytest.fixture(scope="session", name="build_base_image", autouse=True)
def fixture_build_base_image(
    request, framework_version, py_version, processor, tag, docker_base_name
):
    build_base_image = request.config.getoption("--build-base-image")
    if build_base_image:
        return image_utils.build_base_image(
            framework_name=docker_base_name,
            framework_version=framework_version,
            py_version=py_version,
            base_image_tag=tag,
            processor=processor,
            cwd=os.path.join(dir_path, ".."),
        )

    return tag


@pytest.fixture(scope="session", name="sagemaker_session")
def fixture_sagemaker_session(region):
    return Session(boto_session=boto3.Session(region_name=region))


@pytest.fixture(scope="session", name="sagemaker_local_session")
def fixture_sagemaker_local_session(region):
    return LocalSession(boto_session=boto3.Session(region_name=region))


@pytest.fixture(name="aws_id", scope="session")
def fixture_aws_id(request):
    return request.config.getoption("--aws-id")


@pytest.fixture(name="instance_type", scope="session")
def fixture_instance_type(request, processor):
    provided_instance_type = request.config.getoption("--instance-type")
    default_instance_type = "local" if processor == "cpu" else "local_gpu"
    return provided_instance_type or default_instance_type


@pytest.fixture(name="accelerator_type", scope="session")
def fixture_accelerator_type(request):
    return request.config.getoption("--accelerator-type")


@pytest.fixture(name="docker_registry", scope="session")
def fixture_docker_registry(aws_id, region):
    return get_ecr_registry(aws_id, region)


@pytest.fixture(name="ecr_image", scope="session")
def fixture_ecr_image(docker_registry, docker_base_name, tag):
    return "{}/{}:{}".format(docker_registry, docker_base_name, tag)


@pytest.fixture(autouse=True)
def skip_based_on_image_and_marker_combination(request, ecr_image):
    is_stabilityai_only_test = request.node.get_closest_marker("stabilityai_only") is not None
    if is_stabilityai_only_test and "stabilityai" not in ecr_image:
        pytest.skip(
            f"Skipping because {ecr_image} is not StabilityAI image and the test is supposed to run for only stability images"
        )

    is_skip_stabilityai_test = request.node.get_closest_marker("skip_stabilityai") is not None
    if is_skip_stabilityai_test and "stabilityai" in ecr_image:
        pytest.skip(
            f"Skipping because {ecr_image} is StabilityAI image and the test is not StabilityAI test."
        )


@pytest.fixture(autouse=True)
def skip_by_device_type(request, use_gpu, instance_type, accelerator_type):
    is_gpu = use_gpu or instance_type[3] in ["g", "p"]
    is_eia = accelerator_type is not None

    is_neuron_inst = instance_type.startswith("ml.inf1")
    is_neuronx_inst = instance_type.startswith("ml.trn1") or instance_type.startswith("ml.inf2")

    is_neuron_test = request.node.get_closest_marker("neuron_test") is not None
    is_neuronx_test = request.node.get_closest_marker("neuronx_test") is not None

    if is_neuron_test != is_neuron_inst or is_neuronx_test != is_neuronx_inst:
        pytest.skip("Skipping because test running on '{}' instance".format(instance_type))

    # When running GPU test, skip CPU  and neuron test. When running CPU test, skip GPU  and neuron test.
    elif (request.node.get_closest_marker("gpu_test") and not is_gpu) or (
        request.node.get_closest_marker("cpu_test") and is_gpu
    ):
        pytest.skip("Skipping because running on '{}' instance".format(instance_type))

    # When running EIA test, skip the CPU, GPU and Neuron functions
    elif (
        request.node.get_closest_marker("gpu_test") or request.node.get_closest_marker("cpu_test")
    ) and is_eia:
        pytest.skip("Skipping because running on '{}' instance".format(instance_type))

    # When running CPU or GPU or Neuron test, skip EIA test.
    elif request.node.get_closest_marker("eia_test") and not is_eia:
        pytest.skip("Skipping because running on '{}' instance".format(instance_type))


@pytest.fixture(autouse=True)
def skip_by_py_version(request, py_version):
    if request.node.get_closest_marker("skip_py2") and "py2" in py_version:
        pytest.skip("Skipping the test because Python 2 is not supported.")


@pytest.fixture(autouse=True)
def skip_gpu_instance_restricted_regions(region, instance_type):
    if (region in NO_P4_REGIONS and instance_type.startswith("ml.p4")) or (
        region in NO_G5_REGIONS and instance_type.startswith("ml.g5")
    ):
        pytest.skip(
            "Skipping GPU test in region {} with instance type {}".format(region, instance_type)
        )


@pytest.fixture(autouse=True)
def skip_gpu_py2(request, use_gpu, instance_type, py_version, framework_version):
    is_gpu = use_gpu or instance_type[3] in ["g", "p"]
    if (
        request.node.get_closest_marker("skip_gpu_py2")
        and is_gpu
        and "py2" in py_version
        and framework_version == "1.4.0"
    ):
        pytest.skip("Skipping the test until mms issue resolved.")


def _get_remote_override_flags():
    try:
        s3_client = boto3.client("s3")
        sts_client = boto3.client("sts")
        account_id = sts_client.get_caller_identity().get("Account")
        result = s3_client.get_object(
            Bucket=f"dlc-cicd-helper-{account_id}", Key="override_tests_flags.json"
        )
        json_content = json.loads(result["Body"].read().decode("utf-8"))
    except ClientError as e:
        logger.warning("ClientError when performing S3/STS operation: {}".format(e))
        json_content = {}
    return json_content


def _is_test_disabled(test_name, build_name, version):
    """
    Expected format of remote_override_flags:
    {
        "CB Project Name for Test Type A": {
            "CodeBuild Resolved Source Version": ["test_type_A_test_function_1", "test_type_A_test_function_2"]
        },
        "CB Project Name for Test Type B": {
            "CodeBuild Resolved Source Version": ["test_type_B_test_function_1", "test_type_B_test_function_2"]
        }
    }

    :param test_name: str Test Function node name (includes parametrized values in string)
    :param build_name: str Build Project name of current execution
    :param version: str Source Version of current execution
    :return: bool True if test is disabled as per remote override, False otherwise
    """
    remote_override_flags = _get_remote_override_flags()
    remote_override_build = remote_override_flags.get(build_name, {})
    if version in remote_override_build:
        return not remote_override_build[version] or any(
            [test_keyword in test_name for test_keyword in remote_override_build[version]]
        )
    return False


@pytest.fixture(autouse=True)
def disable_test(request):
    test_name = request.node.name
    # We do not have a regex pattern to find CB name, which means we must resort to string splitting
    build_arn = os.getenv("CODEBUILD_BUILD_ARN")
    build_name = build_arn.split("/")[-1].split(":")[0] if build_arn else None
    version = os.getenv("CODEBUILD_RESOLVED_SOURCE_VERSION")

    if build_name and version and _is_test_disabled(test_name, build_name, version):
        pytest.skip(f"Skipping {test_name} test because it has been disabled.")


@pytest.fixture(autouse=True)
def skip_test_successfully_executed_before(request):
    """
    "cache/lastfailed" contains information about failed tests only. We're running SM tests in separate threads for each image.
    So when we retry SM tests, successfully executed tests executed again because pytest doesn't have that info in /.cache.
    But the flag "--last-failed-no-failures all" requires pytest to execute all the available tests.
    The only sign that a test passed last time - lastfailed file exists and the test name isn't in that file.
    The method checks whether lastfailed file exists and the test name is not in it.
    """
    test_name = request.node.name
    lastfailed = request.config.cache.get("cache/lastfailed", None)

    if lastfailed is not None and not any(
        test_name in failed_test_name for failed_test_name in lastfailed.keys()
    ):
        pytest.skip(f"Skipping {test_name} because it was successfully executed for this commit")
