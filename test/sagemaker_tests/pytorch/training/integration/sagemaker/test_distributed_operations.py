# Copyright 2018-2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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

import os

import boto3
import pytest
import sagemaker
from sagemaker import utils
from sagemaker.instance_group import InstanceGroup
from sagemaker.pytorch import PyTorch
from sagemaker import Session
from six.moves.urllib.parse import urlparse
from test.test_utils import get_framework_and_version_from_tag, get_cuda_version_from_tag
from packaging.version import Version
from packaging.specifiers import SpecifierSet
from ....training import get_efa_test_instance_type
from ...integration import (
    data_dir,
    dist_operations_path,
    fastai_path,
    mnist_script,
    DEFAULT_TIMEOUT,
    mnist_path,
    gpt2_path,
)
from ...integration.sagemaker.timeout import timeout
from .... import invoke_pytorch_helper_function
from . import invoke_pytorch_estimator

MULTI_GPU_INSTANCE = "ml.g5.12xlarge"
RESOURCE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "resources")


def validate_or_skip_smmodelparallel(ecr_image):
    if not can_run_smmodelparallel(ecr_image):
        pytest.skip("Model Parallelism is supported on CUDA 11 on PyTorch v1.6 and above")


def can_run_smmodelparallel(ecr_image):
    _, image_framework_version = get_framework_and_version_from_tag(ecr_image)
    image_cuda_version = get_cuda_version_from_tag(ecr_image)
    return Version(image_framework_version) in SpecifierSet(">=1.6") and Version(
        image_cuda_version.strip("cu")
    ) >= Version("110")


def validate_or_skip_smmodelparallel_efa(ecr_image):
    if not can_run_smmodelparallel_efa(ecr_image):
        pytest.skip("EFA is only supported on CUDA 11, and on PyTorch 1.8.1 or higher")


def skip_unsupported_instances_smmodelparallel(instance_type):
    if instance_type.startswith("ml.p5"):
        pytest.skip(f"{instance_type} is not supported by smdataparallel")


def can_run_smmodelparallel_efa(ecr_image):
    _, image_framework_version = get_framework_and_version_from_tag(ecr_image)
    image_cuda_version = get_cuda_version_from_tag(ecr_image)
    return Version(image_framework_version) in SpecifierSet(">=1.8.1") and Version(
        image_cuda_version.strip("cu")
    ) >= Version("110")


@pytest.mark.processor("cpu")
@pytest.mark.multinode(3)
@pytest.mark.model("unknown_model")
@pytest.mark.skip_gpu
@pytest.mark.deploy_test
@pytest.mark.skip_test_in_region
@pytest.mark.team("conda")
def test_dist_operations_cpu(
    framework_version, ecr_image, sagemaker_regions, instance_type, dist_cpu_backend
):
    instance_type = instance_type or "ml.c5.xlarge"
    function_args = {
        "framework_version": framework_version,
        "instance_type": instance_type,
        "dist_backend": dist_cpu_backend,
    }
    invoke_pytorch_helper_function(
        ecr_image, sagemaker_regions, _test_dist_operations, function_args
    )


@pytest.mark.processor("gpu")
@pytest.mark.multinode(3)
@pytest.mark.model("unknown_model")
@pytest.mark.skip_cpu
@pytest.mark.deploy_test
@pytest.mark.team("conda")
def test_dist_operations_gpu(
    framework_version, instance_type, ecr_image, sagemaker_regions, dist_gpu_backend
):
    """
    Test is run as multinode
    """
    instance_type = instance_type or "ml.g5.4xlarge"
    function_args = {
        "framework_version": framework_version,
        "instance_type": instance_type,
        "dist_backend": dist_gpu_backend,
    }
    invoke_pytorch_helper_function(
        ecr_image, sagemaker_regions, _test_dist_operations, function_args
    )


