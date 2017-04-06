# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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

"""Library for creating sequence-to-sequence models in TensorFlow.

Sequence-to-sequence recurrent neural networks can learn complex functions
that map input sequences to output sequences. These models yield very good
results on a number of tasks, such as speech recognition, parsing, machine
translation, or even constructing automated replies to emails.

Before using this module, it is recommended to read the TensorFlow tutorial
on sequence-to-sequence models. It explains the basic concepts of this module
and shows an end-to-end example of how to build a translation model.
  https://www.tensorflow.org/versions/master/tutorials/seq2seq/index.html

Here is an overview of functions available in this module. They all use
a very similar interface, so after reading the above tutorial and using
one of them, others should be easy to substitute.

* Full sequence-to-sequence models.
  - basic_rnn_seq2seq: The most basic RNN-RNN model.
  - tied_rnn_seq2seq: The basic model with tied encoder and decoder weights.
  - embedding_rnn_seq2seq: The basic model with input embedding.
  - embedding_tied_rnn_seq2seq: The tied model with input embedding.
  - embedding_attention_seq2seq: Advanced model with input embedding and
      the neural attention mechanism; recommended for complex tasks.

* Multi-task sequence-to-sequence models.
  - one2many_rnn_seq2seq: The embedding model with multiple decoders.

* Decoders (when you write your own encoder, you can use these to decode;
    e.g., if you want to write a model that generates captions for images).
  - rnn_decoder: The basic decoder based on a pure RNN.
  - attention_decoder: A decoder that uses the attention mechanism.

* Losses.
  - sequence_loss: Loss for a sequence model returning average log-perplexity.
  - sequence_loss_by_example: As above, but not averaging over all examples.

* model_with_buckets: A convenience function to create models with bucketing
    (see the tutorial above for an explanation of why and how to use it).
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# We disable pylint because we need python3 compatibility.
from six.moves import xrange  # pylint: disable=redefined-builtin
from six.moves import zip     # pylint: disable=redefined-builtin

from tensorflow.python import shape
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn
from tensorflow.python.ops import rnn_cell
from tensorflow.python.ops import variable_scope
from tensorflow.python.util import nest

# TODO(ebrevdo): Remove once _linear is fully deprecated.
linear = rnn_cell._linear  # pylint: disable=protected-access

from tensorflow.models.rnn.translate.seq2seq.wrapper_cells import BOWCell

from tensorflow.python.ops.math_ops import tanh

from itertools import chain
import tensorflow as tf

import logging

def _extract_argmax_and_embed(embedding, output_projection=None,
                              update_embedding=True):
  """Get a loop_function that extracts the previous symbol and embeds it.

  Args:
    embedding: embedding tensor for symbols.
    output_projection: None or a pair (W, B). If provided, each fed previous
      output will first be multiplied by W and added B.
    update_embedding: Boolean; if False, the gradients will not propagate
      through the embeddings.

  Returns:
    A loop function.
  """
  def loop_function(prev, _):
    if output_projection is not None:
      prev = nn_ops.xw_plus_b(
          prev, output_projection[0], output_projection[1])
    prev_symbol = math_ops.argmax(prev, 1)
    # Note that gradients will not propagate through the second parameter of
    # embedding_lookup.
    emb_prev = embedding_ops.embedding_lookup(embedding, prev_symbol)
    if not update_embedding:
      emb_prev = array_ops.stop_gradient(emb_prev)
    return emb_prev
  return loop_function

def get_state_size(cell):
  if isinstance(cell.state_size, tuple):
    return cell.state_size[0]+cell.state_size[1]
  else:
    return cell.state_size

def rnn_decoder(decoder_inputs, initial_state, cell, loop_function=None,
                scope=None, feed_prev_p=None):
  """RNN decoder for the sequence-to-sequence model.

  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor with shape [batch_size x cell.state_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    loop_function: If not None, this function will be applied to the i-th output
      in order to generate the i+1-st input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    scope: VariableScope for the created subgraph; defaults to "rnn_decoder".

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing generated outputs.
      state: The state of each cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
        (Note that in some cases, like basic RNN cell or GRU cell, outputs and
         states can be the same. They are different for LSTM cells though.)
  """


  with variable_scope.variable_scope(scope or "rnn_decoder"):
    state = initial_state
    outputs = []
    prev = None
    for i, inp in enumerate(decoder_inputs):
      reuse = (loop_function is not None and prev is not None) or (i > 0)
      with variable_scope.variable_scope(scope or "rnn_decoder", reuse=reuse):

        def inner_loop(prev, inp, i, feed_prev=False):
          if feed_prev and (prev is not None):
            inp = loop_function(prev, i)
          return inp

        if feed_prev_p is not None:
          #logging.info('Decoding with scheduled sampling')
          rv = tf.random_uniform([], minval=0, maxval=1, dtype=tf.float32)
          feed_prev = (loop_function is not None) and tf.less(rv, feed_prev_p)
          inp = control_flow_ops.cond(feed_prev,
                                      lambda: inner_loop(prev, inp, i, feed_prev=False),
                                      lambda: inner_loop(prev, inp, i, feed_prev=True))
        else:
          feed_prev = (loop_function is not None)
          inp = inner_loop(prev, inp, i, feed_prev)
        output, state = cell(inp, state)
        outputs.append(output)
        prev = output
  return outputs, state


def basic_rnn_seq2seq(
    encoder_inputs, decoder_inputs, cell, dtype=dtypes.float32, scope=None):
  """Basic RNN sequence-to-sequence model.

  This model first runs an RNN to encode encoder_inputs into a state vector,
  then runs decoder, initialized with the last encoder state, on decoder_inputs.
  Encoder and decoder use the same RNN cell type, but don't share parameters.

  Args:
    encoder_inputs: A list of 2D Tensors [batch_size x input_size].
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    dtype: The dtype of the initial state of the RNN cell (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "basic_rnn_seq2seq".

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell in the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(scope or "basic_rnn_seq2seq"):
    _, enc_state = rnn.rnn(cell, encoder_inputs, dtype=dtype)
    return rnn_decoder(decoder_inputs, enc_state, cell)


def tied_rnn_seq2seq(encoder_inputs, decoder_inputs, cell,
                     loop_function=None, dtype=dtypes.float32, scope=None):
  """RNN sequence-to-sequence model with tied encoder and decoder parameters.

  This model first runs an RNN to encode encoder_inputs into a state vector, and
  then runs decoder, initialized with the last encoder state, on decoder_inputs.
  Encoder and decoder use the same RNN cell and share parameters.

  Args:
    encoder_inputs: A list of 2D Tensors [batch_size x input_size].
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    loop_function: If not None, this function will be applied to i-th output
      in order to generate i+1-th input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol), see rnn_decoder for details.
    dtype: The dtype of the initial state of the rnn cell (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "tied_rnn_seq2seq".

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope("combined_tied_rnn_seq2seq"):
    scope = scope or "tied_rnn_seq2seq"
    _, enc_state = rnn.rnn(
        cell, encoder_inputs, dtype=dtype, scope=scope)
    variable_scope.get_variable_scope().reuse_variables()
    return rnn_decoder(decoder_inputs, enc_state, cell,
                       loop_function=loop_function, scope=scope)

def embedding_rnn_decoder(decoder_inputs,
                          initial_state,
                          cell,
                          num_symbols,
                          embedding_size,
                          output_projection=None,
                          feed_previous=False,
                          update_embedding_for_previous=True,
                          scope=None,
                          feed_prev_p=None):
  """RNN decoder with embedding and a pure-decoding option.

  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    cell: rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has
      shape [num_symbols]; if provided and feed_previous=True, each fed
      previous output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    scope: VariableScope for the created subgraph; defaults to
      "embedding_rnn_decoder".

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors. The
        output is of shape [batch_size x cell.output_size] when
        output_projection is not None (and represents the dense representation
        of predicted tokens). It is of shape [batch_size x num_decoder_symbols]
        when output_projection is None.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].

  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  schedule = True if feed_prev_p is not None else False
  with variable_scope.variable_scope(scope or "embedding_rnn_decoder") as scope:
    if output_projection is not None:
      dtype = scope.dtype
      proj_weights = ops.convert_to_tensor(output_projection[0], dtype=dtype)
      proj_weights.get_shape().assert_is_compatible_with([None, num_symbols])
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      proj_biases.get_shape().assert_is_compatible_with([num_symbols])

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])
    loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if (feed_previous or schedule) else None
    emb_inp = (
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs)
    return rnn_decoder(emb_inp, initial_state, cell,
                       loop_function=loop_function,
                       feed_prev_p=feed_prev_p)


