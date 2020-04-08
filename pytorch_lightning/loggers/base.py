import argparse
import functools
import operator
from abc import ABC, abstractmethod
from argparse import Namespace
from functools import wraps
from typing import Union, Optional, Dict, Iterable, Any, Callable, List, Sequence, Mapping, Tuple

import numpy as np
import torch


def rank_zero_only(fn: Callable):
    """Decorate a logger method to run it only on the process with rank 0.

    Args:
        fn: Function to decorate
    """

    @wraps(fn)
    def wrapped_fn(self, *args, **kwargs):
        if self.rank == 0:
            fn(self, *args, **kwargs)

    return wrapped_fn


class LightningLoggerBase(ABC):
    """Base class for experiment loggers."""

    def __init__(
            self,
            agg_key_funcs: Optional[Mapping[str, Callable[[Sequence[float]], float]]] = None,
            agg_default_func: Callable[[Sequence[float]], float] = np.mean
    ):
        """
        Args:
            agg_key_funcs:
                Dictionary which maps a metric name to a function, which will
                aggregate the metric values for the same steps.
            agg_default_func:
                Default function to aggregate metric values. If some metric name
                is not presented in the `agg_key_funcs` dictionary, then the
                `agg_default_func` will be used for aggregation.

        Notes:
            `agg_key_funcs` and `agg_default_func` are used only when one logs metrics with
            `LightningLoggerBase.agg_and_log_metrics` method.
        """
        self._rank = 0
        self._prev_step = -1
        self._metrics_to_agg: List[Dict[str, float]] = []
        self._agg_key_funcs = agg_key_funcs if agg_key_funcs else {}
        self._agg_default_func = agg_default_func

    def update_agg_funcs(
            self,
            agg_key_funcs: Optional[Mapping[str, Callable[[Sequence[float]], float]]] = None,
            agg_default_func: Callable[[Sequence[float]], float] = np.mean
    ):
        """Update aggregation methods.

        Args:
            agg_key_funcs:
                Dictionary which maps a metric name to a function, which will
                aggregate the metric values for the same steps.
            agg_default_func:
                Default function to aggregate metric values. If some metric name
                is not presented in the `agg_key_funcs` dictionary, then the
                `agg_default_func` will be used for aggregation.
        """
        if agg_key_funcs:
            self._agg_key_funcs.update(agg_key_funcs)
        if agg_default_func:
            self._agg_default_func = agg_default_func

    @property
    @abstractmethod
    def experiment(self) -> Any:
        """Return the experiment object associated with this logger"""

    def _aggregate_metrics(
            self, metrics: Dict[str, float], step: Optional[int] = None
    ) -> Tuple[int, Optional[Dict[str, float]]]:
        """Aggregates metrics.

        Args:
            metrics: Dictionary with metric names as keys and measured quantities as values
            step: Step number at which the metrics should be recorded

        Returns:
            sStep and aggregated metrics. The return value could be None. In such case, metrics
            are added to the aggregation list, but not aggregated yet.
        """
        # if you still receiving metric from the same step, just accumulate it
        if step == self._prev_step:
            self._metrics_to_agg.append(metrics)
            return step, None

        # compute the metrics
        agg_step, agg_mets = self._finalize_agg_metrics()

        # as new step received reset accumulator
        self._metrics_to_agg = [metrics]
        self._prev_step = step
        return agg_step, agg_mets

    def _finalize_agg_metrics(self):
        """Aggregate accumulated metrics. This shall be called in close."""
        # compute the metrics
        if not self._metrics_to_agg:
            agg_mets = None
        elif len(self._metrics_to_agg) == 1:
            agg_mets = self._metrics_to_agg[0]
        else:
            agg_mets = merge_dicts(self._metrics_to_agg, self._agg_key_funcs, self._agg_default_func)
        return self._prev_step, agg_mets

    def agg_and_log_metrics(self, metrics: Dict[str, float], step: Optional[int] = None):
        """Aggregates and records metrics.
        This method doesn't log the passed metrics instantaneously, but instead
        it aggregates them and logs only if metrics are ready to be logged.

        Args:
            metrics: Dictionary with metric names as keys and measured quantities as values
            step: Step number at which the metrics should be recorded
        """
        agg_step, metrics_to_log = self._aggregate_metrics(metrics=metrics, step=step)

        if metrics_to_log is not None:
            self.log_metrics(metrics=metrics_to_log, step=agg_step)

    @abstractmethod
    def log_metrics(self, metrics: Dict[str, float], step: Optional[int] = None):
        """Records metrics.
        This method logs metrics as as soon as it received them. If you want to aggregate
        metrics for one specific `step`, use the `agg_and_log_metrics` method.

        Args:
            metrics: Dictionary with metric names as keys and measured quantities as values
            step: Step number at which the metrics should be recorded
        """
        pass

    @staticmethod
    def _convert_params(params: Union[Dict[str, Any], Namespace]) -> Dict[str, Any]:
        # in case converting from namespace
        if isinstance(params, Namespace):
            params = vars(params)

        if params is None:
            params = {}

        return params

    @staticmethod
    def _flatten_dict(params: Dict[str, Any], delimiter: str = '/') -> Dict[str, Any]:
        """Flatten hierarchical dict e.g. {'a': {'b': 'c'}} -> {'a/b': 'c'}.

        Args:
            params: Dictionary contains hparams
            delimiter: Delimiter to express the hierarchy. Defaults to '/'.

        Returns:
            Flatten dict.

        Examples:
            >>> LightningLoggerBase._flatten_dict({'a': {'b': 'c'}})
            {'a/b': 'c'}
            >>> LightningLoggerBase._flatten_dict({'a': {'b': 123}})
            {'a/b': 123}
        """

        def _dict_generator(input_dict, prefixes=None):
            prefixes = prefixes[:] if prefixes else []
            if isinstance(input_dict, dict):
                for key, value in input_dict.items():
                    if isinstance(value, (dict, Namespace)):
                        value = vars(value) if isinstance(value, Namespace) else value
                        for d in _dict_generator(value, prefixes + [key]):
                            yield d
                    else:
                        yield prefixes + [key, value if value is not None else str(None)]
            else:
                yield prefixes + [input_dict if input_dict is None else str(input_dict)]

        return {delimiter.join(keys): val for *keys, val in _dict_generator(params)}

    @staticmethod
    def _sanitize_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Returns params with non-primitvies converted to strings for logging

        >>> params = {"float": 0.3,
        ...           "int": 1,
        ...           "string": "abc",
        ...           "bool": True,
        ...           "list": [1, 2, 3],
        ...           "namespace": Namespace(foo=3),
        ...           "layer": torch.nn.BatchNorm1d}
        >>> import pprint
        >>> pprint.pprint(LightningLoggerBase._sanitize_params(params))  # doctest: +NORMALIZE_WHITESPACE
        {'bool': True,
         'float': 0.3,
         'int': 1,
         'layer': "<class 'torch.nn.modules.batchnorm.BatchNorm1d'>",
         'list': '[1, 2, 3]',
         'namespace': 'Namespace(foo=3)',
         'string': 'abc'}
        """
        return {k: v if type(v) in [bool, int, float, str, torch.Tensor] else str(v) for k, v in params.items()}

    @abstractmethod
    def log_hyperparams(self, params: argparse.Namespace):
        """Record hyperparameters.

        Args:
            params: argparse.Namespace containing the hyperparameters
        """

    def save(self) -> None:
        """Save log data."""
        pass

    def finalize(self, status: str) -> None:
        """Do any processing that is necessary to finalize an experiment.

        Args:
            status: Status that the experiment finished with (e.g. success, failed, aborted)
        """
        pass

    def close(self) -> None:
        """Do any cleanup that is necessary to close an experiment."""
        agg_step, metrics_to_log = self._finalize_agg_metrics()

        if metrics_to_log is not None:
            self.log_metrics(metrics=metrics_to_log, step=agg_step)

    @property
    def rank(self) -> int:
        """Process rank. In general, metrics should only be logged by the process with rank 0."""
        return self._rank

    @rank.setter
    def rank(self, value: int) -> None:
        """Set the process rank."""
        self._rank = value

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the experiment name."""

    @property
    @abstractmethod
    def version(self) -> Union[int, str]:
        """Return the experiment version."""


