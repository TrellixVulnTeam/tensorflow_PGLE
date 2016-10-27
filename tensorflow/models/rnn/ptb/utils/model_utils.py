import re, os
import logging
import tensorflow as tf

from tensorflow.models.rnn.ptb.rnnlm import RNNLMModel

class Config(object): pass

class SmallConfig(object):
  """Small config."""
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 2
  num_steps = 20
  hidden_size = 200
  max_epoch = 4
  max_max_epoch = 13
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000

class MediumConfig(object):
  """Medium config."""
  init_scale = 0.05
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 2
  num_steps = 35
  hidden_size = 650
  max_epoch = 6
  max_max_epoch = 39
  keep_prob = 0.5
  lr_decay = 0.8
  batch_size = 20
  vocab_size = 10000

class MediumConfig16k(object):
  """Medium config with 16k vocab."""
  init_scale = 0.05
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 2
  num_steps = 35
  hidden_size = 650
  max_epoch = 6
  max_max_epoch = 39
  keep_prob = 0.5
  lr_decay = 0.8
  batch_size = 20
  vocab_size = 16162

class MediumConfigChars(object):
  """Config for character rnnlm."""
  init_scale = 0.05
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 2
  num_steps = 100
  hidden_size = 1000
  max_epoch = 1
  max_max_epoch = 15
  keep_prob = 0.5
  lr_decay = 0.8
  batch_size = 20
  vocab_size = 90  

class LargeConfig(object):
  """Large config."""
  init_scale = 0.04
  learning_rate = 1.0
  max_grad_norm = 10
  num_layers = 2
  num_steps = 35
  hidden_size = 1500
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 1 / 1.15
  batch_size = 20
  vocab_size = 10000

class LargeConfig50k(object):
  """Large config."""
  init_scale = 0.04
  learning_rate = 1.0
  max_grad_norm = 10
  num_layers = 2
  num_steps = 35
  hidden_size = 1500
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 1 / 1.15
  batch_size = 80
  vocab_size = 50003

class TestConfig(object):
  """Tiny config, for testing."""
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 1
  num_layers = 1
  num_steps = 2
  hidden_size = 2
  max_epoch = 1
  max_max_epoch = 1
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000

def get_config(model_config):
  if model_config == "small":
    return SmallConfig()
  elif model_config == "medium":
    return MediumConfig()
  elif model_config == "medium16k":
    return MediumConfig16k()
  elif model_config == "medium_chars":
    return MediumConfigChars()
  elif model_config == "large":
    return LargeConfig()
  elif model_config == "large50k":
    return LargeConfig50k()
  elif model_config == "test":
    return TestConfig()
  else:
    raise ValueError("Invalid model: %s", model_config)

def read_config(config_file):
  config = Config()
  logging.info("Settings from tensorflow config file:")  
  with open(config_file) as f:
    for line in f:
      key,value = line.strip().split(": ")
      if re.match("^\d+$", value):
        value = int(value)
      elif re.match("^[\d\.]+$", value):
        value = float(value)
      setattr(config, key, value)
      logging.info("{}: {}".format(key, value))
  return config

def create_model(session, config, eval_config, train_dir, optimizer):
  initializer = tf.random_uniform_initializer(-config.init_scale, config.init_scale)
  with tf.variable_scope("model", reuse=None, initializer=initializer):
    model = RNNLMModel(is_training=True, config=config, optimizer=optimizer)
  with tf.variable_scope("model", reuse=True, initializer=initializer):
    mvalid = RNNLMModel(is_training=False, config=config)
    mtest = RNNLMModel(is_training=False, config=eval_config)

  ckpt = tf.train.get_checkpoint_state(train_dir)
  if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
    logging.info("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    logging.info("Created model with fresh parameters.")
    session.run(tf.initialize_all_variables())
  return model, mvalid, mtest

def load_model(session, model_config, train_dir, use_log_probs=False):
  # Create and load model for decoding
  # If model_config is a path, read config from that path, else treat as config name
  if os.path.exists(model_config):
    config = read_config(model_config)
  else:
    config = get_config(model_config)
  config.batch_size = 1
  config.num_steps = 1

  with tf.variable_scope("model", reuse=None):
    model = RNNLMModel(is_training=False, config=config, use_log_probs=use_log_probs)

  ckpt = tf.train.get_checkpoint_state(train_dir)
  if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
    logging.info("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    logging.error("Could not find model in directory %s." % train_dir)
    exit(1)
  return model, config