@pytest.mark.processor("gpu")
@pytest.mark.model("unknown_model")
@pytest.mark.skip_cpu
@pytest.mark.team("conda")
def test_dist_operations_multi_gpu(
    framework_version, ecr_image, sagemaker_regions, dist_gpu_backend
):
    """
    Test is run as single node, but multi-gpu
    """
    function_args = {
        "framework_version": framework_version,
        "instance_type": MULTI_GPU_INSTANCE,
        "dist_backend": dist_gpu_backend,
        "instance_count": 1,
    }
    invoke_pytorch_helper_function(
        ecr_image, sagemaker_regions, _test_dist_operations, function_args
    )


@pytest.mark.processor("gpu")
@pytest.mark.integration("fastai")
@pytest.mark.model("mnist")
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.team("conda")
def test_dist_operations_fastai_gpu(framework_version, ecr_image, sagemaker_regions):
    _, image_framework_version = get_framework_and_version_from_tag(ecr_image)
    if Version(image_framework_version) in SpecifierSet(">=1.9,<1.13"):
        pytest.skip("Fast ai is not supported on PyTorch v1.9.x, v1.10.x, v1.11.x, v1.12.x")
    if Version(image_framework_version) in SpecifierSet("~=2.6.0"):
        pytest.skip("Fast ai doesn't release for PyTorch v2.6.x")

    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": "train_distributed.py",
            "source_dir": fastai_path,
            "role": "SageMakerRole",
            "instance_count": 1,
            "instance_type": MULTI_GPU_INSTANCE,
            "framework_version": framework_version,
        }

        job_name_prefix = "test-pt-fastai"
        pytorch, sagemaker_session = invoke_pytorch_estimator(
            ecr_image, sagemaker_regions, estimator_parameter, job_name=job_name_prefix
        )

    model_s3_url = pytorch.create_model().model_data
    _assert_s3_file_exists(sagemaker_session.boto_region_name, model_s3_url)


@pytest.mark.skip_smdmodelparallel_test
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.usefixtures("feature_smmp_present")
@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("gpt2")
@pytest.mark.processor("gpu")
@pytest.mark.team("smmodelparallel")
@pytest.mark.parametrize("test_script, num_processes", [("train_gpt_simple.py", 8)])
def test_smmodelparallel_gpt2_multigpu_singlenode(
    ecr_image, instance_type, sagemaker_regions, test_script, num_processes
):
    """
    Tests pt gpt2 command via script mode
    """
    framework, framework_version = get_framework_and_version_from_tag(ecr_image)
    if framework == "pytorch" and Version(framework_version) in SpecifierSet("==1.9.*"):
        pytest.skip("Skipping the test for PT1.9")
    instance_type = "ml.p4d.24xlarge"
    smp_version = (
        110
        if framework == "pytorch" and Version(framework_version) in SpecifierSet(">=1.11.0")
        else 109
    )
    hyperparameters = {
        "training_dir": "/opt/ml/input/data/train",
        "max_steps": 100,
        "seed": 12345,
        "fp16": 1,
        "lr": 2.0e-4,
        "lr_decay_iters": 125000,
        "min_lr": 0.00001,
        "lr-decay-style": "linear",
        "warmup": 0.01,
        "logging_freq": 1,
        "max_context_width": 1024,
        "hidden_width": 768,
        "num_layers": 12,
        "num_heads": 12,
        "n_gpus": 8,
        "train_batch_size": 32,
        "microbatches": 1,
        "tensor_parallel_degree": 4,
        "pipeline_parallel_degree": 2,
        "activation_checkpointing": 1,
        "activation_strategy": "group_2",
        "manual_partition": 1,
        "smp_version": smp_version,
    }
    train = sagemaker.session.s3_input(
        "s3://gpt2-data/train_synthetic_small/",
        distribution="FullyReplicated",
        content_type="application/tfrecord",
        s3_data_type="S3Prefix",
    )
    inputs = {"train": train, "test": train}
    validate_or_skip_smmodelparallel(ecr_image)
    mp_params = {
        "partitions": 2,
        "tensor_parallel_degree": 4,
        "microbatches": 1,
        "optimize": "speed",
        "pipeline": "interleaved",
        "ddp": True,
        "auto_partition": False,
        "default_partition": 0,
        "prescaled_batch": True,
        "shard_optimizer_state": True,
    }
    if smp_version >= 110:
        mp_params["fp16"] = True
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": test_script,
            "role": "SageMakerRole",
            "source_dir": gpt2_path,
            "instance_count": 1,
            "instance_type": instance_type,
            "hyperparameters": hyperparameters,
            "distribution": {
                "smdistributed": {
                    "modelparallel": {
                        "enabled": True,
                        "parameters": mp_params,
                    }
                },
                "mpi": {
                    "enabled": True,
                    "processes_per_host": num_processes,
                    "custom_mpi_options": "-verbose --mca orte_base_help_aggregate 0 -x SMDEBUG_LOG_LEVEL=error -x OMPI_MCA_btl_vader_single_copy_mechanism=none ",
                },
            },
        }
        job_name_prefix = "test-pt-smdmp-gpt2-singlenode"
        invoke_pytorch_estimator(
            ecr_image,
            sagemaker_regions,
            estimator_parameter,
            inputs=inputs,
            job_name=job_name_prefix,
        )


