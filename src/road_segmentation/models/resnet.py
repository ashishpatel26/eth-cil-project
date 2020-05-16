import typing

import tensorflow as tf

"""
Implementation of individual blocks and full ResNet backbones according to (https://arxiv.org/abs/1512.03385).
"""


class ResNetBackbone(tf.keras.Model):
    """
    TODO: All documentation
    """

    _INITIAL_FILTERS = 64
    _KERNEL_INITIALIZER = 'he_normal'

    def __init__(
            self,
            blocks: typing.Iterable[int],
            weight_decay: float = 1e-4
    ):
        super(ResNetBackbone, self).__init__()

        # TODO: This does not handle dilation yet

        # Layer 1: Initial convolution
        # TODO: The FastFCN might be using a 'deep_base' layer, investigate that
        self.conv_in = tf.keras.layers.Conv2D(
            filters=self._INITIAL_FILTERS,
            kernel_size=7,
            strides=(2, 2),
            padding='same',
            activation=None,
            kernel_initializer=self._KERNEL_INITIALIZER,
            kernel_regularizer=tf.keras.regularizers.l2(weight_decay)
        )
        self.batch_norm_in = tf.keras.layers.BatchNormalization()
        self.activation_in = tf.keras.layers.ReLU()

        # Layers 2 and later, starts with a max-pool layer
        self.pool_in = tf.keras.layers.MaxPooling2D(pool_size=(2, 2))
        self.residual_layers = [
            ResNetLayer(
                current_blocks,
                self._INITIAL_FILTERS * (2 ** idx),
                downsample=(idx > 0)  # No downsampling on first ResNet block due to the initial max pooling
            )
            for idx, current_blocks in enumerate(blocks)
        ]

    def call(self, inputs, training=None, mask=None):
        # Initial convolution
        with tf.keras.backend.name_scope('conv1'):
            initial_features = self.conv_in(inputs)
            initial_features = self.batch_norm_in(initial_features)
            initial_features = self.activation_in(initial_features)

        # Max-pooling is done on layer 2, here explicitly to simplify the loop
        with tf.keras.backend.name_scope('conv2'):
            features = self.pool_in(initial_features)

        # Layers 2 and later
        for layer_number, current_layer in enumerate(self.residual_layers, start=2):
            with tf.keras.backend.name_scope(f'conv{layer_number}'):
                features = current_layer(features)

        return features


class ResNet50Backbone(ResNetBackbone):
    """
    Convenience class to instantiate a ResNet-50 backbone with the correct number of blocks per layer.
    """

    def __init__(self):
        super(ResNet50Backbone, self).__init__(
            blocks=[3, 4, 6, 3]
        )


class ResNet101Backbone(ResNetBackbone):
    """
    Convenience class to instantiate a ResNet-101 backbone with the correct number of blocks per layer.
    """

    def __init__(self):
        super(ResNet101Backbone, self).__init__(
            blocks=[3, 4, 23, 3]
        )


class ResNetLayer(tf.keras.layers.Layer):
    """
    TODO: All documentation
    """

    def __init__(
            self,
            blocks: int,
            initial_features: int,
            downsample: bool,
            **kwargs
    ):
        super(ResNetLayer, self).__init__(**kwargs)

        pass  # TODO

    def call(self, inputs, **kwargs):
        pass  # TODO


class BottleneckBlock(tf.keras.layers.Layer):
    """
    TODO: All documentation
    """

    def __init__(
            self,
            filters_in: int,
            downsample: bool,
            kernel_initializer: str,
            weight_decay: float,
            **kwargs
    ):
        super(BottleneckBlock, self).__init__(**kwargs)

        # Initial 1x1 convolution
        # FIXME: The FastFCN paper implements downsampling differently, the original ResNet does as follows
        strides_in = (2, 2) if downsample else (1, 1)
        self.conv_in = tf.keras.layers.Conv2D(
            filters=filters_in,
            kernel_size=1,
            strides=strides_in,
            padding='same',
            activation=None,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=tf.keras.regularizers.l2(weight_decay)
        )
        self.batch_norm_in = tf.keras.layers.BatchNormalization()
        self.activation_in = tf.keras.layers.ReLU()

        # Middle 3x3 convolution
        self.conv_middle = tf.keras.layers.Conv2D(
            filters=filters_in,
            kernel_size=3,
            padding='same',
            activation=None,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=tf.keras.regularizers.l2(weight_decay)
        )
        self.batch_norm_middle = tf.keras.layers.BatchNormalization()
        self.activation_middle = tf.keras.layers.ReLU()

        # Output 1x1 convolution without activation (is done externally) and 4x as many filters
        self.conv_out = tf.keras.layers.Conv2D(
            filters=4 * filters_in,
            kernel_size=1,
            padding='same',
            activation=None,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=tf.keras.regularizers.l2(weight_decay)
        )
        self.batch_norm_out = tf.keras.layers.BatchNormalization()

    def call(self, inputs, **kwargs):
        features = self.conv_in(inputs)
        features = self.batch_norm_in(features)
        features1 = self.activation_in(features)

        features = self.conv_middle(features1)
        features = self.batch_norm_middle(features)
        features2 = self.activation_middle(features)

        features = self.conv_out(features2)
        output = self.batch_norm_out(features)

        return output
