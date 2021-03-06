# -*- coding: utf-8 -*-
"""Graph dataset loader

The `GraphDataset` stores the information about how to load 
datasets. To create a dataset, you need to specify the features that the
dataset will load. Here 'features' means an instance of a subclass of the
`MyFeature` class. As an example:

```
class ShortestPathGraphDataset(data_util.GraphDataset):
  def __init__(self, data_dir, min_nodes, max_nodes):
    feature_list = [
      data_util.GraphFeature(
          key='input_graph',
          node_feature_size=5,
          edge_feature_size=1,
          global_feature_size=1,
          dtype='float32',
          description='Graph to input to network'),
      data_util.GraphFeature(
          key='target_graph',
          node_feature_size=2,
          edge_feature_size=2,
          global_feature_size=1,
          dtype='float32',
          description='Graph to output from network'),
      # Example of a non-graph feature
      data_util.TensorFeature(
          key='adj_mat_dense',
          shape=[max_nodes, max_nodes],
          dtype='float32',
          description='Sparse adjacency matrix of input graph'),
    ]
    super(ShortestPathGraphDataset, self).__init__(data_dir, feature_list)
    self.min_nodes = min_nodes
    self.max_nodes = max_nodes
  def gen_sample(self, name):
    # ... Compute graphs and labels on graphs
    return {
        'input_graph': input_graph_dict,
        'target_graph': target_graph_dict,
        'adj_mat_dense': adj_mat_dense,
    }
```

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import glob
import datetime
import abc

import numpy as np
import tensorflow as tf
import yaml
import importlib

import sys

class DataReader(object):
  """Graph dataset generator and loader

  The `GraphDataset` stores the information about how to load and generate
  datasets.
  """
  def __init__(self, data_dir):
    """Initializes GraphDataset

    Args:
      data_dir: string giving the path to where the data is/will be stored
      feature_list: list of objects that are instances of subclesses of
        MyFeature, defining the objects in each element of the dataset
    """
    self.data_dir = data_dir
    # Load config
    with open(os.path.join(self.data_dir,'config.yaml'),'r') as f:
      yaml_list = yaml.load(f)
    feature_list = []

    for yaml_dict in yaml_list:
      # TODO: Determine if this is the proper way to do this
      af = importlib.import_module('.allfeatures',package='tf_data_loader')
      cls = getattr(af, yaml_dict['__name__'])
      feature_list.append(cls.from_yaml_dict(yaml_dict))
    self.features = { feat.key: feat for feat in feature_list }

  def get_parser_op(self):
    """Returns function that parses a tfrecord Example.

    This can be with a `tf.data` dataset for parsing a tfrecord.

    Returns:
      parser_op: function taking in a record and retruning a parsed dictionary
    """
    keys_to_features = {}
    for _, value in self.features.items():
      keys_to_features.update(value.get_feature_read())

    def parser_op(record):
      example = tf.parse_single_example(record, keys_to_features)
      return {
          k: v.tensors_to_item(example)
          for k, v in self.features.items()
      }

    return parser_op

  def get_tf_dataset(self, name):
    """Return tf.data.Dataset object for this dataset from the tfrecords of mode `name`

    This is a primary function used in training, for efficient loading of data.

    Args:
      name: name of the dataset we are in (e.g. 'data_train', 'data_test', etc.)

    Returns:
      dataset: a tf.data.Dataset object for this dataset
    """
    data_sources = glob.glob(
        os.path.join(self.data_dir, name, '*.tfrecords'))
    # Build dataset provider
    dataset = tf.data.TFRecordDataset(data_sources)
    dataset = dataset.map(self.get_parser_op())
    dataset = dataset.repeat(repeat)

    return dataset

  def make_batch_with_iterator(self, iterator, batch_size):
    """Return batch loaded from this dataset from the tfrecords of mode `name`

    This is a primary function used in training, for efficient loading of data.

    Args:
      iterator: iterator made from tf.data.Dataset of this dataset
      batch_size: size (>= 1) of the batch to load

    Returns:
      features: Dictionary of key strings to values of this sample
    """
    batch = []
    batch.append(iterator.get_next())
    if batch_size > 1:
      for _ in range(batch_size-1):
        deps = []
        for _, x in batch[-1].items():
          if isinstance(x, tf.SparseTensor):
            continue
          if isinstance(x, dict):
            x_ = [ v for k, v in x.items() ]
            deps.extend(x_)
          else:
            deps.append(x)
        with tf.control_dependencies(deps):
          batch.append(iterator.get_next())

    # Constructing output sample using known order of the keys
    sample = {}
    for key, value in self.features.items():
      sample[key] = value.stack([batch[b][key] for b in range(batch_size)])
    return sample

  def get_standard_batch(self,
                         name,
                         batch_size,
                         shuffle_data=True,
                         buffer_size=None,
                         repeat=None):
    """Convenience function for loading batch from this dataset's tfrecords of mode `name`

    Args:
      name: name of the dataset we are in (e.g. 'data_train', 'data_test', etc.)
      batch_size: size (>= 1) of the batch to load
      shuffle_data: (boolean, Default=True) Whether to shuffle data or not
      buffer_size: (size, Default=`5*batch_size`) Buffer size for loading
      repeat: (size, Default=`None`) Number of times to repeat the
              dataset, `None` if looping forever.

    Returns:
      features: Dictionary of key strings to values of this sample
    """
    if buffer_size == None:
      buffer_size = 5 * batch_size
    # Gather data
    if shuffle_data:
      np.random.shuffle(data_sources)  # Added to help the shuffle
    data_sources = glob.glob(
        os.path.join(self.data_dir, name, '*.tfrecords'))
    # Build dataset provider
    dataset = tf.data.TFRecordDataset(data_sources)
    dataset = dataset.map(self.get_parser_op())
    dataset = dataset.repeat(repeat)
    if shuffle_data:
      dataset = dataset.shuffle(buffer_size=buffer_size)

    iterator = dataset.make_one_shot_iterator()
    batch = self.make_batch_with_iterator(iterator, batch_size)
    return batch


class DataNpzReader(DataReader):
  """Graph dataset loader

  The `GraphDataset` stores the information about how to load datasets.
  """

  def get_placeholders(self, batch=True):
    """Gets appropriate dictionary of placeholders all features of this dataset.

    In the case you are not using tfrecords, this function builds all the
    appropriate placeholders and returns them in a dictionary. It also handles
    batching of values within the placeholder.

    Args:
      batch: (bool, default=True) Whether to batch the output

    Returns:
      placeholders: Dictionary of key strings to placeholders for this dataset
    """
    # Build placeholders
    placeholders = {}
    sample = {}
    for key, feat in self.features.items():
      ph, val = feat.get_placeholder_and_feature(batch=batch)
      placeholders.update(ph)
      sample[key] = val
    # Other placeholders
    return sample, placeholders

  def get_feed_dict(self, placeholders, value_dict, batch):
    """Get the `feed_dict` for this dataset, mapping placeholders to values.

    This creates the `feed_dict` by mapping the appropriate placeholders to the
    values provided in value_dict. Also handles batching of values within the
    placeholders.

    Args:
      placeholders: Dictionary of key strings to placeholders
      value_dict: Dictionary of key strings values (typically np.ndarrays or
        lists) needed to build this feature
      batch: (bool, default=True) Whether to batch the output

    Returns:
      feed_dict: Dictionary of placeholders to values for this feature
    """
    feed_dict = {}
    for _, value in self.features.items():
      feed_dict.update(value.get_feed_dict(placeholders, value_dict, batch))
    return feed_dict

  def load_npz_file(self, name, index):
    """
    """
    fname = os.path.join(self.data_dir, name, '{:04d}.npz'.format(index))
    with open(fname, 'rb') as npz_file:
      npz_dict = dict(np.load(npz_file))
    return npz_dict


