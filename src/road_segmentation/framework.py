import abc
import argparse
import csv
import datetime
import json
import logging
import os
import typing

import ax
import ax.modelbridge.generation_strategy
import numpy as np
import sklearn
import tensorflow as tf
import tensorflow_addons as tfa

import road_segmentation as rs

_LOG_FORMAT = '%(asctime)s  %(levelname)s [%(name)s]: %(message)s'
_LOG_FILE_NAME = 'log.txt'
_PARAMETER_FILE_NAME = 'parameters.json'


class BaseExperiment(metaclass=abc.ABCMeta):
    """
    Abstract base class to be extended by concrete experiments.
    """

    SEED = 42
    """
    Random seed to be used for consistent RNG initialisation.
    """

    @property
    @abc.abstractmethod
    def tag(self) -> str:
        """
        Returns:
            Experiment tag
        """
        pass

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """
        Returns:
            Human-readable experiment description
        """
        pass

    @abc.abstractmethod
    def create_argument_parser(self, parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """
        Create an argument parser which handles
        arguments specific to this experiment.
        A template parser to be used is given as a parameter.
        General arguments (e.g. debug flag) are added automatically.

        Args:
            parser: Template parser to be extended and returned.

        Returns:
            Argument parser for parsing experiment-specific arguments.

        """
        pass

    @abc.abstractmethod
    def build_parameter_dict(self, args: argparse.Namespace) -> typing.Dict[str, typing.Any]:
        """
        Collect all experimental parameters into a dictionary.
        The result must be sufficient to exactly and completely
        reproduce the experiment.
        Parameters independent of the experiment are added automatically.

        Keys must not start with `base_`.

        Args:
            args: Parsed command line arguments.

        Returns:
            Dictionary where keys are strings, indicating experimental parameters.

        """
        pass

    @abc.abstractmethod
    def _run_actual_experiment(self):
        """
        Runs the actual experiment given that the general setup has been performed.
        This method is to be overridden by child classes to contain their actual experimental logic.
        """
        pass

    def __init__(self):
        # Define attributes for correct linting
        self._parameters = None
        self._log = None  # Internal use
        self._experiment_logger = None  # Exposed to child class
        self._experiment_directory = None
        self._keras_helper = None

    def run(self):
        """
        Runs this experiment.
        """

        # Fix seeds as a failsafe (as early as possible)
        rs.util.fix_seeds(self.SEED)

        # Parse CLI args
        parser = self._create_full_argument_parser()
        args = parser.parse_args()

        # Collect all experimental parameters in a dictionary for reproducibility
        self._parameters = self._build_parameter_dict(args)

        # Initialise experiment directory
        directory_name = f'{self.tag}_{datetime.datetime.now():%y%m%d-%H%M%S}'
        self._experiment_directory = os.path.join(self.parameters['base_log_directory'], directory_name)
        try:
            os.makedirs(self._experiment_directory, exist_ok=False)
        except OSError:
            # Print since logging is not ready yet
            print('Unable to setup output directory')
            return

        # Initialise logging
        self._setup_logging(debug=self.parameters['base_is_debug'])
        self._log = logging.getLogger(__name__)
        self._experiment_logger = logging.getLogger(self.tag)

        self._log.info('Using experiment directory %s', self._experiment_directory)
        self._log.debug('Experiment parameters: %s', self.parameters)

        # Initialise helpers
        self._keras_helper = KerasHelper(self.experiment_directory)

        # Store experiment parameters
        # noinspection PyBroadException
        try:
            with open(os.path.join(self._experiment_directory, _PARAMETER_FILE_NAME), 'w') as f:
                json.dump(self.parameters, f)
        except Exception:
            self._log.exception('Unable to save parameters')
            return

        # Setup is done, run actual experimental logic
        self._run_actual_experiment()
        self._log.info('Finished experiment')

    @property
    def log(self) -> logging.Logger:
        """
        Returns:
            Logger for the current experiment
        """
        return self._experiment_logger

    @property
    def parameters(self) -> typing.Dict[str, typing.Any]:
        """
        Returns:
            Experiment parameters.
        """
        return self._parameters

    @property
    def data_directory(self):
        """
        Returns:
            Root data directory.
        """
        return self.parameters['base_data_directory']

    @property
    def experiment_directory(self):
        """
        Returns:
            Current experiment output directory.
        """
        return self._experiment_directory

    @property
    def keras(self) -> 'KerasHelper':
        """
        Returns:
            Keras helper
        """
        return self._keras_helper

    def _setup_logging(self, debug: bool):
        level = logging.DEBUG if debug else logging.INFO

        # Configure (and install if necessary) root logger
        logging.basicConfig(level=level, format=_LOG_FORMAT)

        # Add file handler to root
        log_file_name = os.path.join(self.experiment_directory, _LOG_FILE_NAME)
        file_handler = logging.FileHandler(log_file_name)
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logging.getLogger().addHandler(file_handler)

    def _create_full_argument_parser(self) -> argparse.ArgumentParser:
        # Create template parser
        parser = argparse.ArgumentParser(self.description)

        # Add general arguments
        parser.add_argument('--datadir', type=str, default=rs.util.DEFAULT_DATA_DIR, help='Root data directory')
        parser.add_argument('--logdir', type=str, default=rs.util.DEFAULT_LOG_DIR, help='Root output directory')
        parser.add_argument('--debug', action='store_true', help='Enable debug logging')

        # Add experiment-specific arguments
        parser = self.create_argument_parser(parser)
        return parser

    def _build_parameter_dict(self, args):
        # Validate parameters from child class
        parameters = self.build_parameter_dict(args)
        if any(filter(lambda key: key is not None and str(key).startswith('base_'), parameters.keys())):
            raise ValueError('Parameter keys must not start with `base_`')

        # Add general parameters
        parameters['base_data_directory'] = args.datadir
        parameters['base_log_directory'] = args.logdir
        parameters['base_is_debug'] = args.debug

        return parameters


class FitExperiment(BaseExperiment, metaclass=abc.ABCMeta):
    """
    Base class for experiments which fit a classifier to data using a single set of parameters.
    """

    @abc.abstractmethod
    def fit(self) -> typing.Any:
        """
        Fit a classifier for the current experiment.

        Returns:
            Fitted classifier and other objects required for prediction and evaluation.
        """
        pass

    @abc.abstractmethod
    def predict(
            self,
            classifier: typing.Any,
            images: typing.Dict[int, np.ndarray]
    ) -> typing.Dict[int, np.ndarray]:
        """
        Run a fitted classifier on a list of images and return their segmentations.
        The resulting segmentations should either have the same resolution
        as the input images or be reduced by the target patch size.

        Args:
            classifier: Fitted classifier and other objects as returned by fit.
            images: Images to run prediction on.
             Keys are ids and values the actual images.
             Each image is of shape H x W x 3, with H and W being multiples of patch size.

        Returns:
            Predicted segmentation masks.
             Keys are ids and values the actual images.
             Each image must be of shape (H / patch size) x (W / patch size).
        """
        pass

    def _run_actual_experiment(self):
        # Fit model
        self._log.info('Fitting model')
        # noinspection PyBroadException
        try:
            classifier = self.fit()
        except Exception:
            self._log.exception('Exception while executing experiment')
            return

        # Predict on test data
        self._log.info('Predicting test data')
        output_file = os.path.join(self.experiment_directory, 'submission.csv')
        try:
            self._log.debug('Reading test inputs')
            test_prediction_input = dict()
            for test_sample_id, test_sample_path in rs.data.cil.test_sample_paths(self.data_directory):
                test_prediction_input[test_sample_id] = rs.data.cil.load_image(test_sample_path)
        except OSError:
            self._log.exception('Unable to read test data')
            return

        self._log.debug('Running classifier on test data')
        test_prediction = self.predict(classifier, test_prediction_input)

        try:
            self._log.debug('Creating submission file')
            with open(output_file, 'w', newline='') as f:
                # Write CSV header
                writer = csv.writer(f, delimiter=',')
                writer.writerow(['Id', 'Prediction'])

                for test_sample_id, predicted_segmentation in test_prediction.items():
                    predicted_segmentation = np.squeeze(predicted_segmentation)
                    if len(predicted_segmentation.shape) != 2:
                        raise ValueError(
                            f'Expected 2D prediction (after squeeze) but got shape {predicted_segmentation.shape}'
                        )

                    input_size = test_prediction_input[test_sample_id].shape[:2]
                    expected_segmentation_size = (
                        input_size[0] // rs.data.cil.PATCH_SIZE,
                        input_size[1] // rs.data.cil.PATCH_SIZE
                    )
                    if predicted_segmentation.shape != expected_segmentation_size:
                        raise ValueError(
                            f'Expected predicted segmentation with shape {expected_segmentation_size} but got {predicted_segmentation.shape}'
                        )

                    # Make sure result is integer values
                    predicted_segmentation = predicted_segmentation.astype(np.int)

                    for patch_y in range(0, predicted_segmentation.shape[0]):
                        for patch_x in range(0, predicted_segmentation.shape[1]):
                            output_id = self._create_output_id(test_sample_id, patch_x, patch_y)
                            writer.writerow([output_id, predicted_segmentation[patch_y, patch_x]])
        except OSError:
            self._log.exception('Unable to write submission data')
            return

        self._log.info('Saved predictions to %s', output_file)

    @classmethod
    def _create_output_id(cls, sample_id: int, patch_x: int, patch_y: int) -> str:
        x = patch_x * rs.data.cil.PATCH_SIZE
        y = patch_y * rs.data.cil.PATCH_SIZE
        return f'{sample_id:03d}_{x}_{y}'


class SearchExperiment(BaseExperiment, metaclass=abc.ABCMeta):
    """
    Base class for experiments which search for a parameter set using Bayesian optimisation.
    """

    _TARGET_METRIC = 'accuracy'

    @abc.abstractmethod
    def build_search_space(self) -> ax.SearchSpace:
        """
        Builds the search space of this parameter search experiment.
        The search space should only consist of fixed parameters or continous range parameters.
        Returns:
            Search space to be used.
        """
        pass

    def _create_full_argument_parser(self) -> argparse.ArgumentParser:
        # Build general parser
        parser = super(SearchExperiment, self)._create_full_argument_parser()

        # Add search-specific arguments
        parser.add_argument('--initial-trials', type=int, default=8, help='Number of initial SOBOL trials')
        parser.add_argument('--optimised-trials', type=int, default=12, help='Number of GP trials')
        parser.add_argument('--folds', type=int, default=5, help='Number of CV folds per trial')

        return parser

    def _build_parameter_dict(self, args):
        # Build general parameter dict
        parameters = super(SearchExperiment, self)._build_parameter_dict(args)

        # Apply fixed arguments of search experiments
        parameters.update({
            'base_search_initial_trials': args.initial_trials,
            'base_search_optimised_trials': args.optimised_trials,
            'base_search_folds': args.folds,
        })

        return parameters

    # noinspection PyBroadException
    def _run_actual_experiment(self):
        self.log.info('Loading data')
        try:
            supervised_images, supervised_masks = rs.data.cil.load_images(
                rs.data.cil.training_sample_paths(self.data_directory)
            )
            self.log.debug('Loaded %d supervised samples', supervised_images.shape[0])

            unsupervised_sample_paths = np.asarray(rs.data.unsupervised.processed_sample_paths(self.data_directory))
            self.log.debug('Loaded %s unsupervised sample paths', len(unsupervised_sample_paths))
        except (OSError, ValueError):
            self.log.exception('Unable to load data')
            return

        self.log.info('Building experiment')
        search_space = self.build_search_space()
        self.log.debug('Built search space %s', search_space)

        objective = ax.Objective(metric=ax.Metric(self._TARGET_METRIC, lower_is_better=False), minimize=False)
        optimization_config = ax.OptimizationConfig(objective, outcome_constraints=None)
        self.log.debug('Built optimization config %s', optimization_config)

        def _evaluation_function_wrapper(
                parameterization: typing.Dict[str, typing.Union[float, str, bool, int]],
                weight: typing.Optional[float] = None
        ) -> typing.Dict[str, typing.Tuple[float, float]]:
            return self._run_trial(parameterization, supervised_images, supervised_masks, unsupervised_sample_paths)

        experiment = ax.SimpleExperiment(
            search_space=search_space,
            name=self.tag,
            evaluation_function=_evaluation_function_wrapper
        )
        experiment.optimization_config = optimization_config
        self.log.debug('Built experiment %s', experiment)

        generation_strategy = self._build_generation_strategy()
        self.log.info('Using generation strategy %s', generation_strategy)

        loop = ax.OptimizationLoop(
            experiment,
            total_trials=self.parameters['base_search_initial_trials'] + self.parameters['base_search_optimised_trials'],
            arms_per_trial=1,
            random_seed=self.SEED,
            wait_time=0,
            run_async=False,
            generation_strategy=generation_strategy
        )
        self.log.info('Running trials')
        loop.full_run()
        self.log.info('Finished all trials')

        best_parameterization, (means, covariances) = loop.get_best_point()
        self.log.info('Best encountered parameters: %s', best_parameterization)
        self.log.info(
            'Best encountered score: mean=%.4f, var=%.4f',
            means[self._TARGET_METRIC],
            covariances[self._TARGET_METRIC][self._TARGET_METRIC]
        )

        experiment_save_path = os.path.join(self.experiment_directory, 'trials.json')
        ax.save(experiment, experiment_save_path)
        self.log.info('Saved experiment to %s', experiment_save_path)

    def _build_generation_strategy(self) -> ax.modelbridge.generation_strategy.GenerationStrategy:
        return ax.modelbridge.generation_strategy.GenerationStrategy([
            ax.modelbridge.generation_strategy.GenerationStep(
                model=ax.Models.SOBOL,
                num_trials=self.parameters['base_search_initial_trials'],
                enforce_num_trials=True,
                model_kwargs={
                    'deduplicate': True,
                    'seed': self.SEED
                }
            ),
            ax.modelbridge.generation_strategy.GenerationStep(
                model=ax.Models.GPEI,
                num_trials=self.parameters['base_search_optimised_trials'],
                enforce_num_trials=True
            )
        ])

    def _run_trial(
            self,
            parameterization: typing.Dict[str, typing.Union[float, str, bool, int]],
            supervised_images: np.ndarray,
            supervised_masks: np.ndarray,
            unsupervised_image_paths: np.ndarray
    ) -> typing.Dict[str, typing.Tuple[float, float]]:
        self.log.debug('Current trial parameters: %s', parameterization)

        supervised_fold_generator = sklearn.model_selection.KFold(
            n_splits=self.parameters['base_search_folds'],
            shuffle=True,
            random_state=self.SEED
        )
        unsupervised_fold_generator = sklearn.model_selection.KFold(
            n_splits=self.parameters['base_search_folds'],
            shuffle=True,
            random_state=self.SEED
        )
        folds = zip(
            supervised_fold_generator.split(supervised_images),
            unsupervised_fold_generator.split(unsupervised_image_paths)
        )
        fold_scores = []
        for fold_idx, (supervised_split, unsupervised_split) in enumerate(folds):
            self.log.info('Running fold %d / %d', fold_idx + 1, self.parameters['base_search_folds'])

            supervised_training_indices, supervised_validation_indices = supervised_split
            unsupervised_training_indices, unsupervised_validation_indices = unsupervised_split

            current_score = self.run_fold(
                parameterization,
                supervised_images[supervised_training_indices],
                supervised_masks[supervised_training_indices],
                supervised_images[supervised_validation_indices],
                supervised_masks[supervised_validation_indices],
                unsupervised_image_paths[unsupervised_training_indices],
                unsupervised_image_paths[unsupervised_validation_indices]
            )
            fold_scores.append(current_score)
            self.log.debug('Fold completed, score: %.4f', current_score)

        score_mean = np.mean(fold_scores)
        score_std = np.std(fold_scores)
        score_sem = score_std / np.sqrt(self.parameters['base_search_folds'])
        self.log.info('Finished trial with score %.4f (std %.4f, sem %.4f)', score_mean, score_std, score_sem)

        return {
            self._TARGET_METRIC: (float(score_mean), float(score_sem))
        }

    @abc.abstractmethod
    def run_fold(
            self,
            parameterization: typing.Dict[str, typing.Union[float, str, bool, int]],
            supervised_training_images: np.ndarray,
            supervised_training_masks: np.ndarray,
            supervised_validation_images: np.ndarray,
            supervised_validation_masks: np.ndarray,
            unsupervised_training_sample_paths: np.ndarray,
            unsupervised_validation_sample_paths: np.ndarray
    ) -> float:
        """
        Runs a single fold with the given data split and parameters.

        Args:
            parameterization: Parameterization to use for the current fit.
            supervised_training_images: Float array containing the images to be used for training.
            supervised_training_masks: Float array containing the masks to be used for training.
            supervised_validation_images: Float array containing the images to be used for validation.
            supervised_validation_masks: Float array containing the masks to be used for validation.
            unsupervised_training_sample_paths:
                String array containing paths to unsupervised images to be used for training.
            unsupervised_validation_sample_paths:
                String array containing paths to unsupervised images to be used for validation (if applicable).

        Returns:
            Score of the current fold, evaluated on the supervised validation data.
        """
        pass


class KerasHelper(object):
    """
    Helper class simplifying Keras usage in the context of the experimental framework.
    """

    def __init__(self, log_dir: str):
        """
        Creates a new keras helper.

        Args:
            log_dir: Root log directory of the current experiment.
        """
        self._log_dir = log_dir
        self._log = logging.getLogger(__name__)

    def tensorboard_callback(self) -> tf.keras.callbacks.Callback:
        """
        Creates a new Keras callback which performs logging consumable through TensorBoard.

        Returns:
            Keras callback enabling logging.
        """

        # Provide cli command via log for convenience
        self._log.info(
            'Setting up tensorboard logging, use with `tensorboard --logdir=%s`', self._log_dir
        )
        return tf.keras.callbacks.TensorBoard(
            self._log_dir,
            histogram_freq=10,
            write_graph=True,
            write_images=False,
            update_freq='epoch',
            profile_batch=0  # Disable profiling to avoid issue: https://github.com/tensorflow/tensorboard/issues/2084
        )

    def periodic_checkpoint_callback(
            self,
            period: int = 20,
            checkpoint_template: str = '{epoch:04d}-{val_loss:.4f}.h5'
    ) -> tf.keras.callbacks.Callback:

        """
        Creates a new Keras callback which periodically saves model checkpoints.

        Args:
            period: Period (in epochs) between subsequent checkpoints.
            checkpoint_template: Checkpoint file name template. This must only use metrics present in the model.

        Returns:
            Keras callback enabling periodic checkpoint saving.
        """
        # Create checkpoint directory
        checkpoint_dir = os.path.join(self._log_dir, 'checkpoints')
        os.makedirs(checkpoint_dir, exist_ok=False)

        # Create path template
        path_template = os.path.join(checkpoint_dir, checkpoint_template)

        return tf.keras.callbacks.ModelCheckpoint(
            path_template,
            save_best_only=False,
            period=period
        )

    def best_checkpoint_callback(
            self,
            metric: str = 'val_binary_mean_accuracy',
            mode: str = 'max',
            path_template: str = None
    ) -> tf.keras.callbacks.Callback:
        """
        Creates a callback which stores a checkpoint of the best model (according to some metric)
        encountered during training.

        The resulting callback will either store a single file (the respective best model)
        or a file each time a new best model is encountered
        (if the path template contains variables such as {epoch:04d}).

        Args:
            metric:
                Metric to be monitored, defaults to the project's target metric.
                When using a model with multiple outputs, the output name is inserted into the metric name by Keras,
                and thus the metric parameter name has to be adjusted accordingly.
            mode:
                Mode (min, max, auto) to be used to compare metrics. See tf.keras.callbacks.ModelCheckpoint for details.
            path_template:
                Optional path template used to determine output files. May contain dynamic template parameters.
                Defaults to default_best_checkpoint_path().
        Returns:
            Callback to be given to Keras during training.
        """

        # Use default path template if none is given
        if path_template is None:
            path_template = self.default_best_checkpoint_path()

        self._log.debug('Best model according to %s will be saved as %s', metric, path_template)

        return tf.keras.callbacks.ModelCheckpoint(
            path_template,
            save_best_only=True,
            monitor=metric,
            mode=mode
        )

    def log_predictions(
            self,
            validation_images: np.ndarray,
            freq: int = 10,
            prediction_idx: typing.Optional[int] = None,
            display_images: np.ndarray = None,
            fixed_model: typing.Optional[tf.keras.Model] = None
    ) -> tf.keras.callbacks.Callback:
        """
        Creates a callback which periodically saves predictions on a set of images of the active model.

        Args:
            validation_images: Images the model is applied to.
            freq: Logging frequency in epochs.
            prediction_idx: If not None then the model is expected to have multiple outputs.
             The output corresponding to this index is used for prediction.
            display_images: Which images to display in the prediction overlay.
             If None then `validation_images` are used. This is useful if the input and output dimensions do not match.
            fixed_model: If provided then this model will be used for prediction.
             Otherwise, the models provided by `set_model` are used.

        Returns:
            Callback to be given to Keras during training.
        """
        return self._LogPredictionsCallback(
            os.path.join(self._log_dir, 'validation_predictions'),
            validation_images,
            freq,
            prediction_idx,
            display_images,
            fixed_model
        )

    def default_best_checkpoint_path(self) -> str:
        """
        Returns:
            Default path (template) used to store the best models via callback.
        """
        return os.path.join(self._log_dir, 'best_model.hdf5')

    @classmethod
    def build_optimizer(
            cls,
            total_steps: int,
            initial_learning_rate: float,
            end_learning_rate: float,
            learning_rate_decay: float,
            momentum: float,
            weight_decay: float
    ) -> tfa.optimizers.SGDW:
        """
        Builds a stochastic gradient descent optimizer with global weight decay.

        Args:
            total_steps: Total number of steps (batches) this optimizer is used for.
            initial_learning_rate: Initial learning rate.
            end_learning_rate: End learning rate after the full number of epochs.
            learning_rate_decay: Power of polynomial learning rate decay.
            momentum: Momentum for the optimizer.
            weight_decay: Weight decay coefficient.

        Returns:
            New stochastic gradient descent optimizer.
        """

        learning_rate_schedule = tf.keras.optimizers.schedules.PolynomialDecay(
            initial_learning_rate=initial_learning_rate,
            decay_steps=total_steps,
            end_learning_rate=end_learning_rate,
            power=learning_rate_decay
        )

        # Determine the weight decay schedule proportional to the learning rate decay schedule
        weight_decay_factor = weight_decay / initial_learning_rate

        # This has to be done that way since weight_decay needs to access the optimizer lazily, hence the lambda
        optimizer = tfa.optimizers.SGDW(
            weight_decay=lambda: weight_decay_factor * learning_rate_schedule(optimizer.iterations),
            learning_rate=learning_rate_schedule,
            momentum=momentum
        )

        return optimizer

    @classmethod
    def default_metrics(cls, threshold: float, model_output_stride: int = 1) -> typing.List[tf.keras.metrics.Metric]:
        """
        Creates a set of default segmentation metrics.

        Args:
            threshold: Threshold to differentiate between foreground and background (e.g. 0.0 for logits).
            model_output_stride: Output stride of the model which is evaluated.

        Returns:
            List of metrics which can be provided to Keras.
        """
        return [
            rs.metrics.BinaryMeanFScore(threshold=threshold, model_output_stride=model_output_stride),
            rs.metrics.BinaryMeanAccuracyScore(threshold=threshold, model_output_stride=model_output_stride),
            rs.metrics.BinaryMeanIoUScore(threshold=threshold, model_output_stride=model_output_stride)
        ]

    def log_learning_rate_callback(self) -> tf.keras.callbacks.Callback:
        """
        Creates a new callback which logs (for TensorBoard) the learning rate during training.

        Returns:
            Callback to be given to Keras during training.
        """
        return self._LogLearningRateCallback(
            os.path.join(self._log_dir, 'learning_rate'),
            profile_batch=0  # Disable profiling to avoid issue: https://github.com/tensorflow/tensorboard/issues/2084
        )

    def log_temperature_callback(self) -> tf.keras.callbacks.Callback:
        """
        Creates a new callback which logs (for TensorBoard) the temperature during training.

        Returns:
            Callback to be given to Keras during training.
        """

        return self._LogTemperatureCallback(
            os.path.join(self._log_dir, 'temperature'),
            profile_batch=0  # Disable profiling to avoid issue: https://github.com/tensorflow/tensorboard/issues/2084
        )

    def decay_temperature_callback(
            self,
            initial_temperature: float,
            min_temperature: float,
            decay_rate: float
    ) -> tf.keras.callbacks.Callback:
        """
        Creates a new callback which decays a temperature exponentially during training.

        Args:
            initial_temperature: Initial temperature value.
            min_temperature: Minimum value to which the temperature is clamped.
            decay_rate: Exponent for exponential decay.

        Returns:
            Callback to be given to Keras during training.
        """
        return self._TemperatureDecayCallback(
            initial_temperature,
            min_temperature,
            decay_rate
        )

    class _LogPredictionsCallback(tf.keras.callbacks.LambdaCallback):
        def __init__(
                self,
                log_dir: str,
                validation_images: np.ndarray,
                freq: int,
                prediction_idx: typing.Optional[int] = None,
                display_images: np.ndarray = None,
                fixed_model: typing.Optional[tf.keras.Model] = None
        ):
            super().__init__(on_epoch_end=lambda epoch, _: self._log_predictions_callback(epoch))

            if display_images is None:
                # Use the same images for display in TensorBoard as for prediction
                display_images = validation_images
            else:
                if validation_images.shape[0] != display_images.shape[0]:
                    raise ValueError(
                        'Expected the same amount of input images and display images but got {} and {} instead.'.format(
                            validation_images.shape[0],
                            display_images.shape[0]
                        )
                    )

            self._writer = tf.summary.create_file_writer(log_dir)
            self._validation_images = validation_images
            self._display_images = display_images
            self._freq = freq
            self._model: typing.Optional[tf.keras.Model] = None
            self._fixed_model: typing.Optional[tf.keras.Model] = fixed_model
            self._prediction_idx = prediction_idx

        def set_model(self, model: tf.keras.Model):
            self._model = model

        def _log_predictions_callback(
                self,
                epoch: int
        ):
            if epoch % self._freq != 0:
                return

            assert self._model is not None

            # Predict segmentations
            if self._fixed_model is not None:
                segmentations = self._fixed_model.predict(self._validation_images)
            else:
                segmentations = self._model.predict(self._validation_images)

            if self._prediction_idx is not None:
                segmentations = segmentations[self._prediction_idx]

            # Prediction are logits, thus convert into correct range
            segmentations = tf.sigmoid(segmentations)

            # Create overlay images
            if self._display_images.shape[1:3] != segmentations.shape[1:3]:
                # Rescale segmentations if necessary
                scaled_segmentations = tf.image.resize(
                    segmentations,
                    size=self._display_images.shape[1:3],
                    method='nearest'
                )
            else:
                scaled_segmentations = segmentations
            mask_strength = 0.7
            overlay_images = mask_strength * scaled_segmentations * self._display_images + (1.0 - mask_strength) * self._display_images
            with self._writer.as_default():
                tf.summary.image('predictions_overlay', overlay_images, step=epoch, max_outputs=overlay_images.shape[0])
                tf.summary.image('predictions', segmentations, step=epoch, max_outputs=segmentations.shape[0])

    class _LogLearningRateCallback(tf.keras.callbacks.TensorBoard):
        def __init__(self, log_dir: str, **kwargs):
            super().__init__(log_dir, **kwargs)
            self._writer = tf.summary.create_file_writer(log_dir)

        def _collect_learning_rate(self):
            lr_schedule = getattr(self.model.optimizer, 'lr', None)
            if isinstance(lr_schedule, tf.keras.optimizers.schedules.LearningRateSchedule):
                learning_rate = tf.keras.backend.get_value(
                    lr_schedule(self.model.optimizer.iterations)
                )
            else:
                learning_rate = getattr(self.model.optimizer, 'lr', None)
            return learning_rate

        def on_epoch_end(self, epoch, logs=None):
            lr = self._collect_learning_rate()

            with self._writer.as_default():
                tf.summary.scalar('epoch_learning_rate', lr, epoch)

        def on_train_end(self, logs=None):
            self._writer.close()

    class _LogTemperatureCallback(tf.keras.callbacks.TensorBoard):
        def __init__(self, log_dir: str, **kwargs):
            super().__init__(log_dir, **kwargs)
            self._writer = tf.summary.create_file_writer(log_dir)

        def on_epoch_end(self, epoch, logs=None):
            with self._writer.as_default():
                tf.summary.scalar('epoch_temperature', self.model.temperature, epoch)

        def on_train_end(self, logs=None):
            self._writer.close()

    class _TemperatureDecayCallback(tf.keras.callbacks.Callback):
        def __init__(
                self,
                initial_temperature: float,
                min_temperature: float,
                decay_rate: float
        ):
            super().__init__()
            self.initial_temperature = initial_temperature
            self.min_temperature = min_temperature
            self.decay_rate = decay_rate

        def on_epoch_end(self, epoch, logs=None):
            decayed_temperature = self.initial_temperature * self.decay_rate ** (epoch + 1)
            decayed_temperature = max(decayed_temperature, self.min_temperature)
            self.model.temperature.assign(decayed_temperature)