def embedding_rnn_seq2seq(encoder_inputs,
                          decoder_inputs,
                          cell,
                          num_encoder_symbols,
                          num_decoder_symbols,
                          embedding_size,
                          output_projection=None,
                          feed_previous=False,
                          dtype=None,
                          scope=None):
  """Embedding RNN sequence-to-sequence model.

  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs RNN decoder, initialized with the last
  encoder state, on embedded decoder_inputs.

  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial state for both the encoder and encoder
      rnn cells (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_rnn_seq2seq"

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors. The
        output is of shape [batch_size x cell.output_size] when
        output_projection is not None (and represents the dense representation
        of predicted tokens). It is of shape [batch_size x num_decoder_symbols]
        when output_projection is None.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(scope or "embedding_rnn_seq2seq") as scope:
    if dtype is not None:
      scope.set_dtype(dtype)
    else:
      dtype = scope.dtype

    # Encoder.
    encoder_cell = rnn_cell.EmbeddingWrapper(
        cell, embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    _, encoder_state = rnn.rnn(encoder_cell, encoder_inputs, dtype=dtype)

    # Decoder.
    if output_projection is None:
      cell = rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)

    if isinstance(feed_previous, bool):
      return embedding_rnn_decoder(
          decoder_inputs,
          encoder_state,
          cell,
          num_decoder_symbols,
          embedding_size,
          output_projection=output_projection,
          feed_previous=feed_previous)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_rnn_decoder(
            decoder_inputs, encoder_state, cell, num_decoder_symbols,
            embedding_size, output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(structure=encoder_state,
                                    flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state



def embedding_rnn_autoencoder_seq2seq(encoder_inputs, decoder_inputs, cell,
                               num_symbols, embedding_size,
                               output_projection=None, feed_previous=False,
                               dtype=dtypes.float32, scope=None, encoder=None,
                               sequence_length=None, bucket_length=None,
                               init_backward=False, hidden_state=None, 
                               initializer=None, legacy=False, feed_prev_p=None):
  with variable_scope.variable_scope(
      scope or "embedding_rnn_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype
    if encoder == "bidirectional":
      encoder_cell_fw = rnn_cell.EmbeddingWrapper(
        cell.get_fw_cell(), embedding_classes=num_symbols,
        embedding_size=embedding_size, initializer=initializer)
      encoder_cell_bw = rnn_cell.EmbeddingWrapper(
        cell.get_bw_cell(), embedding_classes=num_symbols,
        embedding_size=embedding_size, initializer=initializer)
      _, encoder_state, encoder_state_bw = rnn.bidirectional_rnn(
        encoder_cell_fw, encoder_cell_bw,
        encoder_inputs, dtype=dtype,
        sequence_length=sequence_length,
        bucket_length=bucket_length, legacy=legacy)
      if init_backward:
        cell = cell.get_bw_cell()
        initial_state = encoder_state_bw
      else:
        cell = cell.get_fw_cell()
        initial_state = encoder_state
    elif encoder == "reverse":
      encoder_cell = rnn_cell.EmbeddingWrapper(
        cell, embedding_classes=num_symbols,
        embedding_size=embedding_size, initializer=initializer)
      _, encoder_state = rnn.rnn(
        encoder_cell, encoder_inputs, dtype=dtype,
        sequence_length=sequence_length, bucket_length=bucket_length,
        reverse=True)
      initial_state = encoder_state

    if hidden_state is not None and isinstance(cell.state_size, tuple):
      hidden_state = tuple(tf.split(1, 2, hidden_state))

    # Decoder.
    if output_projection is None:
      cell = rnn_cell.OutputProjectionWrapper(cell, num_symbols)

    if isinstance(feed_previous, bool):
      if hidden_state is not None:
        logging.info('Decoding from hidden state')
        outputs, state = embedding_rnn_decoder(
          decoder_inputs, hidden_state, cell, num_symbols,
          embedding_size, output_projection=output_projection,
          feed_previous=feed_previous)
      else:
        outputs, state = embedding_rnn_decoder(
          decoder_inputs, initial_state, cell, num_symbols,
          embedding_size, output_projection=output_projection,
          feed_previous=feed_previous, feed_prev_p=feed_prev_p)
      return outputs, initial_state

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(variable_scope.get_variable_scope(),
                                         reuse=reuse):
        if hidden_state is not None:
          logging.info('Decoding from hidden state')
          outputs, state = embedding_rnn_decoder(
            decoder_inputs, hidden_state, cell, num_symbols,
            embedding_size, output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False)
        else:
          outputs, state = embedding_rnn_decoder(
            decoder_inputs, initial_state, cell, num_symbols,
            embedding_size, output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False, feed_prev_p=feed_prev_p)
        return outputs + [initial_state]

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    return outputs_and_state[:-1], initial_state


def embedding_rnn_vae_seq2seq(encoder_inputs, decoder_inputs, cell,
                              num_symbols, embedding_size, latent_size,
                              transfer_func=None,
                              output_projection=None, feed_previous=False,
                              dtype=dtypes.float32, scope=None, encoder=None,
                              sequence_length=None, bucket_length=None,
                              init_backward=False, latent_state=None,
                              initializer=None, legacy=False,
                              feed_prev_p=None, kl_min=0.0,
                              anneal_scale=None, sample_mean=False,
                              dec_cell=None, concat_encoded=False):
  with variable_scope.variable_scope(
    scope or "embedding_rnn_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype
    if encoder == "bidirectional":
      encoder_cell_fw = rnn_cell.EmbeddingWrapper(
        cell.get_fw_cell(), embedding_classes=num_symbols,
        embedding_size=embedding_size, initializer=initializer)
      encoder_cell_bw = rnn_cell.EmbeddingWrapper(
        cell.get_bw_cell(), embedding_classes=num_symbols,
        embedding_size=embedding_size, initializer=initializer)
      _, encoder_state, encoder_state_bw = rnn.bidirectional_rnn(encoder_cell_fw, encoder_cell_bw,
                                                    encoder_inputs, dtype=dtype,
                                                    sequence_length=sequence_length,
                                                    bucket_length=bucket_length,
                                                    legacy=legacy)
      if init_backward:
        enc_cell = encoder_cell_bw
        dec_cell = dec_cell.get_bw_cell()
        encoder_out_state = encoder_state_bw
      else:
        enc_cell = encoder_cell_fw
        dec_cell = dec_cell.get_fw_cell()
        encoder_out_state = encoder_state
    elif encoder == "reverse":
      enc_cell = rnn_cell.EmbeddingWrapper(
        cell, embedding_classes=num_symbols,
        embedding_size=embedding_size, initializer=initializer)
      _, encoder_out_state = rnn.rnn(
        enc_cell, encoder_inputs, dtype=dtype,
        sequence_length=sequence_length, bucket_length=bucket_length,
        reverse=True)
      dec_cell = dec_cell.get_bw_cell()

    # Decoder.
    if output_projection is None:
      dec_cell = rnn_cell.OutputProjectionWrapper(dec_cell, num_symbols)
 
    # Latent state
    if isinstance(encoder_out_state, tuple):
      encoder_out_state = tf.concat(concat_dim=1, values=encoder_out_state)

    enc_state_size = get_state_size(enc_cell)
    z_mean_w = tf.get_variable('z_mean_w', [enc_state_size, latent_size])
    z_mean_b = tf.get_variable('z_mean_b', [latent_size])
    z_logvar_w = tf.get_variable('z_logvar_w', [enc_state_size, latent_size])
    z_logvar_b = tf.get_variable('z_logvar_b', [latent_size])

    z_mean = transfer_func(tf.add(tf.matmul(encoder_out_state, z_mean_w), z_mean_b))
    z_log_var = transfer_func(tf.add(tf.matmul(encoder_out_state, z_logvar_w), z_logvar_b))
    eps = tf.random_normal(tf.shape(z_log_var), 0, 1, dtype=tf.float32)
    z = tf.add(z_mean, tf.mul(tf.sqrt(tf.exp(z_log_var)), eps))

    kl_loss = -0.5 * (1.0 + z_log_var - tf.square(z_mean) - tf.exp(z_log_var))
    kl_step_av = tf.reduce_mean(kl_loss, [0])
    if kl_min > 0.0:
      kl_obj = tf.reduce_sum(tf.maximum(kl_step_av, kl_min))
    else:
      kl_obj = tf.reduce_sum(kl_step_av)
    if anneal_scale is not None:
      kl_obj = tf.multiply(anneal_scale, kl_obj)
    
    if sample_mean and feed_previous:
      logging.info('Generating directly from mean')
      z = z_mean
    if latent_state is not None:
      logging.info('Decoding from hiden state')
      z = latent_state
      feed_prev_p = None

    if concat_encoded:
      logging.info('Concatenating latent and hidden states')
      if isinstance(enc_cell.state_size, tuple):
        enc_state_tuple = tuple(tf.split(1, 2, encoder_out_state))
        initial_state = (tf.concat(concat_dim=1, values=[state, z]) 
                         for state in enc_state_tuple)
      else:
        initial_state = tf.concat(concat_dim=1, values=[initial_state, z])
    else:
      dec_state_size = get_state_size(dec_cell)
      dec_in_w = tf.get_variable('dec_in_w', [latent_size, dec_state_size])
      dec_in_b = tf.get_variable('dec_in_b', [dec_state_size])
      initial_state = transfer_func(tf.add(tf.matmul(z, dec_in_w), dec_in_b))
      if isinstance(enc_cell.state_size, tuple):
        initial_state = tuple(tf.split(1, 2, initial_state))

    if isinstance(feed_previous, bool):
      outputs, state = embedding_rnn_decoder(
        decoder_inputs, initial_state, dec_cell, num_symbols,
        embedding_size, output_projection=output_projection,
        feed_previous=feed_previous, feed_prev_p=feed_prev_p)
      return outputs, z, kl_obj

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(variable_scope.get_variable_scope(),
                                         reuse=reuse):
        outputs, state = embedding_rnn_decoder(
          decoder_inputs, initial_state, dec_cell, num_symbols,
          embedding_size, output_projection=output_projection,
          feed_previous=feed_previous_bool,
          update_embedding_for_previous=False, feed_prev_p=feed_prev_p)
        return outputs + [z]

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    return outputs_and_state[:-1], outputs_and_state[-1], kl_loss



def embedding_tied_rnn_seq2seq(encoder_inputs,
                               decoder_inputs,
                               cell,
                               num_symbols,
                               embedding_size,
                               num_decoder_symbols=None,
                               output_projection=None,
                               feed_previous=False,
                               dtype=None,
                               scope=None):
  """Embedding RNN sequence-to-sequence model with tied (shared) parameters.

  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_symbols x input_size]). Then it runs an RNN to encode embedded
  encoder_inputs into a state vector. Next, it embeds decoder_inputs using
  the same embedding. Then it runs RNN decoder, initialized with the last
  encoder state, on embedded decoder_inputs. The decoder output is over symbols
  from 0 to num_decoder_symbols - 1 if num_decoder_symbols is none; otherwise it
  is over 0 to num_symbols - 1.

  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    num_symbols: Integer; number of symbols for both encoder and decoder.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_decoder_symbols: Integer; number of output symbols for decoder. If
      provided, the decoder output is over symbols 0 to num_decoder_symbols - 1.
      Otherwise, decoder output is over symbols 0 to num_symbols - 1. Note that
      this assumes that the vocabulary is set up such that the first
      num_decoder_symbols of num_symbols are part of decoding.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has
      shape [num_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype to use for the initial RNN states (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_tied_rnn_seq2seq".

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_symbols] containing the generated
        outputs where output_symbols = num_decoder_symbols if
        num_decoder_symbols is not None otherwise output_symbols = num_symbols.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].

  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  with variable_scope.variable_scope(
      scope or "embedding_tied_rnn_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype

    if output_projection is not None:
      proj_weights = ops.convert_to_tensor(output_projection[0], dtype=dtype)
      proj_weights.get_shape().assert_is_compatible_with([None, num_symbols])
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      proj_biases.get_shape().assert_is_compatible_with([num_symbols])

    embedding = variable_scope.get_variable(
        "embedding", [num_symbols, embedding_size], dtype=dtype)

    emb_encoder_inputs = [embedding_ops.embedding_lookup(embedding, x)
                          for x in encoder_inputs]
    emb_decoder_inputs = [embedding_ops.embedding_lookup(embedding, x)
                          for x in decoder_inputs]

    output_symbols = num_symbols
    if num_decoder_symbols is not None:
      output_symbols = num_decoder_symbols
    if output_projection is None:
      cell = rnn_cell.OutputProjectionWrapper(cell, output_symbols)

    if isinstance(feed_previous, bool):
      loop_function = _extract_argmax_and_embed(
          embedding, output_projection, True) if feed_previous else None
      return tied_rnn_seq2seq(emb_encoder_inputs, emb_decoder_inputs, cell,
                              loop_function=loop_function, dtype=dtype)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      loop_function = _extract_argmax_and_embed(
        embedding, output_projection, False) if feed_previous_bool else None
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(variable_scope.get_variable_scope(),
                                         reuse=reuse):
        outputs, state = tied_rnn_seq2seq(
            emb_encoder_inputs, emb_decoder_inputs, cell,
            loop_function=loop_function, dtype=dtype)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    # Calculate zero-state to know it's structure.
    static_batch_size = encoder_inputs[0].get_shape()[0]
    for inp in encoder_inputs[1:]:
      static_batch_size.merge_with(inp.get_shape()[0])
    batch_size = static_batch_size.value
    if batch_size is None:
      batch_size = array_ops.shape(encoder_inputs[0])[0]
    zero_state = cell.zero_state(batch_size, dtype)
    if nest.is_sequence(zero_state):
      state = nest.pack_sequence_as(structure=zero_state,
                                    flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state

def attention_decoder(decoder_inputs,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False,
                      src_mask = None,
                      maxout_layer=False,
                      embedding_size=None,
                      encoder="reverse",
                      init_const=False,
                      bow_mask=None):
  """RNN decoder with attention for the sequence-to-sequence model.

  In this context "attention" means that, during decoding, the RNN can look up
  information in the additional tensor attention_states, and it does this by
  focusing on a few entries from the tensor. This model has proven to yield
  especially good results in a number of sequence-to-sequence tasks. This
  implementation is based on http://arxiv.org/abs/1412.7449 (see below for
  details). It is recommended for complex sequence-to-sequence tasks.

  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    output_size: Size of the output vectors; if None, we use cell.output_size.
    num_heads: Number of attention heads that read from attention_states.
    loop_function: If not None, this function will be applied to i-th output
      in order to generate i+1-th input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    dtype: The dtype to use for the RNN initial state (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors of
        shape [batch_size x output_size]. These represent the generated outputs.
        Output i is computed from input i (which is either the i-th element
        of decoder_inputs or loop_function(output {i-1}, i)) as follows.
        First, we run the cell on a combination of the input and previous
        attention masks:
          cell_output, new_state = cell(linear(input, prev_attn), prev_state).
        Then, we calculate new attention masks:
          new_attn = softmax(V^T * tanh(W * attention_states + U * new_state))
        and then we calculate the output:
          output = linear(cell_output, new_attn).
      state: The state of each decoder cell the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].

  Raises:
    ValueError: when num_heads is not positive, there are no inputs, shapes
      of attention_states are not set, or input size cannot be inferred
      from the input.
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s"
                     % attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value
    logging.info("Attn_length=%d attn_size=%d" % (attn_length, attn_size))

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(
        attention_states, [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    def is_LSTM_cell(cell):
      if isinstance(cell, rnn_cell.LSTMCell) or \
         isinstance(cell, rnn_cell.BasicLSTMCell):
           return True
      return False

    def is_LSTM_cell_with_dropout(cell):
      if isinstance(cell, rnn_cell.DropoutWrapper):
        if is_LSTM_cell(cell._cell):
           return True
      return False

    def init_state():
      logging.info("Init decoder state for bow")
      for i in xrange(num_heads):
        # matrix of ones
        s = array_ops.ones(array_ops.pack([batch_size, attn_length]), dtype=dtype)
        s.set_shape([None, attn_length])

        # multiply with source mask, then do softmax: a_i = 1/src_length
        if src_mask is not None:
          s = s * src_mask
        a = nn_ops.softmax(s)

        from tensorflow.models.rnn.translate.seq2seq.wrapper_cells import BOWCell
        if isinstance(cell, BOWCell) and \
          (is_LSTM_cell(cell.get_cell()) or \
           is_LSTM_cell_with_dropout(cell.get_cell()) or \
           (isinstance(cell.get_cell(), rnn_cell.MultiRNNCell) and \
            (is_LSTM_cell(cell.get_cell()._cells[0]) or \
             is_LSTM_cell_with_dropout(cell.get_cell()._cells[0])))):
            # C = SUM_t a_t * C~_t or C = SUM_t a_t * i_t * C~_t (hidden is either C~_t or i_t * C~_t, see BOWCell.embed)
            C = math_ops.reduce_sum(
                array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
            h = tanh(C)
            if is_LSTM_cell(cell.get_cell()) or \
              is_LSTM_cell_with_dropout(cell.get_cell()):
              # single LSTM cell
              return array_ops.concat(1, [C, h])
            else:
              # MultiRNNCell (multi LSTM cell)
              unit = array_ops.concat(1, [C, h])
              state = unit
              count = 1
              while (count < cell.get_cell().num_layers):
                state = array_ops.concat(1, [state, unit])
                count += 1
              return state
        else:
          raise NotImplementedError("Need to implement decoder state initialization for non-LSTM cells")

    state = initial_state
    if encoder == "bow":
      if init_const:
        # TODO: don't hardcode batch size
        b_size = 80
        state = variable_scope.get_variable("DecInit", [b_size, cell.state_size])
        logging.info("Init decoder state: {} * {} matrix".format(b_size, cell.state_size))
      else:
        state = init_state()

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(1, query_list)
      for i in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % i):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(
              v[i] * math_ops.tanh(hidden_features[i] + y), [2, 3])
          # multiply with source mask, then do softmax
          if src_mask is not None:
            s = s * src_mask
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prev = None
    batch_attn_size = array_ops.pack([batch_size, attn_size])
    attns = [array_ops.zeros(batch_attn_size, dtype=dtype)
             for _ in xrange(num_heads)]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)

    if maxout_layer:
      logging.info("Output layer consists of: Merge, Bias, Maxout, Linear, Linear")
    if bow_mask is not None:
      logging.info("Use bow mask to locally normalize output layer wrt bow vocabulary")

    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)
      x = linear([inp] + attns, input_size, True)
      # Run the RNN.
      cell_output, state = cell(x, state) # run cell on combination of input and previous attn masks

      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(variable_scope.get_variable_scope(),
                                           reuse=True):
          attns = attention(state) # calculate new attention masks (attention-weighted src vector)
      else:
        attns = attention(state) # calculate new attention masks (attention-weighted src vector)

      if maxout_layer:
        # This tries to imitate the blocks Readout layer, consisting of Merge, Bias, Maxout, Linear, Linear
        # Merge: cell.output_size
        with tf.variable_scope("AttnMergeProjection"):
          merge_output = linear([cell_output] + [inp] + attns, cell.output_size, True)

        # Bias
        b = tf.get_variable("maxout_b", [cell.output_size])
        merge_output_plus_b = tf.nn.bias_add(merge_output, b)

        # Maxout: cell.output_size --> maxout_size
        maxout_size = cell.output_size // 2
        segment_id_list = [ [i,i] for i in xrange(maxout_size) ] # make pairs of segment ids to be max-ed over
        segment_id_list = list(chain(*segment_id_list)) # flatten list
        segment_ids = tf.constant(segment_id_list, dtype=tf.int32)
        maxout_output = tf.transpose(tf.segment_max(tf.transpose(merge_output_plus_b), segment_ids)) # transpose to get shape (cell.output_size, batch_size) and reverse
        maxout_output.set_shape([None, maxout_size])

        # Linear, softmax0 (maxout_size --> embedding_size ), without bias
        with tf.variable_scope("MaxoutOutputProjection_0"):
          output_embed = linear([maxout_output], embedding_size, False)

        # Linear, softmax1 (embedding_size --> vocab_size), with bias
        with tf.variable_scope("MaxoutOutputProjection_1"):
          output = linear([output_embed], output_size, True)
      else:
        with variable_scope.variable_scope("AttnOutputProjection"):
          output = linear([cell_output] + attns, output_size, True) # calculate the output

      if bow_mask is not None:
        # Normalize output layer over subset of target words found in input bag-of-words.
        # To do this without changing the architecture, apply a mask over the output layer
        # that sets all logits for words outside the bag to zero.
        output = output * bow_mask

      if loop_function is not None:
        prev = output
      outputs.append(output)

  logging.info("output size={}".format(output.get_shape()))
  return outputs, state


def embedding_attention_decoder(decoder_inputs,
                                initial_state,
                                attention_states,
                                cell,
                                num_symbols,
                                embedding_size,
                                num_heads=1,
                                output_size=None,
                                output_projection=None,
                                feed_previous=False,
                                update_embedding_for_previous=True,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False,
                                src_mask=None,
                                maxout_layer=False,
                                encoder="reverse",
                                init_const=False,
                                bow_mask=None):
  """RNN decoder with embedding and attention and a pure-decoding option.

  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_size: Size of the output vectors; if None, use output_size.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has shape
      [num_symbols]; if provided and feed_previous=True, each fed previous
      output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    dtype: The dtype to use for the RNN initial states (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].

  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  if output_size is None:
    output_size = cell.output_size
  if output_projection is not None:
    proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
    proj_biases.get_shape().assert_is_compatible_with([num_symbols])

  with variable_scope.variable_scope(
      scope or "embedding_attention_decoder", dtype=dtype) as scope:

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])
    loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None
    emb_inp = [
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs]
    return attention_decoder(
        emb_inp,
        initial_state,
        attention_states,
        cell,
        output_size=output_size,
        num_heads=num_heads,
        loop_function=loop_function,
        initial_state_attention=initial_state_attention,
        src_mask=src_mask,
        maxout_layer=maxout_layer,
        embedding_size=embedding_size,
        encoder=encoder,
        init_const=init_const,
        bow_mask=bow_mask)

