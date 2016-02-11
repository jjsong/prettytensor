# Copyright 2015 Google Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Methods for Pretty Tensor."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections

import tensorflow as tf

from prettytensor import functions
from prettytensor import layers
from prettytensor import pretty_tensor_class as prettytensor
from prettytensor.pretty_tensor_class import DIM_REST
from prettytensor.pretty_tensor_class import DIM_SAME
from prettytensor.pretty_tensor_class import Phase
from prettytensor.pretty_tensor_class import PROVIDED


def _infer_unknown_dims(old_shape, shape_spec):
  """Attempts to replace DIM_REST (if present) with a value.

  Because of DIM_SAME, this has more information to compute a shape value than
  the default reshape's shape function.

  Args:
    old_shape: The current shape of the Tensor as a list.
    shape_spec: A shape spec, see `pt.reshape`.
  Returns:
    A list derived from `shape_spec` with `DIM_SAME` replaced by the value from
    old_shape (if possible) and `DIM_REST` computed (if possible).
  Raises:
    ValueError: If there are two many unknown dimensions or the shape_spec
    requires out of range DIM_SAME.
    TypeError: If shape_spec if not iterable.
  """

  # To compute the dimension of an unknown, we need to track which of the values
  # from old_shape are not copied for the numerator and any values specified as
  # integers for the denominator.
  #
  # After the loop, if any input dimension is unknown and not DIM_SAME, the
  # numerator will be 0. Otherwise it is the product of all non-DIM_SAME
  # dimensions.  This means that the dimension of DIM_REST is
  # numerator / denominator
  numerator_elements = [x if x else 0 for x in old_shape]
  denominator = 1
  unknowns = 0

  result = []
  for i, s in enumerate(shape_spec):
    if s == DIM_SAME:
      if i >= len(old_shape):
        raise ValueError('%d exceeds the input shape' % i)
      if old_shape[i] is None:
        result.append(DIM_SAME)
      else:
        result.append(old_shape[i])
      numerator_elements[i] = 1
    elif s in (DIM_REST, -1, None):
      result.append(-1)
      unknowns += 1
    else:
      x = int(s)
      result.append(x)
      denominator *= x

  numerator = 1
  for x in numerator_elements:
    numerator *= x
  if unknowns > 1:
    raise ValueError('Only one unknown value (-1 or *) is allowed: %s' %
                     shape_spec)
  elif numerator % denominator != 0:
    raise ValueError('Input (%s) cannot be reshaped to %s.' %
                     (old_shape, shape_spec))
  elif unknowns == 0 and numerator > 0 and numerator != denominator:
    raise ValueError('Input (%s) cannot be reshaped to %s.' %
                     (old_shape, shape_spec))
  if numerator and unknowns:
    unknown_elements = int(numerator / denominator)
    return [unknown_elements if x == -1 else x for x in result]
  else:
    return result


