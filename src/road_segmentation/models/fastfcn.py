import typing

import tensorflow as tf

import road_segmentation as rs

"""
Implementation of the key components from
"FastFCN: Rethinking Dilated Convolution in the Backbone for Semantic Segmentation" (arXiv:1903.11816 [cs.CV])
"""


OUTPUT_STRIDE = 8


class FastFCN(tf.keras.Model):
    """
    Full FastFCN model.

    This model takes as an input a 3 channel image and returns a tuple of tensors.
    The first entry contains the target segmentation,
    the second entry the features for the modified SE-loss.
    """

    def __init__(
            self,
            backbone: tf.keras.Model,
            jpu_features: int,
            head_dropout_rate: float,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            dense_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            se_loss_features: int = 1,
            codewords: int = 32,
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None
    ):
        """
        Create a new FastFCN based on the provided backbone model.

        Args:
            backbone: Backbone to be used. The backbone should return 3 tuple of feature maps at strides (8, 16, 32).
            jpu_features: Number of features to be used in the JPU module.
            se_loss_features: Number of features to be used for the encodings in the context encoding module.
            kernel_initializer: Initializer for convolution kernels.
            dense_initializer: Initializer for dense layers (only in the Encoder head).
            head_dropout_rate: Dropout rate for the head.
            kernel_initializer: Initializer for convolution kernels.
            dense_initializer: Initializer for dense layers (only in the Encoder head).
            kernel_regularizer: Regularizer for convolution weights.
        """
        super(FastFCN, self).__init__()

        self.backbone = backbone
        self.upsampling = rs.models.fastfcn.JPUModule(
            features=jpu_features,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )

        self.head = EncoderHead(
            intermediate_features=512,
            se_loss_features=se_loss_features,
            codewords=codewords,
            kernel_initializer=kernel_initializer,
            dense_initializer=dense_initializer,
            dropout_rate=head_dropout_rate,
            kernel_regularizer=kernel_regularizer
        )

    def call(self, inputs, training=None, mask=None):
        """
        Call this segmentation model.

        Args:
            inputs: Batch of 3 channel images.
            training: Additional argument, unused.
            mask: Additional argument, unused.

        Returns:
            Tuple of tensors where the first entry is the (logit) segmentation mask and
                the second entry is the feature map to be used in the modified SE-loss.
        """
        _, input_height, input_width, _ = tf.unstack(tf.shape(inputs))
        padded_inputs = rs.util.pad_to_stride(inputs, target_stride=32, mode='REFLECT')

        intermediate_features = self.backbone(padded_inputs)[-3:]

        upsampled_features = self.upsampling(intermediate_features)

        small_outputs, loss_features = self.head(upsampled_features)

        cropped_outputs = tf.image.resize_with_crop_or_pad(small_outputs, input_height//8, input_width//8)
        return cropped_outputs, loss_features


class FastFCNNoContext(tf.keras.Model):
    """
    FastFCN model without context encoding module.

    This model takes as an input a 3 channel image and returns segmentation predictions.
    """

    def __init__(
            self,
            backbone: tf.keras.Model,
            jpu_features: int,
            head_dropout_rate: float,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None
    ):
        """
        Create a new FastFCN based on the provided backbone model.

        Args:
            backbone: Backbone to be used. The backbone should return 3 tuple of feature maps at strides (8, 16, 32).
            jpu_features: Number of features to be used in the JPU module.
            head_dropout_rate: Dropout rate for the head.
            kernel_initializer: Initializer for convolution kernels.
            kernel_regularizer: Regularizer for convolution weights.
        """
        super(FastFCNNoContext, self).__init__()

        self.backbone = backbone
        self.upsampling = rs.models.fastfcn.JPUModule(
            features=jpu_features,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )

        self.head = FCNHeadNoContext(
            intermediate_features=512,
            kernel_initializer=kernel_initializer,
            dropout_rate=head_dropout_rate,
            kernel_regularizer=kernel_regularizer
        )

    def call(self, inputs, training=None, mask=None):
        """
        Call this segmentation model.

        Args:
            inputs: Batch of 3 channel images.
            training: Additional argument, unused.
            mask: Additional argument, unused.

        Returns:
            Tuple of tensors where the first entry is the (logit) segmentation mask and
                the second entry is the feature map to be used in the modified SE-loss.
        """
        _, input_height, input_width, _ = tf.unstack(tf.shape(inputs))
        padded_inputs = rs.util.pad_to_stride(inputs, target_stride=32, mode='REFLECT')

        intermediate_features = self.backbone(padded_inputs)[-3:]

        upsampled_features = self.upsampling(intermediate_features)

        small_outputs = self.head(upsampled_features)

        outputs = tf.image.resize_with_crop_or_pad(small_outputs, input_height//8, input_width//8)

        return outputs


class JPUModule(tf.keras.layers.Layer):
    """
    Joint Pyramid Upsampling module for upsampling segmentation features.

    This takes as an input a tuple of features from a segmentation backbone at strides (8, 16, 32)
    and outputs a single feature map containing the results from approximate joint upsampling at output stride 8.
    """

    _INTERPOLATION = 'bilinear'
    _DILATION_RATES = (1, 2, 4, 8)

    def __init__(
            self,
            features: int = 512,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer] = 'he_normal',
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None,
            **kwargs
    ):
        """
        Create a JPU module.

        Args:
            features: Number of output features.
            kernel_initializer: Initializer for convolution kernels.
            kernel_regularizer: Regularizer for convolution weights.
            **kwargs: Additional arguments passed to `tf.keras.layers.Layer`.
        """
        super(JPUModule, self).__init__(**kwargs)

        # Per-resolution convolution blocks
        self.initial_s32 = JPUInputBlock(
            features,
            kernel_initializer,
            kernel_regularizer
        )
        self.initial_s16 = JPUInputBlock(
            features,
            kernel_initializer,
            kernel_regularizer
        )
        self.initial_s8 = JPUInputBlock(
            features,
            kernel_initializer,
            kernel_regularizer
        )

        # Upsampling from stride 32 to 16 and 16 to 8
        self.upsampling_s32 = tf.keras.layers.UpSampling2D(
            size=(4, 4),
            interpolation=self._INTERPOLATION
        )
        self.upsampling_s16 = tf.keras.layers.UpSampling2D(
            size=(2, 2),
            interpolation=self._INTERPOLATION
        )

        # Parallel dilated convolutions
        self.separable_blocks = [
            JPUSeparableBlock(features, dilation_rate, kernel_initializer, kernel_regularizer)
            for dilation_rate in self._DILATION_RATES
        ]

    def call(self, inputs, **kwargs):
        """
        Call this layer.

        Args:
            inputs: 3 tuple of input features with strides (8, 16, 32)
            **kwargs: Additional arguments, unused.

        Returns:
            Single upsampled feature map at stride 8.

        """
        inputs_s8, inputs_s16, inputs_s32 = inputs

        # Per-resolution convolutions
        features_s8 = self.initial_s8(inputs_s8)
        features_s16 = self.initial_s16(inputs_s16)
        features_s32 = self.initial_s32(inputs_s32)

        # Upsample and concatenate
        upsampled_s16 = self.upsampling_s16(features_s16)
        upsampled_s32 = self.upsampling_s32(features_s32)
        dilation_inputs = tf.concat([features_s8, upsampled_s16, upsampled_s32], axis=-1)

        # Parallel dilated convolutions
        dilation_outputs = [block(dilation_inputs) for block in self.separable_blocks]

        # The paper proposes to perform a 1x1 convolution here.
        # The reference implementation does that directly in the heads.
        output = tf.concat(dilation_outputs, axis=-1)
        return output


class JPUInputBlock(tf.keras.layers.Layer):
    """
    Single JPU input convolution block.

    This essentially performs a convolution followed by batch normalization and ReLU.
    It is only intended to be used as part of a JPU module.
    """

    def __init__(
            self,
            features: int,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None,
            **kwargs
    ):
        """
        Create a new input convolution block.

        Args:
            features: Number of output features.
            kernel_initializer: Initializer for the convolution filters.
            kernel_regularizer: Regularizer for the convolution weights.
            **kwargs: Additional arguments passed to `tf.keras.layers.Layer`.
        """

        super(JPUInputBlock, self).__init__(**kwargs)

        # Bias in the convolution layer is omitted since the batch normalization adds a bias term itself
        self.conv = tf.keras.layers.Conv2D(
            filters=features,
            kernel_size=3,
            padding='same',
            activation=None,
            use_bias=False,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )
        self.batch_norm = tf.keras.layers.BatchNormalization()
        self.activation = tf.keras.layers.ReLU()

    def call(self, inputs, **kwargs):
        features = self.conv(inputs)
        features = self.batch_norm(features)
        output = self.activation(features)
        return output


class JPUSeparableBlock(tf.keras.layers.Layer):
    """
    Single separable convolution block to be applied in the JPU in parallel.

    This essentially performs a separable dilated convolution followed by batch normalization and ReLU.
    It is only intended to be used as part of a JPU module.
    """

    def __init__(
            self,
            features: int,
            dilation_rate: int,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None,
            **kwargs
    ):
        """
        Create a new separable convolution block.

        Args:
            features: Number of output features.
            dilation_rate: Dilation rate for the separable convolution.
            kernel_initializer: Initializer for the separable convolution filters.
            kernel_regularizer: Regularizer for the separable convolution filters.
            **kwargs: Additional arguments passed to `tf.keras.layers.Layer`.
        """

        super(JPUSeparableBlock, self).__init__(**kwargs)

        # Compared to the original implementation, this only performs batch norm once at the end

        # Bias is omitted since the batch normalization adds a bias term itself
        self.conv = tf.keras.layers.SeparableConv2D(
            features,
            kernel_size=3,
            padding='same',
            dilation_rate=dilation_rate,
            depth_multiplier=1,
            activation=None,
            use_bias=False,
            depthwise_initializer=kernel_initializer,
            pointwise_initializer=kernel_initializer,
            depthwise_regularizer=kernel_regularizer,
            pointwise_regularizer=kernel_regularizer
        )
        self.batch_norm = tf.keras.layers.BatchNormalization()
        self.activation = tf.keras.layers.ReLU()

    def call(self, inputs, **kwargs):
        features = self.conv(inputs)
        features = self.batch_norm(features)
        output = self.activation(features)
        return output


class EncoderHead(tf.keras.layers.Layer):
    """
    Segmentation head which produces segmentations using an Encoder block.

    Calling this layer yields a tuple of tensors.
    The first tuple entry contains the actual (logit) segmentations.
    The second tuple entry contains the features for the modified SE-loss.

    This head performs a 1x1 convolution to compress the input features,
    then applies an Encoder block and finally
    performs a 1x1 convolution generating the actual segmentation.
    """

    def __init__(
            self,
            intermediate_features: int,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            dense_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            se_loss_features: int = 1,
            codewords: int = 32,
            dropout_rate: float = 0.1,
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None,
            **kwargs
    ):
        """
        Create a new Encoder head.

        Args:
            intermediate_features: Number of intermediate feature to compress the input to.
            kernel_initializer: Convolution kernel initializer.
            dense_initializer: Dense weight initializer.
            se_loss_features: Number of features to be used for the encodings in the context encoding module.
            codewords: The number of codewords used in the context encoding module.
            dropout_rate: Rate for pre-output dropout.
            kernel_regularizer: Regularizer for convolution weights.
            **kwargs: Additional arguments passed to `tf.keras.layers.Layer`.
        """

        super(EncoderHead, self).__init__(**kwargs)

        # Input 1x1 convolution
        # The original paper proposes this as part of the JPU but the reference implementation
        # does it as part of the heads. The latter option is chosen here for flexibility reasons.
        # Bias term is omitted since it is applied by the batch normalization directly afterwards.
        self.conv_in = tf.keras.layers.Conv2D(
            filters=intermediate_features,
            kernel_size=1,
            padding='valid',
            activation=None,
            use_bias=False,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )
        self.batch_norm_in = tf.keras.layers.BatchNormalization()
        self.activation_in = tf.keras.layers.ReLU()

        # Actual encoder module
        self.encoder = rs.models.encnet.ContextEncodingModule(
            codewords=codewords,
            se_loss_features=se_loss_features,
            dense_initializer=dense_initializer
        )

        # Output (logits)
        self.dropout = tf.keras.layers.SpatialDropout2D(dropout_rate)
        self.conv_out = tf.keras.layers.Conv2D(
            filters=1,
            kernel_size=1,
            padding='valid',
            activation=None,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )

    def call(self, inputs, **kwargs):
        """
        Call this layer.

        Args:
            inputs: Upsampled features to be used for estimating the segmentation mask.
            **kwargs: Additional arguments, unused.

        Returns:
            Tuple of tensors where the first entry is the (logit) segmentation mask and
                the second entry is the feature map to be used in the modified SE-loss.
        """
        compressed_features = self.conv_in(inputs)
        compressed_features = self.batch_norm_in(compressed_features)
        compressed_features = self.activation_in(compressed_features)

        weighted_features, se_loss_features = self.encoder(compressed_features)

        pre_output_features = self.dropout(weighted_features)
        output_features = self.conv_out(pre_output_features)
        return output_features, se_loss_features


class FCNHeadNoContext(tf.keras.layers.Layer):
    """
    Segmentation head which produces segmentations.

    This head performs a 1x1 convolution to compress the input features,
    then performs a 1x1 convolution generating the actual segmentation.
    """

    def __init__(
            self,
            intermediate_features: int,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            dropout_rate: float = 0.1,
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None,
            **kwargs
    ):
        """
        Create a new Encoder head.

        Args:
            intermediate_features: Number of intermediate feature to compress the input to.
            kernel_initializer: Convolution kernel initializer.
            dropout_rate: Rate for pre-output dropout.
            kernel_regularizer: Regularizer for convolution weights.
            **kwargs: Additional arguments passed to `tf.keras.layers.Layer`.
        """

        super(FCNHeadNoContext, self).__init__(**kwargs)

        # Input 1x1 convolution
        # The original paper proposes this as part of the JPU but the reference implementation
        # does it as part of the heads. The latter option is chosen here for flexibility reasons.
        # Bias term is omitted since it is applied by the batch normalization directly afterwards.
        self.conv_in = tf.keras.layers.Conv2D(
            filters=intermediate_features,
            kernel_size=1,
            padding='valid',
            activation=None,
            use_bias=False,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )
        self.batch_norm_in = tf.keras.layers.BatchNormalization()
        self.activation_in = tf.keras.layers.ReLU()

        # Output (logits)
        self.dropout = tf.keras.layers.SpatialDropout2D(dropout_rate)
        self.conv_out = tf.keras.layers.Conv2D(
            filters=1,
            kernel_size=1,
            padding='valid',
            activation=None,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )

    def call(self, inputs, **kwargs):
        """
        Call this layer.

        Args:
            inputs: Upsampled features to be used for estimating the segmentation mask.
            **kwargs: Additional arguments, unused.

        Returns:
            Tuple of tensors where the first entry is the (logit) segmentation mask and
                the second entry is the feature map to be used in the modified SE-loss.
        """
        compressed_features = self.conv_in(inputs)
        compressed_features = self.batch_norm_in(compressed_features)
        compressed_features = self.activation_in(compressed_features)

        pre_output_features = self.dropout(compressed_features)
        output_features = self.conv_out(pre_output_features)
        return output_features


class FCNHead(tf.keras.layers.Layer):
    """
    Basic head which produces segmentations solely based on features from the smallest stride,
    mostly for testing purposes.

    This head performs three convolutions:
    first a 1x1 convolution to compress the input features,
    a 3x3 convolution to combine features and finally
    a 1x1 convolution generating the actual segmentation.
    """

    def __init__(
            self,
            intermediate_features: int,
            kernel_initializer: typing.Union[str, tf.keras.initializers.Initializer],
            dropout_rate: float = 0.1,
            kernel_regularizer: typing.Optional[tf.keras.regularizers.Regularizer] = None,
            **kwargs
    ):
        """
        Create a new FCN head.

        Args:
            intermediate_features: Number of intermediate feature to compress the input to.
            kernel_initializer: Convolution kernel initializer.
            dropout_rate: Rate for pre-output dropout.
            kernel_regularizer: Regularizer for convolution weights.
            **kwargs: Additional arguments passed to `tf.keras.layers.Layer`.
        """

        super(FCNHead, self).__init__(**kwargs)

        # Input 1x1 convolution
        # The original paper proposes this as part of the JPU but the reference implementation does not.
        # It is performed here primarily for performance and memory reasons.
        # Bias in the convolution layer is omitted since the batch normalization adds a bias term itself
        self.conv_in = tf.keras.layers.Conv2D(
            filters=intermediate_features,
            kernel_size=1,
            padding='valid',
            activation=None,
            use_bias=False,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )
        self.batch_norm_in = tf.keras.layers.BatchNormalization()
        self.activation_in = tf.keras.layers.ReLU()

        # Normal conv -> batch norm -> relu
        # Bias in the convolution layer is omitted since the batch normalization adds a bias term itself
        self.conv_middle = tf.keras.layers.Conv2D(
            filters=intermediate_features,
            kernel_size=3,
            padding='same',
            activation=None,
            use_bias=False,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )
        self.batch_norm_middle = tf.keras.layers.BatchNormalization()
        self.activation_middle = tf.keras.layers.ReLU()

        # Dropout before output
        self.dropout = tf.keras.layers.SpatialDropout2D(dropout_rate)

        # Output (without activation or anything)
        self.conv_out = tf.keras.layers.Conv2D(
            filters=1,
            kernel_size=1,
            padding='valid',
            activation=None,
            kernel_initializer=kernel_initializer,
            kernel_regularizer=kernel_regularizer
        )

    def call(self, inputs, **kwargs):
        compressed_features = self.conv_in(inputs)
        compressed_features = self.batch_norm_in(compressed_features)
        compressed_features = self.activation_in(compressed_features)

        intermediate_features = self.conv_middle(compressed_features)
        intermediate_features = self.batch_norm_middle(intermediate_features)
        intermediate_features = self.activation_middle(intermediate_features)
        intermediate_features = self.dropout(intermediate_features)

        output_features = self.conv_out(intermediate_features)
        return output_features
