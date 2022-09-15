from pathlib import Path
import warnings

import numba
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy import stats
import tensorflow as tf
from tqdm import tqdm

import paltas
import paltas.Analysis

# Mappings from short to long parameter names and back
mdef = 'main_deflector_parameters_'
short_names = dict((
	(mdef + 'theta_E', 'theta_E'),
	('subhalo_parameters_sigma_sub', 'sigma_sub'),
	('subhalo_parameters_shmf_plaw_index', 'shmf_plaw_index'),
	('los_parameters_delta_los', 'delta_los'),
	(mdef + 'center_x', 'center_x'),
	(mdef + 'center_y', 'center_y'),
	(mdef + 'gamma', 'gamma'),
	(mdef + 'gamma1', 'gamma1'),
	(mdef + 'gamma2', 'gamma2'),
	(mdef + 'e1', 'e1'),
	(mdef + 'e2', 'e2')))
long_names = {v: k for k, v in short_names.items()}

# Parameter order used in the March 2022 paper
MARCH_2022_PARAMETERS = (
	'main_deflector_parameters_theta_E',
	'main_deflector_parameters_gamma1',
	'main_deflector_parameters_gamma2',
	'main_deflector_parameters_gamma',
	'main_deflector_parameters_e1',
	'main_deflector_parameters_e2',
	'main_deflector_parameters_center_x',
	'main_deflector_parameters_center_y',
	'subhalo_parameters_sigma_sub')

DEFAULT_PARAMETERS = tuple([
	long_names[p]
	for p in ('theta_E', 'sigma_sub', 'gamma')])
DEFAULT_TRAINING_SET = (
	Path(paltas.__path__[0]) / 'Configs' / 'paper_2203_00690' / 'config_train.py')