@prettytensor.Register
def reshape(input_layer, shape_spec):
  """Reshapes this tensor to the given spec.

  A shape_spec can be a list or tuple of numbers specifying the new shape, but
  also may include the following shorthands for using values from the shape of
  the input:

  1. DIM_SAME ('_') will use the corresponding value from the current shape.
  2. One -1 or DIM_REST ('*') can be used to specify the remainder of the
      values.
  3. An integer will be used as is.

  A compact syntax is also supported for setting shapes. If the new shape is
  only composed of DIM_SAME, DIM_REST/-1 and single digit integers, then a
  string can be passed in. Integers larger than 9 must be passed in as part of a
  sequence.

  1. Flatten to a batch dimension (first by convention): [DIM_SAME, -1] or '_*'.
  2. Expand a Rank 2 Tensor so that it can be used as an image: '_11*'.
  The primary difference between this and `tf.reshape` is that `DIM_SAME` allows
  more shape inference possibilities. For example: given a shape of
  **[None, 3, 7]** if flattening were desired then the caller would have to
  compute the shape and request a reshape of **[-1, 21]** to flatten. Instead of
  brittle or repeated code, this can be inferred if we know that the first dim
  is being copied.

  Another example that is impossible to express as a list of integers is if the
  starting shape were **[None, 3, None]** and we wanted to do the same
  flattening. While the shape cannot be inferred, this can still be expressed as
  '_*' (A.K.A. [DIM_SAME, DIM_REST]).

  Args:
    input_layer: The Pretty Tensor object, supplied.
    shape_spec: The spec for the new shape.
  Returns:
    A Pretty Tensor with the reshaped tensor.
  Raises:
    ValueError: If there are two many unknown dimensions or the shape_spec
    requires out of range DIM_SAME.
  """
  old_shape = input_layer.get_shape().as_list()

  # Extract both a tensor that sets the new shape and as much of the new
  # shape is known. This lets us merge in any extra information we have about
  # the shape.
  try:
    new_shape = _infer_unknown_dims(old_shape, shape_spec)
  except TypeError:
    # shape_spec is not iterable, it is probably a tensor or variable.
    return tf.reshape(input_layer, shape_spec)
  reshape_tensor = []

  # To avoid bloating the graph, we want to capture consecutive integers into
  # a single tf.constant. This allows us to eliminate tf.concat when we know the
  # shape.
  runner = []

  for i, s in enumerate(new_shape):
    if s is DIM_SAME:
      new_shape[i] = None
      if runner:
        reshape_tensor.append(tf.constant(runner))
        runner = []
      # Since we can't statically infer the value, compute it from the graph.
      reshape_tensor.append(tf.gather(tf.shape(input_layer), [i]))
    else:
      runner.append(s)
      if s == -1:
        new_shape[i] = None
  if runner:
    reshape_tensor.append(tf.constant(runner))

  if len(reshape_tensor) == 1:
    reshape_tensor = reshape_tensor[0]
  else:
    reshape_tensor = tf.concat(0, reshape_tensor)
  result = tf.reshape(input_layer, reshape_tensor)
  result.set_shape(new_shape)

  return input_layer.with_tensor(result)


@prettytensor.Register
def flatten(input_layer, preserve_batch=True):
  """Flattens this.

  If preserve_batch is True, the result is rank 2 and the first dim (batch) is
  unchanged. Otherwise the result is rank 1.

  Args:
    input_layer: The Pretty Tensor object, supplied.
    preserve_batch: If True (the default), then preserve the first dimension.
  Returns:
    A LayerWrapper with the flattened tensor.
  """
  if preserve_batch:
    return reshape(input_layer, [DIM_SAME, -1])
  else:
    return reshape(input_layer, [-1])


@prettytensor.Register
def stop_gradient(input_layer):
  """Cuts off the gradient at this point.

  This works on both sequence and regular Pretty Tensors.

  Args:
    input_layer: The input.
  Returns:
    A new Pretty Tensor of the same type with stop_gradient applied.
  """
  if input_layer.is_sequence():
    result = [tf.stop_gradient(t) for t in input_layer.sequence]
    return input_layer.with_sequence(result)
  else:
    return tf.stop_gradient(input_layer)


@prettytensor.Register(assign_defaults='phase')
def dropout(input_layer, keep_prob, phase=Phase.train, name=PROVIDED):
  """Aplies dropout if this is in the train phase."""
  if phase == Phase.train:
    return tf.nn.dropout(input_layer, keep_prob, name=name)
  else:
    return input_layer