class LoggerCollection(LightningLoggerBase):
    """The `LoggerCollection` class is used to iterate all logging actions over the given `logger_iterable`.

    Args:
        logger_iterable: An iterable collection of loggers
    """

    def __init__(self, logger_iterable: Iterable[LightningLoggerBase]):
        super().__init__()
        self._logger_iterable = logger_iterable

    def __getitem__(self, index: int) -> LightningLoggerBase:
        return [logger for logger in self._logger_iterable][index]

    @property
    def experiment(self) -> List[Any]:
        return [logger.experiment for logger in self._logger_iterable]

    def log_metrics(self, metrics: Dict[str, float], step: Optional[int] = None) -> None:
        [logger.log_metrics(metrics, step) for logger in self._logger_iterable]

    def log_hyperparams(self, params: Union[Dict[str, Any], Namespace]) -> None:
        [logger.log_hyperparams(params) for logger in self._logger_iterable]

    def save(self) -> None:
        [logger.save() for logger in self._logger_iterable]

    def finalize(self, status: str) -> None:
        [logger.finalize(status) for logger in self._logger_iterable]

    def close(self) -> None:
        [logger.close() for logger in self._logger_iterable]

    @LightningLoggerBase.rank.setter
    def rank(self, value: int) -> None:
        self._rank = value
        for logger in self._logger_iterable:
            logger.rank = value

    @property
    def name(self) -> str:
        return '_'.join([str(logger.name) for logger in self._logger_iterable])

    @property
    def version(self) -> str:
        return '_'.join([str(logger.version) for logger in self._logger_iterable])


