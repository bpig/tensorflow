# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Base Estimator class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import collections
import copy
import inspect
import itertools
import os
import tempfile
import time

import numpy as np
import six

from tensorflow.contrib import framework as contrib_framework
from tensorflow.contrib import layers
from tensorflow.contrib import metrics as metrics_lib
from tensorflow.contrib.framework import deprecated
from tensorflow.contrib.framework import deprecated_arg_values
from tensorflow.contrib.framework import list_variables
from tensorflow.contrib.framework import load_variable
from tensorflow.contrib.learn.python.learn import evaluable
from tensorflow.contrib.learn.python.learn import graph_actions
from tensorflow.contrib.learn.python.learn import metric_spec
from tensorflow.contrib.learn.python.learn import monitors as monitor_lib
from tensorflow.contrib.learn.python.learn import trainable
from tensorflow.contrib.learn.python.learn.estimators import _sklearn as sklearn
from tensorflow.contrib.learn.python.learn.estimators import run_config
from tensorflow.contrib.learn.python.learn.estimators import tensor_signature
from tensorflow.contrib.learn.python.learn.estimators._sklearn import NotFittedError
from tensorflow.contrib.learn.python.learn.learn_io import data_feeder
from tensorflow.contrib.learn.python.learn.utils import export

from tensorflow.python.framework import errors
from tensorflow.python.framework import ops
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import tensor_shape
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.training import device_setter
from tensorflow.python.training import saver
from tensorflow.python.training import session_run_hook

AS_ITERABLE_DATE = '2016-09-15'
AS_ITERABLE_INSTRUCTIONS = (
    'The default behavior of predict() is changing. The default value for\n'
    'as_iterable will change to True, and then the flag will be removed\n'
    'altogether. The behavior of this flag is described below.')

class ModeKeys(object):
    TRAIN = 'train'
    EVAL = 'eval'
    INFER = 'infer'

class ModelFnOps(
    collections.namedtuple(
        'ModelFnOps',
        ['predictions', 'loss', 'training_op', 'default_metrics', 'signature_fn'])):
    pass

def _get_input_fn(x, y, input_fn, feed_fn, batch_size, shuffle=False, epochs=1):
    """Make inputs into input and feed functions."""
    if input_fn is None:
        if x is None:
            raise ValueError('Either x or input_fn must be provided.')
        
        if contrib_framework.is_tensor(x) or \
                (y is not None and contrib_framework.is_tensor(y)):
            raise ValueError('Inputs cannot be tensors. Please provide input_fn.')
        
        if feed_fn is not None:
            raise ValueError('Can not provide both feed_fn and x or y.')
        
        df = data_feeder.setup_train_data_feeder(
            x, y, n_classes=None, batch_size=batch_size, shuffle=shuffle, epochs=epochs)
        
        return df.input_builder, df.get_feed_dict_fn()
    
    if (x is not None) or (y is not None):
        raise ValueError('Can not provide both input_fn and x or y.')
    if batch_size is not None:
        raise ValueError('Can not provide both input_fn and batch_size.')
    
    return input_fn, feed_fn

def infer_real_valued_columns_from_input_fn(input_fn):
    """Creates `FeatureColumn` objects for inputs defined by `input_fn`.
  
    This interprets all inputs as dense, fixed-length float values. This creates
    a local graph in which it calls `input_fn` to build the tensors, then discards
    it.
  
    Args:
      input_fn: Input function returning a tuple of:
          features - Dictionary of string feature name to `Tensor` or `Tensor`.
          target - `Tensor` of target objects.
  
    Returns:
      List of `FeatureColumn` objects.
    """
    with ops.Graph().as_default():
        features, _ = input_fn()
        return layers.infer_real_valued_columns(features)

def infer_real_valued_columns_from_input(x):
    """Creates `FeatureColumn` objects for inputs defined by input `x`.
  
    This interprets all inputs as dense, fixed-length float values.
  
    Args:
      x: Real-valued matrix of shape [n_samples, n_features...]. Can be
         iterator that returns arrays of features.
  
    Returns:
      List of `FeatureColumn` objects.
    """
    input_fn, _ = _get_input_fn(
        x=x, y=None, input_fn=None, feed_fn=None, batch_size=None)
    return infer_real_valued_columns_from_input_fn(input_fn)

def _get_arguments(func):
    """Returns list of arguments this function has."""
    if hasattr(func, '__code__'):
        # Regular function.
        return inspect.getargspec(func).args
    elif hasattr(func, '__call__'):
        # Callable object.
        return _get_arguments(func.__call__)
    elif hasattr(func, 'func'):
        # Partial function.
        return _get_arguments(func.func)

