# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
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

"""This file contains code to build and run the tensorflow graph for the sequence-to-sequence model"""

import os
import time
import numpy as np
import tensorflow as tf
from attention_decoder import attention_decoder
from tensorflow.contrib.tensorboard.plugins import projector
from tensorflow.python.util import nest
from tensorflow.python.ops import rnn_cell_impl as rnc

_state_size_with_prefix = rnc._state_size_with_prefix  # will need a workaround with higher versions

FLAGS = tf.app.flags.FLAGS


def get_initial_cell_state(cell, initializer, batch_size, dtype):
	"""Return state tensor(s), initialized with initializer.
  Args:
	cell: RNNCell.
	batch_size: int, float, or unit Tensor representing the batch size.
	initializer: function with two arguments, shape and dtype, that
		determines how the state is initialized.
	dtype: the data type to use for the state.
  Returns:
	If `state_size` is an int or TensorShape, then the return value is a
	`N-D` tensor of shape `[batch_size x state_size]` initialized
	according to the initializer.
	If `state_size` is a nested list or tuple, then the return value is
	a nested list or tuple (of the same structure) of `2-D` tensors with
  the shapes `[batch_size x s]` for each s in `state_size`.
  Snippet from : https://r2rt.com/non-zero-initial-states-for-recurrent-neural-networks.html
  """
	state_size = cell.state_size  # starting state. returns the size of individual states
	if nest.is_sequence(state_size):
		state_size_flat = nest.flatten(state_size)
		init_state_flat = [  # this part for multi-layered RNN
			initializer(_state_size_with_prefix(s), batch_size, dtype, i)
			for i, s in enumerate(state_size_flat)]
		init_state = nest.pack_sequence_as(structure=state_size,
										   flat_sequence=init_state_flat)
	else:
		init_state_size = _state_size_with_prefix(state_size)
		init_state = initializer(init_state_size, batch_size, dtype, None)

	return init_state


def make_variable_state_initializer(**kwargs):
	def variable_state_initializer(shape, batch_size, dtype, index):
		"""
	shape : shape of the cell of the RNNCell
	batch_size : int, float, or unit Tensor representing the batch size.
	dtype: the data type to use for the state. Typically float32
	index : not sure 

	"""
		args = kwargs.copy()

		if args.get('name'):
			args['name'] = args['name'] + '_' + str(index)  # naming the variable ?
		else:
			args['name'] = 'init_state_' + str(index)

		args['shape'] = shape
		args['dtype'] = dtype

		var = tf.get_variable(**args)  # name, shape, dtype
		var = tf.expand_dims(var, 0)  # 1 * shape
		var = tf.tile(var, tf.stack([batch_size] + [1] * len(shape)))
		var.set_shape(_state_size_with_prefix(shape, prefix=[None]))
		return var

	return variable_state_initializer