# TODO(eiderman): Give a good name for this function: Maybe InnerProductIsh ?
# pylint: disable=invalid-name
@prettytensor.Register(assign_defaults=('l2loss', 'stddev'))
class diagonal_matrix_mul(prettytensor.VarStoreMethod):
  """Diagonal Matrix Multiplication."""

  def __call__(self, input_layer, init=None, stddev=None, l2loss=None):
    """Performs a diagonal matrix multiplication with a learned vector.

    This creates the parameter vector.

    Args:
      input_layer: The input_layer.
      init: An optional initialization. If not specified, uses Xavier
        initialization.
      stddev: A standard deviation to use in parameter initialization.
      l2loss: An l2 weight decay to apply.
    Returns:
      A Pretty Tensor handle to the layer.
    Raises:
      ValueError: if the head_shape is not rank 2  or the number of input nodes
      (second dim) is not known.
    """
    size = input_layer.shape[-1]
    if init is None:
      if stddev is None:
        init = layers.xavier_init(size, 0)
      elif stddev:
        init = tf.truncated_normal_initializer(stddev=stddev)
      else:
        init = tf.zeros_initializer
    param = self.variable('weights', [size], init)
    layers.add_l2loss(input_layer.bookkeeper, param, l2loss)

    return input_layer.with_tensor(input_layer * param, parameters=self.vars)
# pylint: enable=invalid-name


# pylint: disable=invalid-name
@prettytensor.Register(assign_defaults=('activation_fn', 'l2loss', 'stddev'))
class fully_connected(prettytensor.VarStoreMethod):

  def __call__(self,
               input_layer,
               size,
               activation_fn=None,
               l2loss=None,
               init=None,
               stddev=None,
               bias=True,
               bias_init=0.,
               name=PROVIDED):
    """Adds the parameters for a fully connected layer and returns a tensor.

    The current head must be a rank 2 Tensor.

    Args:
      input_layer: The Pretty Tensor object, supplied.
      size: The number of neurons
      activation_fn: A tuple of (activation_function, extra_parameters). Any
        function that takes a tensor as its first argument can be used. More
        common functions will have summaries added (e.g. relu).
      l2loss: Set to a value greater than 0 to use L2 regularization to decay
        the weights.
      init: An optional initialization. If not specified, uses Xavier
        initialization.
      stddev: A standard deviation to use in parameter initialization.
      bias: Set to False to not have a bias.
      bias_init: The initial value for the bias.
      name: The name for this operation is also used to create/find the
        parameter variables.
    Returns:
      A Pretty Tensor handle to the layer.
    Raises:
      ValueError: if the head_shape is not rank 2  or the number of input nodes
      (second dim) is not known.
    """
    if len(input_layer.shape) != 2:
      raise ValueError(
          'Cannot perform fully connected on tensor with shape %s' %
          input_layer.shape)
    in_size = input_layer.shape[1]
    if input_layer.shape[1] is None:
      raise ValueError('Number of input nodes must be known.')
    books = input_layer.bookkeeper
    if init is None:
      if stddev is None:
        init = layers.xavier_init(in_size, size)
      elif stddev:
        init = tf.truncated_normal_initializer(stddev=stddev)
      else:
        init = tf.zeros_initializer
    elif stddev is not None:
      raise ValueError('Do not set both init and stddev.')
    dtype = input_layer.tensor.dtype
    params = self.variable(
        'weights',
        [in_size, size],
        init,
        dt=dtype)
    y = tf.matmul(input_layer, params)
    layers.add_l2loss(books, params, l2loss)
    if bias:
      y += self.variable(
          'bias',
          [size],
          tf.constant_initializer(bias_init),
          dt=dtype)

    if activation_fn is not None:
      if not isinstance(activation_fn, collections.Sequence):
        activation_fn = (activation_fn,)
      y = layers.apply_activation(
          books,
          y,
          activation_fn[0],
          activation_args=activation_fn[1:])
    books.add_histogram_summary(y, '%s/activations' % y.op.name)
    return input_layer.with_tensor(y, parameters=self.vars)
# pylint: enable=invalid-name


@prettytensor.Register
def apply_with_summary(input_layer, operation, *op_args, **op_kwargs):
  """Applies the given operation to this and sets the new head.

  Args:
    input_layer: The input layer for this op.
    operation: An operation that takes a tensor and the supplied args.
    *op_args: Extra arguments for operation.
    **op_kwargs: Keyword arguments for the operation.
  Returns:
    A new layer with operation applied.
  """
  return layers.apply_activation(
      input_layer.bookkeeper,
      input_layer.tensor,
      operation,
      activation_args=op_args,
      activation_kwargs=op_kwargs)


