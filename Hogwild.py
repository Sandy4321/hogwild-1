# Copyright (c) 2018, NVIDIA CORPORATION. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import tensorflow as tf
#from tensorflow.python.estimator.training import _DELAY_SECS_PER_WORKER, _MAX_DELAY_SECS
from tensorflow.python import estimator
estimator.training._DELAY_SECS_PER_WORKER = 0
import argparse
import time
import os
import json
import numpy as np

class _LoggerHook(tf.train.SessionRunHook):
  """Logs loss and runtime."""
  def __init__(self, log_frequency):
    self.log_frequency = log_frequency
    if FLAGS.log_dir is not None:
      self.fname = os.path.join(FLAGS.log_dir, 'log')
    else:
      self.fname = '/tmp/log'
    super(_LoggerHook, self).__init__()

  def begin(self):
    self._start_time = time.time()
    self._start_time -= min(
      estimator.training._DELAY_SECS_PER_WORKER*FLAGS.task_index,
      estimator.training._MAX_DELAY_SECS)

  def before_run(self, run_context):
    return tf.train.SessionRunArgs(tf.train.get_global_step())

  def after_run(self, run_context, run_values):
    step = run_values.results + 1
    if step % self.log_frequency == 0:
      current_time = time.time()
      duration = current_time - self._start_time

      examples_per_sec = step * FLAGS.batch_size / duration
      sec_per_batch = duration / step

      format_str = "Step {}: {:0.1f} examples/sec; {:0.4f} sec/batch\n"
      self.save(format_str.format(step, examples_per_sec, sec_per_batch))

  def save(self, string):
    with open(self.fname, 'a') as f:
      f.write(string)    

  def end(self, session):
      if FLAGS.job_name == 'worker': # and FLAGS.task_index == 0:
        total_time = time.time() - self._start_time
        self.save("Training complete, total time {:0.3f} s\n".format(total_time))


def model_fn(
    features,
    labels,
    mode,
    params):
  net = tf.feature_column.input_layer(features, params['feature_columns'])
  with tf.variable_scope('embedding') as embedding_scope:
    embedding_dim = params['hidden_units'].pop(0)
    embedding_matrix = tf.get_variable('embedding_matrix', shape=(params['dense_size'], embedding_dim))

    indices = tf.cast(net[:, :2], tf.int64)
    ids = tf.cast(net[:, 2], tf.int64)
    values = net[:, 3]

    dense_shape = [params['batch_size'], params['max_nnz']]
    sp_ids = tf.SparseTensor(indices=indices, values=ids, dense_shape=dense_shape)
    sp_weights = tf.SparseTensor(indices=indices, values=values, dense_shape=dense_shape)

    net = tf.nn.embedding_lookup_sparse(
      embedding_matrix,
      sp_ids=sp_ids,
      sp_weights=sp_weights,
      combiner='sum')
    net = tf.nn.relu(net)

  for i, units in enumerate(params['hidden_units']):
    with tf.variable_scope('hiddenlayer{}'.format(i+1)) as hidden_layer_scope:
      net = tf.layers.dense(net, units=units, activation=tf.nn.relu)

  logits = tf.layers.dense(net, params['n_classes'], activation=None)
  loss = tf.losses.sparse_softmax_cross_entropy(labels=labels, logits=logits)

  if mode == tf.estimator.ModeKeys.TRAIN:
    optimizer = tf.train.GradientDescentOptimizer(0.001)
    train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())
    return tf.estimator.EstimatorSpec(mode, loss=loss, train_op=train_op)

  predicted_classes = tf.argmax(logits, 1)
  if mode == tf.estimator.ModeKeys.PREDICT:
    predictions = {
        'class_ids': predicted_classes[:, tf.newaxis],
        'probabilities': tf.nn.softmax(logits),
        'logits': logits,
    }
    return tf.estimator.EstimatorSpec(mode, predictions=predictions)

  accuracy = tf.metrics.accuracy(
    labels=labels,
    predictions=predicted_classes,
    name='acc_op')
  metrics = {'accuracy': accuracy}
  tf.summary.scalar('accuracy', accuracy[1])
  if mode == tf.estimator.ModeKeys.EVAL:
    return tf.estimator.EstimatorSpec(
      mode, loss=loss, eval_metric_ops=metrics)