def merge_dicts(
        dicts: Sequence[Mapping],
        agg_key_funcs: Optional[Mapping[str, Callable[[Sequence[float]], float]]] = None,
        default_func: Callable[[Sequence[float]], float] = np.mean
) -> Dict:
    """Merge a sequence with dictionaries into one dictionary by aggregating the
    same keys with some given function.

    Args:
        dicts:
            Sequence of dictionaries to be merged.
        agg_key_funcs:
            Mapping from key name to function. This function will aggregate a
            list of values, obtained from the same key of all dictionaries.
            If some key has no specified aggregation function, the default one
            will be used. Default is: None (all keys will be aggregated by the
            default function).
        default_func:
            Default function to aggregate keys, which are not presented in the
            `agg_key_funcs` map.

    Returns:
        Dictionary with merged values.

    Examples:
        >>> import pprint
        >>> d1 = {'a': 1.7, 'b': 2.0, 'c': 1}
        >>> d2 = {'a': 1.1, 'b': 2.2, 'v': 1}
        >>> d3 = {'a': 1.1, 'v': 2.3}
        >>> dflt_func = min
        >>> agg_funcs = {'a': np.mean, 'v': max}
        >>> pprint.pprint(merge_dicts([d1, d2, d3], agg_funcs, dflt_func))
        {'a': 1.3, 'b': 2.0, 'c': 1, 'v': 2.3}
    """

    keys = list(functools.reduce(operator.or_, [set(d.keys()) for d in dicts]))
    d_out = {}
    for k in keys:
        fn = agg_key_funcs.get(k, default_func) if agg_key_funcs else default_func
        agg_val = fn([v for v in [d_in.get(k) for d_in in dicts] if v is not None])
        d_out[k] = agg_val

    return d_out
