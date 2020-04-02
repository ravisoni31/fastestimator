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

from typing import TypeVar, List, Optional

import tensorflow as tf
import torch

Tensor = TypeVar('Tensor', tf.Tensor, torch.Tensor, torch.autograd.Variable)


def concat(tensors: List[Tensor], axis: int = 0) -> Optional[Tensor]:
    if len(tensors) == 0:
        return None
    if isinstance(tensors[0], tf.Tensor):
        return tf.concat(tensors, axis=axis)
    elif isinstance(tensors[0], torch.Tensor):
        return torch.cat(tensors, dim=axis)
    else:
        raise ValueError("Unrecognized tensor type {}".format(type(tensors[0])))