class GaussianInference:

	@classmethod
	def from_folder(cls, folder_path, **kwargs):
		"""Build inference object from folder. Assumes you already did
			run_network_on the folder.

		Arguments:
		 - folder: path to the folder with data

		Other arguments will be passed to GaussianInference.__init__,
		overriding values from the npz if desired.
		"""
		return cls.from_npz(Path(folder_path) / 'network_outputs.npz', **kwargs)

	@classmethod
	def from_npz(cls, npz_path, **kwargs):
		"""Build inference object from an npz generated by run_network_on

		Arguments:
		 - npz_path: path to the npz

		Other arguments will be passed to GaussianInference.__init__,
		overriding values from the npz if desired.
		"""
		with np.load(npz_path, allow_pickle=True) as npz:
			if 'image_prec' not in npz and 'image_cov' in npz:
				npz = dict(npz)
				npz['image_prec'] = np.linalg.inv(npz['image_cov'])

			kwargs_new = dict(
				y_pred=npz['image_mean'][:],
				prec_pred=npz['image_prec'][:],
				all_parameters=npz['param_names'][:],
				population_mean=npz['population_mean'][:],
				population_cov=npz['population_cov'][:],
				training_population_mean=npz['training_population_mean'][:],
				training_population_cov=npz['training_population_cov'][:],
			)
			# Allow overriding of npz values with kwargs
			kwargs_new = {**kwargs_new, **kwargs}
			return cls(**kwargs_new)

	def __init__(
			self,
			y_pred,
			prec_pred=None,
			cov_pred=None,
			population_mean=None,
			population_cov=None,
			test_config_path=None,
			log_sigma=False,
			select_parameters=MARCH_2022_PARAMETERS,
			all_parameters=MARCH_2022_PARAMETERS,
			training_population_mean=None,
			training_population_cov=None,
			n_images=None,
			):
		"""Infer means and (uncorrelated) standard deviations / sigmas
			of a lens population.

		Provide at least the following:
		 - y_pred;
		 - One of prec_pred (preferred) or cov_pred;
		 - One of (population_mean & population_cov) or test_config_path,

		Arguments:
		 - y_pred: (n_images, n_params) array with predicted means
		 - pred_pred: (n_images, n_params, n_params) array with predicted
			inverse covariances
		 - cov_pred: (n_images, n_params, n_params) array with predicted
			covariances
		 - test_config_path: Path to config used to generate the data.
		 	Used to extract population_mean & population_cov if not provided,
			can be omitted otherwise.
		 - population_mean: (n_params) array with population means of params
		 - population_cov: (n_params, n_params) array with population
		 	covariance matrix.
		 - log_sigma: if True, hyperprior will be uniform in Log[sigma]'s.
			Otherwise in hyperprior will be uniform in sigma's.
		 - select_parameters: sequence of strings, parameter names to do
			inference for. Others will be ignored.
		 - all_parameters: sequence of strings, parameter names ordered
			as in y_pred. Defaults to March 2022 paper settings.
		 - training_population_mean: (n_params) array, population_mean
		 	of training set. Defaults to March 2022 paper settings.
		 - training_population_cov: (n_params, n_params) array with
		 	population_cov of training set. Defaults to March 2022 paper.
		 - n_images: if specified, use only the first n_images images.
		"""
		# Get mean/cov of the training data (interim prior)
		if training_population_mean is None:
			training_population_mean, training_population_cov = \
				extract_mu_cov(DEFAULT_TRAINING_SET, all_parameters)

		# Get true mean/cov of this population (for guess initialization)
		if population_mean is None:
			if test_config_path is None:
				raise ValueError("Provide test_config_path or mean and cov")
			population_mean, population_cov = extract_mu_cov(test_config_path, all_parameters)

		if prec_pred is None:
			if cov_pred is not None:
				# Recover precision matrices from covariance matrices
				prec_pred = np.linalg.inv(cov_pred)
			else:
				raise ValueError("Provide prec_pred or cov_pred")
		prec_pred = symmetrize_batch(prec_pred)

		# Down-select images (if needed)
		if n_images is not None:
			y_pred = y_pred[:n_images,...]
			prec_pred = prec_pred[:n_images,...]

		# Get indices of parameters to select
		select_is = [
			list(all_parameters).index(p) 
			for p in select_parameters]
		# Apply the parameter selection to the vectors/matrices we need
		params = np.asarray(all_parameters)[select_is].tolist()
		y_pred = y_pred[:,select_is]
		prec_pred = select_from_matrix_stack(prec_pred, select_is)
		training_population_mean = training_population_mean[select_is]
		population_mean = population_mean[select_is]
		training_population_cov = training_population_cov[select_is][:,select_is]
		population_cov = population_cov[select_is][:,select_is]

		n_params = len(params)

		true_hyperparameters = np.concatenate([
			population_mean,
			cov_to_std(population_cov)[0]])
		if log_sigma:
			true_hyperparameters[n_params:] = np.log(
				true_hyperparameters[n_params:])

		# A uniform hyperprior, mainly to force some parameters positive.
		# (For MAP/MLE even this is not needed.)
		# Remember sigma_sub can be negative -- it will be treated as zero.
		positive_is = np.asarray(
			[params.index(long_names[p])
			 for p in ['theta_E', 'gamma']])

		# jit because this will be called from inside jitted functions
		# TODO: make staticmethod
		@numba.njit
		def log_hyperprior(hyperparameters):
			if hyperparameters[positive_is].min() < 0:
				return -np.inf
			# The hyperparameters we get are always log(sigma),
			# since paltas always uses log(sigma).
			log_sigmas = hyperparameters[n_params:]
			if log_sigmas.min() < -15:
				return -np.inf
			return 0

		# Initialize the  posterior and give it the network predictions.
		# TODO: can we still multiprocess.Pool now that these are local vars?
		# Do we care?
		prob_class = (
			paltas.Analysis.hierarchical_inference.ProbabilityClassAnalytical(
				training_population_mean, 
				training_population_cov, 
				log_hyperprior))
		prob_class.set_predictions(
			mu_pred_array_input=y_pred,
			prec_pred_array_input=prec_pred)

		# Store attributes we need later
		self.prob_class = prob_class
		self.log_sigma = log_sigma
		self.params = params
		self.n_params = n_params
		self.true_hyperparameters = true_hyperparameters
		self.positive_is = positive_is

	def log_posterior(self, x):
		if self.log_sigma:
			# Paltas' posterior always takes log(sigma) parameters,
			# so the hyperprior will be uniform in log(sigma) ...
			return self.prob_class.log_post_omega(x)
		else:
			# Don't want to modify input in-place
			x = x.copy()
			# We got sigmas, but paltas expects log sigmas
			sigmas = x[self.n_params:]
			if sigmas.min() <= 0:
				# No need to ask Paltas, impossible
				return -np.inf
			x[self.n_params:] = np.log(sigmas)
			return self.prob_class.log_post_omega(x)

	def _summary_df(self):
		sigma_prefix = 'log_std' if self.log_sigma else 'std'
		return pd.DataFrame(
			dict(param=(
					['mean_' + p for p in self.params]
					+ [sigma_prefix + '_' + p for p in self.params]
				 ),
				 truth=self.true_hyperparameters))

	def frequentist_asymptotic(
			self,
			use_bounds=False,
			hessian_step=1e-4,
			hessian_method='central',
			**kwargs):
		"""Returns the maximum likelihood estimate and covariance matrix
		for asymptotic frequentist confidence intervals.

		Arguments:
		 - use_bounds: Whether to pass bounds to inference. Defaults to False
		 	to be more robust to non-Gaussianity (e.g. in std_sigma_sub).
		 - hessian_step: step size to use in Hessian computation.
		 - hessian_method: method to use in Hessian computation;
			see numdifftools for details.
		Other arguments will be passed to scipy.minimize.

		Returns tuple of:
		 - DataFrame with results
		 - estimated covariance matrix

		The covariance matrix is estimated from the Hessian of the -2 log
		likelihood; the Hessian is estimated using finite-difference methods.
		"""
		import numdifftools

		if self.log_sigma:
			warnings.warn("Not using a uniform prior, good luck...")

		# Bounds (0, inf) for positive parameters and the sigmas
		if use_bounds:
			bounds = [
				(0, None) if i in self.positive_is 
				else (None, None)
				for i in range(self.n_params)]
			bounds += [(np.exp(-14), None)] * self.n_params
		else:
			bounds = None

		# Find the maximum a posteriori / maximum likelihood estimate
		# (they agree for a uniform prior)
		# .. or at least a local max close to the truth.
		def objective(x):
			return -2*self.log_posterior(x)
		with warnings.catch_warnings():
			warnings.filterwarnings(
				"ignore",
				message='invalid value encountered in subtract'
			)
			optresult = minimize(
				objective,
				x0=self.true_hyperparameters,
				bounds=bounds,
				**kwargs,
			)

		# Estimate covariance using the inverse Hessian
		# the minimizers's Hessian inverse estimate (optresult.hess_inv) is
		# not reliable enough, so do a new calculation with numdifftools
		hess = numdifftools.Hessian(
			objective,
			base_step=hessian_step,
			method=hessian_method)(optresult.x)
		cov = np.linalg.inv(hess)

		summary = self._summary_df()
		summary['fit'] = optresult.x
		summary['fit_unc'] = cov_to_std(cov)[0]

		return summary, cov

	def bayesian_mcmc(
			self,
			initial_walker_scatter=1e-3,
			n_samples=int(1e4),
			n_burnin=int(1e3),
			n_walkers=40,
			**kwargs):
		"""Return MCMC inference results

		Arguments:
		 - initial_walker_scatter: amplitude with which to vary walker
			positions. Multiplied by an (n_walkers, n_hyperparams) vector.
		 - n_samples: Number of MCMC samples to use (excluding burn-in)
		 - n_burnin: Number of burn-in samples to use
		 - n_walkers: Number of walkers to use
		Other arguments passed to emcee.EnsembleSampler

		Returns tuple with:
		 - DataFrame with summary of results
		 - chain excluding burn-in, (n_samples, n_hyperparams) array
		"""
		import emcee

		ndim = len(self.true_hyperparameters)

		# Scatter the initial walker states around the true values
		cur_state = (
			self.true_hyperparameters
			+ initial_walker_scatter * np.random.randn(n_walkers, ndim))
		if not self.log_sigma:
			# Don't start at negative sigmas: reflect initial state in 0
			cur_state_sigmas = cur_state[:,self.n_params:]
			cur_state[:,self.n_params:] = np.where(
				cur_state_sigmas < 0,
				-cur_state_sigmas,
				cur_state_sigmas)

		sampler = emcee.EnsembleSampler(
			n_walkers,
			ndim,
			self.log_posterior,
			**kwargs)
		sampler.run_mcmc(cur_state, n_burnin + n_samples, progress=True)
		chain = sampler.chain[:,n_burnin:,:].reshape((-1,ndim))

		summary = self._summary_df()
		summary['fit'] = chain.mean(axis=0)
		summary['fit_unc'] = chain.std(axis=0)

		return summary, chain


