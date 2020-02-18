import tensorflow as tf

from detection_src.constants import SHUFFLE_BUFFER_SIZE, NUM_THREADS, RESIZE_METHOD
from detection_src.input_pipeline.random_image_crop import random_image_crop
from detection_src.input_pipeline.other_augmentations import random_color_manipulations, \
    random_flip_left_right, random_pixel_value_scale, random_jitter_boxes


class Pipeline:
    """Input pipeline for training or evaluating object detectors."""

    def __init__(self, filenames, is_training, batch_size,
                 load_size=286, fine_size=256, shuffle=False):
        self.is_training = is_training
        self.load_size = load_size
        self.fine_size = fine_size
        self.batch_size = batch_size

        def get_num_samples(filename):
            return sum(1 for _ in tf.python_io.tf_record_iterator(filename))

        num_examples = 0
        for filename in filenames:
            num_examples_in_file = get_num_samples(filename)
            assert num_examples_in_file > 0
            num_examples += num_examples_in_file
        self.num_examples = num_examples
        assert self.num_examples > 0

        dataset = tf.data.Dataset.from_tensor_slices(filenames)
        self.num_shards = len(filenames)

        if shuffle:
            dataset = dataset.shuffle(buffer_size=self.num_shards)

        dataset = dataset.flat_map(tf.data.TFRecordDataset)
        dataset = dataset.prefetch(buffer_size=batch_size * 2)

        # if shuffle:
        #     dataset = dataset.shuffle(buffer_size=SHUFFLE_BUFFER_SIZE)
        dataset = dataset.map(self._parse_and_preprocess, num_parallel_calls=NUM_THREADS)

        # we need batches of fixed size
        padded_shapes = ([self.fine_size, self.fine_size, 3], [3], [None, 4], [], [])
        dataset = dataset.padded_batch(batch_size, padded_shapes, drop_remainder=True)
        dataset = dataset.prefetch(buffer_size=1)

        self.iterator = dataset.make_initializable_iterator()

    def get_init_op_and_next_el(self):
        """
        :return:
            init_op: for initializer
            next_el: to get next data (feature, shape, boxes, num_boxes, filename)
        """
        init_op = self.iterator.initializer
        next_el = self.iterator.get_next()
        return init_op, next_el, self.num_shards

    def _parse_and_preprocess(self, example_proto):
        """What this function does:
        1. Parses one record from a tfrecords file and decodes it.
        2. (optionally) Augments it.
        Returns:
            image: a float tensor with shape [image_height, image_width, 3],
                an RGB image with pixel values in the range [0, 1].
            boxes: a float tensor with shape [num_boxes, 4].
            num_boxes: an int tensor with shape [].
            filename: a string tensor with shape [].
        """
        features = {
            'filename': tf.FixedLenFeature([], tf.string),
            'img_shape': tf.FixedLenFeature([3], tf.int64),
            'image': tf.FixedLenFeature([], tf.string),
            'ymin': tf.FixedLenSequenceFeature([], tf.float32, allow_missing=True),
            'xmin': tf.FixedLenSequenceFeature([], tf.float32, allow_missing=True),
            'ymax': tf.FixedLenSequenceFeature([], tf.float32, allow_missing=True),
            'xmax': tf.FixedLenSequenceFeature([], tf.float32, allow_missing=True),
        }
        parsed_features = tf.parse_single_example(example_proto, features)

        # get image
        image = tf.image.decode_jpeg(parsed_features['image'], channels=3)
        image = tf.image.convert_image_dtype(image, tf.float32)
        # now pixel values are scaled to [0, 1] range

        # get ground truth boxes, they must be in from-zero-to-one format
        boxes = tf.stack([
            parsed_features['ymin'], parsed_features['xmin'],
            parsed_features['ymax'], parsed_features['xmax']
        ], axis=1)
        boxes = tf.to_float(boxes)
        # it is important to clip here!
        boxes = tf.clip_by_value(boxes, clip_value_min=0.0, clip_value_max=1.0)

        if self.is_training:
            image, boxes = random_image_crop(
                image, boxes, probability=0.9,
                min_object_covered=0.9,
                aspect_ratio_range=(0.93, 1.07),
                area_range=(0.4, 0.9),
                overlap_thresh=0.4
            )
            image = tf.image.resize_images(image, size=[self.fine_size, self.fine_size])
        else:
            image = tf.image.resize_images(image, size=[self.fine_size, self.fine_size])
        image = tf.clip_by_value(image, clip_value_min=0.0, clip_value_max=1.0)
        image = (image * 2.0) - 1.0
        # now pixel values are scaled to [-1, 1] range

        img_shape = tf.to_int32(parsed_features['img_shape'])
        num_boxes = tf.to_int32(tf.shape(boxes)[0])
        filename = parsed_features['filename']
        return image, img_shape, boxes, num_boxes, filename