def main():
  n_classes = 10

  gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.5, allow_growth=True)
  session_config = tf.ConfigProto(gpu_options=gpu_options)
  config = tf.estimator.RunConfig(
    model_dir=FLAGS.model_dir,
    session_config=session_config,
    save_checkpoints_steps=FLAGS.log_frequency if FLAGS.model_dir is not None else None,
    save_checkpoints_secs=None)    

  columns = [
    tf.feature_column.numeric_column('batch_idx', dtype=tf.int32),
    tf.feature_column.numeric_column('idx_row', dtype=tf.int32),
    tf.feature_column.numeric_column('embedding_row', dtype=tf.int32),
    tf.feature_column.numeric_column('value', dtype=tf.float32)]

  estimator_params = {
    'feature_columns': columns,
    'max_nnz': FLAGS.max_nnz,
    'hidden_units': FLAGS.hidden_sizes,
    'batch_size': FLAGS.batch_size,
    'n_classes': n_classes,
    'dense_size': FLAGS.dense_size}

  estimator = tf.estimator.Estimator(
    model_fn=model_fn,
    params=estimator_params,
    config=config)

  def train_input_gen():
    generate_size = lambda: np.random.randint(FLAGS.min_nnz, FLAGS.max_nnz)
    generate_idx = lambda : np.random.randint(estimator_params['dense_size'], size=generate_size())
    while True:
      nz_idx = [generate_idx() for _ in range(FLAGS.batch_size)]
      batch_idx = np.repeat(np.arange(FLAGS.batch_size), [len(i) for i in nz_idx])
      idx_row = [np.arange(len(idx)) for idx in nz_idx]
      values = [np.random.uniform(5, size=len(idx)) for idx in nz_idx]
      X = {
        'batch_idx': batch_idx,
        'idx_row': np.concatenate(idx_row),
        'embedding_row': np.concatenate(nz_idx),
        'value': np.concatenate(values)}
      y = np.random.randint(n_classes, size=FLAGS.batch_size)
      yield X, y

  def train_input_fn():
    dtypes = ({col.name: col.dtype for col in columns}, tf.int32)
    dataset = tf.data.Dataset.from_generator(train_input_gen, dtypes)
    return dataset.make_one_shot_iterator().get_next()

  train_hooks = [_LoggerHook(FLAGS.log_frequency)]
  if FLAGS.profile_dir is not None and FLAGS.job_name=='worker':
    print('Profiling at {}'.format(FLAGS.profile_dir))
    train_hooks.append(tf.train.ProfilerHook(
      save_steps=FLAGS.log_frequency,
      output_dir=FLAGS.profile_dir,
      show_dataflow=True,
      show_memory=True))

  train_spec = tf.estimator.TrainSpec(input_fn=train_input_fn, max_steps=FLAGS.steps, hooks=train_hooks)
  eval_spec = tf.estimator.EvalSpec(input_fn=train_input_fn)
  tf.estimator.train_and_evaluate(estimator, train_spec, eval_spec)

if __name__ == '__main__':
  parser = argparse.ArgumentParser()

  # Flags for defining cluster
  parser.add_argument(
    "--job_name",
    type=str,
    default="worker",
    choices=["worker", "ps", "chief"],
    help="One of 'chief', 'worker', or 'ps'")

  parser.add_argument(
    "--task_index",
    type=int,
    default=0,
    help="Index of task within the job")

  parser.add_argument(
    "--num_tasks",
    type=int,
    default=1,
    help="Number of worker tasks")

  # Flags for defining model properties
  parser.add_argument(
    "--hidden_sizes",
    type=int,
    nargs="+",
    default=[128, 64],
    help="hidden dimensions of mlp")

  parser.add_argument(
    "--batch_size",
    type=int,
    default=8,
    help="batch size")

  parser.add_argument(
    "--steps",
    type=int,
    default=2000,
    help="total number of gradient updates to apply")

  parser.add_argument(
    "--model_dir",
    type=str,
    default=None,
    help="where to save model checkpoints")

  parser.add_argument(
    "--log_frequency",
    type=int,
    default=100,
    help="number of steps between print logging")

  parser.add_argument(
    "--log_dir",
    type=str,
    default=None,
    help="Where to save print log")

  parser.add_argument(
    "--profile_dir",
    type=str,
    default=None,
    help="where to save profiler timelines")

  # sparsity flags
  parser.add_argument(
    "--dense_size",
    type=int,
    default=1<<20,
    help="Dimensionality of input space")

  parser.add_argument(
    "--max_nnz",
    type=int,
    default=100,
    help="maximum number of nonzero elements in a sample")

  parser.add_argument(
    "--min_nnz",
    type=int,
    default=10,
    help="minimum number of nonzero elements in a sample")

  FLAGS, unparsed = parser.parse_known_args()
  if FLAGS.dense_size < 30:
    FLAGS.dense_size = 1 << FLAGS.dense_size

  cluster = {
    'ps': ['localhost: 2221'],
    'chief': ['localhost:2222'],
    'worker': ['localhost:{}'.format(i+2223) for i in range(FLAGS.num_tasks)]
  }
  os.environ['TF_CONFIG'] = json.dumps(
    {'cluster': cluster,
     'task': {'type': FLAGS.job_name, 'index': FLAGS.task_index}})
  main()
