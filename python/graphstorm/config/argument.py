"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Arguments and config
"""

import argparse
import logging
import math
import os
import shutil
import sys
import warnings
from typing import Any, Dict

import yaml
import torch as th
import torch.nn.functional as F
from dgl.distributed.constants import DEFAULT_NTYPE, DEFAULT_ETYPE

from .config import (
    # Encoders
    BUILTIN_ENCODER, BUILTIN_GNN_ENCODER,
    # Backends
    SUPPORTED_BACKEND,
    # Edge feature operations
    BUILTIN_EDGE_FEAT_MP_OPS,
    # Loss functions
    BUILTIN_CLASS_LOSS_CROSS_ENTROPY, BUILTIN_CLASS_LOSS_FUNCTION,
    BUILTIN_LP_LOSS_CONTRASTIVELOSS, BUILTIN_LP_LOSS_CROSS_ENTROPY, BUILTIN_LP_LOSS_FUNCTION,
    BUILTIN_REGRESSION_LOSS_FUNCTION, BUILTIN_REGRESSION_LOSS_MSE, BUILTIN_CLASS_LOSS_FOCAL,
    # Tasks
    BUILTIN_TASK_EDGE_CLASSIFICATION, BUILTIN_TASK_EDGE_REGRESSION,
    BUILTIN_TASK_LINK_PREDICTION, BUILTIN_TASK_NODE_CLASSIFICATION,
    BUILTIN_TASK_NODE_REGRESSION, BUILTIN_TASK_RECONSTRUCT_EDGE_FEAT,
    BUILTIN_TASK_RECONSTRUCT_NODE_FEAT, LINK_PREDICTION_MAJOR_EVAL_ETYPE_ALL,
    SUPPORTED_TASKS,
    # Filenames
    GS_RUNTIME_TRAINING_CONFIG_FILENAME,
    GS_RUNTIME_GCONSTRUCT_FILENAME,
    # GNN normalization
    BUILTIN_GNN_NORM,
    # Early stopping strategies
    EARLY_STOP_AVERAGE_INCREASE_STRATEGY, EARLY_STOP_CONSECUTIVE_INCREASE_STRATEGY,
    # Task tracking
    GRAPHSTORM_SAGEMAKER_TASK_TRACKER, SUPPORTED_TASK_TRACKER,
    # Link prediction
    BUILTIN_LP_DISTMULT_DECODER, GRAPHSTORM_LP_EMB_L2_NORMALIZATION,
    GRAPHSTORM_LP_EMB_NORMALIZATION_METHODS, SUPPORTED_LP_DECODER,
    # Model layers
    GRAPHSTORM_MODEL_ALL_LAYERS, GRAPHSTORM_MODEL_DECODER_LAYER,
    GRAPHSTORM_MODEL_EMBED_LAYER, GRAPHSTORM_MODEL_LAYER_OPTIONS,
    # Utility functions and classes
    TaskInfo, get_mttask_id, FeatureGroup
)

from ..utils import TORCH_MAJOR_VER, get_log_level, get_graph_name, get_rank

from ..eval import SUPPORTED_CLASSIFICATION_METRICS
from ..eval import SUPPORTED_REGRESSION_METRICS
from ..eval import SUPPORTED_LINK_PREDICTION_METRICS
from ..eval import (SUPPORTED_HIT_AT_METRICS, SUPPORTED_FSCORE_AT_METRICS,
                    SUPPORTED_RECALL_AT_PRECISION_METRICS, SUPPORTED_PRECISION_AT_RECALL_METRICS)
from ..eval import is_float

from ..dataloading import BUILTIN_LP_UNIFORM_NEG_SAMPLER
from ..dataloading import BUILTIN_LP_JOINT_NEG_SAMPLER

__all__ = [
    "get_argument_parser",
]

def get_argument_parser():
    """ Get GraphStorm CLI argument parser.

    This argument parser can accept and parse all GraphStorm model training and inference
    configurations defined in a yaml file. It also can accept and parse the corresponding
    arugments in GraphStorm launch CLIs. Specifically, it will parses yaml config file first,
    and then parses arguments to overwrite parameters defined in the yaml file or add new
    parameters.

    This ``get_argument_parser()`` is also useful when users want to convert customized models
    to use GraphStorm CLIs.

    Examples:
    ----------

    .. code:: python

        from graphstorm.config import get_argument_parser, GSConfig

        if __name__ == '__main__':
            # use GraphStorm argument parser to accept configuration yaml file and other arguments
            arg_parser = get_argument_parser()

            # parse all arguments and split GraphStorm's built-in arguments from the customized ones
            gs_args, unknown_args = arg_parser.parse_known_args()

            print(f'GS arguments: {gs_args}')
            print(f'Non GS arguments: {unknown_args}')

            # use gs_args to create a GSConfig object
            config = GSConfig(gs_args)

    Return
    -------
    parser: an ArgumentParser
        The parser include all GraphStorm model training and inference configurations.
    """
    parser = argparse.ArgumentParser(description="GSGNN Arguments")
    parser.add_argument('--logging-level', type=str, default="info",
                        help="Change the logging level. " + \
                               "Potential values are 'debug', 'info', 'warning', 'error'." + \
                               "The default value is 'info'.")
    parser.add_argument('--logging-file', type=str, default=argparse.SUPPRESS,
                        help='The file where the logging is saved to.')
    # Required parameters
    parser.add_argument(
        "--yaml_config_file",
        "--cf",
        help="pointer to the yaml configuration file of the experiment",
        type=str,
        required=True,
    )

    if TORCH_MAJOR_VER >= 2:
        parser.add_argument(
                "--local-rank",
                type=int,
                default=0,
                help="local_rank for distributed training on gpus",
                )
    else:
        parser.add_argument(
                "--local_rank",
                type=int,
                default=0,
                help="local_rank for distributed training on gpus",
                )

    # Optional parameters to override arguments in yaml config
    parser = _add_initialization_args(parser)
    # basic args
    parser = _add_gsgnn_basic_args(parser)
    # gnn args
    parser = _add_gnn_args(parser)
    parser = _add_input_args(parser)
    parser = _add_output_args(parser)
    parser = _add_task_tracker(parser)
    parser = _add_hyperparam_args(parser)
    parser = _add_rgcn_args(parser)
    parser = _add_rgat_args(parser)
    parser = _add_link_prediction_args(parser)
    parser = _add_node_classification_args(parser)
    parser = _add_edge_classification_args(parser)
    parser = _add_task_general_args(parser)
    parser = _add_lm_model_args(parser)
    parser = _add_distill_args(parser)
    return parser

# pylint: disable=no-member
class GSConfig:
    """GSgnn configuration class.

    GSConfig contains all GraphStorm model training and inference configurations, which can
    either be loaded from a yaml file specified in the ``--cf`` argument, or from CLI arguments.
    """
    def __init__(self, cmd_args):
        """ Construct a GSConfig object.

        Parameters:
        ------------
        cmd_args: Arguments
            Commend line arguments.
        """
        # need to config the logging at very beginning. Otherwise, logging will not work.
        log_level = get_log_level(cmd_args.logging_level) \
                if hasattr(cmd_args, "logging_level") else logging.INFO
        log_file = cmd_args.logging_file if hasattr(cmd_args, "logging_file") else None
        if log_file is None:
            # need to force the logging to reset the existing logging handlers
            # in order to make sure this config is effective.
            logging.basicConfig(level=log_level, force=True)
        else:
            logging.basicConfig(filename=log_file, level=log_level, force=True)
        # enable DeprecationWarning
        warnings.simplefilter('always', DeprecationWarning)

        self.yaml_paths = cmd_args.yaml_config_file
        # Load all arguments from yaml config
        configuration = self.load_yaml_config(cmd_args.yaml_config_file)

        multi_task_config = None
        if 'multi_task_learning' in configuration['gsf']:
            multi_task_config = configuration['gsf']['multi_task_learning']
            del configuration['gsf']['multi_task_learning']

        self.set_attributes(configuration)
        # Override class attributes using command-line arguments
        self.override_arguments(cmd_args)
        self.local_rank = cmd_args.local_rank

        logging.debug(str(configuration))
        cmd_args_dict = cmd_args.__dict__
        # Print overriden arguments.
        for arg_key in cmd_args_dict:
            if arg_key not in ["yaml_config_file", "local_rank"]:
                logging.debug("Overriding Argument: %s", arg_key)
        # We do argument check as early as possible to prevent config bugs.
        self.handle_argument_conflicts()

        # parse multi task learning config and save it into self._multi_tasks
        if multi_task_config is not None:
            self._parse_multi_tasks(multi_task_config)
        else:
            self._multi_tasks = None

        # If model output is configured, save the runtime train config as a yaml file there,
        # and the graph construction config, if one exists in the input
        if hasattr(self, "_save_model_path") and self._save_model_path:
            # Ensure model output directory exists
            os.makedirs(self._save_model_path, exist_ok=True)

            # Save a copy of train config with runtime args
            train_config_output_path = os.path.join(
                self._save_model_path, GS_RUNTIME_TRAINING_CONFIG_FILENAME)
            self._save_runtime_train_config(train_config_output_path)

            # Copy over graph construction config, if one exists
            gconstruct_config_output_path = os.path.join(
                self._save_model_path, GS_RUNTIME_GCONSTRUCT_FILENAME)
            self._copy_graph_construct_config(gconstruct_config_output_path)

    def _copy_graph_construct_config(self, output_data_config):
        """ Copy graph construct config to the model output path.
        """
        # Copy data configuration file if available
        if get_rank() == 0:
            try:
                part_config_dir = os.path.dirname(self.part_config)
                input_data_config = os.path.join(part_config_dir, GS_RUNTIME_GCONSTRUCT_FILENAME)
                if os.path.exists(input_data_config):
                    shutil.copy2(
                        input_data_config,
                        output_data_config
                    )
                else:
                    warnings.warn(
                        f"Graph construction config {GS_RUNTIME_GCONSTRUCT_FILENAME} "
                        f"not found in {part_config_dir}. "
                        "This is expected for older models (trained with version < 0.5). "
                        "You will need to copy over the graph construction  "
                        "config for model deployment.")
            except Exception as e: # pylint: disable=broad-exception-caught
                warnings.warn(
                    f"Failed to copy {GS_RUNTIME_GCONSTRUCT_FILENAME} to model output: {str(e)}. "
                    "You  will need to copy over the graph construction "
                    "config for model deployment.")


    def set_attributes(self, configuration):
        """Set class attributes from 2nd level arguments in yaml config"""
        if 'lm_model' in configuration:
            lm_model = configuration['lm_model']
            assert "node_lm_models" in lm_model or "distill_lm_models" in lm_model, \
                "either node_lm_models or distill_lm_models must be provided"
            # if node_lm_models is not defined, ignore the lm model
            if "node_lm_models" in lm_model:
                # has node language model configuration, e.g.,
                # lm_model:
                #   node_lm_models:
                #     -
                #       lm_type: bert
                #       model_name: "bert-base-uncased"
                #       gradient_checkpoint: true
                #       node_types:
                #         - n_0
                #         - n_1
                #     -
                #       lm_type: bert
                #       model_name: "allenai/scibert_scivocab_uncased"
                #       gradient_checkpoint: true
                #       node_types:
                #         - n_2
                node_lm_models = lm_model['node_lm_models']
                setattr(self, "_node_lm_configs", node_lm_models)
            else:
                # has distill language model configuration, e.g.,
                # lm_model:
                #   distill_lm_models:
                #     -
                #       lm_type: DistilBertModel
                #       model_name: "distilbert-base-uncased"
                distill_lm_models = lm_model['distill_lm_models']
                setattr(self, "_distill_lm_configs", distill_lm_models)

        # handle gnn config
        gnn_family = configuration['gsf']
        for family, param_family in gnn_family.items():
            for key, val in param_family.items():
                setattr(self, f"_{key}", val)

            if family == BUILTIN_TASK_LINK_PREDICTION:
                setattr(self, "_task_type", BUILTIN_TASK_LINK_PREDICTION)
            elif family == BUILTIN_TASK_EDGE_CLASSIFICATION:
                setattr(self, "_task_type", BUILTIN_TASK_EDGE_CLASSIFICATION)
            elif family == BUILTIN_TASK_EDGE_REGRESSION:
                setattr(self, "_task_type", BUILTIN_TASK_EDGE_REGRESSION)
            elif family == BUILTIN_TASK_NODE_CLASSIFICATION:
                setattr(self, "_task_type", BUILTIN_TASK_NODE_CLASSIFICATION)
            elif family == BUILTIN_TASK_NODE_REGRESSION:
                setattr(self, "_task_type", BUILTIN_TASK_NODE_REGRESSION)

        if 'udf' in configuration:
            udf_family = configuration['udf']
            # directly add udf configs as config arguments
            for key, val in udf_family.items():
                setattr(self, key, val)

    def set_task_attributes(self, configuration):
        """ Set graph task specific attributes

            This function is called when GSConfig is used to
            store graph task specific information in multi-task learning.

            .. code:: python

                task_info = GSConfig.__new__(GSConfig)
                task_info.set_task_attributes(task_config)

                target_ntype = task_info.target_ntype

            By reusing GSConfig object, we can use the same code base
            for single task learning and multi-task learning.

        Parameters
        ----------
        configuration: dict
            Task specific config
        """
        for key, val in configuration.items():
            setattr(self, f"_{key}", val)

    def _save_runtime_train_config(self, output_path: str):
        """Save a YAML file that combines the input YAML with runtime args.

        Parameters:
        -----------
        output_path : str
            Path where to save the config.
        """
        # Get the original YAML content
        yaml_config = self.load_yaml_config(self.yaml_paths)

        # Update with runtime args (all attributes starting with '_')
        for attr_name, attr_value in vars(self).items():
            if attr_name.startswith('_') and not attr_name.startswith('__'):
                # Extract the key without the underscore
                key = attr_name[1:]

                # Find the appropriate section to update
                if 'gsf' in yaml_config:
                    for section in yaml_config['gsf'].values():
                        if isinstance(section, dict) and key in section:
                            section[key] = attr_value
                            break
                    else:
                        # If not found in any section, add to the `runtime` section
                        if 'runtime' not in yaml_config['gsf']:
                            yaml_config['gsf']['runtime'] = {}
                        yaml_config['gsf']['runtime'].update({key: attr_value})
                else:
                    raise ValueError("GraphStorm configuration needs a 'gsf' section")

        # Try to save to model output location
        try:
            # Save the combined config
            with open(output_path, 'w', encoding="utf-8") as f:
                yaml.dump(yaml_config, f, default_flow_style=False)

            logging.info("Saved combined configuration to %s", output_path)
        except FileNotFoundError:
            warnings.warn(
                f"Could not save config: directory {os.path.dirname(output_path)} not accessible")
        except PermissionError:
            warnings.warn(f"Could not save config: permission denied for {output_path}")
        except Exception as e: # pylint: disable=broad-exception-caught
            warnings.warn(f"Could not save config: {str(e)}")


    def _parse_general_task_config(self, task_config):
        """ Parse the genral task info

        Parameters
        ----------
        task_config: dict
            Task config
        """
        if "mask_fields" in task_config:
            mask_fields = task_config["mask_fields"]
            assert len(mask_fields) == 3, \
                "The mask_fileds should be a list as [train-mask, validation-mask, test-mask], " \
                f"but get {mask_fields}."
        else:
            mask_fields = (None, None, None)

        task_weight = task_config["task_weight"] \
            if "task_weight" in task_config else 1.0
        assert task_weight > 0, f"task_weight should be larger than 0, but get {task_weight}."

        batch_size = self.batch_size \
            if "batch_size" not in task_config else task_config["batch_size"]
        return mask_fields, task_weight, batch_size

    def _parse_node_classification_task(self, task_config):
        """ Parse the node classification task info.

        Parameters
        ----------
        task_config: dict
            Node classification task config.
        """
        task_type = BUILTIN_TASK_NODE_CLASSIFICATION
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_node_class_arguments()

        target_ntype = task_info.target_ntype
        label_field = task_info.label_field

        task_id = get_mttask_id(task_type=task_type,
                                ntype=target_ntype,
                                label=label_field)
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)

        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_node_regression_task(self, task_config):
        """ Parse the node regression task info.

        Parameters
        ----------
        task_config: dict
            Node regression task config.
        """
        task_type = BUILTIN_TASK_NODE_REGRESSION
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_node_regression_arguments()

        target_ntype = task_info.target_ntype
        label_field = task_info.label_field

        task_id = get_mttask_id(task_type=task_type,
                                ntype=target_ntype,
                                label=label_field)
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)

        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_edge_classification_task(self, task_config):
        """ Parse the edge classification task info

        Parameters
        ----------
        task_config: dict
            Edge classification task config
        """
        task_type = BUILTIN_TASK_EDGE_CLASSIFICATION
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_edge_class_arguments()

        target_etype = task_info.target_etype
        label_field = task_info.label_field

        task_id = get_mttask_id(task_type=task_type,
                                etype=target_etype,
                                label=label_field)
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)
        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_edge_regression_task(self, task_config):
        """ Parse the edge regression task info

        Parameters
        ----------
        task_config: dict
            Edge regression task config
        """
        task_type = BUILTIN_TASK_EDGE_REGRESSION
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_edge_regression_arguments()

        target_etype = task_info.target_etype
        label_field = task_info.label_field

        task_id = get_mttask_id(task_type=task_type,
                                etype=target_etype,
                                label=label_field)
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)
        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_link_prediction_task(self, task_config):
        """ Parse the link prediction task info

        Parameters
        ----------
        task_config: dict
           Link prediction task config
        """
        task_type = BUILTIN_TASK_LINK_PREDICTION
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_link_prediction_arguments()

        train_etype = task_info.train_etype
        task_id = get_mttask_id(
            task_type=task_type,
            etype=train_etype if train_etype is not None else "ALL_ETYPE")
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)
        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_reconstruct_node_feat(self, task_config):
        """ Parse the reconstruct node feature task info

        Parameters
        ----------
        task_config: dict
            Reconstruct node feature task config
        """
        task_type = BUILTIN_TASK_RECONSTRUCT_NODE_FEAT
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_node_feat_reconstruct_arguments()

        target_ntype = task_info.target_ntype
        label_field = task_info.reconstruct_nfeat_name

        task_id = get_mttask_id(task_type=task_type,
                                ntype=target_ntype,
                                label=label_field)
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)

        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_reconstruct_edge_feat(self, task_config):
        """ Parse the reconstruct edge feature task info

        Parameters
        ----------
        task_config: dict
            Reconstruct edge feature task config.
        """
        task_type = BUILTIN_TASK_RECONSTRUCT_EDGE_FEAT
        mask_fields, task_weight, batch_size = \
            self._parse_general_task_config(task_config)
        task_config["batch_size"] = batch_size

        task_info = GSConfig.__new__(GSConfig)
        task_info.set_task_attributes(task_config)
        setattr(task_info, "_task_type", task_type)
        task_info.verify_edge_feat_reconstruct_arguments()

        target_etype = task_info.target_etype
        label_field = task_info.reconstruct_efeat_name

        task_id = get_mttask_id(task_type=task_type,
                                etype=target_etype,
                                label=label_field)
        setattr(task_info, "train_mask", mask_fields[0])
        setattr(task_info, "val_mask", mask_fields[1])
        setattr(task_info, "test_mask", mask_fields[2])
        setattr(task_info, "task_weight", task_weight)

        return TaskInfo(task_type=task_type,
                        task_id=task_id,
                        task_config=task_info)

    def _parse_multi_tasks(self, multi_task_config):
        """ Parse multi-task configuration

        The Yaml config for multi-task learning looks like:

        .. code-block:: yaml
            multi_task_learning:
              - node_classification:
                target_ntype: "movie"
                label_field: "label"
                mask_fields:
                  - "train_mask_field_nc"
                  - "val_mask_field_nc"
                  - "test_mask_field_nc"
                task_weight: 1.0
                eval_metric:
                  - "accuracy"
              - edge_classification:
                target_etype:
                  - "user,rating,movie"
                label_field: "rate"
                multilabel: false
                mask_fields:
                  - "train_mask_field_ec"
                  - "val_mask_field_ec"
                  - "test_mask_field_ec"
                task_weight: 0.5 # weight of the task
              - link_prediction:
                num_negative_edges: 4
                num_negative_edges_eval: 100


        Parameters
        ----------
        multi_task_config: list
            A list of configs for multiple tasks.
        """
        assert len(multi_task_config) > 1, \
            "There must be at least two tasks"

        tasks = []
        for task_config in multi_task_config:
            assert isinstance(task_config, dict) and len(task_config) == 1, \
                "When defining multiple tasks for " \
                "training, define one task each time."

            if "node_classification" in task_config:
                task = self._parse_node_classification_task(
                    task_config["node_classification"])
            elif "node_regression" in task_config:
                task = self._parse_node_regression_task(
                    task_config["node_regression"])
            elif "edge_classification" in task_config:
                task = self._parse_edge_classification_task(
                    task_config["edge_classification"])
            elif "edge_regression" in task_config:
                task = self._parse_edge_regression_task(
                    task_config["edge_regression"])
            elif "link_prediction" in task_config:
                task = self._parse_link_prediction_task(
                    task_config["link_prediction"])
            elif "reconstruct_node_feat" in task_config:
                task = self._parse_reconstruct_node_feat(
                    task_config["reconstruct_node_feat"])
            elif "reconstruct_edge_feat" in task_config:
                task = self._parse_reconstruct_edge_feat(
                    task_config["reconstruct_edge_feat"])
            else:
                raise ValueError(f"Invalid task type in multi-task learning {task_config}.")
            tasks.append(task)
        logging.debug("Multi-task learning with %d tasks", len(tasks))
        self._multi_tasks = tasks

    def load_yaml_config(self, yaml_path) -> Dict[str, Any]:
        """Helper function to load a yaml config file"""
        with open(yaml_path, "r", encoding='utf-8') as stream:
            try:
                return yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                raise ValueError(f"Yaml error - check yaml file {exc}")

    def override_arguments(self, cmd_args):
        """Override arguments in yaml config using command-line arguments"""
        # TODO: Support overriding for all arguments in yaml
        cmd_args_dict = cmd_args.__dict__
        for arg_key, arg_val in cmd_args_dict.items():
            if arg_key not in ["yaml_config_file", "local_rank"]:
                if arg_key == "save_model_path" and arg_val.lower() == "none":
                    arg_val = None
                if arg_key == "save_embed_path" and arg_val.lower() == "none":
                    arg_val = None
                if arg_key == "save_prediction_path" and arg_val.lower() == "none":
                    arg_val = None

                # for basic attributes
                setattr(self, f"_{arg_key}", arg_val)

    def verify_node_feat_reconstruct_arguments(self):
        """Verify the correctness of arguments for node feature reconstruction tasks.

            .. versionadded:: 0.4.0
        """
        _ = self.target_ntype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.reconstruct_nfeat_name

    def verify_edge_feat_reconstruct_arguments(self):
        """Verify the correctness of arguments for edge feature reconstruction tasks.
        """
        _ = self.target_etype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.reconstruct_efeat_name

    def verify_node_class_arguments(self):
        """ Verify the correctness of arguments for node classification tasks.
        """
        _ = self.target_ntype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.label_field
        _ = self.num_classes
        _ = self.multilabel
        _ = self.multilabel_weights
        _ = self.imbalance_class_weights
        _ = self.class_loss_func

    def verify_node_regression_arguments(self):
        """ Verify the correctness of arguments for node regression tasks.
        """
        _ = self.target_ntype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.label_field
        _ = self.regression_loss_func

    def verify_edge_class_arguments(self):
        """ Verify the correctness of arguments for edge classification tasks.
        """
        _ = self.target_etype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.label_field
        _ = self.num_classes
        _ = self.multilabel
        _ = self.multilabel_weights
        _ = self.imbalance_class_weights
        _ = self.decoder_type
        _ = self.num_decoder_basis
        _ = self.decoder_edge_feat
        _ = self.class_loss_func

    def verify_edge_regression_arguments(self):
        """ Verify the correctness of arguments for edge regression tasks.
        """
        _ = self.target_etype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.label_field
        _ = self.decoder_type
        _ = self.num_decoder_basis
        _ = self.decoder_edge_feat
        _ = self.regression_loss_func

    def verify_link_prediction_arguments(self):
        """ Verify the correctness of arguments for link prediction tasks.
        """
        _ = self.target_etype
        _ = self.batch_size
        _ = self.eval_metric
        _ = self.train_etype
        _ = self.eval_etype
        _ = self.train_negative_sampler
        _ = self.eval_negative_sampler
        _ = self.num_negative_edges
        _ = self.num_negative_edges_eval
        _ = self.reverse_edge_types_map
        _ = self.exclude_training_targets
        _ = self.lp_loss_func
        _ = self.lp_decoder_type
        _ = self.gamma
        _ = self.report_eval_per_type


    def verify_arguments(self, is_train):
        """ Verify the correctness of arguments.

        Parameters
        ----------
        is_train : bool
            Whether this is for training.
        """
        # Trigger the checks in the arguments.
        _ = self.save_perf_results_path
        _ = self.profile_path
        _ = self.graph_name
        _ = self.backend
        _ = self.ip_config
        _ = self.part_config
        _ = self.node_id_mapping_file
        _ = self.edge_id_mapping_file
        _ = self.verbose
        _ = self.use_wholegraph_embed
        _ = self.use_graphbolt

        # Data
        _ = self.node_feat_name
        _ = self.edge_feat_name
        _ = self.edge_feat_mp_op
        _ = self.decoder_edge_feat

        # Evaluation
        _ = self.fixed_test_size
        _ = self.eval_fanout
        _ = self.use_mini_batch_infer
        _ = self.eval_batch_size
        _ = self.eval_frequency
        _ = self.no_validation
        _ = self.save_prediction_path
        _ = self.eval_etype
        if self.task_type is not None:
            _ = self.eval_metric

        # Model training.
        if is_train:
            _ = self.batch_size
            _ = self.fanout
            _ = self.lm_train_nodes
            _ = self.lm_tune_lr
            _ = self.lr
            _ = self.max_grad_norm
            _ = self.grad_norm_type
            _ = self.gnn_norm
            _ = self.decoder_norm
            _ = self.sparse_optimizer_lr
            _ = self.num_epochs
            _ = self.save_model_path
            _ = self.save_model_frequency
            _ = self.topk_model_to_save
            _ = self.early_stop_burnin_rounds
            _ = self.early_stop_rounds
            _ = self.early_stop_strategy
            _ = self.use_early_stop
            _ = self.wd_l2norm
            _ = self.train_negative_sampler
            _ = self.train_etype
            _ = self.remove_target_edge_type

        # LM module
        if self.node_lm_configs:
            _ = self.lm_infer_batch_size
            _ = self.freeze_lm_encoder_epochs

        if self.distill_lm_configs:
            _ = self.textual_data_path

        # I/O related
        _ = self.restore_model_layers
        _ = self.restore_model_path
        _ = self.restore_optimizer_path
        _ = self.save_embed_path
        _ = self.save_embed_format

        # Model architecture
        _ = self.dropout
        _ = self.decoder_type
        _ = self.num_decoder_basis
        _ = self.decoder_bias
        # Encoder related
        _ = self.construct_feat_ntype
        _ = self.construct_feat_encoder
        _ = self.construct_feat_fanout
        encoder_type = self.model_encoder_type
        if encoder_type == "lm":
            assert self.node_lm_configs is not None
        else:
            _ = self.input_activate
            _ = self.hidden_size
            _ = self.num_layers
            _ = self.out_emb_size
            _ = self.use_self_loop
            _ = self.use_node_embeddings
            _ = self.num_bases
            _ = self.num_heads
            _ = self.num_ffn_layers_in_gnn

        _ = self.return_proba
        _ = self.alpha_l2norm


        # ngnn
        _ = self.num_ffn_layers_in_input
        _ = self.num_ffn_layers_in_decoder

        # Logging.
        _ = self.task_tracker
        _ = self.log_report_frequency

        _ = self.task_type
        # For classification/regression tasks.
        if self.task_type in [BUILTIN_TASK_NODE_CLASSIFICATION, BUILTIN_TASK_EDGE_CLASSIFICATION]:
            _ = self.label_field
            _ = self.num_classes
            _ = self.multilabel
            _ = self.multilabel_weights
            _ = self.imbalance_class_weights
        if self.task_type in [BUILTIN_TASK_NODE_CLASSIFICATION, BUILTIN_TASK_NODE_REGRESSION]:
            _ = self.target_ntype
            _ = self.eval_target_ntype
        if self.task_type in [BUILTIN_TASK_EDGE_CLASSIFICATION, BUILTIN_TASK_EDGE_REGRESSION]:
            _ = self.target_etype
        if self.task_type in [BUILTIN_TASK_EDGE_CLASSIFICATION, BUILTIN_TASK_EDGE_REGRESSION,
                              BUILTIN_TASK_LINK_PREDICTION] and is_train:
            _ = self.exclude_training_targets
            _ = self.reverse_edge_types_map
        # For link prediction tasks.
        if self.task_type == BUILTIN_TASK_LINK_PREDICTION:
            _ = self.gamma
            _ = self.lp_decoder_type
            _ = self.lp_edge_weight_for_loss
            _ = self.contrastive_loss_temperature
            _ = self.lp_loss_func
            _ = self.num_negative_edges
            _ = self.eval_negative_sampler
            _ = self.num_negative_edges_eval
            _ = self.model_select_etype
            _ = self.lp_embed_normalizer

        # For inference tasks in particular
        if self.task_type in [
            BUILTIN_TASK_NODE_CLASSIFICATION,
            BUILTIN_TASK_NODE_REGRESSION,
        ]:
            _ = self.infer_all_target_nodes

    def _turn_off_gradient_checkpoint(self, reason):
        """Turn off `gradient_checkpoint` flags in `node_lm_configs`
        """
        for i, _ in enumerate(self.node_lm_configs):
            if self.node_lm_configs[i]["gradient_checkpoint"]:
                logging.warning("%s can not work with gradient checkpoint. " \
                        + "Turn gradient checkpoint to False", reason)
                self.node_lm_configs[i]["gradient_checkpoint"] = False

    def handle_argument_conflicts(self):
        """Check and resolve argument conflicts
        """
        # 1. language model conflicts
        if self.node_lm_configs is not None:
            # gradient checkpoint does not work with freeze_lm_encoder_epochs
            # When freeze_lm_encoder_epochs is set, turn off gradient checkpoint
            if self.freeze_lm_encoder_epochs > 0:
                self._turn_off_gradient_checkpoint("freeze_lm_encoder_epochs")
            # GLEM fine-tuning of LM conflicts with gradient checkpoint
            if self.training_method["name"] == "glem":
                self._turn_off_gradient_checkpoint("GLEM model")
        # TODO(xiangsx): Add more check

###################### Configurations ######################

    @property
    def save_perf_results_path(self):
        """ Path for saving performance results. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_save_perf_results_path"):
            return self._save_perf_results_path
        return None

    @property
    def profile_path(self):
        """ The path of the folder where the profiling results are saved. Default is None.
        """
        if hasattr(self, "_profile_path"):
            return self._profile_path
        return None

    @property
    def graph_name(self):
        """ Name of the graph, loaded from the ``--part-config`` argument.
        """
        return get_graph_name(self.part_config)

    @property
    def backend(self):
        """ Distributed training backend. GraphStorm support ``gloo`` or ``nccl``.
            Default is ``gloo``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_backend"):
            assert self._backend in SUPPORTED_BACKEND, \
                f"backend must be in {SUPPORTED_BACKEND}"
            return self._backend

        return "gloo"

    @property
    def ip_config(self):
        """ IP config file that contains all IP addresses of instances in a cluster.
            In the file, each line stores one IP address. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_ip_config"):
            assert os.path.isfile(self._ip_config), \
                    f"IP config file {self._ip_config} does not exist"
            return self._ip_config
        else:
            return None

    @property
    def part_config(self):
        """ Path to the graph partition configuration file. Must provide.
        """
        # pylint: disable=no-member
        assert hasattr(self, "_part_config"), "Graph partition config must be provided"
        assert os.path.isfile(self._part_config), \
            f"Partition config file {self._part_config} does not exist"
        return self._part_config

    @property
    def node_id_mapping_file(self):
        """ A path to the folder that stores node ID mapping files generated by the
            graph partition algorithm.
            Graph partition will shuffle node IDs and edge IDs according
            to the node partition assignment. We expect partition algorithms
            will save node ID mappings to map new node IDs to their original
            node IDs.
            GraphStorm assumes node ID mappings are stored as a single object
            along with the partition config file.
        """
        path = os.path.dirname(self.part_config)
        # See graphstorm.gconstruct.utils.partition_graph for more detials
        node_id_mapping_file = os.path.join(path, "node_mapping.pt")
        if os.path.isfile(node_id_mapping_file):
            return node_id_mapping_file

        # Check whether the id_mapping file is generated by
        # dgl tools/distpartitioning/convert_partition.py
        # See https://github.com/dmlc/dgl/blob/
        # eb43489397daf5506494d2cc5eaf7d7ff9dbefff/tools/distpartitioning/utils.py#L578-L583.
        part_dirs = [part_path for part_path in os.listdir(path) \
                     if part_path.startswith("part")]
        node_id_mapping_file = os.path.join(os.path.join(path, part_dirs[0]),
                                            "orig_nids.dgl")
        # if orig_nids.dgl exists, there means there are id mapping there.
        # Rank 0 need to load all the mapping files.
        return path if os.path.isfile(node_id_mapping_file) else None

    @property
    def edge_id_mapping_file(self):
        """ A path to the folder that stores edge ID mapping files generated by the
            graph partition algorithm.
            Graph partition will shuffle node IDs and edge IDs according
            to the node partition assignment. We expect partition algorithms
            will save edge ID mappings to map new edge IDs to their original
            edge IDds.
            GraphStorm assumes edge ID mappings are stored as a single object
            along with the partition config file.
        """
        path = os.path.dirname(self.part_config)
        # See graphstorm.gconstruct.utils.partition_graph for more detials
        edge_id_mapping_file = os.path.join(path, "edge_mapping.pt")
        if os.path.isfile(edge_id_mapping_file):
            return edge_id_mapping_file

        # Check whether the id_mapping file is generated by
        # dgl tools/distpartitioning/convert_partition.py
        # See https://github.com/dmlc/dgl/blob/
        # eb43489397daf5506494d2cc5eaf7d7ff9dbefff/tools/distpartitioning/utils.py#L578-L583.
        part_dirs = [part_path for part_path in os.listdir(path) \
                     if part_path.startswith("part")]
        edge_id_mapping_file = os.path.join(os.path.join(path, part_dirs[0]),
                                            "orig_eids.dgl")
        # if orig_eids.dgl exists, there means there are id mapping there.
        # Rank 0 need to load all the mapping files.
        return path if os.path.isfile(edge_id_mapping_file) \
            else None

    @property
    def verbose(self):
        """ Verbose for print out more running information. Default is False.
        """
        # pylint: disable=no-member
        if hasattr(self, "_verbose"):
            assert self._verbose in [True, False]
            return self._verbose

        return False

    @property
    def use_wholegraph_embed(self):
        """ Whether to use WholeGraph to store intermediate embeddings/tensors generated
            during training or inference, e.g., "cache_lm_emb", "sparse_emb", etc.
            Default is None.
        """
        if hasattr(self, "_use_wholegraph_embed"):
            assert self._use_wholegraph_embed in [True, False], \
                "Invalid value for _use_wholegraph_embed. Must be either True or False."
            return self._use_wholegraph_embed
        else:
            return None

    @property
    def use_graphbolt(self):
        """ Whether to use GraphBolt in-memory graph representation.
            See https://docs.dgl.ai/stochastic_training/ for details. Default is False.
        """
        if hasattr(self, "_use_graphbolt"):
            assert self._use_graphbolt in [True, False], \
                "Invalid value for _use_graphbolt. Must be either True or False."
            return self._use_graphbolt
        else:
            return False

    ###################### language model support #########################
    # Bert related
    @property
    def lm_tune_lr(self):
        """ Learning rate for fine-tuning language models.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lm_tune_lr"):
            lm_tune_lr = float(self._lm_tune_lr)
            assert lm_tune_lr > 0.0, "Bert tune learning rate must > 0.0"
            return lm_tune_lr

        return self.lr

    @property
    def lm_train_nodes(self):
        """ Number of nodes used in LM model fine-tuning. Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lm_train_nodes"):
            assert self._lm_train_nodes >= -1, \
                "Number of LM trainable nodes must larger or equal to -1." \
                "0 means no LM trainable nodes" \
                "-1 means all nodes are LM trainable nodes"
            return self._lm_train_nodes

        # By default, do not turn on co-training
        return 0

    @property
    def lm_infer_batch_size(self):
        """ Mini-batch size used to do LM model inference. Default is 32.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lm_infer_batch_size"):
            assert self._lm_infer_batch_size > 0, \
                "Batch size for LM model inference must larger than 0"
            return self._lm_infer_batch_size

        return 32

    @property
    def freeze_lm_encoder_epochs(self):
        """ Before fine-tuning LM models, how many epochs GraphStorm will take to
            warmup a GNN model. Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_freeze_lm_encoder_epochs"):
            assert self._freeze_lm_encoder_epochs >= 0, \
                "Number of warmup epochs must be larger than or equal to 0"

            assert self._freeze_lm_encoder_epochs == 0 or \
                self.model_encoder_type not in ["lm", "mlp"], \
                "Encoder type lm (language model) and mlp (encoder layer only) " \
                "do not work with language model warmup. It will cause torch " \
                "DDP error"
            return self._freeze_lm_encoder_epochs

        return 0

    @property
    def training_method(self):
        """ Setting up the LM/GNN co-training method
        """
        if hasattr(self, "_training_method"):
            training_method_name = self._training_method["name"]
            assert training_method_name in ("default", "glem"),\
                f"Training method {training_method_name} is unavailable"
            if training_method_name == "glem":
                glem_defaults = {
                    "em_order_gnn_first": False,
                    "inference_using_gnn": True,
                    "pl_weight": 0.5,
                    "num_pretrain_epochs": 5
                }
                for key, val in glem_defaults.items():
                    self._training_method["kwargs"].setdefault(key, val)
                if self.freeze_lm_encoder_epochs > 0:
                    logging.warning("GLEM does not support 'freeze_lm_encoder_epochs'"\
                                    "it will be ignored")
            return self._training_method
        return {"name": "default", "kwargs": {}}

    def _check_node_lm_config(self, lm_config):
        assert "lm_type" in lm_config, "lm_type (type of language model," \
            "e.g., bert) must be provided for node_lm_models."
        assert "model_name" in lm_config, "language model model_name must " \
            "be provided for node_lm_models."
        if "gradient_checkpoint" not in lm_config:
            lm_config["gradient_checkpoint"] = False
        assert "node_types" in lm_config, "node types must be provided for " \
            "node_lm_models"
        assert len(lm_config["node_types"]) >= 1, "number of node types " \
            "must be larger than 1"

    @property
    def node_lm_configs(self):
        """ check node lm config
        """
        if hasattr(self, "_node_lm_configs"):
            if self._node_lm_configs is None:
                return None

            # node lm_config is not None
            assert isinstance(self._node_lm_configs, list), \
                "Node language model config is not None. It must be a list"
            assert len(self._node_lm_configs) > 0, \
                "Number of node language model config must larger than 0"

            for lm_config in self._node_lm_configs:
                self._check_node_lm_config(lm_config)

            return self._node_lm_configs

        # By default there is no node_lm_config
        return None

    def _check_distill_lm_config(self, lm_config):
        assert "lm_type" in lm_config, "lm_type (type of language model," \
            "e.g., DistilBertModel) must be provided for distill_lm_models."
        assert "model_name" in lm_config, "pre-trained model_name must " \
            "be provided for distill_lm_models."

    @property
    def distill_lm_configs(self):
        """ check distill lm config
        """
        if hasattr(self, "_distill_lm_configs"):
            assert self._distill_lm_configs is not None, \
                "distill_lm_configs cannot be None."
            # distill lm_config is not None
            assert isinstance(self._distill_lm_configs, list), \
                "Distill language model config is not None. It must be a list"
            assert len(self._distill_lm_configs) > 0, \
                "Number of distill language model config must larger than 0"

            for lm_config in self._distill_lm_configs:
                self._check_distill_lm_config(lm_config)

            return self._distill_lm_configs

        # By default there is no distill_lm_config
        return None

    @property
    def cache_lm_embed(self):
        """ Whether to cache the LM embeddings on files.
        """
        if hasattr(self, "_cache_lm_embed"):
            return self._cache_lm_embed
        else:
            return None

    ###################### general gnn model related ######################
    @property
    def model_encoder_type(self):
        """ The encoder module used to encode graph data. It can be a GNN encoder or
            a non-GNN encoder, e.g., language models and MLPs. Default is None.
        """
        # pylint: disable=no-member
        if self.distill_lm_configs is None:
            assert hasattr(self, "_model_encoder_type"), \
                "Model encoder type should be provided"
            assert self._model_encoder_type in BUILTIN_ENCODER, \
                f"Model encoder type should be in {BUILTIN_ENCODER}"
            return self._model_encoder_type
        else:
            return None

    @property
    def max_grad_norm(self):
        """ Maximum gradient clip which limits the magnitude of gradients during training in
            order to prevent issues like exploding gradients, and to improve the stability and
            convergence of the training process. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_max_grad_norm"):
            max_grad_norm = float(self._max_grad_norm)
            assert max_grad_norm > 0
            return self._max_grad_norm
        return None

    @property
    def grad_norm_type(self):
        """ Value of the type of norm that is used to compute the gradient norm. Default is 2.
        """
        # pylint: disable=no-member
        if hasattr(self, "_grad_norm_type"):
            grad_norm_type = self._grad_norm_type
            assert grad_norm_type > 0 or grad_norm_type == 'inf'
            return self._grad_norm_type
        return 2

    @property
    def input_activate(self):
        """ Input layer activation funtion type. Either None or ``relu``. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_input_activate"):
            if self._input_activate == "none":
                return None
            elif self._input_activate == "relu":
                return F.relu
            else:
                raise RuntimeError("Only support input activate flag 'none' for None "
                                   "and 'relu' for torch.nn.functional.relu")
        return None

    @property
    def edge_feat_name(self):
        """ User provided edge feature names. Default is None.

        .. versionchanged:: 0.4.0
            The ``edge_feat_name`` property is supported.

        It can be in the following formats:

        - ``feat_name``: global feature name for all edge types, i.e., for any edge, its
          corresponding feature name is <feat_name>.
        - ``"etype0:feat0","etype1:feat0,feat1",...``: different edge types have
          different edge features under different names. The edge type should be in a
          canonical edge type, i.e., `src_node_type,relation_type,dst_node_type`.

        This method parses given edge feature name list, and return either a string
        corresponding a global feature name, or a dictionary corresponding different
        edge types with diffent feature names.
        """
        # pylint: disable=no-member
        if hasattr(self, "_edge_feat_name"):
            feat_names = self._edge_feat_name
            if len(feat_names) == 1 and \
                ":" not in feat_names[0]:
                # global feat_name
                return feat_names[0]

            # per edge type feature
            fname_dict = {}

            for feat_name in feat_names:
                feat_info = feat_name.split(":")
                assert len(feat_info) == 2, \
                        f"Unknown format of the feature name: {feat_name}, " + \
                        "must be: etype:feat_name."
                # check and convert canonical edge type string
                assert isinstance(feat_info[0], str), \
                    f"The edge type should be a string, but got {feat_info[0]}"
                can_etype = tuple(item.strip() for item in feat_info[0].split(","))
                assert len(can_etype) == 3, \
                        f"Unknown format of the edge type {feat_info[0]}, must be: " + \
                         "src_node_type,relation_type,dst_node_type."
                assert can_etype not in fname_dict, \
                        f"You already specify the feature names of {can_etype} " \
                        f"as {fname_dict[can_etype]}."
                assert isinstance(feat_info[1], str), \
                    f"Feature name of {can_etype} should be a string, but got {feat_info[1]} " + \
                    f"with type {type(feat_info[1])}."
                # multiple features separated by ','
                fname_dict[can_etype] = [item.strip() for item in feat_info[1].split(",")]
            return fname_dict

        # By default, return None which means there is no node feature
        return None

    @property
    def edge_feat_mp_op(self):
        """ The operation for using edge features during message passing computation.
            Defaut is "concat".

        .. versionadded:: 0.4.0
            The ``edge_feat_mp_op`` argument.

            GraphStorm supports five message passing operations for edge features, including:

            - "concat":concatinate the source node feature with the edge feauture together,
              and then pass them to the destination node.
            - "add":add the source node feature with the edge feauture together,
              and then pass them to the destination node.
            - "sub":substract the edge feauture from the source node feature,
              and then pass them to the destination node.
            - "mul":multiple the source node feature with the edge feauture,
              and then pass them to the destination node.
            - "div":divid the source node feature by the edge feauture together,
              and then pass them to the destination node.

        """
        # pylint: disable=no-member
        if not hasattr(self, "_edge_feat_mp_op"):
            return "concat"
        assert self._edge_feat_mp_op in BUILTIN_EDGE_FEAT_MP_OPS, \
            "The edge feature message passing operation must be one of " + \
            f"{BUILTIN_EDGE_FEAT_MP_OPS}, but got {self._edge_feat_mp_op}."
        return self._edge_feat_mp_op

    @property
    def node_feat_name(self):
        """ User provided node feature name. Default is None.

        The input can be in the following formats:

        - ``feat_name``: global feature name for all node types, i.e., for any node, its
          corresponding feature name is <feat_name>. For example,
          if ``node_feat_name`` is set to ``feat``, GraphStorm will
          assume every node has a ``feat`` feature.
        - ``"ntype0:feat0","ntype1:feat0,feat1",...``: different node types have different
          node features with different names. For example if ``node_feat_name``
          is set to ``["user:age","movie:title,genre"]``.
          The ``user` nodes will take ``age`` as their features.
          The ``movie`` nodes will take both ``title`` and
          ``genre`` as their features.
          By default, for nodes of the same type, their features are
          first concatenated into a unified tensor, which is then
          transformed through an MLP layer.

        .. versionchanged:: 0.5.0

            Since 0.5.0, GraphStorm supports using different MLPs,
            to encode different input node features of the same node.
            For example, suppose the ``moive`` nodes have two features
            ``title`` and ``genre``, GraphStorm can encode ``title``
            feature with the encoder f(x) and encode ``genre`` feature
            with the encoder g(x).

            To use different MLPs for different features of one
            node type, users can take the following format for ``node_feat_name``:
            ``"ntype0:feat0","ntype1:feat0","ntype1:feat1",...``.
            GraphStorm will create an MLP encoder for ``feat0`` of ``ntype1``
            and another MLP encoder for ``feat1`` of ``ntype1``.

            The return value can be:

              - None
              - A string
              - A dict of list of strings
              - A dict of list of FeatureGroup
        """
        # pylint: disable=no-member
        if hasattr(self, "_node_feat_name"):
            feat_names = self._node_feat_name
            if len(feat_names) == 1 and \
                ":" not in feat_names[0]:
                # global feat_name
                return feat_names[0]

            # per node type feature
            fname_dict = {}

            for feat_name in feat_names:
                feat_info = feat_name.split(":")
                assert len(feat_info) == 2, \
                        f"Unknown format of the feature name: {feat_name}, " + \
                        "must be NODE_TYPE:FEAT_NAME."
                ntype = feat_info[0]
                assert isinstance(feat_info[1], str), \
                    f"Feature name of {ntype} should be a string not {feat_info[1]}"
                # multiple features separated by ','
                feats = [item.strip() for item in feat_info[1].split(",")]
                if ntype in fname_dict:
                    # One node type may have multiple
                    # feature groups.
                    # Each group will be stored as a
                    # list of strings.
                    if isinstance(fname_dict[ntype][0], str):
                        # The second feature group
                        fname_dict[ntype] = [FeatureGroup(
                            feature_group=fname_dict[ntype])]

                    fname_dict[ntype].append(FeatureGroup(
                        feature_group=feats
                    ))
                    logging.debug("%s nodes has %d feature groups",
                                 ntype, len(fname_dict[ntype]))
                else:
                    # Note(xiang): for backward compatibility,
                    # we do not change the data format
                    # of fname_dict when ntype has
                    # only one feature group.
                    fname_dict[ntype] = feats
                    logging.debug("%s nodes has %s features",
                                ntype, fname_dict[ntype])
            return fname_dict

        # By default, return None which means there is no node feature
        return None

    def _check_fanout(self, fanout, fot_name):
        try:
            if fanout[0].isnumeric() or fanout[0] == "-1":
                # Fanout in format of 20,10,5,...
                fanout = [int(val) for val in fanout]
            else:
                # Fanout in format of
                # etype2:20@etype3:20@etype1:20,etype2:10@etype3:4@etype1:2
                # Each etype should be a canonical etype in format of
                # srcntype/relation/dstntype

                fanout = [{tuple(k.split(":")[0].split('/')): int(k.split(":")[1]) \
                    for k in val.split("@")} for val in fanout]
        except Exception: # pylint: disable=broad-except
            assert False, f"{fot_name} Fanout should either in format 20,10 " \
                "when all edge type have the same fanout or " \
                "etype2:20@etype3:20@etype1:20," \
                "etype2:10@etype3:4@etype1:2 when you want to " \
                "specify a different fanout for different edge types" \
                "Each etype (e.g., etype2) should be a canonical etype in format of" \
                "srcntype/relation/dstntype"

        assert len(fanout) == self.num_layers, \
            f"You have a {self.num_layers} layer GNN, " \
            f"but you only specify a {fot_name} fanout for {len(fanout)} layers."
        return fanout

    @property
    def fanout(self):
        """ The fanouts of GNN layers. The values of fanouts must be integers larger
            than 0. The number of fanouts must equal to ``num_layers``. Must provide.

            It accepts two formats:

            - ``20,10``, which defines the number of neighbors
            to sample per edge type for each GNN layer with the i_th element being the
            fanout for the ith GNN layer.

            - "etype2:20@etype3:20@etype1:10,etype2:10@etype3:4@etype1:2", which defines
            the numbers of neighbors to sample for different edge types for each GNN layers
            with the i_th element being the fanout for the i_th GNN layer.
        """
        # pylint: disable=no-member
        if self.model_encoder_type in BUILTIN_GNN_ENCODER:
            assert hasattr(self, "_fanout"), \
                    "Training fanout must be provided"

            fanout = self._fanout.split(",")
            return self._check_fanout(fanout, "Train")
        return [-1] * self.num_layers

    @property
    def eval_fanout(self):
        """ The fanout of each GNN layers used in evaluation and inference. Default is same
            as the ``fanout``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_fanout"):
            fanout = self._eval_fanout.split(",")
            return self._check_fanout(fanout, "Evaluation")
        else:
            # By default use -1 as full neighbor
            return [-1] * self.num_layers

    @property
    def fixed_test_size(self):
        """ The number of validation and test data used during link prediction training
            and evaluation. This is useful for reducing the overhead of doing link prediction
            evaluation when the graph size is large. Default is None.
        """
        # TODO: support fixed_test_size in node prediction and edge prediction tasks.
        # pylint: disable=no-member
        if hasattr(self, "_fixed_test_size"):
            assert self._fixed_test_size > 0, \
                "fixed_test_size must be larger than 0"
            return self._fixed_test_size

        # Use the full test set
        return None


    @property
    def textual_data_path(self):
        """ The path to load the textual data for distillation. User need to specify
            a path of directory with two sub-directory for ``train`` and ``val`` split.
            Default is None.
        """
        if hasattr(self, "_textual_data_path"):
            return self._textual_data_path
        return None

    @property
    def max_distill_step(self):
        """ The maximum training steps for each node type for distillation. Default is 10000.
        """
        # only needed by distillation
        if hasattr(self, "_max_distill_step"):
            assert self._max_distill_steps > 0, \
                "Maximum training steps should be greater than 0."
            return self._max_distill_step
        else:
            # default max training steps
            return 10000

    @property
    def max_seq_len(self):
        """ The maximum sequence length of tokenized textual data for distillation.
            Default is 1024.
        """
        # only needed by distillation
        if hasattr(self, "_max_seq_len"):
            assert self._max_seq_len > 0, \
                "Maximum sequence length for distillation should be greater than 0."
            return self._max_seq_len
        else:
            # default maximum sequence length
            return 1024

    @property
    def hidden_size(self):
        """ The dimension of hidden GNN layers. Must be an integer larger than 0.
            Default is None.
        """
        # pylint: disable=no-member
        if self.distill_lm_configs is None:
            assert hasattr(self, "_hidden_size"), \
                "hidden_size must be provided when pretrain a embedding layer, " \
                "or train a GNN model"
            assert isinstance(self._hidden_size, int), \
                "Hidden embedding size must be an integer"
            assert self._hidden_size > 0, \
                "Hidden embedding size must be larger than 0"
            return self._hidden_size
        else:
            return None

    @property
    def num_layers(self):
        """ Number of GNN layers. Must be an integer larger than 0 if given.
            Default is 0, which means no GNN layers.
        """
        # pylint: disable=no-member
        if self.model_encoder_type in BUILTIN_GNN_ENCODER:
            assert hasattr(self, "_num_layers"), \
                "Number of GNN layers must be provided"
            assert isinstance(self._num_layers, int), \
                "Number of GNN layers must be an integer"
            assert self._num_layers > 0, \
                "Number of GNN layers must be larger than 0"
            return self._num_layers
        else:
            # not used by non-GNN models
            return 0

    @property
    def out_emb_size(self):
        """ The dimension of embeddings output from the last GNN layer. It will be ignored when
            num_layers <= 1. Must be an integer larger than 0.
            Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_out_emb_size"):
            if self._num_layers <= 1:
                logging.warning("The out_emb_size is ignored given num_layers <= 1.")
                return None
            assert isinstance(self._out_emb_size, int), \
                "Output embedding size must be an integer."
            assert self._out_emb_size > 0, \
                "Output embedding size must be larger than 0."
            return self._out_emb_size
        else:
            return None

    @property
    def use_mini_batch_infer(self):
        """ Whether to do mini-batch inference or full graph inference. Default is
            False for link prediction, and True for other tasks.
        """
        # pylint: disable=no-member
        if hasattr(self, "_use_mini_batch_infer"):
            assert self._use_mini_batch_infer in [True, False], \
                "Use mini batch inference flag must be True or False"
            return self._use_mini_batch_infer

        if self.task_type in [BUILTIN_TASK_LINK_PREDICTION]:
            # For Link prediction inference, using mini-batch
            # inference is much less efficient than full-graph
            # inference in most cases.
            # So we set it to False by default
            return False
        else:
            # By default, for node classification/regression and
            # edge classification/regression tasks,
            # using mini batch inference reduces memory cost
            # So we set it to True by default
            return True

    @property
    def gnn_norm(self):
        """ Normalization method for GNN layers. Options include ``batch`` or ``layer``.
            Default is None.
        """
        # pylint: disable=no-member
        if not hasattr(self, "_gnn_norm"):
            return None
        assert self._gnn_norm in BUILTIN_GNN_NORM, \
            "Normalization type must be one of batch or layer"

        return self._gnn_norm

    ###################### I/O related ######################
    ### Restore model ###
    @property
    def restore_model_layers(self):
        """ GraphStorm model layers to load. Currently, three neural network layers are supported,
            i.e., ``embed`` (input layer), ``gnn`` and ``decoder``. Default is to restore all three
            of these layers.
        """
        # pylint: disable=no-member
        model_layers = GRAPHSTORM_MODEL_ALL_LAYERS
        if hasattr(self, "_restore_model_layers"):
            assert self.restore_model_path is not None, \
                "restore-model-path must be provided if restore-model-layers is specified."
            model_layers = self._restore_model_layers.split(',')
            for layer in model_layers:
                assert layer in GRAPHSTORM_MODEL_LAYER_OPTIONS, \
                    f"{layer} is not supported, must be any of {GRAPHSTORM_MODEL_LAYER_OPTIONS}"
        # GLEM restore layers to the LM component, thus conflicting with all layers:
        # use [GRAPHSTORM_MODEL_EMBED_LAYER, GRAPHSTORM_MODEL_DECODER_LAYER] to restore an LM
        # checkpoint with decoder trained for node classification.
        # For example, the check point is saved from a GLEM model.
        # use [GRAPHSTORM_MODEL_EMBED_LAYER] if the checkpoint doesn't contain such decoder for LM.
        if self.training_method["name"] == "glem":
            if model_layers == GRAPHSTORM_MODEL_ALL_LAYERS:
                logging.warning("Restoring GLEM's LM from checkpoint only support %s and %s.'\
                                'Setting to: '%s'",
                                [GRAPHSTORM_MODEL_EMBED_LAYER],
                                [GRAPHSTORM_MODEL_EMBED_LAYER, GRAPHSTORM_MODEL_DECODER_LAYER],
                                GRAPHSTORM_MODEL_EMBED_LAYER
                                )
                model_layers = [GRAPHSTORM_MODEL_EMBED_LAYER]
        return model_layers

    @property
    def restore_model_path(self):
        """ A path where GraphStorm model parameters are saved. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_restore_model_path"):
            return self._restore_model_path
        return None

    @property
    def restore_optimizer_path(self):
        """ A path storing optimizer status corresponding to GraphML model parameters.
            Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_restore_optimizer_path"):
            return self._restore_optimizer_path
        return None

    ### Save model ###
    @property
    def save_embed_path(self):
        """ Path to save the generated node embeddings. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_save_embed_path"):
            return self._save_embed_path
        return None

    @property
    def save_embed_format(self):
        """ Specify the format of saved embeddings.
        """
        # pylint: disable=no-member
        if hasattr(self, "_save_embed_format"):
            assert self._save_embed_format in ["pytorch", "hdf5"], \
                f"{self._save_embed_format} is not supported for save_embed_format." \
                f"Supported format ['pytorch', 'hdf5']."
            return self._save_embed_format
        # default to be 'pytorch'
        return "pytorch"

    @property
    def save_model_path(self):
        """ A path to save GraphStorm model parameters and the corresponding optimizer status.
            Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_save_model_path"):
            return self._save_model_path
        return None

    @property
    def save_model_frequency(self):
        """ The Number of iterations to save model once. By default, GraphStorm will save
            models at the end of each epoch if ``save_model_path`` is provided. Default is
            -1, which means only save at the end of each epoch.
        """
        # pylint: disable=no-member
        if hasattr(self, "_save_model_frequency"):
            assert self.save_model_path is not None, \
                'To save models, please specify a valid path. But got None'
            assert self._save_model_frequency > 0, \
                f'save-model-frequency must large than 0, but got {self._save_model_frequency}'
            return self._save_model_frequency
        # By default, use -1, means do not auto save models
        return -1

    @property
    def topk_model_to_save(self):
        """ The number of top best validation performance GraphStorm model to save.

            If ``topk_model_to_save`` is set and ``save_model_frequency`` is not set,
            GraphStorm will try to save models after each epoch and keep at most ``K`` models.
            If ``save_model_frequency`` is set, GraphStorm will try to save models every number
            of ``save_model_frequency`` iteration and keep at most ``K`` models.
        """
        # pylint: disable=no-member
        if hasattr(self, "_topk_model_to_save"):
            assert self._topk_model_to_save > 0, "Top K best model must > 0"
            assert self.save_model_path is not None, \
                'To save models, please specify a valid path. But got None'

            return self._topk_model_to_save
        else:
            # By default saving all models
            return math.inf

    #### Task tracker and print options ####
    @property
    def task_tracker(self):
        """ A task tracker used to formalize and report model performance metrics.

            The supported task trackers includes SageMaker (sagemaker_task_tracker) and
            TensorBoard (tensorboard_task_tracker). The user can specify it in the
            yaml configuration as following:

            .. code:: json

                basic:
                    task_tracker: "tensorboard_task_tracker"

            The default is ``sagemaker_task_tracker``, which will log the metrics using
            Python logging facility.

            For TensorBoard tracker, users can specify a file directory to store the
            logs by providing the file path information in a format of
            ``tensorboard_task_tracker:FILE_PATH``. The tensorboard logs will be stored
            under ``FILE_PATH``.

            .. versionchanged:: 0.4.1
                Add support for tensorboard tracker.
        """
        # pylint: disable=no-member
        if hasattr(self, "_task_tracker"):
            tracker_info = self._task_tracker.split(":")
            task_tracker_name = tracker_info[0]

            assert task_tracker_name in SUPPORTED_TASK_TRACKER, \
                f"Task tracker must be one of {SUPPORTED_TASK_TRACKER}," \
                f"But got {task_tracker_name}"
            return task_tracker_name

        # By default, use SageMaker task tracker
        # It works as normal print
        return GRAPHSTORM_SAGEMAKER_TASK_TRACKER

    @property
    def task_tracker_logpath(self):
        """ A path for a task tracker to store the logs.

            SageMaker trackers will ignore this property.

            For TensorBoard tracker, users can specify a file directory
            to store the logs by providing the file path information in
            a format of ``tensorboard_task_tracker:FILE_PATH``. The
            task_tracker_logpath will be set to ``FILE_PATH``.

            Default: None

            .. versionadded:: 0.4.1
        """
        # pylint: disable=no-member
        if hasattr(self, "_task_tracker"):
            tracker_info = self._task_tracker.split(":")
            # task_tracker information in the format of
            # tensorboard_task_tracker:FILE_PATH
            if len(tracker_info) > 1:
                return tracker_info[1]
            else:
                return None
        return None

    @property
    def log_report_frequency(self):
        """ Get print/log frequency in number of iterations
        """
        # pylint: disable=no-member
        if hasattr(self, "_log_report_frequency"):
            assert self._log_report_frequency > 0, \
                "log_report_frequency should be larger than 0"
            return self._log_report_frequency

        # By default, use 1000
        return 1000

    ###################### Model training related ######################
    @property
    def decoder_bias(self):
        """ Decoder bias. decoder_bias must be a boolean. Default is True.
        """
        # pylint: disable=no-member
        if hasattr(self, "_decoder_bias"):
            assert self._decoder_bias in [True, False], \
                "decoder_bias should be in [True, False]"
            return self._decoder_bias
        # By default, decoder bias is True
        return True

    @property
    def dropout(self):
        """ Dropout probability. Dropout must be a float value in [0,1). Dropout is applied
            to every GNN layer. Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_dropout"):
            assert self._dropout >= 0.0 and self._dropout < 1.0
            return self._dropout
        # By default, there is no dropout
        return 0.0

    @property
    # pylint: disable=invalid-name
    def lr(self):
        """ Learning rate for dense parameters of input encoders, model encoders,
            and decoders. Must provide.
        """
        assert hasattr(self, "_lr"), "Learning rate must be specified"
        lr = float(self._lr) # pylint: disable=no-member
        assert lr > 0.0, \
            "Learning rate for Input encoder, GNN encoder " \
            "and task decoder must be larger than 0.0"

        return lr

    @property
    def num_epochs(self):
        """ Number of training epochs. Must be integer and larger than 0 if given.
            Default is 0.
        """
        if hasattr(self, "_num_epochs"):
            # if 0, only inference or testing
            assert self._num_epochs >= 0, "Number of epochs must >= 0"
            return self._num_epochs
        # default, inference only
        return 0

    @property
    def batch_size(self):
        """ Mini-batch size. It defines the batch size of each trainer. The global batch
            size equals to the number of trainers multiply the batch_size. For example,
            suppose we have 2 machines each of which has 8 GPUs, and set batch_size to 128.
            The global batch size will be 2 * 8 * 128 = 2048. Must provide.
        """
        # pylint: disable=no-member
        assert hasattr(self, "_batch_size"), "Batch size must be specified"
        assert self._batch_size > 0
        return self._batch_size

    @property
    def sparse_optimizer_lr(self): # pylint: disable=invalid-name
        """ Learning rate for the optimizer corresponding to learnable sparse embeddings.
            Default is same as ``lr``.
        """
        if hasattr(self, "_sparse_optimizer_lr"):
            sparse_optimizer_lr = float(self._sparse_optimizer_lr)
            assert sparse_optimizer_lr > 0.0, \
                "Sparse optimizer learning rate must be larger than 0"
            return sparse_optimizer_lr

        return self.lr

    @property
    def use_node_embeddings(self):
        """ Whether to create extra learnable embeddings for nodes.
            These learnable embeddings will be concatenated with nodes' own features
            to form the inputs for model training. Default is False.
        """
        # pylint: disable=no-member
        if hasattr(self, "_use_node_embeddings"):
            assert self._use_node_embeddings in [True, False]
            return self._use_node_embeddings
        # By default do not use extra node embedding
        # It will make the model transductive
        return False

    @property
    def construct_feat_ntype(self):
        """ The node types that require to reconstruct node features during node feature
            reconstruction learning. Default is an empty list.
        """
        if hasattr(self, "_construct_feat_ntype") \
                and self._construct_feat_ntype is not None:
            return self._construct_feat_ntype
        else:
            return []

    @property
    def construct_feat_encoder(self):
        """ The encoder used to reconstruct node features during node feature
            reconstruction learning. Options include all built-in GNN encoders, i.e.,
            ``rgcn``, ``rgat``, and ``hgt``. Default is ``rgcn``.
        """
        if hasattr(self, "_construct_feat_encoder"):
            assert self._construct_feat_encoder == "rgcn", \
                    "Feature construction currently only support rgcn."
            return self._construct_feat_encoder
        else:
            return "rgcn"

    @property
    def construct_feat_fanout(self):
        """ The fanout used to reconstruct node features during node feature
            reconstruction learning. Default is 5.
        """
        if hasattr(self, "_construct_feat_fanout"):
            assert isinstance(self._construct_feat_fanout, int), \
                    "The fanout for feature construction should be integers."
            assert self._construct_feat_fanout > 0 or self._construct_feat_fanout == -1, \
                    "The fanout for feature construction should be positive or -1 " + \
                    "if we use all neighbors to construct node features."
            return self._construct_feat_fanout
        else:
            return 5

    @property
    def wd_l2norm(self):
        """ Weight decay used by ``torch.optim.Adam``. Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_wd_l2norm"):
            try:
                wd_l2norm = float(self._wd_l2norm)
            except:
                raise ValueError("wd_l2norm must be a floating point " \
                                 f"but get {self._wd_l2norm}")
            return wd_l2norm
        return 0

    @property
    def alpha_l2norm(self):
        """ Coefficiency of the l2 norm of dense parameters. GraphStorm adds a regularization loss,
            i.e., l2 norm of dense parameters, to the final loss. It uses alpha_l2norm to re-scale
            the regularization loss. Specifically, loss = loss + alpha_l2norm * regularization_loss.
            Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_alpha_l2norm"):
            try:
                alpha_l2norm = float(self._alpha_l2norm)
            except:
                raise ValueError("alpha_l2norm must be a floating point " \
                                 f"but get {self._alpha_l2norm}")
            return alpha_l2norm
        return .0

    @property
    def use_self_loop(self):
        """ Whether to include nodes' own feature as a special relation type. Detault is True.
        """
        # pylint: disable=no-member
        if hasattr(self, "_use_self_loop"):
            assert self._use_self_loop in [True, False]
            return self._use_self_loop
        # By default use self loop
        return True

    ### control evaluation ###
    @property
    def eval_batch_size(self):
        """ Mini-batch size for computing GNN embeddings in evaluation. Default is 10000.
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_batch_size"):
            assert self._eval_batch_size > 0
            return self._eval_batch_size
        # (Israt): Larger batch sizes significantly improve runtime efficiency. Increasing the
        # batch size from 1K to 10K reduces end-to-end inference time from 45 mins to 19 mins
        # in link prediction on OGBN-paers100M dataset with 16-dimensional length. However,
        # using an overly large batch size can lead to GPU out-of-memory (OOM) issues. Therefore,
        # a heuristic approach has been taken, and 10K has been chosen as a balanced default
        # value. More details can be found at https://github.com/awslabs/graphstorm/pull/66.
        return 10000

    @property
    def eval_frequency(self):
        """ The frequency of doing evaluation. GraphStorm trainers do evaluation at the end of
            each epoch. When ``eval_frequency`` is set, every ``eval_frequency`` iteration,
            trainers will do evaluation once. Default is only do evaluation at the end of each
            epoch.
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_frequency"):
            assert self._eval_frequency > 0, "eval_frequency should larger than 0"
            return self._eval_frequency
        # set max value (Never do evaluation with in an epoch)
        return sys.maxsize

    @property
    def no_validation(self):
        """ When set to true, will not perform evaluation (validation) during training.
            Default is False.
        """
        if hasattr(self, "_no_validation"):
            assert self._no_validation in [True, False]
            return self._no_validation

        # We do validation by default
        return False

    ### control early stop ###
    @property
    def early_stop_burnin_rounds(self):
        """ Burn-in rounds before starting to check for the early stop condition. Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_early_stop_burnin_rounds"):
            assert isinstance(self._early_stop_burnin_rounds, int), \
                "early_stop_burnin_rounds should be an integer"
            assert self._early_stop_burnin_rounds >= 0, \
                "early_stop_burnin_rounds should be larger than or equal to 0"
            return self._early_stop_burnin_rounds

        return 0

    @property
    def early_stop_rounds(self):
        """ The number of rounds for validation scores used to decide to stop training early.
            Default is 3.
        """
        # pylint: disable=no-member
        if hasattr(self, "_early_stop_rounds"):
            assert isinstance(self._early_stop_rounds, int), \
                "early_stop_rounds should be an integer"
            assert self._early_stop_rounds > 0, \
                "early_stop_rounds should be larger than 0"
            return self._early_stop_rounds

        # at least 3 iterations
        return 3

    @property
    def early_stop_strategy(self):
        """ The strategy used to decide if stop training early. GraphStorm supports two
            strategies: 1) ``consecutive_increase``, and 2) ``average_increase``.
            Default is ``average_increase``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_early_stop_strategy"):
            assert self._early_stop_strategy in \
                [EARLY_STOP_CONSECUTIVE_INCREASE_STRATEGY, \
                    EARLY_STOP_AVERAGE_INCREASE_STRATEGY], \
                "The supported early stop strategies are " \
                f"[{EARLY_STOP_CONSECUTIVE_INCREASE_STRATEGY}, " \
                f"{EARLY_STOP_AVERAGE_INCREASE_STRATEGY}]"
            return self._early_stop_strategy

        return EARLY_STOP_AVERAGE_INCREASE_STRATEGY

    @property
    def use_early_stop(self):
        """ Whether to use early stopping during training. Default is False.
        """
        # pylint: disable=no-member
        if hasattr(self, "_use_early_stop"):
            assert self._use_early_stop in [True, False], \
                "use_early_stop should be in [True, False]"
            return self._use_early_stop

        # By default do not enable early stop
        return False

    ## RGCN only ##
    @property
    def num_bases(self):
        """ Number of bases used in RGCN weights. Default is -1.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_bases"):
            assert isinstance(self._num_bases, int)
            assert self._num_bases > 0 or self._num_bases == -1, \
                "num_bases should be larger than 0 or -1"
            return self._num_bases
        # By default do not use num_bases
        return -1

    ## RGAT and HGT only ##
    @property
    def num_heads(self):
        """ Number of attention heads used in RGAT and HGT weights. Default is 4.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_heads"):
            assert self._num_heads > 0, \
                "num_heads should be larger than 0"
            return self._num_heads
        # By default use 4 heads
        return 4

    ############ task related #############
    ###classification/regression related ####
    @property
    def label_field(self):
        """ The field name of labels in a graph data. Must provide for classification
            and regression tasks.

            For node classification tasks, GraphStorm uses
            ``graph.nodes[target_ntype].data[label_field]`` to access node labels.
            For edge classification tasks, GraphStorm uses
            ``graph.edges[target_etype].data[label_field]`` to access edge labels.
        """
        # pylint: disable=no-member
        assert hasattr(self, "_label_field"), \
            "Must provide the feature name of labels through label_field"
        return self._label_field

    @property
    def use_pseudolabel(self):
        """ Whether use pseudolabeling for unlabeled nodes in semi-supervised training

            It only works with node-level tasks.
        """
        if hasattr(self, "_use_pseudolabel"):
            assert self._use_pseudolabel in (True, False)
            return self._use_pseudolabel
        return False

    @property
    def num_classes(self):
        """ The cardinality of labels in a classification task. Used by node classification
            and edge classification. Must provide for classification tasks.
        """
        # pylint: disable=no-member
        assert hasattr(self, "_num_classes"), \
            "Must provide the number possible labels through num_classes"
        if isinstance(self._num_classes, dict):
            for num_classes in self._num_classes.values():
                if num_classes == 1 and self.class_loss_func == BUILTIN_CLASS_LOSS_FOCAL:
                    if get_rank() == 0:
                        warnings.warn(f"Allowing num_classes=1 with {BUILTIN_CLASS_LOSS_FOCAL} "
                                    "loss is deprecated and will be removed "
                                    "in future versions.",
                                    DeprecationWarning)
                else:
                    assert num_classes > 1, \
                        "num_classes for classification tasks must be 2 or greater."
        else:
            if self._num_classes == 1 and self.class_loss_func == BUILTIN_CLASS_LOSS_FOCAL:
                if get_rank() == 0:
                    warnings.warn(f"Allowing num_classes=1 with {BUILTIN_CLASS_LOSS_FOCAL} "
                                "loss is deprecated and will be removed "
                                "in future versions.",
                                DeprecationWarning)
            else:
                assert self._num_classes > 1, \
                    "num_classes for classification tasks must be 2 or greater."
        return self._num_classes

    @property
    def multilabel(self):
        """ Whether the task is a multi-label classification task. Used by node
            classification and edge classification. Default is False.
        """

        def check_multilabel(multilabel):
            assert multilabel in [True, False]
            return multilabel

        if hasattr(self, "_num_classes") and isinstance(self.num_classes, dict):
            if hasattr(self, "_multilabel"):
                num_classes, multilabel = self.num_classes, self._multilabel
                assert isinstance(multilabel, dict)
                return {ntype: check_multilabel(multilabel[ntype]) for ntype in num_classes}
            return {ntype: False for ntype in self.num_classes}
        else:
            if hasattr(self, "_multilabel"):
                return check_multilabel(self._multilabel)
            return False

    @property
    def multilabel_weights(self):
        """Used to specify label weight of each class in a multi-label classification task.
            It is feed into ``th.nn.BCEWithLogitsLoss`` as ``pos_weight``.

            The weights should be in the following format 0.1,0.2,0.3,0.1,0.0, ...
            Default is None.
        """

        def check_multilabel_weights(multilabel, multilabel_weights, num_classes):
            assert multilabel is True, "Must be a multi-label classification task."
            try:
                weights = multilabel_weights.split(",")
                weights = [float(w) for w in weights]
            except Exception: # pylint: disable=broad-except
                raise RuntimeError("The weights should in following format 0.1,0.2,0.1,0.0")
            for w in weights:
                assert w >= 0., "multilabel weights can not be negative values"
            assert len(weights) == num_classes, \
                "Each class must have an assigned weight"
            return th.tensor(weights)

        if hasattr(self, "_num_classes") and isinstance(self.num_classes, dict):
            if hasattr(self, "_multilabel_weights"):
                multilabel = self.multilabel
                num_classes = self.num_classes
                multilabel_weights = self._multilabel_weights
                ntype_weights = {}
                for ntype in num_classes:
                    if ntype in multilabel_weights:
                        ntype_weights[ntype] = check_multilabel_weights(multilabel[ntype],
                                                                        multilabel_weights[ntype],
                                                                        num_classes[ntype])
                    else:
                        ntype_weights[ntype] = None
                return ntype_weights
            return {ntype: None for ntype in self.num_classes}
        else:
            if hasattr(self, "_multilabel_weights"):
                return check_multilabel_weights(self.multilabel,
                                                self._multilabel_weights,
                                                self.num_classes)

            return None

    @property
    def return_proba(self):
        """ Whether to return all the predictions or the maximum prediction in classification
            tasks. Set True to return predictions and False to return maximum prediction.
            Default is True.
        """
        if hasattr(self, "_return_proba"):
            assert self._return_proba in [True, False], \
                "Return all the predictions when True else return the maximum prediction."

            if self._return_proba is True and \
                self.task_type in [BUILTIN_TASK_NODE_REGRESSION, BUILTIN_TASK_EDGE_REGRESSION]:
                logging.warning("node regression and edge regression tasks "
                      "automatically ignore --return-proba flag. Regression "
                      "prediction results will be returned.")
            return self._return_proba
        # By default, return all the predictions
        return True

    @property
    def imbalance_class_weights(self):
        """ Used to specify a manual rescaling weight given to each class
            in a single-label multi-class classification task.
            It is used in imbalanced label use cases. It is feed into
            ``th.nn.CrossEntropyLoss``. Default is None.

            Customer should provide the weight in the following format: 0.1,0.2,0.3,0.1, ...
        """

        def check_imbalance_class_weights(imbalance_class_weights, num_classes):
            try:
                weights = imbalance_class_weights.split(",")
                weights = [float(w) for w in weights]
            except Exception: # pylint: disable=broad-except
                raise RuntimeError("The weights should in following format 0.1,0.2,0.3,0.1")
            for w in weights:
                assert w > 0., "Each weight should be larger than 0."
            assert len(weights) == num_classes, \
                "Each class must have an assigned weight"
            return th.tensor(weights)

        if hasattr(self, "_num_classes") and isinstance(self.num_classes, dict):
            if hasattr(self, "_imbalance_class_weights"):
                assert isinstance(self._imbalance_class_weights, dict), \
                    print('The imbalance_class_weights should be dictionary')
                num_classes = self.num_classes
                imbalance_class_weights = self._imbalance_class_weights
                ntype_weights = {}
                for ntype in num_classes:
                    if ntype in imbalance_class_weights:
                        ntype_weights[ntype] = check_imbalance_class_weights(
                            imbalance_class_weights[ntype],
                            num_classes[ntype]
                            )
                    else:
                        ntype_weights[ntype] = None
                return ntype_weights
            return {ntype: None for ntype in self.num_classes}
        else:
            if hasattr(self, "_imbalance_class_weights"):
                return check_imbalance_class_weights(self._imbalance_class_weights,
                                                     self.num_classes)
            return None

    ###classification/regression inference related ####
    @property
    def save_prediction_path(self):
        """ Path to save prediction results. This is used in classification or regression
            inference. Default is same as the ``save_embed_path``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_save_prediction_path"):
            return self._save_prediction_path

        # if save_prediction_path is not specified in inference
        # use save_embed_path
        return self.save_embed_path

    @property
    def infer_all_target_nodes(self):
        """ Whether to force inference to run on all nodes for types specified
        by target-ntypes, ignoring any mask. Default is False.
        """
        # pylint: disable=no-member
        if hasattr(self, "_infer_all_target_nodes"):
            assert self._infer_all_target_nodes in [True, False], \
                "infer_all_target_nodes should be in [True, False] (bool)"
            return self._infer_all_target_nodes

        # By default, do not force inference on all nodes/edges
        return False

    ### Node related task variables ###
    @property
    def target_ntype(self):
        """ The node type for prediction. By default, GraphStorm will assume the input graph
            is a homogeneous graph and set ``target_ntype`` to ``_N``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_target_ntype"):
            return self._target_ntype
        else:
            logging.warning("There is not target ntype provided, "
                            "will treat the input graph as a homogeneous graph")
            return DEFAULT_NTYPE

    @property
    def eval_target_ntype(self):
        """ The node type for evaluation prediction
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_target_ntype"):
            assert isinstance(self._eval_target_ntype, str), \
                "Now we only support single ntype evaluation"
            return self._eval_target_ntype
        else:
            if isinstance(self.target_ntype, str):
                return self.target_ntype
            elif isinstance(self.target_ntype, list):
                # (wlcong) Now only support single ntype evaluation
                logging.warning("Now only support single ntype evaluation")
                return self.target_ntype[0]
            else:
                return None

    #### edge related task variables ####
    @property
    def reverse_edge_types_map(self):
        """ A list of reverse edge type info. Default is an empty dictionary.

            Each information is in the following format:
            ``<head,relation,reverse relation,tail>``. For example:
            ``["query,adds,rev-adds,asin", "query,clicks,rev-clicks,asin"]``.
        """
        # link prediction or edge classification
        assert self.task_type in [BUILTIN_TASK_LINK_PREDICTION, \
            BUILTIN_TASK_EDGE_CLASSIFICATION, BUILTIN_TASK_EDGE_REGRESSION], \
            f"Only {BUILTIN_TASK_LINK_PREDICTION}, " \
            f"{BUILTIN_TASK_EDGE_CLASSIFICATION} and "\
            f"{BUILTIN_TASK_EDGE_REGRESSION} use reverse_edge_types_map"

        # pylint: disable=no-member
        if hasattr(self, "_reverse_edge_types_map"):
            if self._reverse_edge_types_map is None:
                return {} # empty dict
            assert isinstance(self._reverse_edge_types_map, list), \
                "Reverse edge type map should has following format: " \
                "[\"head,relation,reverse relation,tail\", " \
                "\"head,relation,reverse relation,tail\", ...]"

            reverse_edge_types_map = {}
            try:
                for etype_info in self._reverse_edge_types_map:
                    head, rel, rev_rel, tail = etype_info.split(",")
                    reverse_edge_types_map[(head, rel, tail)] = (tail, rev_rel, head)
            except Exception: # pylint: disable=broad-except
                assert False, \
                    "Reverse edge type map should has following format: " \
                    "[\"head,relation,reverse relation,tail\", " \
                    "\"head,relation,reverse relation,tail\", ...]" \
                    f"But get {self._reverse_edge_types_map}"

            return reverse_edge_types_map

        # By default return an empty dict
        return {}

    ### Edge classification and regression tasks ###
    @property
    def target_etype(self):
        """ The list of canonical etypes that will be added as training targets in edge
            classification and regression tasks.  If not provided, GraphStorm will assume
            the input graph is a homogeneous graph and set ``target_etype`` to
            ``('_N', '_E', '_N')``.
        """
        # TODO(xiangsx): Only support single task edge classification/regression.
        # Support multiple tasks when needed.
        # pylint: disable=no-member
        if not hasattr(self, "_target_etype"):
            logging.warning("There is not target etype provided, "
                            "will treat the input graph as a homogeneous graph")
            return [DEFAULT_ETYPE]
        assert isinstance(self._target_etype, list), \
            "target_etype must be a list in format: " \
            "[\"query,clicks,asin\", \"query,search,asin\"]."
        assert len(self._target_etype) > 0, \
            "There must be at least one target etype."
        if len(self._target_etype) != 1:
            logging.warning("only %s will be used." + \
                "Currently, GraphStorm only supports single task edge " + \
                "classification/regression. Please contact GraphStorm " + \
                "dev team to support multi-task.", str(self._target_etype[0]))

        return [tuple(target_etype.split(',')) for target_etype in self._target_etype]

    @property
    def remove_target_edge_type(self):
        """ Whether to remove the training target edge type for message passing.
            Default is True.

            If set to True, Graphstorm will set the fanout of training target edge
            type as zero. This is only used with edge classification.
            If the edge classification is to predict the existence of an edge between
            two nodes, GraphStorm should remove the target edge in the message passing to
            avoid information leak. If it's to predict some attributes associated with
            an edge, GraphStorm may not need to remove the target edge.
            Since it is unclear what to predict, to be safe, remove the target
            edge in message passing by default.
        """
        # pylint: disable=no-member
        if hasattr(self, "_remove_target_edge_type"):
            assert self._remove_target_edge_type in [True, False]
            return self._remove_target_edge_type

        # By default, remove training target etype during
        # message passing to avoid information leakage
        logging.warning("remove_target_edge_type is set to True by default. "
                        "If your edge classification task is not predicting "
                        "the existence of the target edge, we suggest you to "
                        "set it to False.")
        return True

    @property
    def decoder_type(self):
        """ The type of edge clasification or regression decoders. Built-in decoders include
            ``DenseBiDecoder`` and ``MLPDecoder``. Default is ``DenseBiDecoder``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_decoder_type"):
            return self._decoder_type

        # By default, use DenseBiDecoder
        return "DenseBiDecoder"

    @property
    def num_decoder_basis(self):
        """ The number of basis for the ``DenseBiDecoder`` decoder in edge prediction task.
            Default is 2.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_decoder_basis"):
            assert self._num_decoder_basis > 1, \
                "Decoder basis must be larger than 1"
            return self._num_decoder_basis

        # By default, return 2
        return 2

    @property
    def decoder_edge_feat(self):
        """ A list of edge features that can be used by a decoder to
            enhance its performance. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_decoder_edge_feat"):
            assert self.task_type in \
                (BUILTIN_TASK_EDGE_CLASSIFICATION, BUILTIN_TASK_EDGE_REGRESSION), \
                "Decoder edge feature only works with " \
                "edge classification or regression tasks"
            decoder_edge_feats = self._decoder_edge_feat
            assert len(decoder_edge_feats) == 1, \
                "We only support edge classifcation or regression on one edge type"

            if ":" not in decoder_edge_feats[0]:
                # global feat_name
                return decoder_edge_feats[0]

            # per edge type feature
            feat_name = decoder_edge_feats[0]
            feat_info = feat_name.split(":")
            assert len(feat_info) == 2, \
                    f"Unknown format of the feature name: {feat_name}, " + \
                    "must be EDGE_TYPE:FEAT_NAME"
            etype = tuple(feat_info[0].split(","))
            assert etype in self.target_etype, \
                f"{etype} must in the training edge type list {self.target_etype}"
            return {etype: feat_info[1].split(",")}

        return None


    ### Link Prediction specific ###
    @property
    def train_negative_sampler(self):
        """ The negative sampler used for link prediction training.
            Built-in samplers include ``uniform``, ``joint``, ``localuniform``,
            ``all_etype_uniform`` and ``all_etype_joint``. Default is ``uniform``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_train_negative_sampler"):
            return self._train_negative_sampler
        return BUILTIN_LP_UNIFORM_NEG_SAMPLER

    @property
    def eval_negative_sampler(self):
        """ The negative sampler used for link prediction training.
            Built-in samplers include ``uniform``, ``joint``, ``localuniform``,
            ``all_etype_uniform`` and ``all_etype_joint``. Default is ``joint``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_negative_sampler"):
            return self._eval_negative_sampler

        # use Joint neg for efficiency
        return BUILTIN_LP_JOINT_NEG_SAMPLER

    @property
    def num_negative_edges(self):
        """ Number of negative edges sampled for each positive edge during training.
            Default is 16.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_negative_edges"):
            assert self._num_negative_edges > 0, \
                "Number of negative edges must larger than 0"
            return self._num_negative_edges
        # Set default value to 16.
        return 16

    @property
    def num_negative_edges_eval(self):
        """ Number of negative edges sampled for each positive edge during validation and testing.
            Default is 1000.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_negative_edges_eval"):
            assert self._num_negative_edges_eval > 0, \
                "Number of negative edges must larger than 0"
            return self._num_negative_edges_eval
        # Set default value to 1000.
        return 1000

    @property
    def lp_decoder_type(self):
        """ The decoder type for loss function in link prediction tasks.
            Currently GraphStorm supports ``dot_product``, ``distmult``,
            ``transe`` (``transe_l1`` and ``transe_l2``), and ``rotate``. Default is ``distmult``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lp_decoder_type"):
            decoder_type = self._lp_decoder_type.lower()
            assert decoder_type in SUPPORTED_LP_DECODER, \
                f"Link prediction decoder {self._lp_decoder_type} not supported. " \
                f"GraphStorm only supports {SUPPORTED_LP_DECODER}"
            return decoder_type

        # Set default value to distmult
        return BUILTIN_LP_DISTMULT_DECODER

    @property
    def lp_embed_normalizer(self):
        """ Type of normalization method used to normalize node embeddings in link prediction
            tasks. Currently GraphStorm only supports l2 normalization (``l2_norm``). Default
            is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lp_embed_normalizer"):
            normalizer = self._lp_embed_normalizer.lower()
            assert normalizer in GRAPHSTORM_LP_EMB_NORMALIZATION_METHODS, \
                f"Link prediction embedding normalizer {normalizer} not supported. " \
                f"GraphStorm only support {GRAPHSTORM_LP_EMB_NORMALIZATION_METHODS}"

            # TODO: Check the compatibility between the loss function
            # and the normalizer. Right now only l2 norm is supported
            # and it is compatible with both cross entropy loss and
            # contrastive loss.
            return normalizer

        if self.lp_loss_func == BUILTIN_LP_LOSS_CONTRASTIVELOSS:
            # By default, normalize the emb with l2 normalization
            # when the loss function is contrastive loss
            return GRAPHSTORM_LP_EMB_L2_NORMALIZATION
        return None

    @property
    def contrastive_loss_temperature(self):
        """ Temperature of link prediction contrastive loss. This is used to rescale the
        link prediction positive and negative scores for the loss. Default is 1.0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_contrastive_loss_temperature"):
            assert self.lp_loss_func == BUILTIN_LP_LOSS_CONTRASTIVELOSS, \
                "Use contrastive-loss-temperature only when the loss function is " \
                f"{BUILTIN_LP_LOSS_CONTRASTIVELOSS} loss."

            contrastive_loss_temperature = float(self._contrastive_loss_temperature)
            assert contrastive_loss_temperature > 0.0, \
                "Contrastive loss temperature must be larger than 0"
            return contrastive_loss_temperature

        return 1.0

    @property
    def lp_edge_weight_for_loss(self):
        """ Edge feature field name for edge weight. The edge weight is used to rescale the
            positive edge loss for link prediction tasks. Default is None.

            The edge_weight can be in following format:

            - ``weight_name``: global weight name, if an edge has weight,
            the corresponding weight name is ``weight_name``.

            - ``"src0,rel0,dst0:weight0","src0,rel0,dst0:weight1",...``:
            different edge types have different edge weights.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lp_edge_weight_for_loss"):
            assert self.task_type == BUILTIN_TASK_LINK_PREDICTION, \
                "Edge weight for loss only works with link prediction"

            if self.lp_loss_func in [ BUILTIN_LP_LOSS_CONTRASTIVELOSS]:
                logging.warning("lp_edge_weight_for_loss does not work with "
                                "%s loss in link prediction."
                                "Disable edge weight for link prediction loss.",
                                BUILTIN_LP_LOSS_CONTRASTIVELOSS)
                return None

            edge_weights = self._lp_edge_weight_for_loss
            if len(edge_weights) == 1 and \
                ":" not in edge_weights[0]:
                # global feat_name
                return edge_weights[0]

            # per edge type feature
            weight_dict = {}
            for weight_name in edge_weights:
                weight_info = weight_name.split(":")
                etype = tuple(weight_info[0].split(","))
                assert etype not in weight_dict, \
                    f"You already specify the weight names of {etype}" \
                    f"as {weight_dict[etype]}"

                # TODO: if train_etype is None, we need to check if
                # etype exists in g.
                assert self.train_etype is None or etype in self.train_etype, \
                    f"{etype} must in the training edge type list"
                assert isinstance(weight_info[1], str), \
                    f"Feature name of {etype} should be a string instead of {weight_info[1]}"
                weight_dict[etype] = [weight_info[1]]
            return weight_dict

        return None

    def _get_predefined_negatives_per_etype(self, negatives):
        if len(negatives) == 1 and \
            ":" not in negatives[0]:
            # global feat_name
            return negatives[0]

        # per edge type feature
        negative_dict = {}
        for negative in negatives:
            negative_info = negative.split(":")
            assert len(negative_info) == 2, \
                "negative dstnode information must be provided in format of " \
                f"src,relation,dst:feature_name, but get {negative}"

            etype = tuple(negative_info[0].split(","))
            assert len(etype) == 3, \
                f"Edge type must in format of (src,relation,dst), but get {etype}"
            assert etype not in negative_dict, \
                f"You already specify the fixed negative of {etype} " \
                f"as {negative_dict[etype]}"

            negative_dict[etype] = negative_info[1]
        return negative_dict

    @property
    def train_etypes_negative_dstnode(self):
        """ The list of canonical edge types that have hard negative edges
            constructed by corrupting destination nodes during training.

            For each edge type to use different fields to store the hard negatives,
            the format of the arguement is:

            .. code:: json

                train_etypes_negative_dstnode:
                    - src_type,rel_type0,dst_type:negative_nid_field
                    - src_type,rel_type1,dst_type:negative_nid_field

            or, for all edge types to use the same field to store the hard negatives,
            the format of the arguement is:

            .. code:: json

                train_etypes_negative_dstnode:
                    - negative_nid_field
        """
        # pylint: disable=no-member
        if hasattr(self, "_train_etypes_negative_dstnode"):
            assert self.task_type == BUILTIN_TASK_LINK_PREDICTION, \
                "Hard negative only works with link prediction"
            hard_negatives = self._train_etypes_negative_dstnode
            return self._get_predefined_negatives_per_etype(hard_negatives)

        # By default fixed negative is not used
        return None

    @property
    def num_train_hard_negatives(self):
        """ Number of hard negatives to sample for each edge type during training.
            Default is None.

            For each edge type to have a number of hard negatives,
            the format of the arguement is:

            .. code:: json

                num_train_hard_negatives:
                    - src_type,rel_type0,dst_type:num_negatives
                    - src_type,rel_type1,dst_type:num_negatives

            or, for all edge types to have the same number of hard negatives,
            the format of the arguement is:

            .. code:: json

                num_train_hard_negatives:
                    - num_negatives
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_train_hard_negatives"):
            assert self.task_type == BUILTIN_TASK_LINK_PREDICTION, \
                "Hard negative only works with link prediction"
            num_negatives = self._num_train_hard_negatives
            if len(num_negatives) == 1 and \
                ":" not in num_negatives[0]:
                # global feat_name
                return int(num_negatives[0])

            # per edge type feature
            num_hard_negative_dict = {}
            for num_negative in num_negatives:
                negative_info = num_negative.split(":")
                assert len(negative_info) == 2, \
                    "Number of train hard negative information must be provided in format of " \
                    f"src,relation,dst:10, but get {num_negative}"
                etype = tuple(negative_info[0].split(","))
                assert len(etype) == 3, \
                    f"Edge type must in format of (src,relation,dst), but get {etype}"
                assert etype not in num_hard_negative_dict, \
                    f"You already specify the fixed negative of {etype} " \
                    f"as {num_hard_negative_dict[etype]}"

                num_hard_negative_dict[etype] = int(negative_info[1])
            return num_hard_negative_dict

        return None

    @property
    def eval_etypes_negative_dstnode(self):
        """ The list of canonical edge types that have hard negative edges
            constructed by corrupting destination nodes during evaluation.

            For each edge type to use different fields to store the hard negatives,
            the format of the arguement is:

            .. code:: json

                eval_etypes_negative_dstnode:
                    - src_type,rel_type0,dst_type:negative_nid_field
                    - src_type,rel_type1,dst_type:negative_nid_field

            or, for all edge types to use the same field to store the hard negatives,
            the format of the arguement is:

            .. code:: json

                eval_etypes_negative_dstnode:
                    - negative_nid_field
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_etypes_negative_dstnode"):
            assert self.task_type == BUILTIN_TASK_LINK_PREDICTION, \
                "Fixed negative only works with link prediction"
            fixed_negatives = self._eval_etypes_negative_dstnode
            return self._get_predefined_negatives_per_etype(fixed_negatives)

        # By default fixed negative is not used
        return None

    @property
    def train_etype(self):
        """ The list of canonical edge types that will be added as training target.
            If not provided, all edge types will be used as training target. A canonical
            edge type should be formatted as ``src_node_type,relation_type,dst_node_type``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_train_etype"):
            if self._train_etype is None:
                return None
            assert isinstance(self._train_etype, list)
            assert len(self._train_etype) > 0

            return [tuple(train_etype.split(',')) for train_etype in self._train_etype]
        # By default return None, which means use all edge types
        return None

    @property
    def eval_etype(self):
        """ The list of canonical edge types that will be added as evaluation target.
            If not provided, all edge types will be used as evaluation target. A canonical
            edge type should be formatted as ``src_node_type,relation_type,dst_node_type``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_eval_etype"):
            if self._eval_etype is None:
                return None
            assert isinstance(self._eval_etype, list)
            assert len(self._eval_etype) > 0
            return [tuple(eval_etype.split(',')) for eval_etype in self._eval_etype]
        # By default return None, which means use all edge types
        return None

    @property
    def exclude_training_targets(self):
        """ Whether to remove the training targets from the GNN computation graph.
            Default is True.
        """
        # pylint: disable=no-member
        if hasattr(self, "_exclude_training_targets"):
            assert self._exclude_training_targets in [True, False]

            if self._exclude_training_targets is True:
                assert len(self.reverse_edge_types_map) > 0, \
                    "When exclude training targets is used, " \
                    "Reverse edge types map must be provided."
            return self._exclude_training_targets

        # By default, exclude training targets
        assert len(self.reverse_edge_types_map) > 0, \
            "By default, exclude training targets is used." \
            "Reverse edge types map must be provided."
        return True

    @property
    def gamma(self):
        """ Common hyperparameter symbol gamma. Default is None.
        """
        if hasattr(self, "_gamma"):
            return float(self._gamma)

        return None

    @property
    def alpha(self):
        """ Common hyperparameter symbol alpha. Alpha is used in focal loss for binary
            classification. Default is None.
        """
        if hasattr(self, "_alpha"):
            return float(self._alpha)

        return None

    @property
    def class_loss_func(self):
        """ Classification loss function. Builtin loss functions include
            ``cross_entropy`` and ``focal``. Default is ``cross_entropy``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_class_loss_func"):
            assert self._class_loss_func in BUILTIN_CLASS_LOSS_FUNCTION, \
                f"Only support {BUILTIN_CLASS_LOSS_FUNCTION} " \
                "loss functions for classification tasks"
            return self._class_loss_func

        return BUILTIN_CLASS_LOSS_CROSS_ENTROPY

    @property
    def regression_loss_func(self):
        """ Regression loss function. Builtin loss functions include
            ``mse`` and ``shrinkage``. Default is ``mse``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_regression_loss_func"):
            assert self._regression_loss_func in BUILTIN_REGRESSION_LOSS_FUNCTION, \
                f"Only support {BUILTIN_REGRESSION_LOSS_FUNCTION} " \
                "loss functions for regression tasks"
            return self._regression_loss_func

        return BUILTIN_REGRESSION_LOSS_MSE


    @property
    def lp_loss_func(self):
        """ Link prediction loss function. Builtin loss functions include
            ``cross_entropy`` and ``contrastive``. Default is ``cross_entropy``.
        """
        # pylint: disable=no-member
        if hasattr(self, "_lp_loss_func"):
            assert self._lp_loss_func in BUILTIN_LP_LOSS_FUNCTION
            return self._lp_loss_func
        # By default, return None
        # which means using the default evaluation metrics for different tasks.
        return BUILTIN_LP_LOSS_CROSS_ENTROPY

    @property
    def adversarial_temperature(self):
        """ A hyperparameter value of temperature of adversarial cross entropy loss for link
            prediction tasks. Default is None.
        """
        # pylint: disable=no-member
        if hasattr(self, "_adversarial_temperature"):
            assert self.lp_loss_func in [BUILTIN_LP_LOSS_CROSS_ENTROPY], \
                f"adversarial_temperature only works with {BUILTIN_LP_LOSS_CROSS_ENTROPY}"
            return self._adversarial_temperature
        return None

    @property
    def task_type(self):
        """ Graph machine learning task type. GraphStorm supported task types include
            "node_classification", "node_regression", "edge_classification",
            "edge_regression", and "link_prediction". Must provided.
        """
        # pylint: disable=no-member
        if hasattr(self, "_task_type"):
            assert self._task_type in SUPPORTED_TASKS, \
                    f"Supported task types include {SUPPORTED_TASKS}, " \
                    f"but got {self._task_type}"
            return self._task_type
        else:
            return None

    @property
    def report_eval_per_type(self):
        """ Whether report evaluation metrics per node type or edge type.
            If True, report evaluation results for each node type/edge type."
            If False, report an average result.
        """
        # pylint: disable=no-member
        if hasattr(self, "_report_eval_per_type"):
            assert self._report_eval_per_type in [True, False], \
                "report_eval_per_type must be True or False"
            return self._report_eval_per_type

        return False

    @property
    def eval_metric(self):
        """ Evaluation metric(s) used during evaluation. The input can be a string specifying
            the evaluation metric to report,  or a list of strings specifying a list of
            evaluation metrics to report. The first evaluation metric is treated as the
            major metric and is used to choose the best trained model. Default values
            depend on ``task_type``. For classification tasks, the default value is ``accuracy``;
            For regression tasks, the default value is ``rmse``. For link prediction tasks,
            the default value is ``mrr``.
        """
        # pylint: disable=no-member
        # Task is node classification
        if self.task_type in [BUILTIN_TASK_NODE_CLASSIFICATION, \
            BUILTIN_TASK_EDGE_CLASSIFICATION]:
            if isinstance(self.num_classes, dict):
                for num_classes in self.num_classes.values():
                    assert num_classes > 0, \
                        "For node classification, num_classes must be provided"
            else:
                assert self.num_classes > 0, \
                    "For node classification, num_classes must be provided"

            # check evaluation metrics
            if hasattr(self, "_eval_metric"):
                if isinstance(self._eval_metric, str):
                    eval_metric = self._eval_metric.lower()
                    if eval_metric.startswith(SUPPORTED_HIT_AT_METRICS):
                        assert eval_metric[len(SUPPORTED_HIT_AT_METRICS)+1:].isdigit(), \
                            "hit_at_k evaluation metric for classification " \
                            f"must end with an integer, but get {eval_metric}."
                    elif eval_metric.startswith(SUPPORTED_RECALL_AT_PRECISION_METRICS):
                        assert is_float(
                            eval_metric[len(SUPPORTED_RECALL_AT_PRECISION_METRICS) + 1:]), \
                        "recall_at_precision_beta evaluation metric for classification " \
                        f"must end with an integer or float, but get {eval_metric}."
                        assert (0 < float(eval_metric[
                                      len(SUPPORTED_RECALL_AT_PRECISION_METRICS)+1:]) <= 1), \
                            "The beta in recall_at_precision_beta evaluation metric must be in " \
                            "(0, 1], but get " \
                            f"{float(eval_metric[len(SUPPORTED_RECALL_AT_PRECISION_METRICS)+1:])}."
                    elif eval_metric.startswith(SUPPORTED_PRECISION_AT_RECALL_METRICS):
                        assert is_float(
                            eval_metric[len(SUPPORTED_PRECISION_AT_RECALL_METRICS) + 1:]), \
                            "precision_at_recall_beta evaluation metric for classification " \
                            f"must end with an integer or float, but get {eval_metric}."
                        assert (0 < float(eval_metric[
                                      len(SUPPORTED_PRECISION_AT_RECALL_METRICS)+1:]) <= 1), \
                            "The beta in precision_at_recall_beta evaluation metric must be in " \
                            "(0, 1], but get " \
                            f"{float(eval_metric[len(SUPPORTED_PRECISION_AT_RECALL_METRICS)+1:])}."
                    elif eval_metric.startswith(SUPPORTED_FSCORE_AT_METRICS):
                        assert is_float(eval_metric[len(SUPPORTED_FSCORE_AT_METRICS)+1:]), \
                            'fscore_at_beta evaluation metric for classification ' \
                            f'must end with an integer or float, but get {eval_metric}.'
                    else:
                        assert eval_metric in SUPPORTED_CLASSIFICATION_METRICS, \
                            f"Classification evaluation metric should be " \
                            f"in {SUPPORTED_CLASSIFICATION_METRICS}" \
                            f"but get {self._eval_metric}."
                    eval_metric = [eval_metric]
                elif isinstance(self._eval_metric, list) and len(self._eval_metric) > 0:
                    eval_metric = []
                    for metric in self._eval_metric:
                        metric = metric.lower()
                        if metric.startswith(SUPPORTED_HIT_AT_METRICS):
                            assert metric[len(SUPPORTED_HIT_AT_METRICS)+1:].isdigit(), \
                                "hit_at_k evaluation metric for classification " \
                                f"must end with an integer, but get {metric}."
                        elif metric.startswith(SUPPORTED_RECALL_AT_PRECISION_METRICS):
                            assert is_float(
                                metric[len(SUPPORTED_RECALL_AT_PRECISION_METRICS)+1:]), \
                                "recall_at_precision_beta evaluation metric for classification " \
                                f"must end with an integer or float, but get {metric}."
                            assert (0 < float(metric[
                                          len(SUPPORTED_RECALL_AT_PRECISION_METRICS)+1:]) <= 1), \
                                "The beta in recall_at_precision_beta evaluation metric must be " \
                                "in (0, 1], but get {}.".format(
                                    float(metric[
                                          len(SUPPORTED_RECALL_AT_PRECISION_METRICS)+1:]))
                        elif metric.startswith(SUPPORTED_PRECISION_AT_RECALL_METRICS):
                            assert is_float(
                                metric[len(SUPPORTED_PRECISION_AT_RECALL_METRICS)+1:]), \
                                "precision_at_recall_beta evaluation metric for classification " \
                                f"must end with an integer or float, but get {metric}."
                            assert (0 < float(metric[
                                          len(SUPPORTED_PRECISION_AT_RECALL_METRICS)+1:]) <= 1), \
                                "The beta in precision_at_recall_beta evaluation metric must be " \
                                "in (0, 1], but get {}.".format(
                                    float(metric[
                                          len(SUPPORTED_PRECISION_AT_RECALL_METRICS) + 1:]))
                        elif metric.startswith(SUPPORTED_FSCORE_AT_METRICS):
                            assert is_float(metric[len(SUPPORTED_FSCORE_AT_METRICS)+1:]), \
                                'fscore_at_beta evaluation metric for classification ' \
                                f'must end with an integer or float, but get {metric}.'
                        else:
                            assert metric in SUPPORTED_CLASSIFICATION_METRICS, \
                                f"Classification evaluation metric should be " \
                                f"in {SUPPORTED_CLASSIFICATION_METRICS}" \
                                f"but get {self._eval_metric}."
                        eval_metric.append(metric)
                else:
                    assert False, "Classification evaluation metric " \
                        "should be a string or a list of string"
                    # no eval_metric
            else:
                eval_metric = ["accuracy"]
        elif self.task_type in [BUILTIN_TASK_NODE_REGRESSION, \
            BUILTIN_TASK_EDGE_REGRESSION, BUILTIN_TASK_RECONSTRUCT_NODE_FEAT,
            BUILTIN_TASK_RECONSTRUCT_EDGE_FEAT]:
            if hasattr(self, "_eval_metric"):
                if isinstance(self._eval_metric, str):
                    eval_metric = self._eval_metric.lower()
                    assert eval_metric in SUPPORTED_REGRESSION_METRICS, \
                        f"Regression evaluation metric should be " \
                        f"in {SUPPORTED_REGRESSION_METRICS}, " \
                        f"but get {self._eval_metric}."
                    eval_metric = [eval_metric]
                elif isinstance(self._eval_metric, list) and len(self._eval_metric) > 0:
                    eval_metric = []
                    for metric in self._eval_metric:
                        metric = metric.lower()
                        assert metric in SUPPORTED_REGRESSION_METRICS, \
                            f"Regression evaluation metric should be " \
                            f"in {SUPPORTED_REGRESSION_METRICS}" \
                            f"but get {self._eval_metric}."
                        eval_metric.append(metric)
                else:
                    assert False, "Regression evaluation metric " \
                        "should be a string or a list of string"
                    # no eval_metric
            else:
                if self.task_type in [BUILTIN_TASK_RECONSTRUCT_NODE_FEAT,
                                      BUILTIN_TASK_RECONSTRUCT_EDGE_FEAT]:
                    eval_metric = ["mse"]
                else:
                    eval_metric = ["rmse"]
        elif self.task_type == BUILTIN_TASK_LINK_PREDICTION:
            if hasattr(self, "_eval_metric"):
                if isinstance(self._eval_metric, str):
                    eval_metric = self._eval_metric.lower()
                    if eval_metric.startswith(SUPPORTED_HIT_AT_METRICS):
                        assert eval_metric[len(SUPPORTED_HIT_AT_METRICS) + 1:].isdigit(), \
                            "hit_at_k evaluation metric for link prediction " \
                            f"must end with an integer, but get {eval_metric}."
                    else:
                        assert eval_metric in SUPPORTED_LINK_PREDICTION_METRICS, \
                            f"Link prediction evaluation metric should be " \
                            f"in {SUPPORTED_LINK_PREDICTION_METRICS}" \
                            f"but get {self._eval_metric}."
                    eval_metric = [eval_metric]
                elif isinstance(self._eval_metric, list) and len(self._eval_metric) > 0:
                    eval_metric = []
                    for metric in self._eval_metric:
                        metric = metric.lower()
                        if metric.startswith(SUPPORTED_HIT_AT_METRICS):
                            assert metric[len(SUPPORTED_HIT_AT_METRICS) + 1:].isdigit(), \
                                "hit_at_k evaluation metric for link prediction " \
                                f"must end with an integer, but get {metric}."
                        else:
                            assert metric in SUPPORTED_LINK_PREDICTION_METRICS, \
                                f"Link prediction evaluation metric should be " \
                                f"in {SUPPORTED_LINK_PREDICTION_METRICS}" \
                                f"but get {self._eval_metric}."
                        eval_metric.append(metric)
                else:
                    assert False, "Link prediction evaluation metric " \
                        "should be a string or a list of string"
                    # no eval_metric
            else:
                eval_metric = ["mrr"]
        else:
            assert False, "Unknow task type"

        return eval_metric

    @property
    def model_select_etype(self):
        """ Canonical etype used for selecting the best model. Default is on
            all edge types.
        """
        # pylint: disable=no-member
        if hasattr(self, "_model_select_etype"):
            etype = self._model_select_etype.split(",")
            assert len(etype) == 3, \
                "If you want to select model based on eval value of " \
                "a specific etype, the model_select_etype must be a " \
                "canonical etype in the format of src,rel,dst"
            return tuple(etype)

        # Per edge type lp evaluation is disabled.
        return LINK_PREDICTION_MAJOR_EVAL_ETYPE_ALL

    @property
    def num_ffn_layers_in_input(self):
        """ Number of extra feedforward neural network layers to be added in the input layer.
            Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_ffn_layers_in_input"):
            assert self._num_ffn_layers_in_input >= 0, \
                "Number of extra MLP layers in input layer must be larger or equal than 0"
            return self._num_ffn_layers_in_input
        # Set default mlp layer number in the input layer to 0
        return 0

    @property
    def num_ffn_layers_in_gnn(self):
        """ Number of extra feedforward neural network layers to be added between GNN layers.
            Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_ffn_layers_in_gnn"):
            assert self._num_ffn_layers_in_gnn >= 0, \
                "Number of extra MLP layers between GNN layers must be larger or equal than 0"
            return self._num_ffn_layers_in_gnn
        # Set default mlp layer number between gnn layer to 0
        return 0

    @property
    def num_ffn_layers_in_decoder(self):
        """ Number of extra feedforward neural network layers to be added in the decoder layer.
            Default is 0.
        """
        # pylint: disable=no-member
        if hasattr(self, "_num_ffn_layers_in_decoder"):
            assert self._num_ffn_layers_in_decoder >= 0, \
                "Number of extra MLP layers in decoder must be larger or equal than 0"
            return self._num_ffn_layers_in_decoder
        # Set default mlp layer number between gnn layer to 0
        return 0

    @property
    def decoder_norm(self):
        """ Normalization (Batch or Layer)
        """
        # pylint: disable=no-member
        if not hasattr(self, "_decoder_norm"):
            return None
        assert self._decoder_norm in BUILTIN_GNN_NORM, \
            "Normalization type must be one of batch or layer"

        return self._decoder_norm

    ################## Reconstruct node feats ###############
    @property
    def reconstruct_nfeat_name(self):
        """ node feature name for reconstruction
        """
        assert hasattr(self, "_reconstruct_nfeat_name"), \
            "reconstruct_nfeat_name must be provided for reconstruct_node_feat tasks(s)."
        assert isinstance(self._reconstruct_nfeat_name, str), \
            "The name of the node feature for reconstruction must be a string." \
            "For a node feature reconstruction task, it only " \
            "reconstruct one node feature on one node type."
        return self._reconstruct_nfeat_name

    ################## Reconstruct edge feats ###############
    @property
    def reconstruct_efeat_name(self):
        """ edge feature name for reconstruction

            .. versionadded:: 0.4.0
        """
        assert hasattr(self, "_reconstruct_efeat_name"), \
            "reconstruct_efeat_name must be provided for reconstruct_edge_feat task(s)."
        assert isinstance(self._reconstruct_efeat_name, str), \
            "The name of the edge feature for reconstruction must be a string." \
            "For a edge feature reconstruction task, it only " \
            "reconstruct one edge feature on one edge type."
        return self._reconstruct_efeat_name

    ################## Multi task learning ##################
    @property
    def multi_tasks(self):
        """ Tasks in multi-task learning
        """
        assert hasattr(self, "_multi_tasks"), \
            "multi_task_learning must be set in the task config"
        return self._multi_tasks

def _add_initialization_args(parser):
    group = parser.add_argument_group(title="initialization")
    group.add_argument(
        "--verbose",
        type=lambda x: (str(x).lower() in ['true', '1']),
        default=argparse.SUPPRESS,
        help="Print more information.",
    )
    group.add_argument(
        "--use-wholegraph-embed",
        type=lambda x: (str(x).lower() in ['true', '1']),
        default=argparse.SUPPRESS,
        help="Whether to use WholeGraph to store intermediate embeddings/tensors generated \
            during training or inference, e.g., cache_lm_emb, sparse_emb, etc."
    )
    group.add_argument(
        "--use-graphbolt",
        type=lambda x: (str(x).lower() in ['true', '1']),
        default=argparse.SUPPRESS,
        help=(
            "Whether to use GraphBolt graph representation. "
            "See https://docs.dgl.ai/stochastic_training/ for details"
        )
    )
    return parser

def _add_gsgnn_basic_args(parser):
    group = parser.add_argument_group(title="graphstorm gnn")
    group.add_argument('--backend', type=str, default=argparse.SUPPRESS,
            help='PyTorch distributed backend')
    group.add_argument('--ip-config', type=str, default=argparse.SUPPRESS,
            help='The file for IP configuration')
    group.add_argument('--part-config', type=str, default=argparse.SUPPRESS,
            help='The path to the partition config file')
    group.add_argument("--save-perf-results-path",
            type=str,
            default=argparse.SUPPRESS,
            help="Folder path to save performance results of model evaluation.")
    group.add_argument("--profile-path",
            type=str,
            help="The path of the folder that contains the profiling results.")
    return parser

def _add_gnn_args(parser):
    group = parser.add_argument_group(title="gnn")
    group.add_argument('--model-encoder-type', type=str, default=argparse.SUPPRESS,
            help='Model type can either be gnn or lm to specify the model encoder')
    group.add_argument(
        "--input-activate", type=str, default=argparse.SUPPRESS,
        help="Define the activation type in the input layer")
    group.add_argument("--node-feat-name", nargs='+', type=str, default=argparse.SUPPRESS,
            help="Node feature field name. It can be in following format: "
            "1) '--node-feat-name feat_name': global feature name, "
            "if a node has node feature,"
            "the corresponding feature name is <feat_name>"
            "2)'--node-feat-name ntype0:feat0,feat1 ntype1:feat0,feat1 ...': "
            "different node types have different node features.")
    group.add_argument("--edge-feat-name", nargs='+', type=str, default=argparse.SUPPRESS,
            help="Edge feature field name. It can be in following format: "
            "1) '--edge-feat-name feat_name': global feature name, "
            "if an edge has feature,"
            "the corresponding feature name is <feat_name>"
            "2)'--edge-feat-name etype0:feat0 etype1:feat0,feat1,...': "
            "different edge types have different edge features.")
    group.add_argument("--edge-feat-mp-op", type=str, default=argparse.SUPPRESS,
            help="The operation for using edge feature in message passing computation."
                      "Supported operations include {BUILTIN_EDGE_FEAT_MP_OPS}")
    group.add_argument("--fanout", type=str, default=argparse.SUPPRESS,
            help="Fan-out of neighbor sampling. This argument can either be --fanout 20,10 or "
                 "--fanout etype2:20@etype3:20@etype1:20,etype2:10@etype3:4@etype1:2"
                 "Each etype (e.g., etype2) should be a canonical etype in format of"
                 "srcntype/relation/dstntype")
    group.add_argument("--eval-fanout", type=str, default=argparse.SUPPRESS,
            help="Fan-out of neighbor sampling during minibatch evaluation. "
                 "This argument can either be --eval-fanout 20,10 or "
                 "--eval-fanout etype2:20@etype3:20@etype1:20,etype2:10@etype3:4@etype1:2"
                 "Each etype (e.g., etype2) should be a canonical etype in format of"
                 "srcntype/relation/dstntype")
    group.add_argument("--hidden-size", type=int, default=argparse.SUPPRESS,
            help="The number of features in the hidden state")
    group.add_argument("--num-layers", type=int, default=argparse.SUPPRESS,
            help="number of layers in the GNN")
    group.add_argument("--num-ffn-layers-in-input", type=int, default=argparse.SUPPRESS,
                       help="number of extra feedforward neural network layers in input layer.")
    group.add_argument("--num-ffn-layers-in-gnn", type=int, default=argparse.SUPPRESS,
                       help="number of extra feedforward neural network layers between GNN layers.")
    group.add_argument("--num-ffn-layers-in-decoder", type=int, default=argparse.SUPPRESS,
                       help="number of extra feedforward neural network layers in decoder layer.")
    parser.add_argument(
            "--use-mini-batch-infer",
            help="Whether to use mini-batch or full graph inference during evalution",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS
    )

    return parser

def _add_input_args(parser):
    group = parser.add_argument_group(title="input")
    group.add_argument('--restore-model-layers', type=str, default=argparse.SUPPRESS,
                       help='Which GraphStorm neural network layers to load.'
                            'The argument ca be --restore-model-layers embed or '
                            '--restore-model-layers embed,gnn,decoder')
    group.add_argument('--restore-model-path', type=str, default=argparse.SUPPRESS,
            help='Restore the model weights saved in the specified directory.')
    group.add_argument('--restore-optimizer-path', type=str, default=argparse.SUPPRESS,
            help='Restore the optimizer snapshot saved in the specified directory.')
    return parser

def _add_output_args(parser):
    group = parser.add_argument_group(title="output")
    group.add_argument("--save-embed-path", type=str, default=argparse.SUPPRESS,
            help="Save the embddings in the specified directory. "
                 "Use none to turn off embedding saveing")
    group.add_argument("--save-embed-format", type=str, default=argparse.SUPPRESS,
            help="Specify the format for saved embeddings. Valid format: ['pytorch', 'hdf5']")
    group.add_argument('--save-model-frequency', type=int, default=argparse.SUPPRESS,
            help='Save the model every N iterations.')
    group.add_argument('--save-model-path', type=str, default=argparse.SUPPRESS,
            help='Save the model to the specified file. Use none to turn off model saveing')
    group.add_argument("--topk-model-to-save",
            type=int, default=argparse.SUPPRESS,
            help="the number of the k top best validation performance model to save")

    # inference related output args
    parser = _add_inference_args(parser)

    return parser

def _add_task_tracker(parser):
    group = parser.add_argument_group(title="task_tracker")
    group.add_argument("--task-tracker", type=str, default=argparse.SUPPRESS,
            help=f'Task tracker name. Now we support {SUPPORTED_TASK_TRACKER}')
    group.add_argument("--log-report-frequency", type=int, default=argparse.SUPPRESS,
            help="Task running log report frequency. "
                 "In training, every log_report_frequency, the task states are reported")
    return parser

def _add_hyperparam_args(parser):
    group = parser.add_argument_group(title="hp")
    group.add_argument("--dropout", type=float, default=argparse.SUPPRESS,
            help="dropout probability")
    group.add_argument(
            "--decoder-bias",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="Whether to use decoder bias")
    group.add_argument("--gnn-norm", type=str, default=argparse.SUPPRESS, help="norm type")
    group.add_argument("--lr", type=float, default=argparse.SUPPRESS,
            help="learning rate")
    group.add_argument("-e", "--num-epochs", type=int, default=argparse.SUPPRESS,
            help="number of training epochs")
    group.add_argument("--batch-size", type=int, default=argparse.SUPPRESS,
            help="Mini-batch size. Must be larger than 0")
    group.add_argument("--sparse-optimizer-lr", type=float, default=argparse.SUPPRESS,
            help="sparse optimizer learning rate")
    group.add_argument("--max-grad-norm", type=float, default=argparse.SUPPRESS,
            help="maximum L2 norm of gradients")
    group.add_argument("--grad-norm-type", type=float, default=argparse.SUPPRESS,
            help="norm type for gradient clips")
    group.add_argument(
            "--use-node-embeddings",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="Whether to use extra learnable node embeddings")
    group.add_argument("--construct-feat-ntype", type=str, nargs="+",
            help="The node types whose features are constructed from neighbors' features.")
    group.add_argument("--construct-feat-encoder", type=str, default=argparse.SUPPRESS,
            help="The encoder used for constructing node features.")
    group.add_argument("--construct-feat-fanout", type=int, default=argparse.SUPPRESS,
            help="The fanout used for constructing node features.")
    group.add_argument("--wd-l2norm", type=float, default=argparse.SUPPRESS,
            help="weight decay l2 norm coef")
    group.add_argument("--alpha-l2norm", type=float, default=argparse.SUPPRESS,
            help="coef for scale unused weights l2norm")
    group.add_argument(
            "--use-self-loop",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="include self feature as a special relation")

    # control evaluation
    group.add_argument("--eval-batch-size", type=int, default=argparse.SUPPRESS,
            help="Mini-batch size for computing GNN embeddings in evaluation.")
    group.add_argument('--eval-frequency',
            type=int,
            default=argparse.SUPPRESS,
            help="How often to run the evaluation. "
                 "Every #eval-frequency iterations.")
    group.add_argument(
            '--no-validation',
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="If no-validation is set to True, "
                 "there will be no evaluation during training.")
    # early stop
    group.add_argument("--early-stop-burnin-rounds",
            type=int, default=argparse.SUPPRESS,
            help="Burn-in rounds before start checking for the early stop condition.")
    group.add_argument("--early-stop-rounds",
            type=int, default=argparse.SUPPRESS,
            help="The number of rounds for validation scores to average to decide on early stop")
    group.add_argument("--early-stop-strategy",
            type=str, default=argparse.SUPPRESS,
            help="Specify the early stop strategy. "
            "It can be either consecutive_increase or average_increase")
    group.add_argument("--use-early-stop",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help='whether to use early stopping by monitoring the validation loss')
    return parser

def _add_lm_model_args(parser):
    group = parser.add_argument_group(title="lm model")
    group.add_argument("--lm-tune-lr", type=float, default=argparse.SUPPRESS,
            help="learning rate for fine-tuning language model")
    group.add_argument("--lm-train-nodes", type=int, default=argparse.SUPPRESS,
            help="number of nodes used in LM model fine-tuning")
    group.add_argument("--lm-infer-batch-size", type=int, default=argparse.SUPPRESS,
            help="Batch size used in LM model inference")
    group.add_argument("--freeze-lm-encoder-epochs", type=int, default=argparse.SUPPRESS,
            help="Before fine-tuning LM model, how many epochs we will take "
                 "to warmup a GNN model")
    group.add_argument("--max-seq-len", type=int, default=argparse.SUPPRESS,
                       help="The maximum of sequence length for distillation")
    group.add_argument("--cache-lm-embed",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="Whether to cache the LM embeddings in files. " + \
                    "If the LM embeddings have been saved before, load the saved embeddings " + \
                    "instead of computing the LM embeddings again.")
    return parser

def _add_rgat_args(parser):
    group = parser.add_argument_group(title="rgat")
    group.add_argument("--num-heads", type=int, default=argparse.SUPPRESS,
            help="number of attention heads")
    return parser

def _add_rgcn_args(parser):
    group = parser.add_argument_group(title="rgcn")
    group.add_argument("--num-bases", type=int, default=argparse.SUPPRESS,
            help="number of filter weight matrices, default: -1 [use all]")
    return parser

def _add_node_classification_args(parser):
    group = parser.add_argument_group(title="node classification")
    group.add_argument("--target-ntype", type=str, default=argparse.SUPPRESS,
                       help="the node type for prediction")
    group.add_argument("--label-field", type=str, default=argparse.SUPPRESS,
                       help="the label field in the data")
    group.add_argument(
            "--multilabel",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="Whether the task is a multi-label classifiction task")
    group.add_argument(
            "--multilabel-weights",
            type=str,
            default=argparse.SUPPRESS,
            help="Used to specify the weight of positive examples of each class in a "
            "multi-label classifiction task."
            "It is feed into th.nn.BCEWithLogitsLoss."
            "The weights should in following format 0.1,0.2,0.3,0.1,0.0 ")
    group.add_argument(
            "--imbalance-class-weights",
            type=str,
            default=argparse.SUPPRESS,
            help="Used to specify a manual rescaling weight given to each class "
            "in a single-label multi-class classification task."
            "It is feed into th.nn.CrossEntropyLoss or th.nn.BCEWithLogitsLoss."
            "The weights should be in the following format 0.1,0.2,0.3,0.1,0.0 ")
    group.add_argument("--num-classes", type=int, default=argparse.SUPPRESS,
                       help="The cardinality of labels in a classifiction task")
    group.add_argument("--return-proba",
                       type=lambda x: (str(x).lower() in ['true', '1']),
                       default=argparse.SUPPRESS,
                       help="Whether to return the probabilities of all the predicted \
                       results or only the maximum one. Set True to return the \
                       probabilities. Set False to return the maximum one.")
    group.add_argument(
        "--use-pseudolabel",
        type=lambda x: (str(x).lower() in ['true', '1']),
        default=argparse.SUPPRESS,
        help="Whether use pseudolabeling for unlabeled nodes in semi-supervised training")
    return parser

def _add_edge_classification_args(parser):
    group = parser.add_argument_group(title="edge prediction")
    group.add_argument('--target-etype', nargs='+', type=str, default=argparse.SUPPRESS,
            help="The list of canonical etype that will be added as"
                "a training target with the target e type "
                "in this application, for example "
                "--train-etype query,clicks,asin or"
                "--train-etype query,clicks,asin query,search,asin if not specified"
                "then no aditional training target will "
                "be considered")
    group.add_argument("--decoder-edge-feat", nargs='+', type=str, default=argparse.SUPPRESS,
                       help="A list of edge features that can be used by a decoder to "
                            "enhance its performance. It can be in following format: "
                            "--decoder-edge-feat feat or "
                            "--decoder-edge-feat query,clicks,asin:feat0,feat1 "
                            "If not specified, decoder will not use edge feats")

    group.add_argument("--num-decoder-basis", type=int, default=argparse.SUPPRESS,
                       help="The number of basis for the decoder in edge prediction task")

    group.add_argument('--decoder-type', type=str, default=argparse.SUPPRESS,
                       help="Decoder type can either be  DenseBiDecoder or "
                            "MLPDecoder to specify the model decoder")
    group.add_argument("--decoder-norm", type=str, default=argparse.SUPPRESS,
                       help="decoder norm type")

    group.add_argument(
            "--remove-target-edge-type",
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="Whether to remove the target edge type for message passing")

    return parser

def _add_link_prediction_args(parser):
    group = parser.add_argument_group(title="link prediction")
    group.add_argument("--lp-decoder-type", type=str, default=argparse.SUPPRESS,
            help="Link prediction decoder type.")
    group.add_argument("--num-negative-edges", type=int, default=argparse.SUPPRESS,
            help="Number of edges consider for the negative batch of edges.")
    group.add_argument("--fixed-test-size", type=int, default=argparse.SUPPRESS,
            help="Fixed number of test data used in evaluation.")
    group.add_argument("--num-negative-edges-eval", type=int, default=argparse.SUPPRESS,
            help="Number of edges consider for the negative "
                 "batch of edges for the model evaluation. "
                 "If the MRR saturates at high values or has "
                 "large variance increase this number.")
    group.add_argument("--train-negative-sampler", type=str, default=argparse.SUPPRESS,
            help="The algorithm of sampling negative edges for link prediction.training ")
    group.add_argument("--eval-negative-sampler", type=str, default=argparse.SUPPRESS,
            help="The algorithm of sampling negative edges for link prediction evaluation")
    group.add_argument('--eval-etype', nargs='+', type=str, default=argparse.SUPPRESS)
    group.add_argument('--train-etype', nargs='+', type=str, default=argparse.SUPPRESS,
            help="The list of canonical etype that will be added as"
                "a training target with the target e type "
                "in this application for example "
                "--train-etype query,clicks,asin or"
                "--train-etype query,clicks,asin query,search,asin if not specified"
                "then no aditional training target will "
                "be considered")
    group.add_argument(
            '--exclude-training-targets',
            type=lambda x: (str(x).lower() in ['true', '1']),
            default=argparse.SUPPRESS,
            help="Whether to remove the training targets from the "
                 "computation graph before the forward pass.")
    group.add_argument('--reverse-edge-types-map',
            nargs='+', type=str, default=argparse.SUPPRESS,
            help="A list of reverse egde type info. Each information is in the following format:"
                    "<head,relation,reverse relation,tail>, for example "
                    "--reverse-edge-types-map query,adds,rev-adds,asin or"
                    "--reverse-edge-types-map query,adds,rev-adds,asin "
                    "query,clicks,rev-clicks,asin")
    group.add_argument(
            "--gamma",
            type=float,
            default=argparse.SUPPRESS,
            help="Common hyperparameter symbol gamma."
    )
    group.add_argument(
            "--alpha",
            type=float,
            default=argparse.SUPPRESS,
            help="Common hyperparameter symbol alpha."
    )
    group.add_argument("--class-loss-func", type=str, default=argparse.SUPPRESS,
            help="Classification loss function.")
    group.add_argument("--regression-loss-func", type=str, default=argparse.SUPPRESS,
            help="Regression loss function.")
    group.add_argument("--lp-loss-func", type=str, default=argparse.SUPPRESS,
            help="Link prediction loss function.")
    group.add_argument("--contrastive-loss-temperature", type=float, default=argparse.SUPPRESS,
            help="Temperature of link prediction contrastive loss.")
    group.add_argument("--adversarial-temperature", type=float, default=argparse.SUPPRESS,
            help="Temperature of adversarial cross entropy loss for link prediction tasks.")
    group.add_argument("--lp-embed-normalizer", type=str, default=argparse.SUPPRESS,
            help="Normalization method used to normalize node embeddings in"
                 "link prediction. Supported methods "
                 f"include {GRAPHSTORM_LP_EMB_NORMALIZATION_METHODS}")
    group.add_argument("--lp-edge-weight-for-loss", nargs='+', type=str, default=argparse.SUPPRESS,
            help="Edge feature field name for edge weights. It can be in following format: "
            "1) '--lp-edge-weight-for-loss feat_name': global feature name, "
            "if all edge types use the same edge weight field."
            "The corresponding feature name is <feat_name>"
            "2)'--lp-edge-weight-for-loss query,adds,asin:weight0 query,clicks,asin:weight1 ..."
            "Different edge types have different weight fields.")
    group.add_argument("--model-select-etype", type=str, default=argparse.SUPPRESS,
            help="Canonical edge type used for selecting best model during "
                 "link prediction training. It can be in following format:"
                "1) '--model-select-etype ALL': Use the average of the evaluation "
                "metrics of each edge type to select the best model"
                "2) '--model-select-etype query,adds,item': Use the evaluation "
                "metric of the edge type (query,adds,item) to select the best model")
    group.add_argument("--train-etypes-negative-dstnode", nargs='+',
            type=str, default=argparse.SUPPRESS,
            help="Edge feature field name for user defined negative destination ndoes "
            "for training. The negative nodes are used to construct hard negative edges "
            "by corrupting positive edges' destination nodes."
            "It can be in following format: "
            "1) '--train-etypes-negative-dstnode negative_nid_field', "
            "if all edge types use the same negative destination node filed."
            "2) '--train-etypes-negative-dstnode query,adds,asin:neg0 query,clicks,asin:neg1 ...'"
            "Different edge types have different negative destination node fields."
            )
    group.add_argument("--eval-etypes-negative-dstnode", nargs='+',
            type=str, default=argparse.SUPPRESS,
            help="Edge feature field name for user defined negative destination ndoes "
            "for evaluation. The negative nodes are used to construct negative edges "
            "by corrupting test edges' destination nodes."
            "It can be in following format: "
            "1) '--eval-etypes-negative-dstnode negative_nid_field', "
            "if all edge types use the same negative destination node filed."
            "2) '--eval-etypes-negative-dstnode query,adds,asin:neg0 query,clicks,asin:neg1 ...'"
            "Different edge types have different negative destination node fields."
            )
    group.add_argument("--num-train-hard-negatives", nargs='+',
            type=str, default=argparse.SUPPRESS,
            help="Number of hard negatives for each edge type during training."
            "It can be in following format: "
            "1) '--num-train-hard-negatives 10', "
            "if all edge types use the same number of hard negatives."
            "2) '--num-train-hard-negatives query,adds,asin:5 query,clicks,asin:10 ...'"
            "Different edge types have different number of hard negatives.")

    return parser

def _add_task_general_args(parser):
    group = parser.add_argument_group(title="train task")
    group.add_argument('--eval-metric', nargs='+', type=str, default=argparse.SUPPRESS,
            help="The list of canonical etype that will be added as"
                "the evaluation metric used. Supported metrics are accuracy,"
                "precision_recall, or roc_auc multiple metrics"
                "can be specified e.g. --eval-metric accuracy precision_recall")
    group.add_argument(
        '--report-eval-per-type', type=lambda x: (str(x).lower() in ['true', '1']),
        default=argparse.SUPPRESS,
        help=(
            "Whether to report evaluation metrics per node type or edge type. "
            "If set to 'True', report evaluation results for each node type/edge type. "
            "Otherwise, report an average evaluation result."
        )
    )
    return parser

def _add_inference_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group(title="infer")
    group.add_argument("--save-prediction-path", type=str, default=argparse.SUPPRESS,
                       help="Where to save the prediction results.")
    group.add_argument(
        "--infer-all-target-nodes",
        type=lambda x: (str(x).lower() in ['true', '1']),
        default=argparse.SUPPRESS,
        help="When set to 'true', will force inference to run on all target node types.")
    return parser

def _add_distill_args(parser):
    group = parser.add_argument_group(title="distill")
    group.add_argument("--textual-data-path", type=str, default=argparse.SUPPRESS,
                       help="Where to load the textual data for distillation.")
    group.add_argument("--max-distill-step", type=int, default=argparse.SUPPRESS,
                       help="The maximum of training step for each node type for distillation")
    return parser