def extract_mu_cov(config_path, params):
	"""Return (mean, cov) arrays of distribution of params
	as defined by paltas config at config_path
	"""
	if not str(config_path).endswith('.py'):
		# Maybe the user gave a folder name
		# If it has only one python file, fine, that must be the config
		py_files = list(Path(config_path).glob('*.py'))
		if len(py_files) == 1:
			config_path = py_files[0]
		else:
			raise ValueError(f"{config_path} has multiple python files")

	ch = paltas.Configs.config_handler.ConfigHandler(config_path)

	# Frst extract the mean and std of all possible parameters
	# Most are mean deflector parameters ...
	mean_std = {
		pname: _get_mean_std(
			ch.config_dict['main_deflector']['parameters'][short_names[pname]],
			short_names[pname])
		for pname in params if pname.startswith('main_deflector')
	}
	# ... except for sigma_sub
	# TODO: other subhalo params!
	mean_std[long_names['sigma_sub']] = _get_mean_std(
		ch.config_dict['subhalo']['parameters']['sigma_sub'],
		'sigma_sub')

	# Produce mean vector / cov matrix in the right order
	mu, std = np.array([mean_std[pname] for pname in params]) .T
	cov = np.diag(std**2)
	return mu, cov


def _get_mean_std(x, pname):
	# Helper to extract mean/std given a paltas config value
	# (paltas configs contain .rvs methods, not dists themselves)
	if isinstance(x, (int, float)):
		# Value was kept constant
		return x, 0
	# Let's hope it is a scipy stats distribution, so we can
	# back out the mean and std through sneaky ways
	self = x.__self__
	dist = self.dist
	if not isinstance(dist, (
			stats._continuous_distns.norm_gen)):
		warnings.warn(
			f"Approximating {dist.name} for {pname} with a normal distribution",
			UserWarning)
	return self.mean(), self.std()