@prettytensor.Register()
def _rapply(input_layer, operation, *op_args, **op_kwargs):
  """Applies the given operation to this after expanding op_args.

  Args:
    input_layer: The input layer for this op.
    operation: An operation that takes a tensor and the supplied args.
    *op_args: Extra arguments for operation.
    **op_kwargs: Keyword arguments for the operation.
  Returns:
    A new layer with operation applied.
  """
  op_args = list(op_args)
  op_args.append(input_layer.tensor)
  return input_layer.with_tensor(operation(*op_args, **op_kwargs))


@prettytensor.Register(method_name='apply')
def apply_op(input_layer, operation, *op_args, **op_kwargs):
  """Applies the given operation to this before without adding any summaries.

  Args:
    input_layer: The input layer for this op.
    operation: An operation that takes a tensor and the supplied args.
    *op_args: Extra arguments for operation.
    **op_kwargs: Keyword arguments for the operation.
  Returns:
    A new layer with operation applied.
  """
  return input_layer.with_tensor(
      operation(input_layer.tensor, *op_args, **op_kwargs))


@prettytensor.Register
def __getitem__(input_layer, key):  # pylint: disable=invalid-name
  if input_layer.is_sequence():
    return input_layer.with_tensor(input_layer.sequence[key])
  else:
    return input_layer.tensor[key]


@prettytensor.Register
def join(input_layer, others, include_self=True, join_function=None):
  """Joins the provided PrettyTensors with this using the join function.

  Args:
    input_layer: The input layer for this op.
    others: Sequence of PrettyTensor objects.
    include_self: Whether or not this includes itself or if the value is only
      derived from others.
    join_function: The function to use for joining, must accept a list of
      tensors. Use None for concat on the final dimension.
  Returns:
    self.
  """
  if include_self:
    list_of_tensors = [input_layer]
    list_of_tensors.extend(others)
  else:
    list_of_tensors = others
  return prettytensor.join_pretty_tensors(
      list_of_tensors, input_layer, join_function)


def _check_split_dims(num_splits, split_dim, shape):
  if split_dim >= len(shape):
    raise ValueError('split_dim out of bounds: %d  %s' % (split_dim, shape))
  if shape[split_dim] % num_splits != 0:
    raise ValueError(
        'Failure to split %s tensor at split_dim=%d\nMust divide the split '
        'dimension evenly: %d mod %d != 0' %
        (shape, split_dim, shape[split_dim], num_splits))


@prettytensor.Register
def unzip(input_layer, split_dim=0, num_splits=2):
  """Unzips the head Tensor along the split_dim into num_splits Equal chunks.

  Examples:

  * `[1, 2, 3, 4] -> [1, 3], [2, 4]`
  * `[[1, 1], [2, 2], [3, 3], [4, 4]] -> [[1, 1], [3, 3]], [[2, 2], [4, 4]]`

  Args:
    input_layer: The chainable object, supplied.
    split_dim: The dimension to split along. Defaults to batch.
    num_splits: The number of splits.
  Returns:
    A list of PrettyTensors.
  Raises:
    ValueError: If split_dim is out of range or isn't divided evenly by
      num_splits.
  """
  shape = input_layer.shape
  _check_split_dims(num_splits, split_dim, shape)
  splits = functions.unzip(input_layer, split_dim, shape[split_dim], num_splits)
  return input_layer.with_sequence(splits)


@prettytensor.Register
def concat(input_layer, concat_dim, other_tensors):
  """Concatenates input PrettyTensor with other_tensors along the specified dim.

  This adds the Pretty Tensor passed via input_layer to the front of the list of
  tensors to concat.

  Args:
    input_layer: The input layer.
    concat_dim: The dimension along which to concat.
    other_tensors: The tensors to concatenate with.
  Returns:
    A new PrettyTensor.
  """
  result = [input_layer]
  result.extend(other_tensors)
  return tf.concat(concat_dim, result)