class SummarizationModel(object):
	"""A class to represent a sequence-to-sequence model for text summarization. Supports both baseline mode, pointer-generator mode, and coverage"""

	def __init__(self, hps, vocab):
		self._hps = hps
		self._vocab = vocab
		self.use_glove = hps.use_glove
		if hps.mode=='train':
			if hps.use_glove:
				self._vocab.set_glove_embedding(hps.glove_path,hps.emb_dim)
		
		if hps.use_regularizer:
			self.beta_l2 = hps.beta_l2
		else:
			self.beta_l2 = 0.0

		self._regularizer = tf.contrib.layers.l2_regularizer(scale=self.beta_l2)
			 

		


	def _add_placeholders(self):
		"""Add placeholders to the graph. These are entry points for any input data."""
		hps = self._hps
	   
		# encoder part
		self._enc_batch = tf.placeholder(tf.int32, [hps.batch_size, None], name='enc_batch')
		self._enc_lens = tf.placeholder(tf.int32, [hps.batch_size], name='enc_lens')
		self._enc_padding_mask = tf.placeholder(tf.float32, [hps.batch_size, None], name='enc_padding_mask')
		
		if FLAGS.query_encoder:
			self._query_batch = tf.placeholder(tf.int32, [hps.batch_size, None], name='query_batch')
			self._query_lens = tf.placeholder(tf.int32, [hps.batch_size], name='query_lens')
			self._query_padding_mask = tf.placeholder(tf.float32, [hps.batch_size, None], name='query_padding_mask')

		if FLAGS.pointer_gen:
			self._enc_batch_extend_vocab = tf.placeholder(tf.int32, [hps.batch_size, None],
														  name='enc_batch_extend_vocab')
			self._max_art_oovs = tf.placeholder(tf.int32, [], name='max_art_oovs')

		if FLAGS.word_gcn:
			# tf.logging.info(hps.num_word_dependency_labels)
			self._word_adj_in = [
				{lbl: tf.sparse_placeholder(tf.float32, shape=[None, None], name='word_adj_in_{}'.format(lbl)) for lbl
				 in range(hps.num_word_dependency_labels)} for _ in range(hps.batch_size)]
			self._word_adj_out = [
				{lbl: tf.sparse_placeholder(tf.float32, shape=[None, None], name='word_adj_out_{}'.format(lbl)) for lbl
				 in range(hps.num_word_dependency_labels)} for _ in range(hps.batch_size)]
			if hps.mode == 'train':
				self._word_gcn_dropout = tf.placeholder_with_default(hps.word_gcn_dropout, shape=(), name='dropout')
			else:
				self._word_gcn_dropout = tf.placeholder_with_default(1.0, shape=(), name='dropout')

			self._max_word_seq_len = tf.placeholder(tf.int32, shape=(), name='max_word_seq_len')
			self._word_neighbour_count = tf.placeholder(tf.float32, [hps.batch_size, None], name='word_neighbour_count')

		if FLAGS.query_gcn:
			self._query_adj_in = [
				{lbl: tf.sparse_placeholder(tf.float32, shape=[None, None], name='query_adj_in_{}'.format(lbl)) for lbl
				 in range(hps.num_word_dependency_labels)} for _ in range(hps.batch_size)]
			self._query_adj_out = [
				{lbl: tf.sparse_placeholder(tf.float32, shape=[None, None], name='query_adj_out_{}'.format(lbl)) for lbl
				 in range(hps.num_word_dependency_labels)} for _ in range(hps.batch_size)]
			if hps.mode == 'train':
				self._query_gcn_dropout = tf.placeholder_with_default(hps.query_gcn_dropout, shape=(), name='query_dropout')
			else:
				self._query_gcn_dropout = tf.placeholder_with_default(1.0, shape=(), name='query_dropout')

			self._max_query_seq_len = tf.placeholder(tf.int32, shape=(), name='max_query_seq_len')
			self._query_neighbour_count = tf.placeholder(tf.float32, [hps.batch_size, None], name='query_neighbour_count')

		# decoder part
		self._dec_batch = tf.placeholder(tf.int32, [hps.batch_size, hps.max_dec_steps], name='dec_batch')
		self._target_batch = tf.placeholder(tf.int32, [hps.batch_size, hps.max_dec_steps], name='target_batch')
		self._dec_padding_mask = tf.placeholder(tf.float32, [hps.batch_size, hps.max_dec_steps],
												name='dec_padding_mask')

		if hps.mode == "decode" and hps.coverage:
			self.prev_coverage = tf.placeholder(tf.float32, [hps.batch_size, None], name='prev_coverage')

	def _make_feed_dict(self, batch, just_enc=False):
		"""Make a feed dictionary mapping parts of the batch to the appropriate placeholders.

		Args:
		  batch: Batch object
		  just_enc: Boolean. If True, only feed the parts needed for the encoder.
		"""
		hps = self._hps
		feed_dict = {}
		feed_dict[self._enc_batch] = batch.enc_batch
		feed_dict[self._enc_lens] = batch.enc_lens
		feed_dict[self._enc_padding_mask] = batch.enc_padding_mask
		
		if FLAGS.query_encoder:
			feed_dict[self._query_batch] = batch.query_batch
			feed_dict[self._query_lens] = batch.query_lens
			feed_dict[self._query_padding_mask] = batch.query_padding_mask

		if FLAGS.pointer_gen:
			feed_dict[self._enc_batch_extend_vocab] = batch.enc_batch_extend_vocab
			feed_dict[self._max_art_oovs] = batch.max_art_oovs

		if FLAGS.word_gcn:
			feed_dict[self._max_word_seq_len] = batch.max_word_len
			feed_dict[self._word_neighbour_count] = batch.word_neighbour_count
			word_adj_in = batch.word_adj_in
			word_adj_out = batch.word_adj_out
			for i in range(hps.batch_size):
				for lbl in range(hps.num_word_dependency_labels):
					feed_dict[self._word_adj_in[i][lbl]] = tf.SparseTensorValue(
						indices=np.array([word_adj_in[i][lbl].row, word_adj_in[i][lbl].col]).T,
						values=word_adj_in[i][lbl].data,
						dense_shape=word_adj_in[i][lbl].shape)

					feed_dict[self._word_adj_out[i][lbl]] = tf.SparseTensorValue(
						indices=np.array([word_adj_out[i][lbl].row, word_adj_out[i][lbl].col]).T,
						values=word_adj_out[i][lbl].data,
						dense_shape=word_adj_out[i][lbl].shape)
   
		if FLAGS.query_gcn:
			feed_dict[self._max_query_seq_len] = batch.max_query_len
			feed_dict[self._query_neighbour_count] = batch.query_neighbour_count
			query_adj_in = batch.query_adj_in
			query_adj_out = batch.query_adj_out
			for i in range(hps.batch_size):
				for lbl in range(hps.num_word_dependency_labels):
					feed_dict[self._query_adj_in[i][lbl]] = tf.SparseTensorValue(
						indices=np.array([query_adj_in[i][lbl].row, query_adj_in[i][lbl].col]).T,
						values=query_adj_in[i][lbl].data,
						dense_shape=query_adj_in[i][lbl].shape)

					feed_dict[self._query_adj_out[i][lbl]] = tf.SparseTensorValue(
						indices=np.array([query_adj_out[i][lbl].row, query_adj_out[i][lbl].col]).T,
						values=query_adj_out[i][lbl].data,
						dense_shape=query_adj_out[i][lbl].shape)
	  
		if not just_enc:
			feed_dict[self._dec_batch] = batch.dec_batch
			feed_dict[self._target_batch] = batch.target_batch
			feed_dict[self._dec_padding_mask] = batch.dec_padding_mask

		return feed_dict


	def _add_encoder(self, encoder_inputs, seq_len,name='encoder'):
		"""Add a single-layer bidirectional LSTM encoder to the graph.

		Args:
		  encoder_inputs: A tensor of shape [batch_size, <=max_enc_steps, emb_size].
		  seq_len: Lengths of encoder_inputs (before padding). A tensor of shape [batch_size].

		Returns:
		  encoder_outputs:
			A tensor of shape [batch_size, <=max_enc_steps, 2*hidden_dim]. It's 2*hidden_dim because it's the concatenation of the forwards and backwards states.
		  fw_state, bw_state:
			Each are LSTMStateTuples of shape ([batch_size,hidden_dim],[batch_size,hidden_dim])
		"""
		with tf.variable_scope(name):
			if self._hps.use_lstm:
				cell_fw = tf.contrib.rnn.LSTMCell(self._hps.hidden_dim, initializer=tf.contrib.layers.xavier_initializer(),
											  state_is_tuple=True)
				cell_bw = tf.contrib.rnn.LSTMCell(self._hps.hidden_dim, initializer=tf.contrib.layers.xavier_initializer(),
											  state_is_tuple=True)
			else:
				cell_fw = tf.contrib.rnn.BasicRNNCell(self._hps.hidden_dim)
				cell_bw = tf.contrib.rnn.BasicRNNCell(self._hps.hidden_dim)

			(encoder_outputs, (fw_st, bw_st)) = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, encoder_inputs,
																				dtype=tf.float32,
																				sequence_length=seq_len,
																				swap_memory=True)
			encoder_outputs = tf.concat(axis=2, values=encoder_outputs)  # concatenate the forwards and backwards states
		return encoder_outputs, fw_st, bw_st



	def _add_gcn_layer(self, gcn_in, in_dim, gcn_dim, batch_size, max_nodes, max_labels, adj_in, adj_out, neighbour_count, num_layers=1,
					   use_gating=False, use_skip=True, use_normalization=True, dropout=1.0, name="GCN", use_label_information=False): #output becomes gcn_in ka dimension 

		out = []
		true_input = gcn_in
		true_in_dim = in_dim
		out.append(gcn_in)
		#    return tf.nn.relu(tf.zeros([batch_size, max_nodes, gcn_dim]))
		if not self._hps.use_label_information:
			max_labels = 1
		for layer in range(num_layers):
			gcn_in = out[-1]  # out contains the output of all the GCN layers, intitally contains input to first GCN Layer
			if len(out) > 1: in_dim = gcn_dim  # After first iteration the in_dim = gcn_dim
			with tf.variable_scope('%s-%d' % (name, layer)):

				act_sum = tf.zeros([batch_size, max_nodes, gcn_dim])

				w_in = tf.get_variable('w_in', [in_dim, gcn_dim], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)                           
				w_out = tf.get_variable('w_out', [in_dim, gcn_dim], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
				w_loop = tf.get_variable('w_loop', [in_dim, gcn_dim],  initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
				b_layer = tf.get_variable('b_layer', [1], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer) #gating for the highway networks


				b_out = tf.get_variable('b_out', [1, gcn_dim], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)
			 
				# for code optimisation only
				pre_com_o_in = tf.tensordot(gcn_in, w_in, axes=[[2], [0]])
				pre_com_o_out = tf.tensordot(gcn_in, w_out, axes=[[2], [0]])
				pre_com_o_loop = tf.tensordot(gcn_in, w_loop, axes=[[2], [0]])

				if use_gating:
					w_gin = tf.get_variable('w_gin', [in_dim, 1], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
					w_gout = tf.get_variable('w_gout', [in_dim, 1], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
					w_gloop = tf.get_variable('w_gloop', [in_dim, 1], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
											  

					# for code optimisation only
					pre_com_o_gin = tf.tensordot(gcn_in, w_gin, axes=[[2], [0]])
					pre_com_o_gout = tf.tensordot(gcn_in, w_gout, axes=[[2], [0]])
					pre_com_o_gloop = tf.tensordot(gcn_in, w_gloop, axes=[[2], [0]])
				

				for lbl in range(max_labels):

					with tf.variable_scope('label-%d_name-%s_layer-%d' % (lbl, name, layer)) as scope:
						if use_gating:
							b_common = tf.get_variable('b_gout', [1], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)

						
						
						if use_gating:
							b_gout = tf.get_variable('b_gout', [1], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)
							b_gin = tf.get_variable('b_gin', [1], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)
							b_gloop = tf.get_variable('b_gloop',[1], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)
						
						if use_skip:
							w_skip = tf.get_variable('w_skip', [gcn_dim,true_in_dim], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
						
					with tf.name_scope('in_arcs-%s_name-%s_layer-%d' % (lbl, name, layer)):
						inp_in = pre_com_o_in  
						in_t = tf.stack(
							[tf.sparse_tensor_dense_matmul(adj_in[i][lbl], inp_in[i]) for i in range(batch_size)])
						if dropout != 1.0: in_t = tf.nn.dropout(in_t, keep_prob=dropout)

						
						if use_gating:
							inp_gin = pre_com_o_gin + tf.expand_dims(b_gin, axis=0)
							in_gate = tf.stack(
								[tf.sparse_tensor_dense_matmul(adj_in[i][lbl], inp_gin[i]) for i in range(batch_size)])
							in_gsig = tf.sigmoid(in_gate)
							in_act = in_t * in_gsig
						else:
							in_act = in_t
						
						in_act = in_t

					with tf.name_scope('out_arcs-%s_name-%s_layer-%d' % (lbl, name, layer)):
						inp_out = pre_com_o_out 
						out_t = tf.stack(
							[tf.sparse_tensor_dense_matmul(adj_out[i][lbl], inp_out[i]) for i in range(batch_size)])
						if dropout != 1.0: out_t = tf.nn.dropout(out_t, keep_prob=dropout)
					
						if use_gating:
							inp_gout = pre_com_o_gout + tf.expand_dims(b_gout, axis=0)
							out_gate = tf.stack([tf.sparse_tensor_dense_matmul(adj_out[i][lbl], inp_gout[i]) for i in
												 range(batch_size)])
							out_gsig = tf.sigmoid(out_gate)
							out_act = out_t * out_gsig
						else:
							out_act = out_t
					
						out_act = out_t

					act_sum += in_act + out_act 

				
				with tf.name_scope('self_loop'):
					inp_loop = pre_com_o_loop
					if dropout != 1.0: inp_loop = tf.nn.dropout(inp_loop, keep_prob=dropout)
					
					if use_gating:
						inp_gloop = pre_com_o_gloop + tf.expand_dims(b_gloop, axis=0)
						loop_gsig = tf.sigmoid(inp_gloop)
						loop_act = inp_loop * loop_gsig
					else:
						loop_act = inp_loop
					

				act_sum += loop_act
				act_sum = act_sum + tf.expand_dims(b_out,axis=0) 
				
				if use_normalization:

					neighbour_count_ = tf.expand_dims(neighbour_count,-1)
					act_sum = act_sum/neighbour_count_ 
				

				gcn_out = tf.nn.relu(act_sum)
				
				if use_skip: #residue is called skip
					
 					if in_dim!= gcn_dim:
						w_adjust = tf.get_variable('w_adjust', [in_dim, gcn_dim], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
						gcn_in = tf.tensordot(gcn_in, w_adjust,axes=[[2],[0]])
						tf.logging.info('Input transformed for residual upadate')		
	
					gcn_out = b_layer * gcn_in + (1.0 - b_layer) * gcn_out # weighted residual connection
				
				out.append(gcn_out)

		return gcn_out #batch_size * max_enc_len * gcn_dim

	def _reduce_states(self, fw_st, bw_st):
		"""Add to the graph a linear layer to reduce the encoder's final FW and BW state into a single initial state for the decoder. This is needed because the encoder is bidirectional but the decoder is not.

	Args:
	  fw_st: LSTMStateTuple with hidden_dim units.
	  bw_st: LSTMStateTuple with hidden_dim units.

	Returns:
	  state: LSTMStateTuple with hidden_dim units.
	"""
		hidden_dim = self._hps.hidden_dim
		with tf.variable_scope('reduce_final_st'):
			if self._hps.use_lstm:
			# Define weights and biases to reduce the cell and reduce the state
				w_reduce_c = tf.get_variable('w_reduce_c', [hidden_dim * 2, hidden_dim], dtype=tf.float32,
										 initializer=self.rand_unif_init, regularizer=self._regularizer)
				bias_reduce_c = tf.get_variable('bias_reduce_c', [hidden_dim], dtype=tf.float32,
											initializer=self.rand_unif_init, regularizer=self._regularizer)
			
			bias_reduce_h = tf.get_variable('bias_reduce_h', [hidden_dim], dtype=tf.float32,
											initializer=self.rand_unif_init, regularizer=self._regularizer)
			
			w_reduce_h = tf.get_variable('w_reduce_h', [hidden_dim * 2, hidden_dim], dtype=tf.float32,
										 initializer=self.rand_unif_init, regularizer=self._regularizer)


			# Apply linear layer
			if self._hps.use_lstm:
				old_c = tf.concat(axis=1, values=[fw_st.c, bw_st.c])  # Concatenation of fw and bw cell
				new_c = tf.nn.relu(tf.matmul(old_c, w_reduce_c) + bias_reduce_c)  # Get new cell from old cell
			
				old_h = tf.concat(axis=1, values=[fw_st.h, bw_st.h])  # Concatenation of fw and bw state
				new_h = tf.nn.relu(tf.matmul(old_h, w_reduce_h) + bias_reduce_h)  # Get new state from old state
			else:
				old_h = tf.concat(axis=1, values=[fw_st, bw_st])  # Concatenation of fw and bw state
				new_h = tf.nn.relu(tf.matmul(old_h, w_reduce_h) + bias_reduce_h)  # Get new state from old state


			if self._hps.use_lstm:
				return tf.contrib.rnn.LSTMStateTuple(new_c, new_h)  # Return new cell and state
			else:
				return new_h

	def _add_decoder(self, inputs):
		"""Add attention decoder to the graph. In train or eval mode, you call this once to get output on ALL steps. In decode (beam search) mode, you call this once for EACH decoder step.

	Args:
	  inputs: inputs to the decoder (word embeddings). A list of tensors shape (batch_size, emb_dim)

	Returns:
	  outputs: List of tensors; the outputs of the decoder
	  out_state: The final state of the decoder
	  attn_dists: A list of tensors; the attention distributions
	  p_gens: A list of tensors shape (batch_size, 1); the generation probabilities
	  coverage: A tensor, the current coverage vector
	"""
		hps = self._hps
		if hps.use_lstm:
			cell = tf.contrib.rnn.LSTMCell(hps.hidden_dim, state_is_tuple=True, initializer=self.rand_unif_init)
		else:
			cell = tf.contrib.rnn.BasicRNNCell(hps.hidden_dim)

	
		if hps.no_lstm_encoder:
			self._dec_in_state = get_initial_cell_state(cell, make_variable_state_initializer(), hps.batch_size,
														tf.float32)
			# TODO Feed the averaged gcn word vectors

		prev_coverage = self.prev_coverage if hps.mode == "decode" and hps.coverage else None  # In decode mode, we run attention_decoder one step at a time and so need to pass in the previous step's coverage vector each time

		if hps.query_encoder:
		  outputs, out_state, attn_dists, p_gens, coverage = attention_decoder(inputs, self._dec_in_state,
																			 self._enc_states, self._enc_padding_mask,
																			 cell, use_query=True, query_states=self._query_states, query_padding_mask=self._query_padding_mask,
																			 initial_state_attention=( hps.mode == "decode"),use_lstm= hps.use_lstm, pointer_gen=hps.pointer_gen, use_coverage=hps.coverage,
																			 prev_coverage=prev_coverage)
		else:
		  outputs, out_state, attn_dists, p_gens, coverage = attention_decoder(inputs, self._dec_in_state,
																			 self._enc_states, self._enc_padding_mask,
																			 cell, initial_state_attention=( hps.mode == "decode"),  use_lstm=hps.use_lstm,
																			 pointer_gen=hps.pointer_gen, use_coverage=hps.coverage,
																			 prev_coverage=prev_coverage)

		return outputs, out_state, attn_dists, p_gens, coverage

	def _calc_final_dist(self, vocab_dists, attn_dists):
		"""Calculate the final distribution, for the pointer-generator model

	Args:
	  vocab_dists: The vocabulary distributions. List length max_dec_steps of (batch_size, vsize) arrays. The words are in the order they appear in the vocabulary file.
	  attn_dists: The attention distributions. List length max_dec_steps of (batch_size, attn_len) arrays

	Returns:
	  final_dists: The final distributions. List length max_dec_steps of (batch_size, extended_vsize) arrays.
	"""
		with tf.variable_scope('final_distribution'):
			# Multiply vocab dists by p_gen and attention dists by (1-p_gen)
			vocab_dists = [p_gen * dist for (p_gen, dist) in zip(self.p_gens, vocab_dists)]
			attn_dists = [(1 - p_gen) * dist for (p_gen, dist) in zip(self.p_gens, attn_dists)]

			# Concatenate some zeros to each vocabulary dist, to hold the probabilities for in-article OOV words
			extended_vsize = self._vocab.size() + self._max_art_oovs  # the maximum (over the batch) size of the extended vocabulary
			extra_zeros = tf.zeros((self._hps.batch_size, self._max_art_oovs))
			vocab_dists_extended = [tf.concat(axis=1, values=[dist, extra_zeros]) for dist in
									vocab_dists]  # list length max_dec_steps of shape (batch_size, extended_vsize)

			# Project the values in the attention distributions onto the appropriate entries in the final distributions
			# This means that if a_i = 0.1 and the ith encoder word is w, and w has index 500 in the vocabulary, then we add 0.1 onto the 500th entry of the final distribution
			# This is done for each decoder timestep.
			# This is fiddly; we use tf.scatter_nd to do the projection
			batch_nums = tf.range(0, limit=self._hps.batch_size)  # shape (batch_size)
			batch_nums = tf.expand_dims(batch_nums, 1)  # shape (batch_size, 1)
			attn_len = tf.shape(self._enc_batch_extend_vocab)[1]  # number of states we attend over
			batch_nums = tf.tile(batch_nums, [1, attn_len])  # shape (batch_size, attn_len)
			indices = tf.stack((batch_nums, self._enc_batch_extend_vocab), axis=2)  # shape (batch_size, enc_t, 2)
			shape = [self._hps.batch_size, extended_vsize]
			attn_dists_projected = [tf.scatter_nd(indices, copy_dist, shape) for copy_dist in
									attn_dists]  # list length max_dec_steps (batch_size, extended_vsize)

			# Add the vocab distributions and the copy distributions together to get the final distributions
			# final_dists is a list length max_dec_steps; each entry is a tensor shape (batch_size, extended_vsize) giving the final distribution for that decoder timestep
			# Note that for decoder timesteps and examples corresponding to a [PAD] token, this is junk - ignore.
			final_dists = [vocab_dist + copy_dist for (vocab_dist, copy_dist) in
						   zip(vocab_dists_extended, attn_dists_projected)]

			return final_dists

	def _add_emb_vis(self, embedding_var):
		"""Do setup so that we can view word embedding visualization in Tensorboard, as described here:
	https://www.tensorflow.org/get_started/embedding_viz
	Make the vocab metadata file, then make the projector config file pointing to it."""
		train_dir = os.path.join(FLAGS.log_root, "train")
		vocab_metadata_path = os.path.join(train_dir, "vocab_metadata.tsv")
		self._vocab.write_metadata(vocab_metadata_path)  # write metadata file
		summary_writer = tf.summary.FileWriter(train_dir)
		config = projector.ProjectorConfig()
		embedding = config.embeddings.add()
		embedding.tensor_name = embedding_var.name
		embedding.metadata_path = vocab_metadata_path
		projector.visualize_embeddings(summary_writer, config)

	def _add_seq2seq(self):
		"""Add the whole sequence-to-sequence model to the graph."""
		hps = self._hps
		vsize = self._vocab.size()  # size of the vocabulary
		
		
		with tf.variable_scope('seq2seq'):
			# Some initializers
			self.rand_unif_init = tf.random_uniform_initializer(-hps.rand_unif_init_mag, hps.rand_unif_init_mag,
																seed=123)
			self.trunc_norm_init = tf.truncated_normal_initializer(stddev=hps.trunc_norm_init_std)


			# Add embedding matrix (shared by the encoder and decoder inputs)
			with tf.variable_scope('embedding'):
				if hps.mode == "train":
					if self.use_glove:
					  tf.logging.info('glove')
					  embedding = tf.get_variable('embedding', dtype=tf.float32, initializer=tf.cast(self._vocab.glove_emb,tf.float32),trainable=hps.emb_trainable, regularizer=self._regularizer)
					
					else:
					  embedding = tf.get_variable('embedding', [vsize, hps.emb_dim], dtype=tf.float32, initializer=self.trunc_norm_init, trainable=hps.emb_trainable, regularizer=self._regularizer)
				
				else:
					embedding = tf.get_variable('embedding', [vsize, hps.emb_dim], dtype=tf.float32)

				if hps.mode == "train": self._add_emb_vis(embedding)  # add to tensorboard
				emb_enc_inputs = tf.nn.embedding_lookup(embedding,
														self._enc_batch)  # tensor with shape (batch_size, max_enc_steps, emb_size)
				if hps.query_encoder:
				  emb_query_inputs = tf.nn.embedding_lookup(embedding, self._query_batch) # tensor with shape (batch_size, max_query_steps, emb_size)
				
				emb_dec_inputs = [tf.nn.embedding_lookup(embedding, x) for x in tf.unstack(self._dec_batch,
																						   axis=1)]  # list length max_dec_steps containing shape (batch_size, emb_size)
			if self._hps.concat_with_word_embedding: #intermediate concat
				w_word = tf.get_variable('w_word', [self._hps.hidden_dim * 2 + self._hps.emb_dim, self._hps.hidden_dim * 2], dtype=tf.float32, initializer=self.trunc_norm_init, trainable=True, regularizer=self._regularizer)		
				b_word = tf.get_variable('b_word', [1, self._hps.hidden_dim*2], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)
				

			if self._hps.no_lstm_encoder:  # use gcn directly
				self._enc_states = emb_enc_inputs
				in_dim = hps.emb_dim
				# Note self._dec_in_state is set inside the _add_decoder for this option
			else:
				# Add the encoder.
				enc_outputs, fw_st, bw_st = self._add_encoder(emb_enc_inputs, self._enc_lens)

				if self._hps.stacked_lstm: #lstm over lstm
					enc_outputs, fw_st, bw_st = self._add_encoder(enc_outputs, self._enc_lens,name='stacked_encoder')
	


				self._enc_states = enc_outputs
				in_dim = self._hps.hidden_dim * 2
				# Our encoder is bidirectional and our decoder is unidirectional so we need to reduce the final encoder hidden state to the right size to be the initial decoder hidden state
				self._dec_in_state = self._reduce_states(fw_st, bw_st)


			if self._hps.word_gcn:
				
				if self._hps.use_gcn_lstm_parallel:
					gcn_in = emb_enc_inputs
					in_dim = hps.emb_dim
				else:
					if self._hps.concat_with_word_embedding: #interm concat
						b_highway = tf.get_variable('b_highway', [1], initializer=tf.constant_initializer(0.0))

						if hps.emb_dim!= hps.hidden_dim * 2 :
							w_adjust = tf.get_variable('w_adjust', [hps.emb_dim, hps.hidden_dim*2], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
							emb_enc_inputs = tf.tensordot(emb_enc_inputs, w_adjust,axes=[[2],[0]])
							tf.logging.info('Input transformed for residual upadate')		
						
						gcn_in = b_highway * emb_enc_inputs + (1.0 - b_highway) * self._enc_states
						in_dim = self._hps.hidden_dim * 2
						
					else:
						gcn_in = self._enc_states
						if self._hps.no_lstm_encoder:
							in_dim = hps.emb_dim
						else:
							in_dim = self._hps.hidden_dim*2

				gcn_dim = hps.word_gcn_dim			
			
				gcn_outputs = self._add_gcn_layer(gcn_in=gcn_in, in_dim=in_dim, gcn_dim=hps.word_gcn_dim,
												  batch_size=hps.batch_size, max_nodes=self._max_word_seq_len,
												  max_labels=hps.num_word_dependency_labels, adj_in=self._word_adj_in,
												  adj_out=self._word_adj_out,neighbour_count=self._word_neighbour_count, 
												  num_layers=hps.word_gcn_layers,
												  use_gating=hps.word_gcn_gating, use_skip=hps.word_gcn_skip, dropout=self._word_gcn_dropout,
												  name="gcn_word")
				
				

				if self._hps.concat_gcn_lstm and self._hps.word_gcn: #upper concat
					b_upper_concat = tf.get_variable('b_upper_concat', [1], initializer=tf.constant_initializer(0.0))
					
					if hps.word_gcn_dim!= hps.hidden_dim * 2:
						w_adjust_upper_concat = tf.get_variable('w_adjust_upper_concat', [hps.word_gcn_dim, hps.hidden_dim*2], initializer=tf.contrib.layers.xavier_initializer(), regularizer=self._regularizer)
						gcn_outputs = tf.tensordot(gcn_outputs, w_adjust_upper_concat,axes=[[2],[0]])
					
					self._enc_states = b_upper_concat * enc_outputs + (1.0 - b_upper_concat) * gcn_outputs

				
				else:
					self._enc_states = gcn_outputs  # note we return the last output from the gcn directly instead of all the outputs outputs
	

			if self._hps.query_encoder:
				if self._hps.no_lstm_query_encoder:
					self._query_states = emb_query_inputs
					q_in_dim = hps.emb_dim
			  	else:    
					query_outputs, fw_st_q, bw_st_q = self._add_encoder(emb_query_inputs, self._query_lens,name='query_encoder')
					self._query_states = query_outputs
					q_in_dim = self._hps.hidden_dim * 2

			  	if self._hps.query_gcn:
			  		if self._hps.use_gcn_lstm_parallel:
			  			q_gcn_in = emb_query_inputs
			  			q_in_dim = hps.emb_dim
			  		else:
			  			q_gcn_in = self._query_states
			  			if self._hps.no_lstm_query_encoder:
			  				q_in_dim = hps.emb_dim
			  				
			  			else:
			  				q_in_dim = self._hps.hidden_dim * 2
					
					q_gcn_outputs = self._add_gcn_layer(gcn_in=q_gcn_in, in_dim=q_in_dim, gcn_dim=hps.query_gcn_dim,
													batch_size=hps.batch_size, max_nodes=self._max_query_seq_len,
													max_labels=hps.num_word_dependency_labels, adj_in=self._query_adj_in,
													adj_out=self._query_adj_out, neighbour_count=self._query_neighbour_count, 
													num_layers=hps.query_gcn_layers,
													use_gating=hps.query_gcn_gating, use_skip=hps.query_gcn_skip, dropout=self._query_gcn_dropout,
													name="gcn_query")
					
					if self._hps.concat_gcn_lstm and self._hps.query_gcn: #deprecated
						if self._hps.simple_concat:
							self._query_states = tf.concat(axis=2,values=[query_outputs,q_gcn_outputs])
						else:
							self._query_states = tf.nn.relu(tf.add(tf.multiply(q_gcn_outputs,q_gcn_word_w),tf.multiply(query_outputs,q_lstm_word_w)))
						
					else:
						self._query_states = q_gcn_outputs  # note we return the last output from the gcn directly instead of all the outputs outputs


			# Add the decoder.
			with tf.variable_scope('decoder'):
				decoder_outputs, self._dec_out_state, self.attn_dists, self.p_gens, self.coverage = self._add_decoder(
					emb_dec_inputs)

			# Add the output projection to obtain the vocabulary distribution
			with tf.variable_scope('output_projection'):
				w = tf.get_variable('w', [hps.hidden_dim, vsize], dtype=tf.float32, initializer=self.trunc_norm_init, regularizer=self._regularizer)
				w_t = tf.transpose(w)
				v = tf.get_variable('v', [vsize], dtype=tf.float32, initializer=self.trunc_norm_init, regularizer=self._regularizer)
				vocab_scores = []  # vocab_scores is the vocabulary distribution before applying softmax. Each entry on the list corresponds to one decoder step
				for i, output in enumerate(decoder_outputs):
					if i > 0:
						tf.get_variable_scope().reuse_variables()
					vocab_scores.append(tf.nn.xw_plus_b(output, w, v))  # apply the linear layer

				vocab_dists = [tf.nn.softmax(s) for s in
							   vocab_scores]  # The vocabulary distributions. List length max_dec_steps of (batch_size, vsize) arrays. The words are in the order they appear in the vocabulary file.

			# For pointer-generator model, calc final distribution from copy distribution and vocabulary distribution
			if FLAGS.pointer_gen:
				final_dists = self._calc_final_dist(vocab_dists, self.attn_dists)
			else:  # final distribution is just vocabulary distribution
				final_dists = vocab_dists

			if hps.mode in ['train', 'eval']:
				# Calculate the loss
				with tf.variable_scope('loss'):
					if FLAGS.pointer_gen:
						# Calculate the loss per step
						# This is fiddly; we use tf.gather_nd to pick out the probabilities of the gold target words
						loss_per_step = []  # will be list length max_dec_steps containing shape (batch_size)
						batch_nums = tf.range(0, limit=hps.batch_size)  # shape (batch_size)
						for dec_step, dist in enumerate(final_dists):
							targets = self._target_batch[:,
									  dec_step]  # The indices of the target words. shape (batch_size)
							indices = tf.stack((batch_nums, targets), axis=1)  # shape (batch_size, 2)
							gold_probs = tf.gather_nd(dist,
													  indices)  # shape (batch_size). prob of correct words on this step
							losses = -tf.log(gold_probs)
							loss_per_step.append(losses)

						# Apply dec_padding_mask and get loss
						self._loss = _mask_and_avg(loss_per_step, self._dec_padding_mask)

					else:  # baseline model
						self._loss = tf.contrib.seq2seq.sequence_loss(tf.stack(vocab_scores, axis=1),
																	  self._target_batch,
																	  self._dec_padding_mask)  # this applies softmax internally
					if hps.use_regularizer:	
						self._loss += tf.contrib.layers.apply_regularization(self._regularizer, tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))


					tf.summary.scalar('loss', self._loss)

					# Calculate coverage loss from the attention distributions
					if hps.coverage:
						with tf.variable_scope('coverage_loss'):
							self._coverage_loss = _coverage_loss(self.attn_dists, self._dec_padding_mask)
							tf.summary.scalar('coverage_loss', self._coverage_loss)
						self._total_loss = self._loss + hps.cov_loss_wt * self._coverage_loss
						tf.summary.scalar('total_loss', self._total_loss)

		if hps.mode == "decode":
			# We run decode beam search mode one decoder step at a time
			assert len(
				final_dists) == 1  # final_dists is a singleton list containing shape (batch_size, extended_vsize)
			final_dists = final_dists[0]
			topk_probs, self._topk_ids = tf.nn.top_k(final_dists,
													 hps.batch_size * 2)  # take the k largest probs. note batch_size=beam_size in decode mode
			self._topk_log_probs = tf.log(topk_probs)


	def _add_gcn_seq2seq(self):
		"""Add the whole sequence-to-sequence model to the graph."""
		hps = self._hps
		vsize = self._vocab.size()  # size of the vocabulary
		tf.logging.info('Called reverse')
	
		
		with tf.variable_scope('gcn_seq2seq'):
			# Some initializers
			self.rand_unif_init = tf.random_uniform_initializer(-hps.rand_unif_init_mag, hps.rand_unif_init_mag,
																seed=123)
			self.trunc_norm_init = tf.truncated_normal_initializer(stddev=hps.trunc_norm_init_std)

			# Add embedding matrix (shared by the encoder and decoder inputs)
			with tf.variable_scope('embedding'):
				if hps.mode == "train":
					if self.use_glove:
					  tf.logging.info('glove')
					  embedding = tf.get_variable('embedding', dtype=tf.float32, initializer=tf.cast(self._vocab.glove_emb,tf.float32),trainable=hps.emb_trainable, regularizer=self._regularizer)
					
					else:
					  embedding = tf.get_variable('embedding', [vsize, hps.emb_dim], dtype=tf.float32, initializer=self.trunc_norm_init, trainable=hps.emb_trainable, regularizer=self._regularizer)
				
				else:
					embedding = tf.get_variable('embedding', [vsize, hps.emb_dim], dtype=tf.float32)

				if hps.mode == "train": self._add_emb_vis(embedding)  # add to tensorboard
				emb_enc_inputs = tf.nn.embedding_lookup(embedding,
														self._enc_batch)  # tensor with shape (batch_size, max_enc_steps, emb_size)
				if hps.query_encoder:
				  emb_query_inputs = tf.nn.embedding_lookup(embedding, self._query_batch) # tensor with shape (batch_size, max_query_steps, emb_size)

				
				emb_dec_inputs = [tf.nn.embedding_lookup(embedding, x) for x in tf.unstack(self._dec_batch,
																						   axis=1)]  # list length max_dec_steps containing shape (batch_size, emb_size)
	
		
	
			gcn_in = emb_enc_inputs
			in_dim = hps.emb_dim

			gcn_dim = hps.word_gcn_dim			
			

			gcn_outputs = self._add_gcn_layer(gcn_in=gcn_in, in_dim=in_dim, gcn_dim=hps.word_gcn_dim,
												  batch_size=hps.batch_size, max_nodes=self._max_word_seq_len,
												  max_labels=hps.num_word_dependency_labels, adj_in=self._word_adj_in,
												  adj_out=self._word_adj_out,neighbour_count=self._word_neighbour_count, 
												  num_layers=hps.word_gcn_layers,
												  use_gating=hps.word_gcn_gating, use_skip=hps.word_gcn_skip, dropout=self._word_gcn_dropout,
												  name="gcn_word")
		

			if hps.concat_with_word_embedding: #
				interm_outputs_1 = tf.concat(axis=2,values=[emb_enc_inputs,gcn_outputs]) #gcn outputs are now in_dim
				w_word = tf.get_variable('w_word', [hps.word_gcn_dim + self._hps.emb_dim, hps.word_gcn_dim], dtype=tf.float32, initializer=self.trunc_norm_init, trainable=True, regularizer=self._regularizer)		
				b_word = tf.get_variable('b_word', [1, hps.word_gcn_dim], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)
				gcn_outputs = tf.nn.relu(tf.add(tf.tensordot(interm_outputs_1,w_word,axes=[[2],[0]]), b_word)) 
				#gcn_outputs = interm_outputs_1
			enc_outputs, fw_st, bw_st = self._add_encoder(gcn_outputs, self._enc_lens)

			if self._hps.concat_gcn_lstm:
				w_gcn_lstm = tf.get_variable('w_gcn_lstm', [hps.word_gcn_dim + hps.hidden_dim*2, hps.hidden_dim*2], dtype=tf.float32, initializer=self.trunc_norm_init, trainable=True, regularizer=self._regularizer)		
				b_gcn_lstm = tf.get_variable('b_gcn_lstm', [1, hps.hidden_dim*2], initializer=tf.constant_initializer(0.0), regularizer=self._regularizer)

			

			if self._hps.concat_gcn_lstm and self._hps.word_gcn:
				if self._hps.simple_concat:
					self._enc_states = tf.concat(axis=2,values=[enc_outputs,gcn_outputs])
				else:
					interm_outputs_2 = tf.concat(axis=2,values=[enc_outputs,gcn_outputs])
					self._enc_states = tf.nn.relu(tf.add(tf.tensordot(interm_outputs_2,w_gcn_lstm, axes=[[2],[0]]),b_gcn_lstm))
				
			else:
				self._enc_states = enc_outputs

			self._dec_in_state = self._reduce_states(fw_st, bw_st)


			if self._hps.query_encoder:
				q_gcn_in = emb_query_inputs
				q_in_dim = hps.emb_dim
				q_gcn_outputs = q_gcn_in
				
				
				
				if self._hps.query_gcn: #deprecated
					q_gcn_outputs = self._add_gcn_layer(gcn_in=q_gcn_in, in_dim=q_in_dim, gcn_dim=hps.query_gcn_dim,
													batch_size=hps.batch_size, max_nodes=self._max_query_seq_len,
													max_labels=hps.num_word_dependency_labels, adj_in=self._query_adj_in,
													adj_out=self._query_adj_out, neighbour_count=self._query_neighbour_count, 
													num_layers=hps.query_gcn_layers,
													use_gating=hps.query_gcn_gating,  use_skip=hps.query_gcn_skip, dropout=self._query_gcn_dropout,
													name="gcn_query")

					if hps.concat_with_word_embedding:
						q_gcn_outputs = tf.concat(axis=2,values=[emb_query_inputs,q_gcn_outputs])



				query_outputs, fw_st_q, bw_st_q = self._add_encoder(q_gcn_outputs, self._query_lens,name='query_encoder')


				if self._hps.concat_gcn_lstm and self._hps.query_gcn:
					if self._hps.simple_concat:
						self._query_states = tf.concat(axis=2,values=[q_gcn_outputs, query_outputs])
					else:
						self._query_states = tf.add(tf.multiply(q_gcn_outputs,q_gcn_word_w),tf.multiply(query_outputs,q_lstm_word_w))
					
				else:
					self._query_states = query_outputs	
						

			# Add the decoder.
			with tf.variable_scope('decoder'):
				decoder_outputs, self._dec_out_state, self.attn_dists, self.p_gens, self.coverage = self._add_decoder(
					emb_dec_inputs)

			# Add the output projection to obtain the vocabulary distribution
			with tf.variable_scope('output_projection'):
				w = tf.get_variable('w', [hps.hidden_dim, vsize], dtype=tf.float32, initializer=self.trunc_norm_init, regularizer=self._regularizer)
				w_t = tf.transpose(w)
				v = tf.get_variable('v', [vsize], dtype=tf.float32, initializer=self.trunc_norm_init, regularizer=self._regularizer)
				vocab_scores = []  # vocab_scores is the vocabulary distribution before applying softmax. Each entry on the list corresponds to one decoder step
				for i, output in enumerate(decoder_outputs):
					if i > 0:
						tf.get_variable_scope().reuse_variables()
					vocab_scores.append(tf.nn.xw_plus_b(output, w, v))  # apply the linear layer

				vocab_dists = [tf.nn.softmax(s) for s in
							   vocab_scores]  # The vocabulary distributions. List length max_dec_steps of (batch_size, vsize) arrays. The words are in the order they appear in the vocabulary file.

			# For pointer-generator model, calc final distribution from copy distribution and vocabulary distribution
			if FLAGS.pointer_gen:
				final_dists = self._calc_final_dist(vocab_dists, self.attn_dists)
			else:  # final distribution is just vocabulary distribution
				final_dists = vocab_dists

			if hps.mode in ['train', 'eval']:
				# Calculate the loss
				with tf.variable_scope('loss'):
					if FLAGS.pointer_gen:
						# Calculate the loss per step
						# This is fiddly; we use tf.gather_nd to pick out the probabilities of the gold target words
						loss_per_step = []  # will be list length max_dec_steps containing shape (batch_size)
						batch_nums = tf.range(0, limit=hps.batch_size)  # shape (batch_size)
						for dec_step, dist in enumerate(final_dists):
							targets = self._target_batch[:,
									  dec_step]  # The indices of the target words. shape (batch_size)
							indices = tf.stack((batch_nums, targets), axis=1)  # shape (batch_size, 2)
							gold_probs = tf.gather_nd(dist,
													  indices)  # shape (batch_size). prob of correct words on this step
							losses = -tf.log(gold_probs)
							loss_per_step.append(losses)

						# Apply dec_padding_mask and get loss
						self._loss = _mask_and_avg(loss_per_step, self._dec_padding_mask)

					else:  # baseline model
						self._loss = tf.contrib.seq2seq.sequence_loss(tf.stack(vocab_scores, axis=1),
																	  self._target_batch,
																	  self._dec_padding_mask)  # this applies softmax internally
					if self._hps.use_regularizer:	
						self._loss += tf.contrib.layers.apply_regularization(self._regularizer, tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))	
					

					tf.summary.scalar('loss', self._loss)

					# Calculate coverage loss from the attention distributions
					if hps.coverage:
						with tf.variable_scope('coverage_loss'):
							self._coverage_loss = _coverage_loss(self.attn_dists, self._dec_padding_mask)
							tf.summary.scalar('coverage_loss', self._coverage_loss)
						self._total_loss = self._loss + hps.cov_loss_wt * self._coverage_loss
						tf.summary.scalar('total_loss', self._total_loss)

		if hps.mode == "decode":
			# We run decode beam search mode one decoder step at a time
			assert len(
				final_dists) == 1  # final_dists is a singleton list containing shape (batch_size, extended_vsize)
			final_dists = final_dists[0]
			topk_probs, self._topk_ids = tf.nn.top_k(final_dists,
													 hps.batch_size * 2)  # take the k largest probs. note batch_size=beam_size in decode mode
			self._topk_log_probs = tf.log(topk_probs)


	def _add_train_op(self):
		"""Sets self._train_op, the op to run for training."""
		# Take gradients of the trainable variables w.r.t. the loss function to minimize
		loss_to_minimize = self._total_loss if self._hps.coverage else self._loss
		tvars = tf.trainable_variables()
		gradients = tf.gradients(loss_to_minimize, tvars, aggregation_method=tf.AggregationMethod.EXPERIMENTAL_TREE)

		# Clip the gradients
		with tf.device("/gpu:0"):
			grads, global_norm = tf.clip_by_global_norm(gradients, self._hps.max_grad_norm)

		# Add a summary
		tf.summary.scalar('global_norm', global_norm)

		# Apply adagrad optimizer
		if self._hps.optimizer=='adagrad':
			optimizer = tf.train.AdagradOptimizer(self._hps.lr, initial_accumulator_value=self._hps.adagrad_init_acc)
		if self._hps.optimizer=='adam':
			optimizer = tf.train.AdamOptimizer(learning_rate=self._hps.adam_lr)
		with tf.device("/gpu:0"):
			self._train_op = optimizer.apply_gradients(zip(grads, tvars), global_step=self.global_step,
													   name='train_step')

	def build_graph(self):
		"""Add the placeholders, model, global step, train_op and summaries to the graph"""
		tf.logging.info('Building graph...')
		tf.logging.info(self._hps.use_gcn_before_lstm)
		t0 = time.time()
		self._add_placeholders()
		with tf.device("/gpu:0"):
			tf.logging.info(self._hps.word_gcn_gating)
			if self._hps.use_gcn_before_lstm:
				
				self._add_gcn_seq2seq()
			else:
				self._add_seq2seq()
		
		self.global_step = tf.Variable(0, name='global_step', trainable=False)
		if self._hps.mode == 'train':
			self._add_train_op()
		self._summaries = tf.summary.merge_all()
		t1 = time.time()
		tf.logging.info('Time to build graph: %i seconds', t1 - t0)

	def run_train_step(self, sess, batch):
		"""Runs one training iteration. Returns a dictionary containing train op, summaries, loss, global_step and (optionally) coverage loss."""
		feed_dict = self._make_feed_dict(batch)
		to_return = {
			'train_op': self._train_op,
			'summaries': self._summaries,
			'loss': self._loss,
			'global_step': self.global_step,
		}
		if self._hps.coverage:
			to_return['coverage_loss'] = self._coverage_loss
		return sess.run(to_return, feed_dict)

	def run_eval_step(self, sess, batch):
		"""Runs one evaluation iteration. Returns a dictionary containing summaries, loss, global_step and (optionally) coverage loss."""
		feed_dict = self._make_feed_dict(batch)
		to_return = {
			'summaries': self._summaries,
			'loss': self._loss,
			'global_step': self.global_step,
		}
		if self._hps.coverage:
			to_return['coverage_loss'] = self._coverage_loss
		return sess.run(to_return, feed_dict)

	def run_encoder(self, sess, batch,use_query=False):
		"""For beam search decoding. Run the encoder on the batch and return the encoder states and decoder initial state.

	Args:
	  sess: Tensorflow session.
	  batch: Batch object that is the same example repeated across the batch (for beam search)

	Returns:
	  enc_states: The encoder states. A tensor of shape [batch_size, <=max_enc_steps, 2*hidden_dim].
	  dec_in_state: A LSTMStateTuple of shape ([1,hidden_dim],[1,hidden_dim])
	"""
		feed_dict = self._make_feed_dict(batch, just_enc=True)  # feed the batch into the placeholders
		if use_query:
		
			(enc_states, query_states, dec_in_state, global_step) = sess.run([self._enc_states, self._query_states, self._dec_in_state, self.global_step],
														   feed_dict)  # run the encoder
		else:
			(enc_states, dec_in_state, global_step) = sess.run([self._enc_states, self._dec_in_state, self.global_step], feed_dict)

			
		# dec_in_state is LSTMStateTuple shape ([batch_size,hidden_dim],[batch_size,hidden_dim])
		# Given that the batch is a single example repeated, dec_in_state is identical across the batch so we just take the top row.
		if self._hps.use_lstm:
			dec_in_state = tf.contrib.rnn.LSTMStateTuple(dec_in_state.c[0], dec_in_state.h[0])
		else:
			dec_in_state = dec_in_state[0] #verify ?
		if use_query:
			return enc_states, dec_in_state, query_states
		else:
			return enc_states, dec_in_state

	def decode_onestep(self, sess, batch, latest_tokens, enc_states, dec_init_states, prev_coverage, query_states=None):
		"""For beam search decoding. Run the decoder for one step.
	Args:
	  sess: Tensorflow session.
	  batch: Batch object containing single example repeated across the batch
	  latest_tokens: Tokens to be fed as input into the decoder for this timestep
	  enc_states: The encoder states.
	  dec_init_states: List of beam_size LSTMStateTuples; the decoder states from the previous timestep
	  prev_coverage: List of np arrays. The coverage vectors from the previous timestep. List of None if not using coverage.
	  query_states : The query states
	Returns:
	  ids: top 2k ids. shape [beam_size, 2*beam_size]
	  probs: top 2k log probabilities. shape [beam_size, 2*beam_size]
	  new_states: new states of the decoder. a list length beam_size containing
		LSTMStateTuples each of shape ([hidden_dim,],[hidden_dim,])
	  attn_dists: List length beam_size containing lists length attn_length.
	  p_gens: Generation probabilities for this step. A list length beam_size. List of None if in baseline mode.
	  new_coverage: Coverage vectors for this step. A list of arrays. List of None if coverage is not turned on.
	"""

		beam_size = len(dec_init_states)

		# Turn dec_init_states (a list of LSTMStateTuples) into a single LSTMStateTuple for the batch
		cells = [np.expand_dims(state.c, axis=0) for state in dec_init_states]
		hiddens = [np.expand_dims(state.h, axis=0) for state in dec_init_states]
		new_c = np.concatenate(cells, axis=0)  # shape [batch_size,hidden_dim]
		new_h = np.concatenate(hiddens, axis=0)  # shape [batch_size,hidden_dim]
		new_dec_in_state = tf.contrib.rnn.LSTMStateTuple(new_c, new_h)

		feed = {
			self._enc_states: enc_states,
			self._enc_padding_mask: batch.enc_padding_mask,
			self._dec_in_state: new_dec_in_state,
			self._dec_batch: np.transpose(np.array([latest_tokens])),
		}

		to_return = {
			"ids": self._topk_ids,
			"probs": self._topk_log_probs,
			"states": self._dec_out_state,
			"attn_dists": self.attn_dists
		}

		if FLAGS.pointer_gen:
			feed[self._enc_batch_extend_vocab] = batch.enc_batch_extend_vocab
			feed[self._max_art_oovs] = batch.max_art_oovs
			to_return['p_gens'] = self.p_gens

		if FLAGS.word_gcn:
			feed[self._max_word_seq_len] = batch.max_word_len
		
		if FLAGS.query_encoder:
			feed[self._query_states] = query_states
			feed[self._query_padding_mask] = batch.query_padding_mask

		if FLAGS.query_gcn:
			feed[self._max_query_seq_len] = batch.max_query_len

		if self._hps.coverage:
			feed[self.prev_coverage] = np.stack(prev_coverage, axis=0)
			to_return['coverage'] = self.coverage

		results = sess.run(to_return, feed_dict=feed)  # run the decoder step

		# Convert results['states'] (a single LSTMStateTuple) into a list of LSTMStateTuple -- one for each hypothesis
		new_states = [tf.contrib.rnn.LSTMStateTuple(results['states'].c[i, :], results['states'].h[i, :]) for i in
					  xrange(beam_size)]

		# Convert singleton list containing a tensor to a list of k arrays
		assert len(results['attn_dists']) == 1
		attn_dists = results['attn_dists'][0].tolist()

		if FLAGS.pointer_gen:
			# Convert singleton list containing a tensor to a list of k arrays
			assert len(results['p_gens']) == 1
			p_gens = results['p_gens'][0].tolist()
		else:
			p_gens = [None for _ in xrange(beam_size)]

		# Convert the coverage tensor to a list length k containing the coverage vector for each hypothesis
		if FLAGS.coverage:
			new_coverage = results['coverage'].tolist()
			assert len(new_coverage) == beam_size
		else:
			new_coverage = [None for _ in xrange(beam_size)]

		return results['ids'], results['probs'], new_states, attn_dists, p_gens, new_coverage


def _mask_and_avg(values, padding_mask):
	"""Applies mask to values then returns overall average (a scalar)
  Args:
	values: a list length max_dec_steps containing arrays shape (batch_size).
	padding_mask: tensor shape (batch_size, max_dec_steps) containing 1s and 0s.
  Returns:
	a scalar
  """

	dec_lens = tf.reduce_sum(padding_mask, axis=1)  # shape batch_size. float32
	values_per_step = [v * padding_mask[:, dec_step] for dec_step, v in enumerate(values)]
	values_per_ex = sum(values_per_step) / dec_lens  # shape (batch_size); normalized value for each batch member
	return tf.reduce_mean(values_per_ex)  # overall average


def _coverage_loss(attn_dists, padding_mask):
	"""Calculates the coverage loss from the attention distributions.
  Args:
	attn_dists: The attention distributions for each decoder timestep. A list length max_dec_steps containing shape (batch_size, attn_length)
	padding_mask: shape (batch_size, max_dec_steps).
  Returns:
	coverage_loss: scalar
  """
	coverage = tf.zeros_like(attn_dists[0])  # shape (batch_size, attn_length). Initial coverage is zero.
	covlosses = []  # Coverage loss per decoder timestep. Will be list length max_dec_steps containing shape (batch_size).
	for a in attn_dists:
		covloss = tf.reduce_sum(tf.minimum(a, coverage), [1])  # calculate the coverage loss for this step
		covlosses.append(covloss)
		coverage += a  # update the coverage vector
	coverage_loss = _mask_and_avg(covlosses, padding_mask)
	return coverage_loss
