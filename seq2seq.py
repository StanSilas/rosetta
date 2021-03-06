import keras
import keras.layers as L
from keras.models import Model
from keras.preprocessing.sequence import pad_sequences
import numpy as np
import tensorflow as tf


class Seq2SeqWithBPE:

    def __init__(
        self, bpe_input, bpe_target, max_len_input, max_len_target,
        latent_dim=512, dropout=0.5, train_embeddings=True
    ):
        self.bpe_input = bpe_input
        self.bpe_target = bpe_target
        self.max_len_input = max_len_input
        self.max_len_target = max_len_target
        self.latent_dim = latent_dim
        self.dropout = dropout

        self.nr_input_tokens = len(bpe_input.wordvec_index)
        self.nr_target_tokens = len(bpe_target.wordvec_index)

        self.encoder_gru = L.Bidirectional(
            L.GRU(
                latent_dim // 2,
                dropout=dropout,
                return_state=True,
                name='encoder_gru',
                dtype=tf.float32
            ),
            name='encoder_bidirectional'
        )
        self.decoder_gru = L.GRU(
            latent_dim,
            dropout=dropout,
            return_sequences=True,
            return_state=True,
            name='decoder_gru',
            dtype=tf.float32
        )
        self.decoder_dense = L.Dense(
            self.nr_target_tokens,
            activation='softmax',
            name='decoder_outputs',
            dtype=tf.float32
        )

        self.input_embedding = L.Embedding(
            self.nr_input_tokens,
            bpe_input.embedding_dim,
            mask_zero=True,
            weights=[bpe_input.embedding_matrix],
            trainable=train_embeddings,
            name='input_embedding',
            dtype=tf.float32,
        )
        self.target_embedding = L.Embedding(
            self.nr_target_tokens,
            bpe_target.embedding_dim,
            mask_zero=True,
            weights=[bpe_target.embedding_matrix],
            trainable=train_embeddings,
            name='target_embedding',
            dtype=tf.float32,
        )

        self.encoder_inputs = L.Input((max_len_input, ), dtype='int32', name='encoder_inputs')
        self.encoder_embeddings = self.input_embedding(self.encoder_inputs)
        _, self.encoder_state_1, self.encoder_state_2 = self.encoder_gru(
            self.encoder_embeddings
        )
        self.encoder_states = L.concatenate([self.encoder_state_1, self.encoder_state_2])

        self.decoder_inputs = L.Input(
            shape=(max_len_target - 1, ),
            dtype='int32',
            name='decoder_inputs'
        )
        self.decoder_mask = L.Masking(mask_value=0)(self.decoder_inputs)
        self.decoder_embeddings_inputs = self.target_embedding(self.decoder_mask)
        self.decoder_embeddings_outputs, _ = self.decoder_gru(
            self.decoder_embeddings_inputs,
            initial_state=self.encoder_states
        )
        self.decoder_outputs = self.decoder_dense(self.decoder_embeddings_outputs)

        self.model = Model(
            inputs=[self.encoder_inputs, self.decoder_inputs],
            outputs=self.decoder_outputs
        )

        self.inference_encoder_model = Model(
            inputs=self.encoder_inputs,
            outputs=self.encoder_states
        )
            
        self.inference_decoder_state_inputs = L.Input(
            shape=(latent_dim, ),
            dtype='float32',
            name='inference_decoder_state_inputs'
        )
        self.inference_decoder_embeddings_outputs, \
            self.inference_decoder_states = self.decoder_gru(
                self.decoder_embeddings_inputs,
                initial_state=self.inference_decoder_state_inputs
            )
        self.inference_decoder_outputs = self.decoder_dense(
            self.inference_decoder_embeddings_outputs
        )

        self.inference_decoder_model = Model(
            inputs=[self.decoder_inputs, self.inference_decoder_state_inputs],
            outputs=[self.inference_decoder_outputs, self.inference_decoder_states]
        )

    def create_batch_generator(
        self, samples_ids, input_sequences, target_sequences, batch_size
    ):
    
        def batch_generator():
            nr_batches = np.ceil(len(samples_ids) / batch_size)
            while True:
                shuffled_ids = np.random.permutation(samples_ids)
                batch_splits = np.array_split(shuffled_ids, nr_batches)
                for batch_ids in batch_splits:
                    batch_X = pad_sequences(
                        input_sequences.iloc[batch_ids],
                        padding='post',
                        maxlen=self.max_len_input
                    )
                    batch_y = pad_sequences(
                        target_sequences.iloc[batch_ids],
                        padding='post',
                        maxlen=self.max_len_target
                    )
                    batch_y_t_output = keras.utils.to_categorical(
                        batch_y[:, 1:],
                        num_classes=self.nr_target_tokens
                    )
                    batch_x_t_input = batch_y[:, :-1]
                    yield ([batch_X, batch_x_t_input], batch_y_t_output)
        
        return batch_generator()

    def decode_beam_search(self, input_seq, beam_width):
        initial_states = self.inference_encoder_model.predict(input_seq)
        
        top_candidates = [{
            'states': initial_states,
            'idx_sequence': [self.bpe_target.start_token_idx],
            'token_sequence': [self.bpe_target.start_token],
            'score': 0.0,
            'live': True
        }]
        live_k = 1
        dead_k = 0
        
        for _ in range(self.max_len_target):
            if not(live_k and dead_k < beam_width):
                break
            new_candidates = []
            for candidate in top_candidates:
                if not candidate['live']:
                    new_candidates.append(candidate)
                    continue
             
                target_seq = np.zeros((1, self.max_len_target - 1))
                target_seq[0, 0] = candidate['idx_sequence'][-1]
                output, states = self.inference_decoder_model.predict(
                    [target_seq, candidate['states']]
                )
                probs = output[0, 0, :]
            
                for idx in np.argsort(-probs)[:beam_width]:
                    new_candidates.append({
                        'states': states,
                        'idx_sequence': candidate['idx_sequence'] + [idx],
                        'token_sequence': (
                            candidate['token_sequence'] + [self.bpe_target.tokens[idx]]
                        ),
                        # sum -log(prob) numerical more stable than to multiplikate probs
                        # goal now to minimize the score
                        'score': candidate['score'] - np.log(probs[idx]),
                        'live': idx != self.bpe_target.stop_token_idx,
                    })
            
            top_candidates = sorted(
                new_candidates, key=lambda c: c['score']
            )[:beam_width]
            
            alive = np.array([c['live'] for c in top_candidates])
            live_k = sum(alive == True)
            dead_k = sum(alive == False)
            
        return self.bpe_target.sentencepiece.DecodePieces(top_candidates[0]['token_sequence'])
