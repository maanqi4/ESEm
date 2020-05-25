from abc import ABC
import tensorflow as tf
import numpy as np
from GCEm.utils import tf_tqdm


# TODO: I need to define some distance metrics (including uncertainty?) Should these be functions, or objects?
#  Should this be passed to __init__, or calibrate?

# TODO: I'm not yet sure how the MCMC sampling works so this might need adjusting


class Sampler(ABC):
    """
    A class that efficiently samples a Model object for posterior inference
    """

    def __init__(self, model, obs,
                 obs_uncertainty=0., interann_uncertainty=0.,
                 repres_uncertainty=0., struct_uncertainty=0.):
        """
        :param GCEm.model.Model model:
        :param iris.cube.Cube obs: The objective
        :param float obs_uncertainty: Fractional, relative (1 sigma) uncertainty in observations
        :param float repres_uncertainty: Fractional, relative (1 sigma) uncertainty due to the spatial and temporal
         representitiveness of the observations
        :param float interann_uncertainty: Fractional, relative (1 sigma) uncertainty introduced when using a model run
         for a year other than that the observations were measured in.
        :param float struct_uncertainty: Fractional, relative (1 sigma) uncertainty in the model itself.
        """
        self.model = model
        self.obs = obs

        # TODO: Could add an absolute uncertainty term here
        # Get the square of the absolute uncertainty and broadcast it across the batch (since it's the same for each sample)
        observational_var = np.reshape(np.square(obs.data * obs_uncertainty), (1, obs.shape[0]))
        respres_var = np.reshape(np.square(obs.data * repres_uncertainty), (1, obs.shape[0]))
        interann_var = np.reshape(np.square(obs.data * interann_uncertainty), (1, obs.shape[0]))
        struct_var = np.reshape(np.square(obs.data * struct_uncertainty), (1, obs.shape[0]))
        self.total_var = sum([observational_var, respres_var, interann_var, struct_var])

    def sample(self, prior_x, n_samples):
        """
        This is the call that does the actual inference.

        It should call model.sample over the prior, compare with the objective, and then output a posterior
        distribution

        :param objective: This is an Iris cube of observations
        :param prior: Ideally this would either be a numpy array or a tf.probability.distribution, could default to
        uniforms
        :return:
        """
        pass


class ABCSampler(Sampler):

    def sample(self, prior_x=None, n_samples=1, tolerance=0., threshold=3.):
        """
        This is the call that does the actual inference.

        It should call model.sample over the prior, compare with the objective, and then output a posterior
        distribution

        :param tensorflow_probability.distribution prior_x: The distribution to sample parameters from.
         By default it will uniformly sample the unit N-D hypercube
        :param int n_samples: The number of samples to draw
        :param float tolerance: The fraction of samples which are allowed to be over the threshold
        :param float threshold: The number of standard deviations a sample is allowed to be away from the obs
        :return:
        """
        import tensorflow_probability as tfp
        tfd = tfp.distributions

        if prior_x is None:
            prior_x = tfd.Uniform(low=tf.zeros(self.model.n_params, dtype=tf.float64),
                                  high=tf.ones(self.model.n_params, dtype=tf.float64))

        return _tf_sample(self.model, self.obs.data, prior_x, n_samples,
                          self.total_var, tolerance, threshold).numpy()

    def get_implausibility(self, sample_points, batch_size=1):
        """

        :param model:
        :param obs:
        :param sample_points:
        :param int batch_size:
        :return:
        """

        implausibility = _tf_implausibility(self.model, self.obs.data, sample_points,
                                            self.total_var, batch_size=batch_size,
                                            pbar=tf_tqdm(batch_size=batch_size,
                                                         total=sample_points.shape[0])
                                            )

        return self.model._post_process(implausibility, name_prefix='Implausibility in emulated ')

    def batch_constrain(self, sample_points, tolerance=0., threshold=3.0, batch_size=1):
        """

        :param float tolerance: The fraction of samples which are allowed to be over the threshold
        :param float threshold: The number of standard deviations a sample is allowed to be away from the obs
        :param sample_points:
        :param int batch_size:
        :return:
        """

        valid_samples = _tf_constrain(self.model, self.obs.data, sample_points,
                                      self.total_var,
                                      tolerance=tolerance, threshold=threshold,
                                      batch_size=batch_size,
                                      pbar=tf_tqdm(batch_size=batch_size,
                                                   total=sample_points.shape[0]))

        return valid_samples