@prettytensor.Register(method_name='slice')
def slice_(input_layer, begin, size):
  """Extracts a slice from a tensor.

  This operation extracts a slice of size `size` from a tensor `input` starting
  at the location specified by `begin`. The slice `size` is represented as a
  tensor shape, where `size[i]` is the number of elements of the 'i'th dimension
  of 'input' that you want to slice. The starting location (`begin`) for the
  slice is represented as an offset in each dimension of `input`. In other
  words, `begin[i]` is the offset into the 'i'th dimension of 'input' that you
  want to slice from.

  `begin` is zero-based; 'size' is one-based. If `size[i]` is -1,
  all remaining elements in dimension i are included in the
  slice. In other words, this is equivalent to setting:

  `size[i] = input.dim_size(i) - begin[i]`

  This operation requires that:

  `0 <= begin[i] <= begin[i] + size[i] <= Di  for i in [0, n]`

  Examples:

      # 'input' is [[[1, 1, 1], [2, 2, 2]],
      #             [[3, 3, 3], [4, 4, 4]],
      #             [[5, 5, 5], [6, 6, 6]]]
      tf.slice(input, [1, 0, 0], [1, 1, 3]) ==> [[[3, 3, 3]]]
      tf.slice(input, [1, 0, 0], [1, 2, 3]) ==> [[[3, 3, 3],
                                                  [4, 4, 4]]]
      tf.slice(input, [1, 0, 0], [2, 1, 3]) ==> [[[3, 3, 3]],
                                                 [[5, 5, 5]]]

  Args:
    input_layer: A Tensor.
    begin: An int32 or int64 Tensor of length rank(input_layer)
    size: An int32 or int64 Tensor of length rank(input_layer)
  Returns:
    A tensor with the selected slice.
  """
  return tf.slice(input_layer, begin, size)


@prettytensor.Register
def split(input_layer, split_dim=0, num_splits=2):
  """Splits the head Tensor along the split_dim into num_splits Equal chunks.

  Examples:

  * `[1, 2, 3, 4] -> [1, 2], [3, 4]`
  * `[[1, 1], [2, 2], [3, 3], [4, 4]] -> [[1, 1], [2, 2]], [[3, 3], [4, 4]]`

  Args:
    input_layer: The chainable object, supplied.
    split_dim: The dimension to split along. Defaults to batch.
    num_splits: The number of splits.
  Returns:
    A list of PrettyTensors.
  Raises:
    ValueError: If split_dim is out of range or isn't divided evenly by
      num_splits.
  """
  shape = input_layer.shape
  _check_split_dims(num_splits, split_dim, shape)
  splits = tf.split(split_dim, num_splits, input_layer)
  return input_layer.with_sequence(splits)


@prettytensor.Register
def squeeze(input_layer, squeeze_dims=None):
  """Removes dimensions of size 1 from the shape of a tensor.

  This operation returns a tensor of the same type with all singleton
  dimensions removed. If you don't want to remove all singleton dimensions, you
  can remove specific size 1 dimensions by specifying a list of squeeze_dims.

  Args:
    input_layer: A Tensor of any type to squeeze.
    squeeze_dims: An optional list of ints. Defaults to [].

  Returns:
    The sequeezed tensor.
  """

  return tf.squeeze(input_layer, squeeze_dims)


@prettytensor.Register(method_name='map')
def map_(input_layer, fn):
  """Maps the given function across this sequence.

  To map an entire template across the sequence, use the `as_fn` method on the
  template.

  Args:
    input_layer: The input tensor.
    fn: A function of 1 argument that is applied to each item in the sequence.
  Returns:
    A new sequence Pretty Tensor.
  """
  return prettytensor.wrap_sequence([fn(x) for x in input_layer])
