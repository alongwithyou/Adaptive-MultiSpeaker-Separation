# -*- coding: utf-8 -*-
from utils.ops import unpool, variable_summaries, get_scope_variable, scope, log10, AMSGrad
import os
import config
import tensorflow as tf
import haikunator
from itertools import compress, combinations, permutations
from tensorflow.python.saved_model import builder as saved_model_builder
import numpy as np
import json

#############################################
#     Adaptive Front and Back End Model     #
#############################################

class Adapt:
	def __init__(self, runID=None, **kwargs):
		##
		## Model Configuration 
		##
		print kwargs
		if kwargs is not None:
			self.N = kwargs['filters']
			self.max_pool_value = kwargs['max_pool']
			self.l = kwargs['regularization']
			self.folder = kwargs['type']
			self.beta = kwargs['beta']
			self.p = kwargs['sparsity']
			self.window = kwargs['window_size']
			self.optimizer = kwargs['optimizer']
			self.S = kwargs['nb_speakers']
			self.pretraining = kwargs['pretraining']
			self.sepNet = kwargs['separator']
			self.overlap_coef = kwargs['overlap_coef']
			self.args = kwargs

		if runID is None:
			# Run ID for tensorboard
			self.runID = 'AdaptiveNet' + '-' + haikunator.Haikunator().haikunate()
			print 'ID : {}'.format(self.runID)
		else:
			self.runID = 'AdaptiveNet' + '-' + runID


		#Create a graph for this model
		self.graph = tf.Graph()

		with self.graph.as_default():

			tf.set_random_seed(42) # Constant seed for uniform results
			np.random.seed(42)
			# Boolean placeholder signaling if the model is in learning/training mode
			self.training = tf.placeholder(tf.bool, name='is_training')

			# Placeholder for the learning rate
			self.learning_rate = tf.placeholder("float", name='learning_rate')

			# Batch of raw non-mixed audio
			# shape = [ batch size , number of speakers, samples ] = [ B, S, L]
			self.X_non_mix = tf.placeholder("float", [None, None, None], name='non_mix_input')

			# Batch of raw mixed audio - Input data
			# shape = [ batch size , samples ] = [ B , L ]
			self.X_mix = tf.placeholder("float", [None, None], name='mix_input')

			if self.pretraining:
				shape_in = tf.shape(self.X_non_mix)
				self.B = shape_in[0]
				self.L = shape_in[2]

				self.x = tf.concat([self.X_mix,  tf.reshape(self.X_non_mix, [self.B*self.S, self.L])], axis=0)
				self.B_tot = tf.shape(self.x)[0]
			else:

				# Speakers indicies used in the mixtures
				# shape = [ batch size, #speakers]
				self.Ind = tf.placeholder(tf.int32, [None, None], name='indicies')
				shape_in = tf.shape(self.X_mix)

				self.B = shape_in[0]
				self.L = shape_in[1]

				self.x =tf.concat([self.X_mix,  tf.reshape(self.X_non_mix, [self.B*self.S, self.L])], axis=0)

				self.B_tot = self.B*(self.S+1)
					
			if self.pretraining:
				self.front
				self.separator
				self.back
				self.cost
				self.optimize
			else:
				self.front

		# Create a session for this model based on the constructed graph
		config_ = tf.ConfigProto()
		config_.gpu_options.allow_growth = True
		config_.allow_soft_placement = True
		self.sess = tf.Session(graph=self.graph, config=config_)

	def tensorboard_init(self):
		with self.graph.as_default():

			if self.pretraining:
				variable_summaries(self.conv_filter)
				variable_summaries(self.conv_filter_2)

				tf.summary.audio(name= "input/non-mixed", tensor = tf.reshape(self.X_non_mix, [-1, self.L]), sample_rate = config.fs, max_outputs=2)
				tf.summary.audio(name= "input/mixed", tensor = self.x[:self.B], sample_rate = config.fs, max_outputs=1)

				trs = lambda x : tf.transpose(x, [0, 2, 1, 3])
				tf.summary.image(name= "mix", tensor = trs(self.inmix), max_outputs=1)
				tf.summary.image(name= "non_mix", tensor = trs(self.innonmix), max_outputs=2)
				tf.summary.image(name= "separated", tensor = trs(self.ou), max_outputs=2)
				tf.summary.image(name= "separated", tensor = trs(self.unpool_board), max_outputs=2)

				tf.summary.audio(name= "output/reconstructed", tensor = tf.reshape(self.back, [-1, self.L]), sample_rate = config.fs, max_outputs=2)
				with tf.name_scope('loss_values'):
					tf.summary.scalar('loss', self.SDR)
					tf.summary.scalar('mse', tf.reduce_mean(self.mse))
					tf.summary.scalar('SDR', self.sdr_improvement())
					tf.summary.scalar('sparsity', tf.reduce_mean(self.p_hat))
					tf.summary.scalar('sparse_reg', self.sparse_reg)
					tf.summary.scalar('regularization', self.reg)
					tf.summary.scalar('training_cost', self.cost)
					tf.summary.scalar('overlapping', self.overlapping)

			self.merged = tf.summary.merge_all()
			self.train_writer = tf.summary.FileWriter(os.path.join(config.log_dir,self.folder,self.runID,'train'), self.graph)
			self.valid_writer = tf.summary.FileWriter(os.path.join(config.log_dir,self.folder,self.runID,'valid'), self.graph)
			self.saver = tf.train.Saver()

			# Save arguments
			with open(os.path.join(config.log_dir,self.folder,self.runID,'params'), 'w') as f:
				json.dump(self.args, f)

	def create_saver(self, subset=None):
		with self.graph.as_default():
			if subset is None:
				self.saver = tf.train.Saver()
			else:
				self.saver = tf.train.Saver(subset)

	def create_centroids_saver(self):
		with self.graph.as_default():
			self.centroids_saver = tf.train.Saver([self.sepNet.speaker_vectors], max_to_keep=10000000)

	def restore_model(self, path):
		self.saver.restore(self.sess, tf.train.latest_checkpoint(path))
	
	def restore_last_checkpoint(self):
		self.saver.restore(self.sess, tf.train.latest_checkpoint(os.path.join(config.log_dir, self.folder ,self.runID)))


	def savedModel(self):
		with self.graph.as_default():
			path = os.path.join(config.log_dir,self.folder ,self.runID, 'SavedModel')
			builder = saved_model_builder.SavedModelBuilder(path)

			# Build signatures
			input_tensor_info = tf.saved_model.utils.build_tensor_info(self.X_mix)
			output_tensor_info = tf.saved_model.utils.build_tensor_info(self.back)

			signature = tf.saved_model.signature_def_utils.build_signature_def(
				inputs={
					'mixed_audio':
					input_tensor_info
				},
				outputs={
					'unmixed_audio':
					output_tensor_info
				}
				)


			builder.add_meta_graph_and_variables(
				self.sess, ['validating'],
				signature_def_map={'separate_audio':signature}
				)

			builder.save()
			print 'Successfully exported model to %s' % path



	def init(self):
		with self.graph.as_default():
			self.sess.run(tf.global_variables_initializer())
 
	def non_initialized_variables(self):
		with self.graph.as_default():
			global_vars = tf.global_variables()
			is_not_initialized = self.sess.run([~(tf.is_variable_initialized(var)) \
										   for var in global_vars])
			not_initialized_vars = list(compress(global_vars, is_not_initialized))
			print 'not init: ', [v.name for v in not_initialized_vars]
			if len(not_initialized_vars):
				init = tf.variables_initializer(not_initialized_vars)
				return init

	def connect_only_front_to_separator(self, separator, freeze_front=True):
		with self.graph.as_default():
			self.connect_front(separator)
			self.sepNet.output = self.sepNet.prediction
			self.cost = self.sepNet.cost
			if freeze_front:
				self.freeze_front()
			self.optimize
			self.tensorboard_init()

	def connect_front_back_to_separator(self, separator):
		with self.graph.as_default():
			self.connect_front(separator)
			self.sepNet.prediction
			self.sepNet.output = self.sepNet.separate
			self.separator
			self.back
			self.cost

	##
	## Front End creating STFT like data
	##
	@scope
	def front(self):
		# Front-End

		# x : [ Btot , 1, L , 1]
		# Equivalent to B_tot batches of image of height = 1, width = L and 1 channel -> for Conv1D with Conv2D
		input_front =  tf.reshape(self.x, [self.B_tot, 1, self.L, 1])

		# Filter [filter_height, filter_width, input_channels, output_channels] = [1, W, 1, N]
		# self.window_filter = get_scope_variable('window', 'w', shape=[self.window], initializer=tf.contrib.layers.xavier_initializer_conv2d())
		# self.bases = get_scope_variable('bases', 'bases', shape=[self.window, self.N], initializer=tf.contrib.layers.xavier_initializer_conv2d())
		# self.conv_filter = tf.reshape(tf.expand_dims(self.window_filter,1)*self.bases , [1, self.window, 1, self.N])
		self.conv_filter = get_scope_variable('filters_front','filters_front', shape=[1, self.window, 1, self.N], initializer=tf.contrib.layers.xavier_initializer_conv2d())

		# 1 Dimensional convolution along T axis with a window length = self.window
		# And N = 256 filters -> Create a [Btot, 1, T, N]
		self.X = tf.nn.conv2d(input_front, self.conv_filter, strides=[1, 1, 1, 1], padding="SAME", name='Conv_STFT')
		
		# Reshape to Btot batches of T x N images with 1 channel
		self.X = tf.reshape(self.X, [self.B_tot, -1, self.N, 1])

		self.T = tf.shape(self.X)[1]

		# Max Pooling with argmax for unpooling later in the back-end layer
		# Along the T axis (time)
		self.y, argmax = tf.nn.max_pool_with_argmax(self.X, (1, self.max_pool_value, 1, 1),
													strides=[1, self.max_pool_value, 1, 1], padding="SAME")

		y_shape = tf.shape(self.y)
		y = tf.reshape(self.y, [self.B_tot, y_shape[1]*y_shape[2]])
		self.p_hat = tf.reduce_mean(tf.abs(y), 0)
		self.latent_loss = tf.reduce_sum(self.kl_div(self.p, self.p_hat))

		return self.y, argmax


	@scope
	def separator(self):
		# shape = [B_tot, T_, N, 1], shape = [B(1+S), T , N, 1], [B(1+S), T_ , N, 1]
		separator_in, argmax_in = self.front

		argmax_in = argmax_in[:self.B]

		repeats = [self.S, 1, 1 ,1]
		shape = tf.shape(argmax_in)
		argmax_in = tf.expand_dims(argmax_in, 1)
		argmax_in = tf.tile(argmax_in, [1, self.S, 1, 1 ,1])
		argmax_in = tf.reshape(argmax_in, shape*repeats)

		if self.pretraining:
			self.T_max_pooled = tf.shape(separator_in)[1]

			input = tf.reshape(separator_in, [self.B_tot, self.T_max_pooled, self.N])

			input_mix = tf.reshape(input[:self.B, : , :], [self.B, 1, self.T_max_pooled, self.N]) # B first batches correspond to mix input

			input_non_mix = tf.reshape(input[self.B:, : , :], [self.B, self.S, self.T_max_pooled, self.N]) # B*S others non mix
			
			#For Tensorboard
			self.inmix = tf.reshape(input_mix, [self.B, self.T_max_pooled, self.N, 1])
			self.innonmix = tf.reshape(input_non_mix, [self.B*self.S, self.T_max_pooled, self.N, 1])

			input_mix = tf.tile(input_mix, [1, self.S, 1, 1])

			# Compute the overlapping rate:
			nb = 2
			comb = list(combinations(range(self.S), nb))
			len_comb = len(comb)
			combs = tf.reshape(tf.constant(comb), [1, len_comb, 2, 1])
			combs = tf.tile(combs, [self.B, 1, 1, 1])
			batch_range = tf.tile(tf.reshape(tf.range(self.B, dtype=tf.int32), shape=[self.B, 1, 1, 1]), [1, len_comb, nb, 1])
			comb_range = tf.tile(tf.reshape(tf.range(len_comb, dtype=tf.int32), shape=[1, len_comb, 1, 1]), [self.B, 1, nb, 1])
			indicies = tf.concat([batch_range, comb_range, combs], axis=3)
			comb_non_mix = tf.gather_nd(tf.tile(tf.reshape(input_non_mix, [self.B, 1, self.S, self.T_max_pooled*self.N]), [1, len_comb, 1, 1]), indicies) # 

			# Combination of non mixed representations : [B, len(comb), nb, T*N]
			comb_non_mix = tf.abs(comb_non_mix)
			measure = 1.0 - tf.abs(comb_non_mix[:,:,0,:] - comb_non_mix[:,:,1,:]) / tf.reduce_max(comb_non_mix, axis=2)
			overlapping = tf.reduce_sum(measure, -1)
			overlapping = tf.reduce_mean(overlapping, -1) # Mean over combinations
			self.overlapping = tf.reduce_mean(overlapping, -1) # Mean over batches

			# filters = tf.divide(input_non_mix, tf.clip_by_value(input_mix, 1e-4, 1e10))
			# filters = tf.square(input_non_mix) / tf.clip_by_value(tf.reduce_sum(tf.square(input_non_mix), 1, keep_dims=True), 1e-4, 1e10) 
			# output = tf.reshape(input_mix * filters, [self.B*self.S, self.T_max_pooled, self.N, 1])

			# From [a, b, c ,d] -> [a+b+c+d, a+b+c+d, a+b+c+d, a+b+c+d]
			tiled_sum = tf.tile(tf.reduce_sum(input_non_mix, 1, keep_dims=True), [1, self.S, 1, 1])
			# From [a+b+c+d, a+b+c+d, a+b+c+d, a+b+c+d] ->  [b+c+d, a+c+d, a+b+d, a+b+c]
			X_add = tiled_sum - input_non_mix

			output = tf.reshape(input_mix - X_add, [self.B*self.S, self.T_max_pooled, self.N, 1])
			self.ou = output
			return output, argmax_in

		return self.sepNet.output, argmax_in

	@scope
	def back(self):
		# Back-End
		input_tensor, argmax = self.separator

		# Unpooling (the previous max pooling)
		output_shape = [self.B*self.S, self.T, self.N, 1]
		self.unpooled = unpool(input_tensor, argmax, ksize=[1, self.max_pool_value, 1, 1], output_shape= output_shape, scope='unpool')

		output = tf.reshape(self.unpooled, [self.B*self.S, 1, self.T, self.N])

		self.unpool_board = tf.reshape(self.unpooled, [self.B*self.S, self.T, self.N, 1])

		# self.window_filter_2 = get_scope_variable('window_2', 'w_2', shape=[self.window], initializer=tf.contrib.layers.xavier_initializer_conv2d())
		# self.bases_2 = get_scope_variable('bases_2', 'bases_2', shape=[self.window, self.N], initializer=tf.contrib.layers.xavier_initializer_conv2d())
		# self.conv_filter_2 = tf.reshape(tf.expand_dims(self.window_filter_2,1)*self.bases_2 , [1, self.window, 1, self.N])
		self.conv_filter_2 = tf.Variable(self.conv_filter.initialized_value(), name="filters_back")

		output = tf.nn.conv2d_transpose(output , filter=self.conv_filter_2,
									 output_shape=[self.B*self.S, 1, self.L, 1],
									 strides=[1, 1, 1, 1], padding='SAME')

		output = tf.reshape(output, [self.B, self.S, self.L], name='back_output')

		return output


	def logfunc(self, x, x2):
		cx = tf.clip_by_value(x, 1e-10, 1.0)
		cx2 = tf.clip_by_value(x2, 1e-10, 1.0)
		return tf.multiply(x, tf.log(tf.div(cx,cx2)))


	def kl_div(self, p, p_hat):
		inv_p = 1 - p
		inv_p_hat = 1 - p_hat 
		return self.logfunc(p, p_hat) + self.logfunc(inv_p, inv_p_hat)


	@scope
	def cost(self):
		# Definition of cost for Adapt model
		# Regularisation
		# shape = [B_tot, T, N]
		self.sparse_reg = self.beta * self.latent_loss
		
		self.reg = self.l * (tf.nn.l2_loss(self.conv_filter_2)+ tf.nn.l2_loss(self.conv_filter))
		
		# input_shape = [B, S, L]
		# Doing l2 norm on L axis : 
		if self.pretraining:
			print self.X_non_mix
			print self.back
			# self.mse = log10(tf.reduce_sum(tf.square(self.back), axis=-1)/tf.square(tf.reduce_sum(self.back*self.X_non_mix, axis=-1)))
			self.mse = tf.reduce_sum(tf.square(self.X_non_mix - self.back), axis=-1)
			# self.sdr = - tf.reduce_mean(self.back*self.X_non_mix, -1)**2/tf.reduce_mean(tf.square(self.back), -1) 
			# self.sdr = tf.reduce_mean(self.sdr, -1)

		else:
			# Compute loss over all possible permutations
			
			perms = list(permutations(range(self.S))) # ex with 3: [0, 1, 2], [0, 2 ,1], [1, 0, 2], [1, 2, 0], [2, 1, 0], [2, 0, 1]
			length_perm = len(perms)
			perms = tf.reshape(tf.constant(perms), [1, length_perm, self.S, 1])
			perms = tf.tile(perms, [self.B, 1, 1, 1])

			batch_range = tf.tile(tf.reshape(tf.range(self.B, dtype=tf.int32), shape=[self.B, 1, 1, 1]), [1, length_perm, self.S, 1])
			perm_range = tf.tile(tf.reshape(tf.range(length_perm, dtype=tf.int32), shape=[1, length_perm, 1, 1]), [self.B, 1, self.S, 1])
			indicies = tf.concat([batch_range, perm_range, perms], axis=3)

			# [B, P, S, L]
			permuted_back = tf.gather_nd(tf.tile(tf.reshape(self.back, [self.B, 1, self.S, self.L]), [1, length_perm, 1, 1]), indicies) # 

			X_nmr = tf.reshape(self.X_non_mix, [self.B, 1, self.S, self.L])

			cost = tf.reduce_sum(tf.pow(X_nmr - permuted_back, 2), axis=3) / tf.cast(self.L, tf.float32)
			cost = tf.reduce_sum(cost, axis = 2) # Take the mean among speakers
			cost = tf.reduce_min(cost, axis = 1) # Take the permutation minimizing the cost
		
		# shape = [B]
		# Compute mean over batches
		self.SDR  = tf.reduce_sum(self.mse, -1) #+  0.5*tf.reduce_mean(self.sdr) 
		self.SDR  = tf.reduce_mean(self.SDR, -1)
		self.cost = self.SDR + self.sparse_reg + self.reg + self.overlap_coef*self.overlapping

		return self.cost

	def select_optimizer(self,string):
		return {
			'Adam': tf.train.AdamOptimizer,
			'RMS': tf.train.RMSPropOptimizer,
		}[string]

	@scope
	def optimize(self):
		if hasattr(self, 'trainable_variables') == False:
			self.trainable_variables = tf.global_variables()
			print 'ALL VARIABLE TRAINED'	
		print self.trainable_variables

		optimizer = AMSGrad(self.learning_rate, epsilon=0.001)
		gradients, variables = zip(*optimizer.compute_gradients(self.cost, var_list=self.trainable_variables))
		optimize = optimizer.apply_gradients(zip(gradients, variables))
		return optimize

	def sdr_improvement(self):
		# B S L
		s_target = self.X_non_mix 
		s = self.back
		mix = tf.tile(tf.expand_dims(self.X_mix, 1) ,[1, self.S, 1])

		s_target_norm = tf.reduce_sum(tf.square(s_target), axis=-1)
		s_approx_norm = tf.reduce_sum(tf.square(s), axis=-1)
		mix_norm = tf.reduce_sum(tf.square(mix), axis=-1)

		s_target_s_2 = tf.square(tf.reduce_sum(s_target*s, axis=-1))
		s_target_mix_2 = tf.square(tf.reduce_sum(s_target*mix, axis=-1))

		separated = 10. * log10(1.0/((s_target_norm*s_approx_norm)/s_target_s_2 - 1.0))
		non_separated = 10. * log10(1.0/((s_target_norm*mix_norm)/s_target_mix_2 - 1.0))

		val = separated - non_separated
		val = tf.reduce_mean(val , -1) # Mean over speakers
		val = tf.reduce_mean(val , -1) # Mean over batches

		return val

	def save(self, step):
		path = os.path.join(config.log_dir, self.folder ,self.runID, "model")
		self.saver.save(self.sess, path, step)
		return path

	def save_centroids(self, step):
		self.centroids_saver.save(self.sess, os.path.join(config.log_dir, self.folder ,self.runID, "centroids"), global_step=step)

	def train(self, X_mix, X_non_mix, learning_rate, step, I=None):
		if I is None:
			summary, _, cost = self.sess.run([self.merged, self.optimize, self.cost], {self.X_mix: X_mix, self.X_non_mix:X_non_mix, self.training:True, self.learning_rate:learning_rate})
		else:
			summary, _, cost = self.sess.run([self.merged, self.optimize, self.cost], {self.X_mix: X_mix, self.X_non_mix:X_non_mix, self.training:True, self.Ind:I, self.learning_rate:learning_rate})

		self.train_writer.add_summary(summary, step)
		return cost

	def valid_batch(self, X_mix_valid, X_non_mix_valid, I=None):
		if I is None:
			return self.sess.run(self.cost, {self.X_non_mix:X_non_mix_valid, self.X_mix:X_mix_valid,self.training:False})
		return self.sess.run(self.cost, {self.X_non_mix:X_non_mix_valid, self.X_mix:X_mix_valid,self.training:False, self.Ind:I})

	def add_valid_summary(self, val, step):
		summary = tf.Summary()
		summary.value.add(tag="Valid Cost", simple_value=val)
		self.valid_writer.add_summary(summary, step)

	def test_prediction(self, X_mix_test, X_non_mix_test, step):
		pred, y = self.sess.run([self.sepNet.prediction, self.sepNet.y_test_export], {self.X_mix: X_mix_test, self.X_non_mix:X_non_mix_test, self.training:True})
		pred = np.reshape(pred, [X_mix_test.shape[0], -1, 40])
		labels = [['r' if b == 1 else 'b' for b in batch]for batch in y]
		np.save(os.path.join(config.log_dir, self.folder ,self.runID, "bins-{}".format(step)), pred)
		np.save(os.path.join(config.log_dir, self.folder ,self.runID, "labels-{}".format(step)), labels)

	def connect_front(self, separator_class):
		self.sepNet = separator_class(self, **self.args)

	def connect_back(self):
		self.back

	def freeze_front(self):
		training_var = tf.trainable_variables()
		training_var.remove(self.conv_filter)
		self.trainable_variables = training_var

	def freeze_back(self):
		self.trainable_variables.remove(self.bases_2)
		self.trainable_variables.remove(self.window_filter_2)

	# TODO
	def freeze_variables(self):
		training_var = tf.trainable_variables()
		to_train = []
		for var in training_var:
			if 'enhance' in var.name:
				to_train.append(var)
		self.trainable_variables = to_train

	@staticmethod
	def load(path, modified_args):
		# Load parameters used for the desired model to load
		params_path = os.path.join(path, 'params')
		with open(params_path) as f:
			args = json.load(f)
		# Update with new args such as 'pretraining' or 'type'
		args.update(modified_args)

		# Create a new Adapt model with these parameters
		return Adapt(**args)
