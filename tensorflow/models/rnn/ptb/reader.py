# Copyright 2015 Google Inc. All Rights Reserved.
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


"""Utilities for parsing PTB text files."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import os

import numpy as np
import tensorflow as tf


def _read_words(filename):
  with tf.gfile.GFile(filename, "r") as f:
    return f.read().replace("\n", "<eos>").split()


def _build_vocab(filename):
  data = _read_words(filename)

  counter = collections.Counter(data)
  count_pairs = sorted(counter.items(), key=lambda x: (-x[1], x[0]))

  words, _ = list(zip(*count_pairs))
  word_to_id = dict(zip(words, range(len(words))))

  return word_to_id


def _file_to_word_ids(filename, word_to_id):
  data = _read_words(filename)
  return [word_to_id[word] for word in data]


def ptb_raw_data(data_path=None):
  """Load PTB raw data from data directory "data_path".

  Reads PTB text files, converts strings to integer ids,
  and performs mini-batching of the inputs.

  The PTB dataset comes from Tomas Mikolov's webpage:

  http://www.fit.vutbr.cz/~imikolov/rnnlm/simple-examples.tgz

  Args:
    data_path: string path to the directory where simple-examples.tgz has
      been extracted.

  Returns:
    tuple (train_data, valid_data, test_data, vocabulary)
    where each of the data objects can be passed to PTBIterator.
  """

  train_path = os.path.join(data_path, "ptb.train.txt")
  valid_path = os.path.join(data_path, "ptb.valid.txt")
  test_path = os.path.join(data_path, "ptb.test.txt")

  word_to_id = _build_vocab(train_path)
  train_data = _file_to_word_ids(train_path, word_to_id)
  valid_data = _file_to_word_ids(valid_path, word_to_id)
  test_data = _file_to_word_ids(test_path, word_to_id)
  vocabulary = len(word_to_id)
  return train_data, valid_data, test_data, vocabulary

def read_indexed_data(filename, max_train_data_size=0, vocab_size=None):
  data = []
  with tf.gfile.GFile(filename, "r") as f:
    line_nr = 0
    for line in f:
      tok_ids = [ int(x) for x in line.split() ]
      if vocab_size:
        tok_ids = [ tok if tok < vocab_size else 0 for tok in tok_ids ] # 0 = UNK_ID
      tok_ids.append(2) # EOS
      data.extend(tok_ids)
      line_nr += 1
      if max_train_data_size > 0 and \
        line_nr >= max_train_data_size:
        break
  return data

def indexed_data(data_path=None, max_train_data_size=0, vocab_size=None, lang="de", default_filenames=False):
  print("LANG={}".format(lang))
  if default_filenames:
    train_path = os.path.join(data_path, "train.ids." + lang)
    valid_path = os.path.join(data_path, "dev.ids." + lang)
    test_path = os.path.join(data_path, "test.ids." + lang)    
  else:
    if lang == "de":
      train_path = os.path.join(data_path, "train/news2015.ids50003.de")
      valid_path = os.path.join(data_path, "dev/dev.ids50003.de")
      test_path = os.path.join(data_path, "test15/test15.ids50003.de")
    elif lang == "en":
      train_path = os.path.join(data_path, "train/train.ids.en")
      valid_path = os.path.join(data_path, "dev/dev.ids.en")
      test_path = os.path.join(data_path, "test/test.ids.en")
    else:
      print("ERROR: undefined language {}".format(lang))
      import sys
      sys.exit(1)

  train_data = read_indexed_data(train_path, max_train_data_size, vocab_size=vocab_size)
  valid_data = read_indexed_data(valid_path, vocab_size=vocab_size)
  test_data = read_indexed_data(test_path, vocab_size=vocab_size)
  return train_data, valid_data, test_data
  
def indexed_data_test(data_path=None, max_test_data_size=0, vocab_size=None):
  test_path = os.path.join(data_path, "test15/test15.ids50003.de")
  test_data = read_indexed_data(test_path, max_test_data_size, vocab_size)
  return test_data  

def ptb_iterator(raw_data, batch_size, num_steps, start_idx=0):
  """Iterate on the raw PTB data.

  This generates batch_size pointers into the raw PTB data, and allows
  minibatch iteration along these pointers.

  Args:
    raw_data: one of the raw data outputs from ptb_raw_data.
    batch_size: int, the batch size.
    num_steps: int, the number of unrolls.

  Yields:
    Pairs of the batched data, each a matrix of shape [batch_size, num_steps].
    The second element of the tuple is the same data time-shifted to the
    right by one.

  Raises:
    ValueError: if batch_size or num_steps are too high.
  """
  raw_data = np.array(raw_data, dtype=np.int32)

  data_len = len(raw_data)
  batch_len = data_len // batch_size
  data = np.zeros([batch_size, batch_len], dtype=np.int32)
  for i in range(batch_size):
    data[i] = raw_data[batch_len * i:batch_len * (i + 1)]

  epoch_size = (batch_len - 1) // num_steps

  if epoch_size == 0:
    raise ValueError("epoch_size == 0, decrease batch_size or num_steps")

  for i in range(start_idx, epoch_size):
    x = data[:, i*num_steps:(i+1)*num_steps]
    y = data[:, i*num_steps+1:(i+1)*num_steps+1]
    yield (x, y)
