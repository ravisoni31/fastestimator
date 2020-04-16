# Copyright 2019 The FastEstimator Authors. All Rights Reserved.
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
from copy import deepcopy
from typing import Dict, Any, TypeVar, Union, List, Sequence, Optional

import numpy as np
import tensorflow as tf
import torch

from fastestimator.backend.abs import abs
from fastestimator.backend.argmax import argmax
from fastestimator.backend.clip_by_value import clip_by_value
from fastestimator.backend.percentile import percentile
from fastestimator.backend.random_normal_like import random_normal_like
from fastestimator.backend.reduce_max import reduce_max
from fastestimator.backend.reduce_min import reduce_min
from fastestimator.backend.reduce_sum import reduce_sum
from fastestimator.backend.to_number import to_number
from fastestimator.backend.zeros_like import zeros_like
from fastestimator.network import Network
from fastestimator.op.tensorop.model.model import ModelOp
from fastestimator.op.tensorop.gather import Gather
from fastestimator.op.tensorop.gradient.gradient import GradientOp
from fastestimator.op.tensorop.gradient.watch import Watch
from fastestimator.util.util import to_list

Tensor = TypeVar('Tensor', tf.Tensor, torch.Tensor)
Model = TypeVar('Model', tf.keras.Model, torch.nn.Module)