@pytest.mark.skip_smdmodelparallel_test
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.usefixtures("feature_smmp_present")
@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("gpt2")
@pytest.mark.processor("gpu")
@pytest.mark.team("smmodelparallel")
@pytest.mark.parametrize("test_script, num_processes", [("train_gpt_simple.py", 8)])
def test_smmodelparallel_gpt2_multigpu_singlenode_flashattn(
    ecr_image, instance_type, sagemaker_regions, test_script, num_processes
):
    """
    Tests pt gpt2 command via script mode
    """
    framework, framework_version = get_framework_and_version_from_tag(ecr_image)
    if Version(framework_version) in SpecifierSet("<1.12.0"):
        pytest.skip("Skipping the test for older than PT 1.12")
    instance_type = "ml.p4d.24xlarge"
    smp_version = (
        110
        if framework == "pytorch" and Version(framework_version) in SpecifierSet(">=1.11.0")
        else 109
    )
    hyperparameters = {
        "training_dir": "/opt/ml/input/data/train",
        "max_steps": 100,
        "seed": 12345,
        "fp16": 1,
        "lr": 2.0e-4,
        "lr_decay_iters": 125000,
        "min_lr": 0.00001,
        "lr-decay-style": "linear",
        "warmup": 0.01,
        "logging_freq": 1,
        "max_context_width": 1024,
        "hidden_width": 768,
        "num_layers": 12,
        "num_heads": 12,
        "n_gpus": 8,
        "train_batch_size": 32,
        "microbatches": 1,
        "tensor_parallel_degree": 4,
        "pipeline_parallel_degree": 2,
        "activation_checkpointing": 1,
        "activation_strategy": "group_2",
        "manual_partition": 1,
        "smp_version": smp_version,
        "query_key_layer_scaling": 0,
        "assert_flash_attn": 1,
    }
    train = sagemaker.session.s3_input(
        "s3://gpt2-data/train_synthetic_small/",
        distribution="FullyReplicated",
        content_type="application/tfrecord",
        s3_data_type="S3Prefix",
    )
    inputs = {"train": train, "test": train}
    validate_or_skip_smmodelparallel(ecr_image)
    mp_params = {
        "partitions": 2,
        "tensor_parallel_degree": 4,
        "microbatches": 1,
        "optimize": "speed",
        "pipeline": "interleaved",
        "ddp": True,
        "auto_partition": False,
        "default_partition": 0,
        "prescaled_batch": True,
        "shard_optimizer_state": True,
    }
    if smp_version >= 110:
        mp_params["fp16"] = True
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": test_script,
            "role": "SageMakerRole",
            "source_dir": gpt2_path,
            "instance_count": 1,
            "instance_type": instance_type,
            "hyperparameters": hyperparameters,
            "distribution": {
                "smdistributed": {
                    "modelparallel": {
                        "enabled": True,
                        "parameters": mp_params,
                    }
                },
                "mpi": {
                    "enabled": True,
                    "processes_per_host": num_processes,
                    "custom_mpi_options": "-verbose --mca orte_base_help_aggregate 0 -x SMDEBUG_LOG_LEVEL=error -x OMPI_MCA_btl_vader_single_copy_mechanism=none ",
                },
            },
        }
        job_name_prefix = "test-pt-smdmp-gpt2-singlenode-flashattn"
        invoke_pytorch_estimator(
            ecr_image,
            sagemaker_regions,
            estimator_parameter,
            inputs=inputs,
            job_name=job_name_prefix,
        )


