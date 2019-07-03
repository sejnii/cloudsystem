#
# -*- coding: utf-8 -*-
#
# Copyright (c) 2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: EPL-2.0
#


# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from tensorflow.data.experimental import parallel_interleave
from tensorflow.data.experimental import map_and_batch
from tensorflow.python.platform import gfile


def parse_example_proto(example_serialized):
  """Parses an Example proto containing a training example of an image.
  """
  # Dense features in Example proto.
  feature_map = {
    'image/encoded': tf.FixedLenFeature([], dtype=tf.string,
                                        default_value=''),
    'image/class/label': tf.FixedLenFeature([1], dtype=tf.int64,
                                            default_value=-1),
  }
  sparse_float32 = tf.VarLenFeature(dtype=tf.float32)
  # Sparse features in Example proto.
  feature_map.update(
    {k: sparse_float32 for k in ['image/object/bbox/xmin',
                                 'image/object/bbox/ymin',
                                 'image/object/bbox/xmax',
                                 'image/object/bbox/ymax']})

  features = tf.parse_single_example(example_serialized, feature_map)
  label = tf.cast(features['image/class/label'], dtype=tf.int32)

  return features['image/encoded'], label


def eval_image(image, height, width, resize_method,
               central_fraction=0.875, scope=None):
  with tf.name_scope('eval_image'):
    if resize_method == 'crop':
      shape = tf.shape(image)
      image = tf.cond(tf.less(shape[0], shape[1]),
                      lambda: tf.image.resize_images(image,
                                                     tf.convert_to_tensor([256, 256 * shape[1] / shape[0]],
                                                                          dtype=tf.int32)),
                      lambda: tf.image.resize_images(image,
                                                     tf.convert_to_tensor([256 * shape[0] / shape[1], 256],
                                                                          dtype=tf.int32)))
      shape = tf.shape(image)
      y0 = (shape[0] - height) // 2
      x0 = (shape[1] - width) // 2
      distorted_image = tf.image.crop_to_bounding_box(image, y0, x0, height, width)
      distorted_image.set_shape([height, width, 3])
      means = tf.broadcast_to([123.68, 116.78, 103.94], tf.shape(distorted_image))
      return distorted_image - means
    else:  # bilinear
      if image.dtype != tf.float32:
        image = tf.image.convert_image_dtype(image, dtype=tf.float32)
      # Crop the central region of the image with an area containing 87.5% of
      # the original image.
      if central_fraction:
        image = tf.image.central_crop(image, central_fraction=central_fraction)

      if height and width:
        # Resize the image to the specified height and width.
        image = tf.expand_dims(image, 0)
        image = tf.image.resize_bilinear(image, [height, width],
                                         align_corners=False)
        image = tf.squeeze(image, [0])
      image = tf.subtract(image, 0.5)
      image = tf.multiply(image, 2.0)
      return image


class RecordInputImagePreprocessor(object):
  """Preprocessor for images with RecordInput format."""

  def __init__(self,
               height,
               width,
               batch_size,
               num_cores,
               resize_method):

    self.height = height
    self.width = width
    self.batch_size = batch_size
    self.num_cores = num_cores
    self.resize_method = resize_method

  def parse_and_preprocess(self, value):
    # parse
    image_buffer, label_index = parse_example_proto(value)
    # preprocess
    image = tf.image.decode_jpeg(
      image_buffer, channels=3, fancy_upscaling=False, dct_method='INTEGER_FAST')
    image = eval_image(image, self.height, self.width, self.resize_method)

    return (image, label_index)

  def minibatch(self, dataset, subset, cache_data=False):

    with tf.name_scope('batch_processing'):

      glob_pattern = dataset.tf_record_pattern(subset)
      file_names = gfile.Glob(glob_pattern)
      if not file_names:
        raise ValueError('Found no files in --data_dir matching: {}'
                         .format(glob_pattern))
      ds = tf.data.TFRecordDataset.list_files(file_names)

      ds = ds.apply(
        parallel_interleave(
          tf.data.TFRecordDataset, cycle_length=self.num_cores, block_length=5,
          sloppy=True,
          buffer_output_elements=10000, prefetch_input_elements=10000))

      if cache_data:
        ds = ds.take(1).cache().repeat()

      ds = ds.prefetch(buffer_size=10000)
      # ds = ds.prefetch(buffer_size=self.batch_size)

      # num of parallel batches not greater than 56
      max_num_parallel_batches = min(56, 2*self.num_cores)
      ds = ds.apply(
        map_and_batch(
          map_func=self.parse_and_preprocess,
          batch_size=self.batch_size,
          num_parallel_batches=max_num_parallel_batches,
          num_parallel_calls=None))  # this number should be tuned

      ds = ds.prefetch(buffer_size=tf.contrib.data.AUTOTUNE)  # this number can be tuned

      ds_iterator = ds.make_one_shot_iterator()
      images, _ = ds_iterator.get_next()

      return images