class SaliencyNet:
    def __init__(self,
                 model: Model,
                 model_inputs: Union[str, Sequence[str]],
                 model_outputs: Union[str, Sequence[str]],
                 outputs: Union[str, List[str]] = "saliency"):
        mode = "test"
        self.model_op = ModelOp(model=model, mode=mode, inputs=model_inputs, outputs=model_outputs, trainable=False)
        self.outputs = to_list(outputs)
        self.mode = mode
        self.gather_keys = ["SaliencyNet_Target_Index_{}".format(key) for key in self.model_outputs]
        self.network = Network(ops=[
            Watch(inputs=self.model_inputs, mode=mode),
            self.model_op,
            Gather(inputs=self.model_outputs,
                   indices=self.gather_keys,
                   outputs=["SaliencyNet_Intermediate_{}".format(key) for key in self.model_outputs],
                   mode=mode),
            GradientOp(inputs=self.model_inputs,
                       finals=["SaliencyNet_Intermediate_{}".format(key) for key in self.model_outputs],
                       outputs=deepcopy(self.outputs),
                       mode=mode),
        ])

    @property
    def model_inputs(self):
        return deepcopy(self.model_op.inputs)

    @property
    def model_outputs(self):
        return deepcopy(self.model_op.outputs)

    @staticmethod
    def _convert_for_visualization(tensor: Tensor, tile: int = 99) -> np.ndarray:
        """
        Args:
            tensor: Input masks, channel values to be reduced by absolute value summation
            tile: The percentile [0-100] used to set the max value of the image
        Returns:
            A (batch X width X height) image after visualization clipping is applied
        """
        if isinstance(tensor, torch.Tensor):
            channel_axis = 1
        else:
            channel_axis = -1
        flattened_mask = reduce_sum(abs(tensor), axis=channel_axis, keepdims=True)

        non_batch_axes = list(range(len(flattened_mask.shape)))[1:]

        vmax = percentile(flattened_mask, tile, axis=non_batch_axes, keepdims=True)
        vmin = reduce_min(flattened_mask, axis=non_batch_axes, keepdims=True)

        return clip_by_value((flattened_mask - vmin) / (vmax - vmin), 0, 1)

    def get_masks(self, batch: Dict[str, Any]) -> Dict[str, Union[Tensor, np.ndarray]]:
        # Shallow copy batch since we're going to modify its contents later
        batch = {key: val for key, val in batch.items()}
        self.network.load_epoch(mode=self.mode, epoch=0, warmup=False)
        grads_and_preds = self._get_mask(batch)
        for key in self.outputs:
            grads_and_preds[key] = self._convert_for_visualization(grads_and_preds[key])
        self.network.unload_epoch()
        return grads_and_preds

    def _get_mask(self, batch: Dict[str, Any]) -> Dict[str, Tensor]:
        for key in self.gather_keys:
            # If there's no target key, use an empty array which will cause the max-likelihood class to be selected
            batch.setdefault(key, [])
        batch, prediction = self.network.run_step(batch=batch)
        for key in self.model_outputs:
            prediction[key] = argmax(prediction[key], axis=1)
        return prediction

    def _get_integrated_masks(self, batch: Dict[str, Any], nsamples: int = 25) -> Dict[str, Tensor]:
        model_inputs = [batch[ins] for ins in self.model_inputs]

        input_baselines = [zeros_like(ins) + (reduce_max(ins) + reduce_min(ins)) / 2 for ins in model_inputs]
        input_diffs = [
            model_input - input_baseline for model_input, input_baseline in zip(model_inputs, input_baselines)
        ]

        response = {}

        for alpha in np.linspace(0.0, 1.0, nsamples):
            noisy_batch = {key: batch[key] for key in self.gather_keys}
            for idx, input_name in enumerate(self.model_inputs):
                x_step = input_baselines[idx] + alpha * input_diffs[idx]
                noisy_batch[input_name] = x_step
            grads_and_preds = self._get_mask(noisy_batch)
            for key in self.outputs:
                if key in response:
                    response[key] += grads_and_preds[key]
                else:
                    response[key] = grads_and_preds[key]

        for key in self.outputs:
            grad = response[key]
            for diff in input_diffs:
                grad = grad * diff
            response[key] = grad

        return response

    def get_smoothed_masks(self,
                           batch: Dict[str, Any],
                           stdev_spread: float = .15,
                           nsamples: int = 25,
                           nintegration: Optional[int] = None,
                           magnitude: bool = True) -> Dict[str, Union[Tensor, np.ndarray]]:
        """
        Args:
            batch: An input batch of data
            stdev_spread: Amount of noise to add to the input, as fraction of the
                        total spread (x_max - x_min). Defaults to 15%.
            nsamples: Number of samples to average across to get the smooth gradient.
            nintegration: Number of samples to compute when integrating (None to disable)
            magnitude: If true, computes the sum of squares of gradients instead of
                     just the sum. Defaults to true.

        Returns:
            A saliency mask smoothed via the SmoothGrad method
        """
        # Shallow copy batch since we're going to modify its contents later
        batch = {key: val for key, val in batch.items()}

        self.network.load_epoch(mode=self.mode, epoch=0, warmup=False)
        model_inputs = [batch[ins] for ins in self.model_inputs]
        stdevs = [to_number(stdev_spread * (reduce_max(ins) - reduce_min(ins))).item() for ins in model_inputs]

        # Adding noise to the image might cause the max likelihood class value to change, so need to keep track of
        # which class we're comparing to
        response = self._get_mask(batch)
        for gather_key, output_key in zip(self.gather_keys, self.model_outputs):
            batch[gather_key] = response[output_key]

        if magnitude:
            for key in self.outputs:
                response[key] = response[key] * response[key]

        for _ in range(nsamples - 1):
            noisy_batch = {key: batch[key] for key in self.gather_keys}
            for idx, input_name in enumerate(self.model_inputs):
                noise = random_normal_like(model_inputs[idx], std=stdevs[idx])
                x_plus_noise = model_inputs[idx] + noise
                noisy_batch[input_name] = x_plus_noise
            grads_and_preds = self._get_mask(noisy_batch) if not nintegration else self._get_integrated_masks(
                noisy_batch, nsamples=nintegration)
            for name in self.outputs:
                grad = grads_and_preds[name]
                if magnitude:
                    response[name] += grad * grad
                else:
                    response[name] += grad

        for key in self.outputs:
            grad = response[key]
            response[key] = self._convert_for_visualization(grad / nsamples)

        self.network.unload_epoch()

        return response

    def get_integrated_masks(self, batch: Dict[str, Any], nsamples: int = 25) -> Dict[str, Union[Tensor, np.ndarray]]:
        """Generate masks using the integrated gradients method (https://arxiv.org/abs/1703.01365)
        
        Args:
            batch: An input batch of data
            nsamples: Number of samples to average across to get the integrated gradient.
        Returns:
            A saliency mask smoothed via the IntegratedGradient method
        """
        # Shallow copy batch since we're going to modify its contents later
        batch = {key: val for key, val in batch.items()}

        self.network.load_epoch(mode=self.mode, epoch=0, warmup=False)

        # Performing integration might cause the max likelihood class value to change, so need to keep track of
        # which class we're comparing to
        response = self._get_mask(batch)
        for gather_key, output_key in zip(self.gather_keys, self.model_outputs):
            batch[gather_key] = response[output_key]

        response.update(self._get_integrated_masks(batch, nsamples=nsamples))
        for key in self.outputs:
            response[key] = self._convert_for_visualization(response[key])

        self.network.unload_epoch()

        return response