def embedding_attention_seq2seq(encoder_inputs,
                                decoder_inputs,
                                cell,
                                num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False,
                                encoder="reverse",
                                sequence_length=None,
                                bucket_length=None,
                                src_mask=None,
                                maxout_layer=False,
                                init_backward=False,
                                bow_emb_size=None,
                                init_const=False,
                                bow_mask=None,
                                keep_prob=1.0,
                                initializer=None,
                                legacy=False):
  """Embedding sequence-to-sequence model with attention.

  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x embedding_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. It keeps the outputs of this
  RNN at every step to use for attention later. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  embedding_size]). Then it runs attention decoder, initialized with the last
  encoder state, on embedded decoder_inputs and attending to encoder outputs.

  Warning: when output_projection is None, the size of the attention vectors
  and variables will be made proportional to num_decoder_symbols, can be large.

  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial RNN state (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_seq2seq".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states.

  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype
    logging.debug("Encoder")
    # Encoder.
    if encoder == "bidirectional":
      encoder_cell_fw = rnn_cell.EmbeddingWrapper(
        cell.get_fw_cell(), embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size, initializer=initializer)
      encoder_cell_bw = rnn_cell.EmbeddingWrapper(
        cell.get_bw_cell(), embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size, initializer=initializer)
      encoder_outputs, encoder_state, encoder_state_bw = rnn.bidirectional_rnn(encoder_cell_fw, encoder_cell_bw,
                                 encoder_inputs, dtype=dtype,
                                 sequence_length=sequence_length,
                                 bucket_length=bucket_length,
                                 legacy=legacy)

      #logging.debug("Bidirectional state size=%d" % cell.state_size) # this shows double the size for lstms
    elif encoder == "reverse":
      encoder_cell = rnn_cell.EmbeddingWrapper(
        cell, embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size, initializer=initializer)
      encoder_outputs, encoder_state = rnn.rnn(
        encoder_cell, encoder_inputs, dtype=dtype, sequence_length=sequence_length, bucket_length=bucket_length, reverse=True)
      #logging.debug("Unidirectional state size=%d" % cell.state_size)
    elif encoder == "bow":
      if keep_prob < 1:
        logging.info("Applying dropout to input embeddings")
      encoder_outputs, encoder_state = cell.embed(rnn_cell.Embedder, num_encoder_symbols,
                                                  bow_emb_size, encoder_inputs, dtype=dtype,
                                                  keep_prob=keep_prob, initializer=initializer)

    # First calculate a concatenation of encoder outputs to put attention on.
    if encoder == "bow":
      top_states = [array_ops.reshape(e, [-1, 1, bow_emb_size])
                  for e in encoder_outputs]
    else:
      top_states = [array_ops.reshape(e, [-1, 1, cell.output_size])
                  for e in encoder_outputs]
    attention_states = array_ops.concat(1, top_states)

    initial_state = encoder_state
    if encoder == "bidirectional" and init_backward:
      initial_state = encoder_state_bw

    logging.debug("Decoder")
    # Decoder.
    if encoder == "bidirectional":
      if init_backward:
        cell = cell.get_bw_cell()
      else:
        cell = cell.get_fw_cell()

    output_size = None
    if output_projection is None:
      #cell = rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
      output_size = num_decoder_symbols

    if isinstance(feed_previous, bool):
      return embedding_attention_decoder(
          decoder_inputs,
          initial_state,
          attention_states,
          cell,
          num_decoder_symbols,
          embedding_size,
          num_heads=num_heads,
          output_size=output_size,
          output_projection=output_projection,
          feed_previous=feed_previous,
          initial_state_attention=initial_state_attention,
          src_mask=src_mask,
          maxout_layer=maxout_layer,
          encoder=encoder,
          init_const=init_const,
          bow_mask=bow_mask)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_attention_decoder(
            decoder_inputs,
            initial_state,
            attention_states,
            cell,
            num_decoder_symbols,
            embedding_size,
            num_heads=num_heads,
            output_size=output_size,
            output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False,
            initial_state_attention=initial_state_attention,
            src_mask=src_mask,
            maxout_layer=maxout_layer,
            encoder=encoder,
            init_const=init_const,
            bow_mask=bow_mask)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(structure=encoder_state,
                                    flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state


def one2many_rnn_seq2seq(encoder_inputs,
                         decoder_inputs_dict,
                         cell,
                         num_encoder_symbols,
                         num_decoder_symbols_dict,
                         embedding_size,
                         feed_previous=False,
                         dtype=None,
                         scope=None):
  """One-to-many RNN sequence-to-sequence model (multi-task).

  This is a multi-task sequence-to-sequence model with one encoder and multiple
  decoders. Reference to multi-task sequence-to-sequence learning can be found
  here: http://arxiv.org/abs/1511.06114

  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs_dict: A dictionany mapping decoder name (string) to
      the corresponding decoder_inputs; each decoder_inputs is a list of 1D
      Tensors of shape [batch_size]; num_decoders is defined as
      len(decoder_inputs_dict).
    cell: rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols_dict: A dictionary mapping decoder name (string) to an
      integer specifying number of symbols for the corresponding decoder;
      len(num_decoder_symbols_dict) must be equal to num_decoders.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first of
      decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial state for both the encoder and encoder
      rnn cells (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "one2many_rnn_seq2seq"

  Returns:
    A tuple of the form (outputs_dict, state_dict), where:
      outputs_dict: A mapping from decoder name (string) to a list of the same
        length as decoder_inputs_dict[name]; each element in the list is a 2D
        Tensors with shape [batch_size x num_decoder_symbol_list[name]]
        containing the generated outputs.
      state_dict: A mapping from decoder name (string) to the final state of the
        corresponding decoder RNN; it is a 2D Tensor of shape
        [batch_size x cell.state_size].
  """
  outputs_dict = {}
  state_dict = {}

  with variable_scope.variable_scope(
      scope or "one2many_rnn_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype

    # Encoder.
    encoder_cell = rnn_cell.EmbeddingWrapper(
        cell, embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    _, encoder_state = rnn.rnn(encoder_cell, encoder_inputs, dtype=dtype)

    # Decoder.
    for name, decoder_inputs in decoder_inputs_dict.items():
      num_decoder_symbols = num_decoder_symbols_dict[name]

      with variable_scope.variable_scope("one2many_decoder_" + str(
          name)) as scope:
        decoder_cell = rnn_cell.OutputProjectionWrapper(cell,
                                                        num_decoder_symbols)
        if isinstance(feed_previous, bool):
          outputs, state = embedding_rnn_decoder(
              decoder_inputs, encoder_state, decoder_cell, num_decoder_symbols,
              embedding_size, feed_previous=feed_previous)
        else:
          # If feed_previous is a Tensor, we construct 2 graphs and use cond.
          def filled_embedding_rnn_decoder(feed_previous):
            """The current decoder with a fixed feed_previous parameter."""
            # pylint: disable=cell-var-from-loop
            reuse = None if feed_previous else True
            vs = variable_scope.get_variable_scope()
            with variable_scope.variable_scope(vs, reuse=reuse):
              outputs, state = embedding_rnn_decoder(
                  decoder_inputs, encoder_state, decoder_cell,
                  num_decoder_symbols, embedding_size,
                  feed_previous=feed_previous)
            # pylint: enable=cell-var-from-loop
            state_list = [state]
            if nest.is_sequence(state):
              state_list = nest.flatten(state)
            return outputs + state_list

          outputs_and_state = control_flow_ops.cond(
              feed_previous,
              lambda: filled_embedding_rnn_decoder(True),
              lambda: filled_embedding_rnn_decoder(False))
          # Outputs length is the same as for decoder inputs.
          outputs_len = len(decoder_inputs)
          outputs = outputs_and_state[:outputs_len]
          state_list = outputs_and_state[outputs_len:]
          state = state_list[0]
          if nest.is_sequence(encoder_state):
            state = nest.pack_sequence_as(structure=encoder_state,
                                          flat_sequence=state_list)
      outputs_dict[name] = outputs
      state_dict[name] = state

  return outputs_dict, state_dict


def sequence_loss_by_example(logits, targets, weights,
                             average_across_timesteps=True,
                             softmax_loss_function=None, name=None):
  """Weighted cross-entropy loss for a sequence of logits (per example).

  Args:
    logits: List of 2D Tensors of shape [batch_size x num_decoder_symbols].
    targets: List of 1D batch-sized int32 Tensors of the same length as logits.
    weights: List of 1D batch-sized float-Tensors of the same length as logits.
    average_across_timesteps: If set, divide the returned cost by the total
      label weight.
    softmax_loss_function: Function (inputs-batch, labels-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    name: Optional name for this operation, default: "sequence_loss_by_example".

  Returns:
    1D batch-sized float Tensor: The log-perplexity for each sequence.

  Raises:
    ValueError: If len(logits) is different from len(targets) or len(weights).
  """
  if len(targets) != len(logits) or len(weights) != len(logits):
    raise ValueError("Lengths of logits, weights, and targets must be the same "
                     "%d, %d, %d." % (len(logits), len(weights), len(targets)))
  with ops.name_scope(name, "sequence_loss_by_example",
                      logits + targets + weights):
    log_perp_list = []
    for logit, target, weight in zip(logits, targets, weights):
      if softmax_loss_function is None:
        # TODO(irving,ebrevdo): This reshape is needed because
        # sequence_loss_by_example is called with scalars sometimes, which
        # violates our general scalar strictness policy.
        target = array_ops.reshape(target, [-1])
        crossent = nn_ops.sparse_softmax_cross_entropy_with_logits(
            logit, target)
      else:
        crossent = softmax_loss_function(logit, target)
      log_perp_list.append(crossent * weight)
    log_perps = math_ops.add_n(log_perp_list)
    if average_across_timesteps:
      total_size = math_ops.add_n(weights)
      total_size += 1e-12  # Just to avoid division by 0 for all-0 weights.
      log_perps /= total_size
  return log_perps


def sequence_loss(logits, targets, weights,
                  average_across_timesteps=True, average_across_batch=True,
                  softmax_loss_function=None, name=None):
  """Weighted cross-entropy loss for a sequence of logits, batch-collapsed.

  Args:
    logits: List of 2D Tensors of shape [batch_size x num_decoder_symbols].
    targets: List of 1D batch-sized int32 Tensors of the same length as logits.
    weights: List of 1D batch-sized float-Tensors of the same length as logits.
    average_across_timesteps: If set, divide the returned cost by the total
      label weight.
    average_across_batch: If set, divide the returned cost by the batch size.
    softmax_loss_function: Function (inputs-batch, labels-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    name: Optional name for this operation, defaults to "sequence_loss".

  Returns:
    A scalar float Tensor: The average log-perplexity per symbol (weighted).

  Raises:
    ValueError: If len(logits) is different from len(targets) or len(weights).
  """
  with ops.name_scope(name, "sequence_loss", logits + targets + weights):
    cost = math_ops.reduce_sum(sequence_loss_by_example(
        logits, targets, weights,
        average_across_timesteps=average_across_timesteps,
        softmax_loss_function=softmax_loss_function))
    if average_across_batch:
      batch_size = array_ops.shape(targets[0])[0]
      return cost / math_ops.cast(batch_size, cost.dtype)
    else:
      return cost


def model_with_buckets_states(encoder_inputs, decoder_inputs, targets, weights,
                       buckets, seq2seq, softmax_loss_function=None,
                       per_example_loss=False, name=None, encoder_states=None,
                       feed_prev_p=None):
  if len(encoder_inputs) < buckets[-1][0]:
    raise ValueError("Length of encoder_inputs (%d) must be at least that of la"
                     "st bucket (%d)." % (len(encoder_inputs), buckets[-1][0]))
  if len(targets) < buckets[-1][1]:
    raise ValueError("Length of targets (%d) must be at least that of last"
                     "bucket (%d)." % (len(targets), buckets[-1][1]))
  if len(weights) < buckets[-1][1]:
    raise ValueError("Length of weights (%d) must be at least that of last"
                     "bucket (%d)." % (len(weights), buckets[-1][1]))
  all_inputs = encoder_inputs + decoder_inputs + targets + weights
  losses = []
  outputs = []
  states = []
  with ops.op_scope(all_inputs, name, "model_with_buckets"):
    for j, bucket in enumerate(buckets):
      with variable_scope.variable_scope(variable_scope.get_variable_scope(),
                                         reuse=True if j > 0 else None):
        seq2seq_out = seq2seq(encoder_inputs[:bucket[0]],
                              decoder_inputs[:bucket[1]],
                              bucket[0],
                              encoder_states,
                              feed_prev_p)
        outputs.append(seq2seq_out[0])
        states.append(seq2seq_out[1])
        if per_example_loss:
          losses.append(sequence_loss_by_example(
              outputs[-1], targets[:bucket[1]], weights[:bucket[1]],
              softmax_loss_function=softmax_loss_function))            
        else:
          losses.append(sequence_loss(
              outputs[-1], targets[:bucket[1]], weights[:bucket[1]],
              softmax_loss_function=softmax_loss_function))
        if len(seq2seq_out) == 3: # include KL loss
          kl_obj = seq2seq_out[2]
          total_size = math_ops.add_n(weights[:bucket[1]]) + 1e-12  # avoid division by 0
          losses[-1] = (losses[-1], tf.reduce_mean(kl_obj / total_size))
  return outputs, losses, states

def model_with_buckets(encoder_inputs, decoder_inputs, targets, weights,
                       buckets, seq2seq, softmax_loss_function=None,
                       per_example_loss=False, name=None):
  """Create a sequence-to-sequence model with support for bucketing.

  The seq2seq argument is a function that defines a sequence-to-sequence model,
  e.g., seq2seq = lambda x, y: basic_rnn_seq2seq(x, y, rnn_cell.GRUCell(24))

  Args:
    encoder_inputs: A list of Tensors to feed the encoder; first seq2seq input.
    decoder_inputs: A list of Tensors to feed the decoder; second seq2seq input.
    targets: A list of 1D batch-sized int32 Tensors (desired output sequence).
    weights: List of 1D batch-sized float-Tensors to weight the targets.
    buckets: A list of pairs of (input size, output size) for each bucket.
    seq2seq: A sequence-to-sequence model function; it takes 2 input that
      agree with encoder_inputs and decoder_inputs, and returns a pair
      consisting of outputs and states (as, e.g., basic_rnn_seq2seq).
    softmax_loss_function: Function (inputs-batch, labels-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    per_example_loss: Boolean. If set, the returned loss will be a batch-sized
      tensor of losses for each sequence in the batch. If unset, it will be
      a scalar with the averaged loss from all examples.
    name: Optional name for this operation, defaults to "model_with_buckets".

  Returns:
    A tuple of the form (outputs, losses), where:
      outputs: The outputs for each bucket. Its j'th element consists of a list
        of 2D Tensors. The shape of output tensors can be either
        [batch_size x output_size] or [batch_size x num_decoder_symbols]
        depending on the seq2seq model used.
      losses: List of scalar Tensors, representing losses for each bucket, or,
        if per_example_loss is set, a list of 1D batch-sized float Tensors.

  Raises:
    ValueError: If length of encoder_inputsut, targets, or weights is smaller
      than the largest (last) bucket.
  """
  outputs, losses, _ = model_with_buckets_states(encoder_inputs, 
                                                 decoder_inputs, 
                                                 targets, weights, 
                                                 buckets, seq2seq, 
                                                 softmax_loss_function, 
                                                 per_example_loss, name)
  return outputs, losses
