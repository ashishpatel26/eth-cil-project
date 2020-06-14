import argparse
import typing

import numpy as np
import skimage.color
import tensorflow as tf

import road_segmentation as rs

EXPERIMENT_DESCRIPTION = 'FCN Baseline'
EXPERIMENT_TAG = 'baseline_fcn'


def main():
    BaselineFCNExperiment().run()


class BaselineFCNExperiment(rs.framework.Experiment):

    @property
    def tag(self) -> str:
        return EXPERIMENT_TAG

    @property
    def description(self) -> str:
        return EXPERIMENT_DESCRIPTION

    def create_argument_parser(self, parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        # Defaults are roughly based on ADE20k experiments of the original paper
        parser.add_argument('--batch-size', type=int, default=4, help='Training batch size')  # TODO: Should be 16
        parser.add_argument('--learning-rate', type=float, default=1e-2, help='Initial learning rate')
        parser.add_argument('--momentum', type=float, default=0.9, help='SGD momentum')
        parser.add_argument('--weight-decay', type=float, default=1e-4, help='Weight decay for convolution weights')
        parser.add_argument('--epochs', type=int, default=240, help='Number of training epochs')

        return parser

    def build_parameter_dict(self, args: argparse.Namespace) -> typing.Dict[str, typing.Any]:
        return {
            'jpu_features': 512,
            'weight_decay': args.weight_decay,
            'output_upsampling': 'nearest',
            'batch_size': args.batch_size,
            'initial_learning_rate': args.learning_rate,
            'end_learning_rate': 1e-8,  # FIXME: The original authors decay to zero but small non-zero might be better
            'learning_rate_decay': 0.9,
            'momentum': args.momentum,
            'epochs': args.epochs,
            'augmentation_max_relative_scaling': 0.04,  # Scaling +- one output feature, result in [384, 416]
            'augmentation_interpolation': 'bilinear',
            'augmentation_blur_probability': 0.5,
            'augmentation_blur_size': 5,  # 5x5 Gaussian filter for blurring
            'training_image_size': (384, 384)
        }

    def fit(self) -> typing.Any:
        self.log.info('Loading training and validation data')
        try:
            trainig_paths, validation_paths = rs.data.cil.train_validation_sample_paths(self.data_directory)
            training_images, training_masks = rs.data.cil.load_images(trainig_paths)
            validation_images, validation_masks = rs.data.cil.load_images(validation_paths)
            self.log.debug(
                'Loaded %d training and %d validation samples',
                training_images.shape[0],
                validation_images.shape[0]
            )
        except (OSError, ValueError):
            self.log.exception('Unable to load data')
            return

        training_dataset = tf.data.Dataset.from_tensor_slices((training_images, training_masks))
        training_dataset = training_dataset.shuffle(buffer_size=training_images.shape[0])
        training_dataset = training_dataset.map(lambda image, mask: self._augment_sample(image, mask))
        # TODO: Think about prefetching data here if GPU is not fully utilized
        training_dataset = training_dataset.batch(self.parameters['batch_size'])
        self.log.debug('Training data specification: %s', training_dataset.element_spec)

        # Validation images can be directly converted to the model colour space
        validation_dataset = tf.data.Dataset.from_tensor_slices(
            (convert_colorspace(validation_images), validation_masks)
        )
        validation_dataset = validation_dataset.batch(1)

        # Build model
        self.log.info('Building model')
        model = TestFastFCN(
            self.parameters['jpu_features'],
            self.parameters['weight_decay'],
            self.parameters['output_upsampling']
        )

        # TODO: This is for testing only
        model.build(training_dataset.element_spec[0].shape)
        model.summary(line_length=120)

        metrics = self.keras.default_metrics(threshold=0.0)

        steps_per_epoch = np.ceil(training_images.shape[0] / self.parameters['batch_size'])
        self.log.debug('Calculated steps per epoch: %d', steps_per_epoch)
        # TODO: The paper authors do weight decay on an optimizer level, not on a case-by-case basis.
        #  There's a difference! tfa has an optimizer-level SGD with weight decay.
        model.compile(
            optimizer=tf.keras.optimizers.SGD(
                learning_rate=tf.keras.optimizers.schedules.PolynomialDecay(
                    initial_learning_rate=self.parameters['initial_learning_rate'],
                    decay_steps=self.parameters['epochs'] * steps_per_epoch,
                    end_learning_rate=self.parameters['end_learning_rate'],
                    power=self.parameters['learning_rate_decay']
                ),
                momentum=self.parameters['momentum']
            ),
            loss=tf.keras.losses.BinaryCrossentropy(from_logits=True),
            metrics=metrics
        )

        callbacks = [
            self.keras.tensorboard_callback(),
            self.keras.periodic_checkpoint_callback(),
            self.keras.best_checkpoint_callback(),
            self.keras.log_predictions(validation_images)
        ]

        # Fit model
        model.fit(
            training_dataset,
            epochs=self.parameters['epochs'],
            validation_data=validation_dataset,
            callbacks=callbacks
        )

        return model

    def predict(self, classifier: typing.Any, images: typing.Dict[int, np.ndarray]) -> typing.Dict[int, np.ndarray]:
        result = dict()

        for sample_id, image in images.items():
            self.log.debug('Predicting sample %d', sample_id)
            image = np.expand_dims(image, axis=0)

            # Convert to model colour space
            image = convert_colorspace(image)

            raw_prediction, = classifier.predict(image)
            prediction = np.where(raw_prediction >= 0, 1, 0)

            result[sample_id] = prediction

        return result

    def _augment_sample(self, image: tf.Tensor, mask: tf.Tensor) -> typing.Tuple[tf.Tensor, tf.Tensor]:
        # Random Gaussian blurring
        do_blur = tf.random.uniform(shape=[], dtype=tf.float32) < self.parameters['augmentation_blur_probability']
        blurred_image = tf.cond(do_blur, lambda: self._augment_blur(image), lambda: image)
        blurred_image.set_shape(image.shape)  # Must set shape manually since it cannot be inferred from tf.cond

        # Random scaling
        scaling_factor = tf.random.uniform(
            shape=[],
            minval=1.0 - self.parameters['augmentation_max_relative_scaling'],
            maxval=1.0 + self.parameters['augmentation_max_relative_scaling']
        )
        input_height, input_width, _ = tf.unstack(tf.cast(tf.shape(blurred_image), tf.float32))
        scaled_size = tf.cast(
            tf.round((input_height * scaling_factor, input_width * scaling_factor)),
            tf.int32
        )
        scaled_image = tf.image.resize(blurred_image, scaled_size, method=self.parameters['augmentation_interpolation'])
        scaled_mask = tf.image.resize(mask, scaled_size, method='nearest')

        # Combine image and mask to ensure same transformations are applied
        concatenated_sample = tf.concat((scaled_image, scaled_mask), axis=-1)

        # Random flip and rotation, this covers all possible permutations which do not require interpolation
        flipped_sample = tf.image.random_flip_left_right(concatenated_sample)
        num_rotations = tf.random.uniform(shape=[], minval=0, maxval=4, dtype=tf.int32)
        rotated_sample = tf.image.rot90(flipped_sample, num_rotations)

        # Random crop
        crop_size = self.parameters['training_image_size'] + (4,)  # 3 colour channels + 1 mask channel
        cropped_sample = tf.image.random_crop(rotated_sample, crop_size)

        output_image = cropped_sample[:, :, :3]
        output_mask = cropped_sample[:, :, 3:]

        # Convert mask to labels in {0, 1}
        output_mask = tf.round(output_mask)

        # Convert image to CIE Lab
        # This has to be done after the other transformations since some assume RGB inputs
        [output_image_lab, ] = tf.py_function(convert_colorspace, [output_image], [tf.float32])
        output_image_lab.set_shape(output_image.shape)  # Propagate shape

        # FIXME: It would make sense to apply colour shifts but the original paper does not
        return output_image_lab, output_mask

    def _augment_blur(self, image: tf.Tensor) -> tf.Tensor:
        # Pick standard deviation randomly in [0.5, 1)
        sigma = tf.random.uniform(shape=[], minval=0.5, maxval=1.0, dtype=tf.float32)
        sigma_squared = tf.square(sigma)

        # FIXME: This would be quite faster if applied as two 1D convolutions instead of a 2D one

        # Calculate Gaussian filter kernel
        kernel_size = self.parameters['augmentation_blur_size']
        half_kernel_size = kernel_size // 2
        grid_y_squared, grid_x_squared = np.square(
            np.mgrid[-half_kernel_size:half_kernel_size + 1, -half_kernel_size:half_kernel_size + 1]
        )
        coordinates = grid_y_squared + grid_x_squared
        kernel = 1.0 / (2.0 * np.pi * sigma_squared) * tf.exp(
            - coordinates / (2.0 * sigma_squared)
        )
        kernel = tf.reshape(kernel, (kernel_size, kernel_size, 1, 1))
        kernel = tf.repeat(kernel, 3, axis=2)

        # Pad image using reflection padding (not available in depthwise_conv2d)
        padded_image = tf.pad(
            image,
            paddings=((half_kernel_size, half_kernel_size), (half_kernel_size, half_kernel_size), (0, 0)),
            mode='REFLECT'
        )
        padded_image = tf.expand_dims(padded_image, axis=0)

        blurred_image = tf.nn.depthwise_conv2d(padded_image, kernel, strides=(1, 1, 1, 1), padding='VALID')
        output = tf.clip_by_value(blurred_image[0], 0.0, 1.0)
        return output


def convert_colorspace(images: np.ndarray) -> np.ndarray:
    images_lab = skimage.color.rgb2lab(images)

    # Rescale intensity to [0, 1] and a,b to [-1, 1)
    return images_lab / (100.0, 128.0, 128.0)


class TestFastFCN(tf.keras.models.Model):
    """
    FIXME: This is just a test class
    """

    KERNEL_INITIALIZER = 'he_normal'  # FIXME: This is somewhat arbitrarily chosen

    def __init__(
            self,
            jpu_features: int,
            weight_decay: float,
            output_upsampling: str
    ):
        super(TestFastFCN, self).__init__()

        self.backbone = rs.models.resnet.ResNet50Backbone(weight_decay=weight_decay)
        self.upsampling = rs.models.jpu.JPUModule(
            features=jpu_features,
            weight_decay=weight_decay
        )

        # FIXME: Head is only for testing, replace this with EncNet head
        self.head = rs.models.jpu.FCNHead(
            intermediate_features=256,
            kernel_initializer=self.KERNEL_INITIALIZER,
            weight_decay=weight_decay
        )

        # FIXME: Upsampling of the 8x8 output is slightly unnecessary and should be done more in line with the s16 target
        self.output_upsampling = tf.keras.layers.UpSampling2D(size=(8, 8), interpolation=output_upsampling)

        # FIXME: The paper uses an auxiliary FCNHead at the end to calculate the loss, but never for the output...
        #  Does not really make sense and is also not mentioned in the paper I think

    def call(self, inputs, training=None, mask=None):
        _, input_height, input_width, _ = tf.unstack(tf.shape(inputs))
        padded_inputs = pad_to_stride(inputs, target_stride=32, mode='REFLECT')

        intermediate_features = self.backbone(padded_inputs)[-3:]
        upsampled_features = self.upsampling(intermediate_features)
        small_outputs = self.head(upsampled_features)
        padded_outputs = self.output_upsampling(small_outputs)
        outputs = tf.image.resize_with_crop_or_pad(padded_outputs, input_height, input_width)
        return outputs


@tf.function
def pad_to_stride(inputs: tf.Tensor, target_stride: int, mode: str = 'REFLECT') -> tf.Tensor:
    """
    TODO: Documentation

    TODO: This should be moved to util or something like that
    """

    # Calculate total amount to be padded
    missing_y = target_stride - (inputs.shape[1] % target_stride)
    missing_x = target_stride - (inputs.shape[2] % target_stride)

    # Calculate paddings
    # In asymmetric cases the larger padding happens after the features
    paddings = (
        (0, 0),  # Batch
        (missing_y // 2, tf.math.ceil(missing_y / 2)),  # Height
        (missing_x // 2, tf.math.ceil(missing_x / 2)),  # Width
        (0, 0)  # Channels
    )

    return tf.pad(inputs, paddings, mode=mode)


if __name__ == '__main__':
    main()