@tf.function
def constrain(implausibility, tolerance=0., threshold=3.0):
    """
        Return a boolean array indicating if each sample meets the implausibility criteria:

            I < T

    :param np.array implausibility: Distance of each sample from each observation (in S.Ds)
    :param float tolerance: The fraction of samples which are allowed to be over the threshold
    :param float threshold: The number of standard deviations a sample is allowed to be away from the obs
    :return np.array: Boolean array of samples which meet the implausibility criteria
    """
    # Return True (for a sample) if the number of implausibility measures greater
    #  than the threshold is less than or equal to the tolerance
    tolerance = tf.constant(tolerance, dtype=implausibility.dtype)
    threshold = tf.constant(threshold, dtype=implausibility.dtype)
    return tf.less_equal(
                tf.reduce_sum(tf.cast(tf.greater(implausibility, threshold), dtype=implausibility.dtype), axis=1),
                tf.multiply(tolerance, tf.cast(tf.shape(implausibility)[1], dtype=tolerance.dtype))
           )


@tf.function
def _calc_implausibility(emulator_mean, obs, tot_sd):
    return tf.divide(tf.abs(tf.subtract(emulator_mean, obs)), tot_sd)


@tf.function
def _tf_constrain(model, obs, sample_points, total_variance,
                  tolerance, threshold, batch_size, pbar):
    """

    :param model:
    :param Tensor obs:
    :param Tensor sample_points:
    :param Tensor total_variance: Total variance in observational comparison
    :param int batch_size:
    :return:
    """
    with tf.device('/gpu:{}'.format(model._GPU)):

        sample_T = tf.data.Dataset.from_tensor_slices(sample_points)
        dataset = sample_T.batch(batch_size)

        all_valid = tf.zeros((0, ), dtype=tf.bool)

        for data in pbar(dataset):
            # Get batch prediction
            emulator_mean, emulator_var = model._tf_predict(data)

            tot_sd = tf.sqrt(tf.add(emulator_var, total_variance))
            implausibility = _calc_implausibility(emulator_mean, obs, tot_sd)

            valid_samples = constrain(implausibility, tolerance, threshold)
            all_valid = tf.concat([all_valid, valid_samples], 0)

    return all_valid


@tf.function
def _tf_implausibility(model, obs, sample_points, total_variance,
                       batch_size, pbar):
    """

    :param model:
    :param Tensor obs:
    :param Tensor sample_points:
    :param Tensor total_variance: Total variance in observational comparison
    :param int batch_size:
    :return:
    """
    with tf.device('/gpu:{}'.format(model._GPU)):

        sample_T = tf.data.Dataset.from_tensor_slices(sample_points)

        dataset = sample_T.batch(batch_size)

        all_implausibility = tf.zeros((0, obs.shape[0]), dtype=sample_points.dtype)

        for data in pbar(dataset):
            # Get batch prediction
            emulator_mean, emulator_var = model._tf_predict(data)

            tot_sd = tf.sqrt(tf.add(emulator_var, total_variance))
            implausibility = _calc_implausibility(emulator_mean, obs, tot_sd)

            all_implausibility = tf.concat([all_implausibility, implausibility], 0)

    return all_implausibility


# TODO SEPARETLY - Do this without tolerance and threshold by calculating the actual probability and accepting/rejecting against a uniform dist

@tf.function
def _tf_sample(model, obs, dist, n_sample_points, total_variance,
                  tolerance, threshold):
    """

    :param model:
    :param Tensor obs:
    :param Tensor sample_points:
    :param Tensor total_variance: Total variance in observational comparison
    :param int batch_size:
    :return:
    """
    with tf.device('/gpu:{}'.format(model._GPU)):
        samples = tf.zeros((0, 2), dtype=tf.float64)
        i0 = tf.constant(0)

        _, all_samples = tf.while_loop(
            lambda i, m: i < n_sample_points,
            lambda i, m: [i + 1,
                          tf.concat([m, get_valid_sample(model, obs, dist, threshold, tolerance, total_variance)],
                                    axis=0)],
            loop_vars=[i0, samples],
            shape_invariants=[i0.get_shape(), tf.TensorShape([None, 2])])
    return all_samples


@tf.function()
def get_valid_sample(model, obs, dist, threshold, tolerance, total_variance):
    valid = dist.sample()
    valid = tf.while_loop(
        lambda x: tf.math.logical_not(is_valid_sample(model, obs, x, threshold, tolerance, total_variance)),
        lambda x: (dist.sample(),),
        loop_vars=(valid,)
    )
    return tf.reshape(valid, (1, -1))


@tf.function
def is_valid_sample(model, obs, sample, threshold, tolerance, total_variance):
    emulator_mean, emulator_var = model._tf_predict(tf.reshape(sample, (1, -1)))
    tot_sd = tf.sqrt(tf.add(emulator_var, total_variance))
    implausibility = _calc_implausibility(emulator_mean, obs, tot_sd)
    valid = constrain(implausibility, tolerance, threshold)[0]
    return valid