@pytest.mark.skip_smdmodelparallel_test
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.usefixtures("feature_smmp_present")
@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("mnist")
@pytest.mark.processor("gpu")
@pytest.mark.multinode(2)
@pytest.mark.team("smmodelparallel")
@pytest.mark.parametrize("test_script, num_processes", [("smmodelparallel_pt_mnist.py", 8)])
def test_smmodelparallel_mnist_multigpu_multinode(
    ecr_image, instance_type, sagemaker_regions, test_script, num_processes
):
    """
    Tests pt mnist command via script mode
    """
    instance_type = "ml.g5.12xlarge"
    validate_or_skip_smmodelparallel(ecr_image)
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": test_script,
            "role": "SageMakerRole",
            "source_dir": mnist_path,
            "instance_count": 2,
            "instance_type": instance_type,
            "hyperparameters": {
                "assert-losses": 1,
                "amp": 1,
                "ddp": 1,
                "data-dir": "data/training",
                "epochs": 5,
            },
            "distribution": {
                "smdistributed": {
                    "modelparallel": {
                        "enabled": True,
                        "parameters": {
                            "partitions": 2,
                            "microbatches": 4,
                            "optimize": "speed",
                            "pipeline": "interleaved",
                            "ddp": True,
                        },
                    }
                },
                "mpi": {
                    "enabled": True,
                    "processes_per_host": num_processes,
                    "custom_mpi_options": "-verbose --mca orte_base_help_aggregate 0 -x SMDEBUG_LOG_LEVEL=error -x OMPI_MCA_btl_vader_single_copy_mechanism=none ",
                },
            },
        }
        job_name_prefix = "test-pt-smdmp-multinode"
        invoke_pytorch_estimator(
            ecr_image, sagemaker_regions, estimator_parameter, job_name=job_name_prefix
        )


@pytest.mark.skip_smdmodelparallel_test
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.usefixtures("feature_smmp_present")
@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("mnist")
@pytest.mark.processor("gpu")
@pytest.mark.multinode(2)
@pytest.mark.team("smmodelparallel")
@pytest.mark.parametrize("test_script, num_processes", [("smmodelparallel_pt_mnist.py", 8)])
def test_hc_smmodelparallel_mnist_multigpu_multinode(
    ecr_image, instance_type, sagemaker_regions, test_script, num_processes
):
    """
    Tests pt mnist command via script mode
    """
    instance_type = "ml.g5.12xlarge"
    validate_or_skip_smmodelparallel(ecr_image)
    instance_count = 2
    training_group = InstanceGroup("train_group", instance_type, instance_count)
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": test_script,
            "role": "SageMakerRole",
            "source_dir": mnist_path,
            "instance_groups": [training_group],
            "hyperparameters": {
                "assert-losses": 1,
                "amp": 1,
                "ddp": 1,
                "data-dir": "data/training",
                "epochs": 5,
            },
            "distribution": {
                "smdistributed": {
                    "modelparallel": {
                        "enabled": True,
                        "parameters": {
                            "partitions": 2,
                            "microbatches": 4,
                            "optimize": "speed",
                            "pipeline": "interleaved",
                            "ddp": True,
                        },
                    }
                },
                "mpi": {
                    "enabled": True,
                    "processes_per_host": num_processes,
                    "custom_mpi_options": "-verbose --mca orte_base_help_aggregate 0 -x SMDEBUG_LOG_LEVEL=error -x OMPI_MCA_btl_vader_single_copy_mechanism=none ",
                },
                "instance_groups": [training_group],
            },
        }
        job_name_prefix = "test-pt-hc-smdmp-multinode"
        invoke_pytorch_estimator(
            ecr_image, sagemaker_regions, estimator_parameter, job_name=job_name_prefix
        )