def _get_replica_device_setter(config):
    """Creates a replica device setter if required.
  
    Args:
      config: A RunConfig instance.
  
    Returns:
      A replica device setter, or None.
    """
    ps_ops = ['Variable', 'AutoReloadVariable',
              'MutableHashTable', 'MutableHashTableOfTensors']
    
    if config.job_name:
        worker_device = '/job:%s/task:%d' % (config.job_name, config.task)
    else:
        worker_device = '/job:worker'
    
    if config.num_ps_replicas > 0:
        return device_setter.replica_device_setter(
            ps_tasks=config.num_ps_replicas, worker_device=worker_device,
            merge_devices=False, ps_ops=ps_ops, cluster=config.cluster_spec)
    else:
        return None

def _make_metrics_ops(metrics, features, targets, predictions):
    """Add metrics to run on features, targets, and predictions dicts or tensors.
  
    `metrics` contains a specification for how to run metrics. It is a dict
    mapping friendly names to either `MetricSpec` objects, or directly to a metric
    function (assuming that predictions and targets are single tensors), or to
    a `(pred_name, metric)` tuples, which passes `predictions[pred_name]` and
    targets to `metric` (assuming targets is a single tensor).
  
    Users are encouraged to use `MetricSpec` objects, which are more flexible and
    cleaner. They also lead to clearer errors.
  
    Args:
      metrics: A dict mapping names to metrics specification, for example
        `MetricSpec` objects.
      features: A dict of tensors returned from an input_fn as features/inputs.
      targets: A single tensor or a dict of tensors returned from an input_fn as
        labels.
      predictions: A single tensor or a dict of tensors output from a model as
        predictions.
  
    Returns:
      `dict` whose keys are summary names, and values are the result of the
      metric, either:
        - `Tensor` values (in which case only the result of the last eval batch
          will be summarized).
        - `tuple` of 2 `Tensor` objects, update op and value. The update op will
          be run once each eval step, and the value written to summary.
  
    Raises:
      ValueError: If metrics specifications do not work with the type of
        features/targets/predictions provided. Mostly, a dict is given but no
        pred_name specified.
    """
    metrics = metrics or {}
    if isinstance(targets, dict) and len(targets) == 1:
        # Unpack single target into just tensor.
        targets = targets[list(targets.keys())[0]]
    result = {}
    for name, metric in six.iteritems(metrics):
        if isinstance(metric, metric_spec.MetricSpec):
            result[name] = metric.create_metric_ops(features, targets, predictions)
            continue
        
        # TODO(b/31229024): Remove the rest of this loop
        logging.warning('Please specify metrics using MetricSpec. Using bare '
                        'functions or (key, fn) tuples is deprecated and support '
                        'for it will be removed on Oct 1, 2016.')
        
        if isinstance(name, tuple):
            # Multi-head metrics.
            if len(name) != 2:
                raise ValueError('Invalid metric for {}. It returned a tuple with '
                                 'len {}, expected 2.'.format(name, len(name)))
            if not isinstance(predictions, dict):
                raise ValueError(
                    'Metrics passed provide (name, prediction), '
                    'but predictions are not dict. '
                    'Metrics: %s, Predictions: %s.' % (metrics, predictions))
            # Here are two options: targets are single Tensor or a dict.
            if isinstance(targets, dict) and name[1] in targets:
                # If targets are dict and the prediction name is in it, apply metric.
                result[name[0]] = metric(predictions[name[1]], targets[name[1]])
            else:
                # Otherwise pass the targets to the metric.
                result[name[0]] = metric(predictions[name[1]], targets)
        else:
            # Single head metrics.
            if isinstance(predictions, dict):
                raise ValueError(
                    'Metrics passed provide only name, no prediction, '
                    'but predictions are dict. '
                    'Metrics: %s, Targets: %s.' % (metrics, targets))
            result[name] = metric(predictions, targets)
    return result

def _maybe_add_streaming_mean(result, key, value):
    if key in result:
        logging.warning('Metrics already contains %s, skipping.', key)
        return
    result[key] = metrics_lib.streaming_mean(value)