def select_from_matrix_stack(matrix_stack, select_i):
	"""Select specific simultaneous row and column indices
	from a stack of matrices"""
	sel_x, sel_y = np.meshgrid(select_i, select_i, indexing='ij')
	return (
		matrix_stack[:, sel_x.ravel(), sel_y.ravel()]
		.reshape([-1] + list(sel_x.shape)))


def cov_to_std(cov):
	"""Return (std errors, correlation coefficent matrix)
	given covariance matrix cov
	"""
	std_errs = np.diag(cov) ** 0.5
	corr = cov * np.outer(1 / std_errs, 1 / std_errs)
	return std_errs, corr


def run_network_on(
		folder, 
		norm_path=Path('norms.csv'),
		# MD5 is identical to the one from zenodo
		model_path=Path('xresnet34_full_marg_1_final.h5'), 
		train_config_path=DEFAULT_TRAINING_SET,
		batch_size=50,
		n_rotations=1,
		regenerate_tfrecord=False,
		overwrite=False,
		return_result=False,
		output_filename='network_outputs.npz',
		save_penultimate=True):
	"""Run a neural network over a folder with image data.
	Creates output_filename with return values in that folder.

	Arguments:
	 - folder: path to folder with images, metadata, etc.
	 - norm_path: path to norms.csv for the network
	 - model_path: path to h5 of the neural network
	 - train_config_path: path to the dataset used to train
	 	the network.
	 - batch_size: batch size.
	 - n_rotations: if > 1, average point estimates (not covariances)
	 	over rotations of the image, uniform(0, 360, n_rotations)
	 - regenerate_tfrecord: If True, regererates tfrecord file
	 	even if it is already present.
	 - overwrite: If True, first delete existing network_outputs.npz
	 	if present
	 - return_result: If True, also returns stuff saved to the npz
	 - save_penultimate: if True, runs the model twice; the second time
	 	just to save the output of the penultimate layer.
	"""

	# Check input/output folders
	folder = Path(folder)
	assert folder.exists()
	output_path = folder / output_filename
	if output_path.exists():
		if overwrite:
			output_path.unlink()
		else:
			print(f"Already ran network on {folder}, nothing to do")
			return
	
	# Load and process metadata
	metadata_path = folder / 'metadata.csv'
	df = pd.read_csv(metadata_path, index_col=False)
	# Remove silly columns from metadata, saves space
	for prefix, col_name in paltas.Configs.config_handler.EXCLUDE_FROM_METADATA:
		col_name = prefix + '_' + col_name
		if col_name in df:
			del df[col_name]
	# Add dataset name and image number
	# just in case things get mixed around again
	df['dataset_name'] = folder.name
	df['image_i'] = np.arange(len(df))
		
	# Load normalization / parameter names and order
	norm_df = pd.read_csv(norm_path)
	learning_params = norm_df.parameter.values.tolist()

	# Load model
	model = tf.keras.models.load_model(
		Path(model_path),
		custom_objects=dict(loss=None))

	if save_penultimate:
		# Model with the fully-connected head removed
		# (for our model, that's just one layer)
		# TODO: if architecure changes, have to change the index here
		model_conv = tf.keras.Model(
			inputs=model.input,
			outputs=model.get_layer(index=-2).output)
	
	# Extract training and test/population mean and cov
	training_population_mean, training_population_cov = extract_mu_cov(
		train_config_path, learning_params)
	population_mean, population_cov = extract_mu_cov(
		folder, learning_params)
	
	# Ensure we have the tfrecord dataset
	tfr_path = folder / 'data.tfrecord'
	if tfr_path.exists() and regenerate_tfrecord:
		tfr_path.unlink()
	if not tfr_path.exists():
		paltas.Analysis.dataset_generation.generate_tf_record(
			npy_folder=str(folder),
			learning_params=learning_params,
			metadata_path=str(metadata_path),
			tf_record_path=str(tfr_path))

	# Construct the paltas dataset generator
	test_dataset = paltas.Analysis.dataset_generation.generate_tf_dataset(
		tf_record_path=str(tfr_path),
		learning_params=learning_params,
		batch_size=batch_size,
		n_epochs=1,
		norm_images=True,
		input_norm_path=norm_path,
		kwargs_detector=None,  # Don't add more noise
		log_learning_params=None,
		shuffle=False)

	if n_rotations == 0:
		# Should do the same as n_rotations=1, retained for testing.
		image_mean, image_prec = _predict(model, test_dataset, learning_params)
		image_cov = np.linalg.inv(image_prec)

		# Convert to physical units. Modifies image_xxx variables in-place
		paltas.Analysis.dataset_generation.unnormalize_outputs(
			input_norm_path=norm_path,
			learning_params=learning_params,
			mean=image_mean,
			cov_mat=image_cov,
			prec_mat=image_prec,
		)

	else:
		# Compute predictions over several angles
		# (note we skip 2 pi since it's equivalent to zero)
		means, covs = [], []
		for angle in tqdm(np.linspace(0, 2 * np.pi, n_rotations + 1)[:-1], 
						desc='Running neural net over different rotations'):
			# Get predictions on rotated dataset
			_mean, _prec = _predict(
				model, 
				_rotation_generator(test_dataset, learning_params, angle), 
				learning_params)

			# Recover covariance: rotation of precision matrix not yet coded
			_cov = np.linalg.inv(_prec)

			# Convert to physical units. Modifies image_xxx variables in-place
			# NB: must do this before back-rotation!
			paltas.Analysis.dataset_generation.unnormalize_outputs(
				input_norm_path=norm_path,
				learning_params=learning_params,
				mean=_mean,
				cov_mat=_cov,
			)

			# Rotate back to original frame. Modifies in-place
			paltas.Analysis.dataset_generation.rotate_params_batch(
				learning_params, _mean, -angle)
			paltas.Analysis.dataset_generation.rotate_covariance_batch(
				learning_params, _cov, -angle)

			means.append(_mean)
			covs.append(_cov)
		means, covs = np.array(means), np.array(covs)

		# Average predictions obtained from different rotation angles
		image_mean = np.mean(means, axis=0)
		# Paltas paper says: covariances, and image predictions for x_lens and 
		# y_lens, are not averaged over rotations.
		for param in (mdef + 'center_x', mdef + 'center_y'):
			if param in learning_params:
				i = learning_params.index(param)
				image_mean[:,i] = means[0,:,i]
		image_cov = covs[0]
		image_prec = np.linalg.inv(image_cov)
	
	# Enforce symmetry on the precision and covariance matrices.
	# Floating-point errors can spoil this and make the entire inference
	# return -inf. Fun!
	image_cov = symmetrize_batch(image_cov)
	image_prec = symmetrize_batch(image_prec)

	if save_penultimate:
		# Run the model again, saving the output of the penultimate layer
		# A bit wasteful to run it twice, but OK...
		conv_outputs = model_conv.predict(test_dataset)
	else:
		conv_outputs = None

	# Save everything needed for inference to a big npz
	result = dict(
		image_mean=image_mean, 
		image_cov=image_cov,
		image_prec=image_prec,
		# OK, image truths are not needed for inference. But why not save them..
		image_truth=df[learning_params].values,
		population_mean=population_mean,
		population_cov=population_cov,
		training_population_mean=training_population_mean,
		training_population_cov=training_population_cov,
		# Copy over parameter names/orders for interpretability
		param_names=learning_params,
		# These are a bit large, but who cares: just put everything in one file...
		convout=conv_outputs,
		metadata=df.to_records(index=False),
		model_name=Path(model_path).name,
	)

	np.savez_compressed(output_path, **result)
	if return_result:
		return result


def _predict(model, dataset, learning_params):
	# Finally! Actually run the network over the images
	result = model.predict(dataset)

	# Parse the result
	# For diagonal loss, change to:
	# loss = paltas.Analysis.loss_functions.DiagonalCovarianceLoss(
	# ...
	# y_pred, log_var_pred = [x.numpy() for x in loss.convert_output(result)]
	loss = paltas.Analysis.loss_functions.FullCovarianceLoss(
		len(learning_params), flip_pairs=None, weight_terms=None)
	image_mean, image_prec, _ = [x.numpy() for x in loss.convert_output(result)]
	return image_mean, image_prec

def _rotation_generator(dataset, learning_params, angle):
	for images, truths in dataset:
		images = images.numpy()
		truths = truths.numpy()
		images = paltas.Analysis.dataset_generation.rotate_image_batch(
			images,
			learning_params,
			# NB truths is changed in-place!
			truths,
			angle)
		yield images, truths


def symmetrize_batch(x):
	"""Return symmetrized version of an array of matrices"""
	return (x.transpose((0, 2, 1)) + x)/2