@pytest.mark.skip_smdmodelparallel_test
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.usefixtures("feature_smmp_present")
@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("mnist")
@pytest.mark.processor("gpu")
@pytest.mark.multinode(2)
@pytest.mark.team("smmodelparallel")
@pytest.mark.parametrize("test_script, num_processes", [("smmodelparallel_pt_mnist.py", 8)])
@pytest.mark.efa()
def test_smmodelparallel_mnist_multigpu_multinode_efa(
    ecr_image, efa_instance_type, sagemaker_regions, test_script, num_processes
):
    """
    Tests pt mnist command via script mode
    """
    validate_or_skip_smmodelparallel_efa(ecr_image)
    skip_unsupported_instances_smmodelparallel(efa_instance_type)
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": test_script,
            "role": "SageMakerRole",
            "source_dir": mnist_path,
            "instance_count": 2,
            "instance_type": efa_instance_type,
            "hyperparameters": {
                "assert-losses": 1,
                "amp": 1,
                "ddp": 1,
                "data-dir": "data/training",
                "epochs": 5,
            },
            "distribution": {
                "smdistributed": {
                    "modelparallel": {
                        "enabled": True,
                        "parameters": {
                            "partitions": 2,
                            "microbatches": 4,
                            "optimize": "speed",
                            "pipeline": "interleaved",
                            "ddp": True,
                        },
                    }
                },
                "mpi": {
                    "enabled": True,
                    "processes_per_host": num_processes,
                    "custom_mpi_options": "-verbose --mca orte_base_help_aggregate 0 -x SMDEBUG_LOG_LEVEL=error -x OMPI_MCA_btl_vader_single_copy_mechanism=none -x FI_EFA_USE_DEVICE_RDMA=1 -x FI_PROVIDER=efa ",
                },
            },
        }
        job_name_prefix = "test-pt-smdmp-multinode-efa"
        invoke_pytorch_estimator(
            ecr_image, sagemaker_regions, estimator_parameter, job_name=job_name_prefix
        )


@pytest.mark.skip_smdmodelparallel_test
@pytest.mark.skip_cpu
@pytest.mark.skip_py2_containers
@pytest.mark.skip_trcomp_containers
@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("gpt2")
@pytest.mark.processor("gpu")
@pytest.mark.multinode(2)
@pytest.mark.team("smmodelparallel")
@pytest.mark.parametrize("test_script, num_processes", [("train_gpt_simple.py", 8)])
@pytest.mark.efa()
def test_smmodelparallel_gpt2_sdp_multinode_efa(
    ecr_image, efa_instance_type, sagemaker_regions, test_script, num_processes
):
    """
    Tests pt gpt2 command via script mode
    """
    framework, framework_version = get_framework_and_version_from_tag(ecr_image)
    if framework == "pytorch" and Version(framework_version) in SpecifierSet("<1.12.0"):
        pytest.skip("Skipping the test for PT version before 1.12")
    smp_version = 111
    hyperparameters = {
        "training_dir": "/opt/ml/input/data/train",
        "max_steps": 100,
        "seed": 12345,
        "fp16": 1,
        "lr": 2.0e-4,
        "lr_decay_iters": 125000,
        "min_lr": 0.00001,
        "lr-decay-style": "linear",
        "warmup": 0.01,
        "logging_freq": 1,
        "max_context_width": 1024,
        "hidden_width": 768,
        "num_layers": 12,
        "num_heads": 12,
        "n_gpus": 8,
        "train_batch_size": 4,
        "microbatches": 1,
        "tensor_parallel_degree": 1,
        "pipeline_parallel_degree": 1,
        "activation_checkpointing": 1,
        "activation_strategy": "group_2",
        "manual_partition": 1,
        "smp_version": smp_version,
    }
    train = sagemaker.session.s3_input(
        "s3://gpt2-data/train_synthetic_small/",
        distribution="FullyReplicated",
        content_type="application/tfrecord",
        s3_data_type="S3Prefix",
    )
    inputs = {"train": train, "test": train}
    validate_or_skip_smmodelparallel(ecr_image)
    skip_unsupported_instances_smmodelparallel(efa_instance_type)
    mp_params = {
        "partitions": 1,
        "tensor_parallel_degree": 1,
        "microbatches": 1,
        "optimize": "speed",
        "pipeline": "interleaved",
        "ddp": True,
        "auto_partition": False,
        "default_partition": 0,
        "prescaled_batch": True,
        "sharded_data_parallel_degree": 4,
        "offload_activations": True,
    }
    if smp_version >= 110:
        mp_params["fp16"] = True
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": test_script,
            "role": "SageMakerRole",
            "source_dir": gpt2_path,
            "instance_count": 2,
            "instance_type": efa_instance_type,
            "hyperparameters": hyperparameters,
            "distribution": {
                "smdistributed": {
                    "modelparallel": {
                        "enabled": True,
                        "parameters": mp_params,
                    }
                },
                "mpi": {
                    "enabled": True,
                    "processes_per_host": num_processes,
                    "custom_mpi_options": "-verbose --mca orte_base_help_aggregate 0 -x SMDEBUG_LOG_LEVEL=error -x OMPI_MCA_btl_vader_single_copy_mechanism=none ",
                },
            },
        }
        job_name_prefix = "test-pt-smdmp-gpt2-sdp-multinode"
        invoke_pytorch_estimator(
            ecr_image,
            sagemaker_regions,
            estimator_parameter,
            inputs=inputs,
            job_name=job_name_prefix,
        )