class BaseEstimator(
    sklearn.BaseEstimator, evaluable.Evaluable, trainable.Trainable):
    """Abstract BaseEstimator class to train and evaluate TensorFlow models.
  
    Concrete implementation of this class should provide the following functions:
  
      * _get_train_ops
      * _get_eval_ops
      * _get_predict_ops
  
    `Estimator` implemented below is a good example of how to use this class.
    """
    __metaclass__ = abc.ABCMeta
    
    # Note that for Google users, this is overriden with
    # learn_runner.EstimatorConfig.
    # TODO(wicke): Remove this once launcher takes over config functionality
    _Config = run_config.RunConfig  # pylint: disable=invalid-name
    
    def __init__(self, model_dir=None, config=None):
        """Initializes a BaseEstimator instance.
    
        Args:
          model_dir: Directory to save model parameters, graph and etc. This can
            also be used to load checkpoints from the directory into a estimator to
            continue training a previously saved model.
          config: A RunConfig instance.
        """
        # Model directory.
        self._model_dir = model_dir
        if self._model_dir is None:
            self._model_dir = tempfile.mkdtemp()
            logging.warning('Using temporary folder as model directory: %s',
                            self._model_dir)
        
        # Create a run configuration.
        if config is None:
            self._config = BaseEstimator._Config()
            logging.info('Using default config.')
        else:
            self._config = config
        logging.info('Using config: %s', str(vars(self._config)))
        
        # Set device function depending if there are replicas or not.
        self._device_fn = _get_replica_device_setter(self._config)
        
        # Features and targets TensorSignature objects.
        # TODO(wicke): Rename these to something more descriptive
        self._features_info = None
        self._targets_info = None
        
        self._graph = None
    
    @property
    def config(self):
        # TODO(wicke): make RunConfig immutable, and then return it without a copy.
        return copy.deepcopy(self._config)
    
    def fit(self, x=None, y=None, input_fn=None, steps=None, batch_size=None,
            monitors=None, max_steps=None):
        # pylint: disable=g-doc-args,g-doc-return-or-yield
        """See `Trainable`.
    
        Raises:
          ValueError: If `x` or `y` are not `None` while `input_fn` is not `None`.
          ValueError: If both `steps` and `max_steps` are not `None`.
        """
        if (steps is not None) and (max_steps is not None):
            raise ValueError('Can not provide both steps and max_steps.')
        
        input_fn, feed_fn = _get_input_fn(x, y, input_fn, feed_fn=None,
                                          batch_size=batch_size, shuffle=True,
                                          epochs=None)
        loss = self._train_model(input_fn=input_fn,
                                 feed_fn=feed_fn,
                                 steps=steps,
                                 monitors=monitors,
                                 max_steps=max_steps)
        logging.info('Loss for final step: %s.', loss)
        return self
    
    def partial_fit(
            self, x=None, y=None, input_fn=None, steps=1, batch_size=None,
            monitors=None):
        """Incremental fit on a batch of samples.
    
        This method is expected to be called several times consecutively
        on different or the same chunks of the dataset. This either can
        implement iterative training or out-of-core/online training.
    
        This is especially useful when the whole dataset is too big to
        fit in memory at the same time. Or when model is taking long time
        to converge, and you want to split up training into subparts.
    
        Args:
          x: Matrix of shape [n_samples, n_features...]. Can be iterator that
             returns arrays of features. The training input samples for fitting the
             model. If set, `input_fn` must be `None`.
          y: Vector or matrix [n_samples] or [n_samples, n_outputs]. Can be
             iterator that returns array of targets. The training target values
             (class labels in classification, real numbers in regression). If set,
             `input_fn` must be `None`.
          input_fn: Input function. If set, `x`, `y`, and `batch_size` must be
            `None`.
          steps: Number of steps for which to train model. If `None`, train forever.
          batch_size: minibatch size to use on the input, defaults to first
            dimension of `x`. Must be `None` if `input_fn` is provided.
          monitors: List of `BaseMonitor` subclass instances. Used for callbacks
            inside the training loop.
    
        Returns:
          `self`, for chaining.
    
        Raises:
          ValueError: If at least one of `x` and `y` is provided, and `input_fn` is
              provided.
        """
        logging.warning('The current implementation of partial_fit is not optimized'
                        ' for use in a loop. Consider using fit() instead.')
        return self.fit(x=x, y=y, input_fn=input_fn, steps=steps,
                        batch_size=batch_size, monitors=monitors)
    
    def evaluate(
            self, x=None, y=None, input_fn=None, feed_fn=None, batch_size=None,
            steps=None, metrics=None, name=None):
        # pylint: disable=g-doc-args,g-doc-return-or-yield
        """See `Evaluable`.
    
        Raises:
          ValueError: If at least one of `x` or `y` is provided, and at least one of
              `input_fn` or `feed_fn` is provided.
              Or if `metrics` is not `None` or `dict`.
        """
        input_fn, feed_fn = _get_input_fn(x, y, input_fn=input_fn,
                                          feed_fn=feed_fn, batch_size=batch_size,
                                          shuffle=False, epochs=1)
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError('Metrics argument should be None or dict. '
                             'Got %s.' % metrics)
        eval_results, global_step = self._evaluate_model(input_fn=input_fn,
                                                         feed_fn=feed_fn,
                                                         steps=steps,
                                                         metrics=metrics,
                                                         name=name)
        if eval_results is not None:
            eval_results.update({'global_step': global_step})
        return eval_results
    
    @deprecated_arg_values(
        AS_ITERABLE_DATE, AS_ITERABLE_INSTRUCTIONS, as_iterable=False)
    def predict(
            self, x=None, input_fn=None, batch_size=None, outputs=None,
            as_iterable=True):
        """Returns predictions for given features.
    
        Args:
          x: Matrix of shape [n_samples, n_features...]. Can be iterator that
             returns arrays of features. The training input samples for fitting the
             model. If set, `input_fn` must be `None`.
          input_fn: Input function. If set, `x` and 'batch_size' must be `None`.
          batch_size: Override default batch size. If set, 'input_fn' must be
            'None'.
          outputs: list of `str`, name of the output to predict.
            If `None`, returns all.
          as_iterable: If True, return an iterable which keeps yielding predictions
            for each example until inputs are exhausted. Note: The inputs must
            terminate if you want the iterable to terminate (e.g. be sure to pass
            num_epochs=1 if you are using something like read_batch_features).
    
        Returns:
          A numpy array of predicted classes or regression values if the
          constructor's `model_fn` returns a `Tensor` for `predictions` or a `dict`
          of numpy arrays if `model_fn` returns a `dict`. Returns an iterable of
          predictions if as_iterable is True.
    
        Raises:
          ValueError: If x and input_fn are both provided or both `None`.
        """
        input_fn, feed_fn = _get_input_fn(
            x, None, input_fn=input_fn, feed_fn=None, batch_size=batch_size,
            shuffle=False, epochs=1)
        return self._infer_model(
            input_fn=input_fn, feed_fn=feed_fn, outputs=outputs,
            as_iterable=as_iterable)
    
    def get_variable_value(self, name):
        """Returns value of the variable given by name.
    
        Args:
          name: string, name of the tensor.
    
        Returns:
          Numpy array - value of the tensor.
        """
        return load_variable(self.model_dir, name)
    
    def get_variable_names(self):
        """Returns list of all variable names in this model.
    
        Returns:
          List of names.
        """
        return [name for name, _ in list_variables(self.model_dir)]
    
    @property
    def model_dir(self):
        return self._model_dir
    
    @deprecated_arg_values(
        '2016-09-23',
        'The signature of the input_fn accepted by export is changing to be '
        'consistent with what\'s used by tf.Learn Estimator\'s train/evaluate. '
        'input_fn (and in most cases, input_feature_key) will become required '
        'args, and use_deprecated_input_fn will default to False and be removed '
        'altogether.',
        use_deprecated_input_fn=True,
        input_fn=None)
    def export(self,
               export_dir,
               input_fn=export._default_input_fn,  # pylint: disable=protected-access
               input_feature_key=None,
               use_deprecated_input_fn=True,
               signature_fn=None,
               prediction_key=None,
               default_batch_size=1,
               exports_to_keep=None):
        """Exports inference graph into given dir.
    
        Args:
          export_dir: A string containing a directory to write the exported graph
            and checkpoints.
          input_fn: If `use_deprecated_input_fn` is true, then a function that given
            `Tensor` of `Example` strings, parses it into features that are then
            passed to the model. Otherwise, a function that takes no argument and
            returns a tuple of (features, targets), where features is a dict of
            string key to `Tensor` and targets is a `Tensor` that's currently not
            used (and so can be `None`).
          input_feature_key: Only used if `use_deprecated_input_fn` is false. String
            key into the features dict returned by `input_fn` that corresponds to a
            the raw `Example` strings `Tensor` that the exported model will take as
            input. Can only be `None` if you're using a custom `signature_fn` that
            does not use the first arg (examples).
          use_deprecated_input_fn: Determines the signature format of `input_fn`.
          signature_fn: Function that returns a default signature and a named
            signature map, given `Tensor` of `Example` strings, `dict` of `Tensor`s
            for features and `Tensor` or `dict` of `Tensor`s for predictions.
          prediction_key: The key for a tensor in the `predictions` dict (output
            from the `model_fn`) to use as the `predictions` input to the
            `signature_fn`. Optional. If `None`, predictions will pass to
            `signature_fn` without filtering.
          default_batch_size: Default batch size of the `Example` placeholder.
          exports_to_keep: Number of exports to keep.
    
        Returns:
          The string path to the exported directory. NB: this functionality was
          added ca. 2016/09/25; clients that depend on the return value may need
          to handle the case where this function returns None because subclasses
          are not returning a value.
        """
        # pylint: disable=protected-access
        return export._export_estimator(
            estimator=self,
            export_dir=export_dir,
            signature_fn=signature_fn,
            prediction_key=prediction_key,
            input_fn=input_fn,
            input_feature_key=input_feature_key,
            use_deprecated_input_fn=use_deprecated_input_fn,
            default_batch_size=default_batch_size,
            exports_to_keep=exports_to_keep)
        # pylint: enable=protected-access
    
    @abc.abstractproperty
    def _get_train_ops(self, features, targets):
        """Method that builds model graph and returns trainer ops.
    
        Expected to be overriden by sub-classes that require custom support.
    
        Args:
          features: `Tensor` or `dict` of `Tensor` objects.
          targets: `Tensor` or `dict` of `Tensor` objects.
    
        Returns:
          Tuple of train `Operation` and loss `Tensor`.
        """
        pass
    
    @abc.abstractproperty
    def _get_predict_ops(self, features):
        """Method that builds model graph and returns prediction ops.
    
        Args:
          features: `Tensor` or `dict` of `Tensor` objects.
    
        Returns:
          predictions: `Tensor` or `dict` of `Tensor` objects.
        """
        pass
    
    def _get_eval_ops(self, features, targets, metrics):
        """Method that builds model graph and returns evaluation ops.
    
        Expected to be overriden by sub-classes that require custom support.
    
        Args:
          features: `Tensor` or `dict` of `Tensor` objects.
          targets: `Tensor` or `dict` of `Tensor` objects.
          metrics: Dict of metrics to run. If `None`, the default metric functions
            are used; if {}, no metrics are used. Otherwise, `metrics` should map
            friendly names for the metric to a `MetricSpec` object defining which
            model outputs to evaluate against which targets with which metric
            function. Metric ops should support streaming, e.g., returning
            update_op and value tensors. See more details in
            `../../../../metrics/python/metrics/ops/streaming_metrics.py` and
            `../metric_spec.py`.
    
        Returns:
          metrics: `dict` of `Tensor` objects.
        """
        raise NotImplementedError('_get_eval_ops not implemented in BaseEstimator')
    
    @deprecated(
        '2016-09-23',
        'The signature of the input_fn accepted by export is changing to be '
        'consistent with what\'s used by tf.Learn Estimator\'s train/evaluate, '
        'which makes this function useless. This will be removed after the '
        'deprecation date.')
    def _get_feature_ops_from_example(self, examples_batch):
        """Returns feature parser for given example batch using features info.
    
        This function requires `fit()` has been called.
    
        Args:
          examples_batch: batch of tf.Example
    
        Returns:
          features: `Tensor` or `dict` of `Tensor` objects.
    
        Raises:
          ValueError: If `_features_info` attribute is not available (usually
          because `fit()` has not been called).
        """
        if self._features_info is None:
            raise ValueError('Features information missing, was fit() ever called?')
        return tensor_signature.create_example_parser_from_signatures(
            self._features_info, examples_batch)
    
    def _check_inputs(self, features, targets):
        if self._features_info is not None:
            logging.debug('Given features: %s, required signatures: %s.',
                          str(features), str(self._features_info))
            if not tensor_signature.tensors_compatible(features, self._features_info):
                raise ValueError('Features are incompatible with given information. '
                                 'Given features: %s, required signatures: %s.' %
                                 (str(features), str(self._features_info)))
        else:
            self._features_info = tensor_signature.create_signatures(features)
            logging.debug('Setting feature info to %s.', str(self._features_info))
        if targets is not None:
            if self._targets_info is not None:
                logging.debug('Given targets: %s, required signatures: %s.',
                              str(targets), str(self._targets_info))
                if not tensor_signature.tensors_compatible(targets, self._targets_info):
                    raise ValueError('Targets are incompatible with given information. '
                                     'Given targets: %s, required signatures: %s.' %
                                     (str(targets), str(self._targets_info)))
            else:
                self._targets_info = tensor_signature.create_signatures(targets)
                logging.debug('Setting targets info to %s', str(self._targets_info))
    
    def _train_model(self,
                     input_fn,
                     steps,
                     feed_fn=None,
                     init_op=None,
                     init_feed_fn=None,
                     init_fn=None,
                     device_fn=None,
                     monitors=None,
                     log_every_steps=100,
                     fail_on_nan_loss=True,
                     max_steps=None):
        # TODO(wicke): Remove this once Model and associated code are gone.
        if hasattr(self._config, 'execution_mode'):
            if self._config.execution_mode not in ('all', 'train'):
                return
            
            # Stagger startup of worker sessions based on task id.
            sleep_secs = min(
                self._config.training_worker_max_startup_secs,
                self._config.task *
                self._config.training_worker_session_startup_stagger_secs)
            if sleep_secs:
                logging.info('Waiting %d secs before starting task %d.', sleep_secs,
                             self._config.task)
                time.sleep(sleep_secs)
        
        # Device allocation
        device_fn = device_fn or self._device_fn
        
        self._graph = ops.Graph()
        with self._graph.as_default() as g, g.device(device_fn):
            random_seed.set_random_seed(self._config.tf_random_seed)
            global_step = contrib_framework.create_global_step(g)
            features, targets = input_fn()
            self._check_inputs(features, targets)
            train_op, loss_op = self._get_train_ops(features, targets)
            
            # Add default monitors.
            if monitors is None:
                monitors = []
            
            hooks = [m for m in monitors
                     if isinstance(m, session_run_hook.SessionRunHook)]
            
            deprecated_monitors = [
                m for m in monitors
                if not isinstance(m, session_run_hook.SessionRunHook)
                ]
            
            supervisor_is_chief = self._config.is_chief
            if not supervisor_is_chief:
                # Prune list of monitor to the ones runnable on all workers.
                deprecated_monitors = [m for m in deprecated_monitors
                                       if m.run_on_all_workers]
            
            # Setup monitors.
            for monitor in deprecated_monitors:
                monitor.set_estimator(self)
            
            if deprecated_monitors:
                hooks.append(monitor_lib.RunHookAdapterForMonitors(deprecated_monitors))
            
            ops.add_to_collection(ops.GraphKeys.LOSSES, loss_op)
            return graph_actions._monitored_train(  # pylint: disable=protected-access
                graph=g,
                output_dir=self._model_dir,
                train_op=train_op,
                loss_op=loss_op,
                global_step_tensor=global_step,
                init_op=init_op,
                init_feed_dict=init_feed_fn() if init_feed_fn is not None else None,
                init_fn=init_fn,
                log_every_steps=log_every_steps,
                supervisor_is_chief=supervisor_is_chief,
                supervisor_master=self._config.master,
                supervisor_save_model_secs=self._config.save_checkpoints_secs,
                supervisor_save_model_steps=self._config.save_checkpoints_steps,
                supervisor_save_summaries_steps=self._config.save_summary_steps,
                keep_checkpoint_max=self._config.keep_checkpoint_max,
                feed_fn=feed_fn,
                steps=steps,
                fail_on_nan_loss=fail_on_nan_loss,
                hooks=hooks,
                max_steps=max_steps)
    
    def _extract_metric_update_ops(self, eval_dict):
        """Separate update operations from metric value operations."""
        update_ops = []
        value_ops = {}
        for name, metric_ops in eval_dict.items():
            if isinstance(metric_ops, (list, tuple)):
                if len(metric_ops) == 2:
                    value_ops[name] = metric_ops[0]
                    update_ops.append(metric_ops[1])
                else:
                    logging.warning(
                        'Ignoring metric {}. It returned a list|tuple with len {}, '
                        'expected 2'.format(name, len(metric_ops)))
                    value_ops[name] = metric_ops
            else:
                value_ops[name] = metric_ops
        
        if update_ops:
            update_ops = control_flow_ops.group(*update_ops)
        else:
            update_ops = None
        
        return update_ops, value_ops
    
    def _evaluate_model(self,
                        input_fn,
                        steps,
                        feed_fn=None,
                        metrics=None,
                        name=''):
        # TODO(wicke): Remove this once Model and associated code are gone.
        if (hasattr(self._config, 'execution_mode') and
                    self._config.execution_mode not in ('all', 'evaluate', 'eval_evalset')):
            return None, None
        
        # Check that model has been trained.
        checkpoint_path = self._model_dir
        latest_path = saver.latest_checkpoint(checkpoint_path)
        if not latest_path:
            raise NotFittedError("Couldn't find trained model at %s."
                                 % checkpoint_path)
        # Setup output directory.
        eval_dir = os.path.join(self._model_dir, 'eval' if not name else 'eval_' + name)
        
        with ops.Graph().as_default() as g:
            random_seed.set_random_seed(self._config.tf_random_seed)
            global_step = contrib_framework.create_global_step(g)
            features, targets = input_fn()
            self._check_inputs(features, targets)
            eval_dict = self._get_eval_ops(features, targets, metrics)
            update_op, eval_dict = self._extract_metric_update_ops(eval_dict)
            eval_results, current_global_step = graph_actions.evaluate(
                graph=g,
                output_dir=eval_dir,
                checkpoint_path=checkpoint_path,
                eval_dict=eval_dict,
                update_op=update_op,
                global_step_tensor=global_step,
                supervisor_master=self._config.evaluation_master,
                feed_fn=feed_fn,
                max_steps=steps)
            
            return eval_results, current_global_step
    
    def _get_features_from_input_fn(self, input_fn):
        result = input_fn()
        if isinstance(result, (list, tuple)):
            return result[0]
        return result
    
    def _infer_model(self, input_fn, feed_fn=None, outputs=None, as_iterable=True):
        # Check that model has been trained.
        checkpoint_path = saver.latest_checkpoint(self._model_dir)
        if not checkpoint_path:
            raise NotFittedError("Couldn't find trained model at %s."
                                 % self._model_dir)
        
        with ops.Graph().as_default() as g:
            random_seed.set_random_seed(self._config.tf_random_seed)
            contrib_framework.create_global_step(g)
            features = self._get_features_from_input_fn(input_fn)
            predictions = self._get_predict_ops(features)
            # If predictions is single output - wrap it into dict, and remember to
            # return not a dict.
            return_dict = isinstance(predictions, dict)
            if not return_dict:
                predictions = {'predictions': predictions}
            
            # Filter what to run predictions on, if outputs provided.
            if outputs:
                existing_keys = predictions.keys()
                predictions = {
                    key: value for key, value in predictions.items() if key in outputs
                    }
                if not predictions:
                    raise ValueError('Expected to run at least one output from %s, '
                                     'provided %s.' % (existing_keys, outputs))
            
            if as_iterable:
                return self._infer_model_as_iterable(
                    checkpoint_path, predictions, feed_fn, return_dict)
            else:
                return self._infer_model_single(
                    checkpoint_path, predictions, feed_fn, return_dict)
    
    def _infer_model_single(
            self, checkpoint_path, predictions, feed_fn, return_dict):
        if feed_fn is None:
            preds = graph_actions.infer(checkpoint_path, predictions)
        else:
            def _feed_fn():
                while True:
                    yield feed_fn()
            
            outputs = graph_actions.run_feeds(
                output_dict=predictions,
                feed_dicts=_feed_fn(),
                restore_checkpoint_path=checkpoint_path)
            preds = {
                key: np.concatenate([output[key] for output in outputs], axis=0)
                for key in predictions}
        
        return preds if return_dict else preds['predictions']
    
    def _infer_model_as_iterable(
            self, checkpoint_path, predictions, feed_fn, return_dict):
        if feed_fn is None:
            feed_dicts = itertools.repeat(None)
        else:
            def _feed_fn():
                while True:
                    yield feed_fn()
            
            feed_dicts = _feed_fn()
        
        try:
            for output_batch in graph_actions.run_feeds_iter(
                    output_dict=predictions,
                    feed_dicts=feed_dicts,
                    restore_checkpoint_path=checkpoint_path):
                # Unpack batches into individual predictions
                if return_dict:
                    batch_length = list(output_batch.values())[0].shape[0]
                    for i in range(batch_length):
                        yield {key: value[i] for key, value in output_batch.items()}
                else:
                    for pred in output_batch['predictions']:
                        yield pred
        
        except errors.OutOfRangeError:
            # We fall out of the above loop naturally if feed_fn raises StopIteration,
            # or we catch an OutOfRangeError if we've reached the end of inputs.
            logging.info('Reached end of inputs for predict_iter.')

