'''
gpr.py
======
Gaussian process regression:
model training, prediction and uncertainty exploration
This module serves as a high-level wrapper for sparse Gaussian processes module
from Pyro probabilistic programming library (https://pyro.ai/)
for easy work with scientific image (2D) and hyperspectral (3D) data.
Author: Maxim Ziatdinov (email: maxim.ziatdinov@ai4microcopy.com)
'''

import time
import numpy as np
from gpim.kernels import pyro_kernels
from gpim import gprutils
import torch
import pyro
import pyro.contrib.gp as gp
import warnings


class reconstructor:
    """
    Class for Gaussian process-based reconstuction
    of sparse 2D images and 3D spectroscopic datasets,
    and exploration/explotaition routines for the selection
    of next query point.

    Args:
        X (ndarray):
            Grid indices with dimensions :math:`c \\times N \\times M`
            or :math:`c \\times N \\times M \\times L`
            where *c* is equal to the number of coordinates
            (for example, for *xyz* coordinates, *c* = 3)
        y (ndarray):
            Observations (data points) with dimensions
            :math:`N \\times M` or :math:`N \\times M \\times L`.
            Typically, for 2D image *N* and *M* are image height and width,
            whereas for 3D hyperspectral data *N* and *M* are spatial dimensions
            and *L* is a spectorcopic dimension (e.g. voltage or wavelength).
        Xtest (ndarray):
            "Test" points (for prediction with a trained GP model)
            with dimensions :math:`N \\times M` or :math:`N \\times M \\times L`
        kernel (str):
            Kernel type ('RBF', 'Matern52', 'RationalQuadratic')
        lengthscale (list of int or list of two lists with int):
            Determines lower (1st value or 1st list) and upper (2nd value or 2nd list)
            bounds for kernel lengthscales. For list with two integers,
            the kernel will have only one lenghtscale, even if the dataset
            is multi-dimensional. For lists of two lists, the number of elements
            in each list must be equal to the dataset dimensionality.
        sparse (bool):
            Perform sparse GP regression when set to True.
        indpoints (int):
            Number of inducing points for SparseGPRegression.
            Defaults to total_number_of_points // 10.
        learning_rate (float):
            Learning rate for model training
        iterations (int): Number of SVI training iteratons
        use_gpu (bool):
            Uses GPU hardware accelerator when set to 'True'.
            Notice that for large datasets training model without GPU
            is extremely slow.
        verbose (int):
            Level of verbosity (0, 1, or 2)
        seed (int):
            for reproducibility
        **amplitude (float): kernel variance or amplitude squared
        **precision (str):
            Choose between single ('single') and double ('double') precision
    """
    def __init__(self,
                 X,
                 y,
                 Xtest=None,
                 kernel='RBF',
                 lengthscale=None,
                 sparse=False,
                 indpoints=None,
                 learning_rate=5e-2,
                 iterations=1000,
                 use_gpu=False,
                 verbose=1,
                 seed=0,
                 **kwargs):
        """
        Initiates reconstructor parameters
        and pre-processes training and test data arrays
        """
        self.precision = kwargs.get("precision", "double")
        if self.precision == 'single':
            self.tensor_type = torch.FloatTensor
            self.tensor_type_gpu = torch.cuda.FloatTensor
        else:
            self.tensor_type = torch.DoubleTensor
            self.tensor_type_gpu = torch.cuda.DoubleTensor
        self.verbose = verbose
        torch.manual_seed(seed)
        pyro.set_rng_seed(seed)
        pyro.clear_param_store()
        if use_gpu and torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
            torch.set_default_tensor_type(self.tensor_type_gpu)
            use_gpu = True
        else:
            torch.set_default_tensor_type(self.tensor_type)
            use_gpu = False
        input_dim = np.ndim(y)
        self.X, self.y = gprutils.prepare_training_data(
            X, y, precision=self.precision)
        self.do_sparse = sparse
        if lengthscale is None and not kwargs.get("isotropic"):
            lengthscale = [[0. for l in range(input_dim)],
                           [np.mean(y.shape) / 2 for l in range(input_dim)]]  # TODO Make separate lscale for each dim
        elif lengthscale is None and kwargs.get("isotropic"):
            lengthscale = [0., np.mean(y.shape) / 2]
        kernel = pyro_kernels.get_kernel(
            kernel, input_dim, lengthscale, use_gpu,
            amplitude=kwargs.get('amplitude'), precision=self.precision)
        if Xtest is not None:
            self.fulldims = Xtest.shape[1:]
        else:
            self.fulldims = X.shape[1:]
        if Xtest is not None:
            self.Xtest = gprutils.prepare_test_data(
                Xtest, precision=self.precision)
        else:
            self.Xtest = Xtest
        if use_gpu:
            self.X = self.X.cuda()
            self.y = self.y.cuda()
            if self.Xtest is not None:
                self.Xtest = self.Xtest.cuda()
        if not self.do_sparse:
            self.model = gp.models.GPRegression(self.X,  self.y, kernel)
        else:
            if indpoints is None:
                indpoints = len(self.X) // 10
                indpoints = indpoints + 1 if indpoints == 0 else indpoints
            else:
                indpoints = len(self.X) if indpoints > len(self.X) else indpoints
            Xu = self.X[::len(self.X) // indpoints]
            if self.verbose == 2:
                print("# of inducing points for sparse GP regression: {}".format(len(Xu)))
            self.model = gp.models.SparseGPRegression(
                self.X, self.y, kernel, Xu, jitter=1.0e-5)
        if use_gpu:
            self.model.cuda()
        self.learning_rate = learning_rate
        self.iterations = iterations
        self.hyperparams = {}
        self.indpoints_all = []
        self.lscales, self.noise_all, self.amp_all = [], [], []
        self.hyperparams = {
            "lengthscale": self.lscales,
            "noise": self.noise_all,
            "variance": self.amp_all,
            "inducing_points": self.indpoints_all
            }

    def train(self, **kwargs):
        """
        Training sparse GP regression model

        Args:
            **learning_rate (float): learning rate
            **iterations (int): number of SVI training iteratons
        """
        if kwargs.get("learning_rate") is not None:
            self.learning_rate = kwargs.get("learning_rate")
        if kwargs.get("iterations") is not None:
            self.iterations = kwargs.get("iterations")
        if kwargs.get("verbose") is not None:
            self.verbose = kwargs.get("verbose")
        pyro.clear_param_store()
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.learning_rate)
        loss_fn = pyro.infer.Trace_ELBO().differentiable_loss
        start_time = time.time()
        if self.verbose:
            print('Model training...')
        for i in range(self.iterations):
            optimizer.zero_grad()
            loss = loss_fn(self.model.model, self.model.guide)
            loss.backward()
            optimizer.step()
            self.lscales.append(self.model.kernel.lengthscale_map.tolist())
            self.amp_all.append(self.model.kernel.variance_map.item())
            self.noise_all.append(self.model.noise.item())
            if self.do_sparse:
                self.indpoints_all.append(self.model.Xu.detach().cpu().numpy())
            if self.verbose == 2 and (i % 100 == 0 or i == self.iterations - 1):
                print('iter: {} ...'.format(i),
                      'loss: {} ...'.format(np.around(loss.item(), 4)),
                      'amp: {} ...'.format(np.around(self.amp_all[-1], 4)),
                      'length: {} ...'.format(np.around(self.lscales[-1], 4)),
                      'noise: {} ...'.format(np.around(self.noise_all[-1], 7)))
            if i == 100 and self.verbose:
                print('average time per iteration: {} s'.format(
                    np.round(time.time() - start_time, 2) / 100))
        if self.verbose:
            print('training completed in {} s'.format(
                np.round(time.time() - start_time, 2)))
            print('Final parameter values:\n',
                'amp: {}, lengthscale: {}, noise: {}'.format(
                    np.around(self.model.kernel.variance_map.item(), 4),
                    np.around(self.model.kernel.lengthscale_map.tolist(), 4),
                    np.around(self.model.noise.item(), 7)))
        return

    def predict(self, Xtest=None, **kwargs):
        """
        Uses trained GP regression model to make predictions

        Args:
            Xtest (ndarray):
                "Test" points (for prediction with a trained GP model)
                with dimensions :math:`N \\times M` or :math:`N \\times M \\times L`.
                Uses Xtest from __init__ by default. If Xtest is None,
                uses training data X.
        Returns:
            Predictive mean and standard deviation
        """
        if Xtest is None and self.Xtest is None:
            warnings.warn(
                "No test data provided. Using training data for prediction",
                UserWarning)
            self.Xtest = self.X
        elif Xtest is not None:
            self.Xtest = gprutils.prepare_test_data(
                Xtest, precision=self.precision)
            self.fulldims = Xtest.shape[1:]
            if next(self.model.parameters()).is_cuda:
                self.Xtest = self.Xtest.cuda()
        if kwargs.get("verbose") is not None:
            self.verbose = kwargs.get("verbose")
        if self.verbose:
            print("Calculating predictive mean and variance...", end=" ")
        with torch.no_grad():
            mean, cov = self.model(self.Xtest, full_cov=False, noiseless=False)
        mean = mean.cpu().numpy()
        sd = cov.sqrt().cpu().numpy()
        mean = mean.reshape(self.fulldims)
        sd = sd.reshape(self.fulldims)
        if self.verbose:
            print("Done")
        return mean, sd

    def run(self, **kwargs):
        """
        Trains the initialized model and calculate predictive mean and variance

        Args:
            **learning_rate (float):
                learning rate for GP regression model training
            **steps (int):
                number of SVI training iteratons
        Returns:
            Predictive mean, standard deviation and
            dictionary with hyperparameters evolution
            as a function of SVI steps
        """
        if kwargs.get("learning_rate") is not None:
            self.learning_rate = kwargs.get("learning_rate")
        if kwargs.get("iterations") is not None:
            self.iterations = kwargs.get("iterations")
        self.train(learning_rate=self.learning_rate, iterations=self.iterations)
        mean, sd = self.predict()
        if next(self.model.parameters()).is_cuda:
            self.model.cpu()
            torch.set_default_tensor_type(self.tensor_type)
            self.X, self.y = self.X.cpu(), self.y.cpu()
            self.Xtest = self.Xtest.cpu()
            torch.cuda.empty_cache()
        return mean, sd, self.hyperparams

    def step(self, acquisition_function=None,
             batch_size=100, batch_update=False,
             lscale=None, **kwargs):
        """
        Performs single train-predict step for exploration analysis
        returning a new point with maximum value of acquisition function

        Args:
            acquisition_function (python function):
                Function that takes two parameters, mean and sd,
                and applies some math operation to them
                (e.g. :math:`\\upmu - 2 \\times \\upsigma`)
            batch_size (int):
                Number of query points to return
            batch_update:
                Filters the query points based on the specified lengthscale
            lscale (float):
                Lengthscale determining the separation (euclidean)
                distance between query points. Defaults to the kernel
                lengthscale
            **learning_rate (float):
                Learning rate for GP regression model training
            **steps (int):
                Number of SVI training iteratons
        Returns:
            Lists of indices and values for points with maximum uncertainty,
            predictive mean and standard deviation (as flattened numpy arrays)
        """
        if kwargs.get("learning_rate") is not None:
            self.learning_rate = kwargs.get("learning_rate")
        if kwargs.get("iterations") is not None:
            self.iterations = kwargs.get("iterations")
        if lscale is None:
            lscale = self.model.kernel.lengthscale.mean().item()
        # train a model
        self.train(learning_rate=self.learning_rate, iterations=self.iterations)
        # make prediction
        mean, sd = self.predict()
        # find point with maximum value of acquisition function
        sd_ = sd.reshape(self.fulldims)
        mean_ = mean.reshape(self.fulldims)
        vals, inds = gprutils.acquisition(
            mean_, sd_, acquisition_function,
            batch_size, batch_update, lscale)
        return vals, inds, mean, sd