@pytest.mark.integration("smmodelparallel")
@pytest.mark.model("mnist")
@pytest.mark.processor("gpu")
@pytest.mark.skip_cpu
@pytest.mark.efa()
@pytest.mark.skip_py2_containers
@pytest.mark.team("conda")
def test_sanity_efa(ecr_image, efa_instance_type, sagemaker_regions):
    """
    Tests pt mnist command via script mode
    """
    validate_or_skip_smmodelparallel_efa(ecr_image)
    skip_unsupported_instances_smmodelparallel(efa_instance_type)
    efa_test_path = os.path.join(RESOURCE_PATH, "efa", "test_efa.sh")
    with timeout(minutes=DEFAULT_TIMEOUT):
        estimator_parameter = {
            "entry_point": efa_test_path,
            "role": "SageMakerRole",
            "instance_count": 1,
            "instance_type": efa_instance_type,
            "distribution": {
                "mpi": {"enabled": True, "processes_per_host": 1},
            },
        }
        job_name_prefix = "test-pt-efa-sanity"
        invoke_pytorch_estimator(
            ecr_image, sagemaker_regions, estimator_parameter, job_name=job_name_prefix
        )


def _test_dist_operations(
    ecr_image, sagemaker_session, framework_version, instance_type, dist_backend, instance_count=3
):
    with timeout(minutes=DEFAULT_TIMEOUT):
        pytorch = PyTorch(
            entry_point=dist_operations_path,
            role="SageMakerRole",
            instance_count=instance_count,
            instance_type=instance_type,
            sagemaker_session=sagemaker_session,
            image_uri=ecr_image,
            framework_version=framework_version,
            hyperparameters={"backend": dist_backend},
        )

        pytorch = _disable_sm_profiler(sagemaker_session.boto_region_name, pytorch)

        pytorch.sagemaker_session.default_bucket()
        fake_input = pytorch.sagemaker_session.upload_data(
            path=dist_operations_path, key_prefix="pytorch/distributed_operations"
        )
        pytorch.fit(
            {"required_argument": fake_input},
            job_name=utils.unique_name_from_base("test-pt-dist-operations"),
        )


def _assert_s3_file_exists(region, s3_url):
    parsed_url = urlparse(s3_url)
    s3 = boto3.resource("s3", region_name=region)
    s3.Object(parsed_url.netloc, parsed_url.path.lstrip("/")).load()


def _disable_sm_profiler(region, estimator):
    """Disable SMProfiler feature for China regions"""

    if region in ("cn-north-1", "cn-northwest-1"):
        estimator.disable_profiler = True
    return estimator