def _identity_feature_engineering_fn(features, targets):
    return features, targets

class Estimator(BaseEstimator):
    """Estimator class is the basic TensorFlow model trainer/evaluator.
    """
    
    def __init__(self,
                 model_fn=None,
                 model_dir=None,
                 config=None,
                 params=None,
                 feature_engineering_fn=None):
        """Constructs an Estimator instance.
    
        Args:
          model_fn: Model function, takes features and targets tensors or dicts of
                    tensors and returns tuple of:
    
              * predictions: `Tensor`, `SparseTensor` or dictionary of same.
                  Can also be any type that is convertible to a `Tensor` or
                  `SparseTensor`, or dictionary of same.
              * loss: Scalar loss `Tensor`.
              * train_op: Training update `Tensor` or `Operation`.
    
             Supports next three signatures for the function:
    
              * `(features, targets) -> (predictions, loss, train_op)`
              * `(features, targets, mode) -> (predictions, loss, train_op)`
              * `(features, targets, mode, params) -> (predictions, loss, train_op)`
    
            Where
    
              * `features` are single `Tensor` or `dict` of `Tensor`s
                     (depending on data passed to `fit`),
              * `targets` are `Tensor` or `dict` of `Tensor`s (for multi-head
                     models). If mode is `ModeKeys.INFER`, `targets=None` will be
                     passed. If the `model_fn`'s signature does not accept
                     `mode`, the `model_fn` must still be able to handle
                     `targets=None`.
              * `mode` represents if this training, evaluation or
                     prediction. See `ModeKeys`.
              * `params` is a `dict` of hyperparameters. Will receive what
                     is passed to Estimator in `params` parameter. This allows
                     to configure Estimators from hyper parameter tunning.
    
          model_dir: Directory to save model parameters, graph and etc. This can
            also be used to load checkpoints from the directory into a estimator to
            continue training a previously saved model.
          config: Configuration object.
          params: `dict` of hyper parameters that will be passed into `model_fn`.
                  Keys are names of parameters, values are basic python types.
          feature_engineering_fn: Feature engineering function. Takes features and
                                  targets which are the output of `input_fn` and
                                  returns features and targets which will be fed
                                  into `model_fn`. Please check `model_fn` for
                                  a definition of features and targets.
    
        Raises:
          ValueError: parameters of `model_fn` don't match `params`.
        """
        super(Estimator, self).__init__(model_dir=model_dir, config=config)
        if model_fn is not None:
            # Check number of arguments of the given function matches requirements.
            model_fn_args = _get_arguments(model_fn)
            if params is not None and 'params' not in model_fn_args:
                raise ValueError('Estimator\'s model_fn (%s) has less than 4 '
                                 'arguments, but not None params (%s) are passed.' %
                                 (model_fn, params))
            if params is None and 'params' in model_fn_args:
                logging.warning('Estimator\'s model_fn (%s) includes params '
                                'argument, but params are not passed to Estimator.',
                                model_fn)
        self._model_fn = model_fn
        self.params = params
        self._feature_engineering_fn = (
            feature_engineering_fn or _identity_feature_engineering_fn)
    
    def _call_model_fn(self, features, targets, mode):
        """Calls model function with support of 2, 3 or 4 arguments."""
        features, targets = self._feature_engineering_fn(features, targets)
        model_fn_args = _get_arguments(self._model_fn)
        if 'mode' in model_fn_args:
            if 'params' in model_fn_args:
                predictions, loss, train_op = self._model_fn(
                    features, targets, mode=mode, params=self.params)
            else:
                predictions, loss, train_op = self._model_fn(
                    features, targets, mode=mode)
        else:
            predictions, loss, train_op = self._model_fn(features, targets)
        
        # Validate train_op.
        if train_op is None:
            if mode == ModeKeys.TRAIN:
                raise ValueError('Missing train_op.')
        elif not isinstance(train_op, ops.Operation):
            train_op = ops.convert_to_tensor(train_op).op
        
        # Validate loss.
        if loss is None:
            if mode in (ModeKeys.TRAIN, ModeKeys.EVAL):
                raise ValueError('Missing loss.')
        else:
            loss = ops.convert_to_tensor(loss)
            loss_shape = loss.get_shape()
            if loss_shape.num_elements() not in (None, 1):
                raise ValueError('Loss must be scalar: %s.' % loss)
            if not loss_shape.is_compatible_with(tensor_shape.scalar()):
                loss = array_ops.reshape(loss, [])
        
        # Validate predictions.
        if predictions is None:
            if mode == ModeKeys.INFER:
                raise ValueError('Missing predictions.')
        else:
            if isinstance(predictions, dict):
                predictions = {
                    k: contrib_framework.convert_to_tensor_or_sparse_tensor(v)
                    for k, v in six.iteritems(predictions)
                    }
            else:
                predictions = contrib_framework.convert_to_tensor_or_sparse_tensor(
                    predictions)
        
        return predictions, loss, train_op
    
    def _get_train_ops(self, features, targets):
        """Method that builds model graph and returns trainer ops.
    
        Expected to be overriden by sub-classes that require custom support.
        This implementation uses `model_fn` passed as parameter to constructor to
        build model.
    
        Args:
          features: `Tensor` or `dict` of `Tensor` objects.
          targets: `Tensor` or `dict` of `Tensor` objects.
    
        Returns:
          Tuple of train `Operation` and loss `Tensor`.
        """
        _, loss, train_op = self._call_model_fn(features, targets, ModeKeys.TRAIN)
        return train_op, loss
    
    def _get_eval_ops(self, features, targets, metrics):
        """Method that builds model graph and returns evaluation ops.
    
        Expected to be overriden by sub-classes that require custom support.
        This implementation uses `model_fn` passed as parameter to constructor to
        build model.
    
        Args:
          features: `Tensor` or `dict` of `Tensor` objects.
          targets: `Tensor` or `dict` of `Tensor` objects.
          metrics: Dict of metrics to run. If None, the default metric functions
            are used; if {}, no metrics are used. Otherwise, `metrics` should map
            friendly names for the metric to a `MetricSpec` object defining which
            model outputs to evaluate against which targets with which metric
            function. Metric ops should support streaming, e.g., returning
            update_op and value tensors. See more details in
            `../../../../metrics/python/metrics/ops/streaming_metrics.py` and
            `../metric_spec.py`.
    
        Returns:
          `dict` whose keys are summary names, and values are either:
            - `Tensor` values (in which case only the result of the last eval batch
              will be summarized).
            - `tuple` of 2 `Tensor` objects, update op and value. The update op will
              be run once each eval step, and the value written to summary.
    
        Raises:
          ValueError: if `metrics` don't match `targets`.
        """
        predictions, loss, _ = self._call_model_fn(features, targets, ModeKeys.EVAL)
        result = _make_metrics_ops(metrics, features, targets, predictions)
        _maybe_add_streaming_mean(result, 'loss', loss)
        
        # TODO(ptucker): Work-around until we have an easier way to specify metrics
        # from model_fn.
        if predictions is not None:
            if isinstance(predictions, dict):
                for k, v in six.iteritems(predictions):
                    _maybe_add_streaming_mean(result, k, v)
            else:
                _maybe_add_streaming_mean(result, 'predictions', predictions)
        
        return result
    
    def _get_predict_ops(self, features):
        """Method that builds model graph and returns prediction ops.
    
        Expected to be overriden by sub-classes that require custom support.
        This implementation uses `model_fn` passed as parameter to constructor to
        build model.
    
        Args:
          features: `Tensor` or `dict` of `Tensor` objects.
    
        Returns:
          predictions: `Tensor` or `dict` of `Tensor` objects.
        """
        targets = tensor_signature.create_placeholders_from_signatures(
            self._targets_info)
        predictions, _, _ = self._call_model_fn(features, targets, ModeKeys.INFER)
        return predictions
