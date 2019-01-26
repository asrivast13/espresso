# Copyright (c) 2018-present, Yiming Wang
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import torch
import torch.nn as nn
import torch.nn.functional as F

from fairseq import options, utils
from fairseq.modules import AdaptiveSoftmax, speech_attention
from . import (
    FairseqEncoder, FairseqIncrementalDecoder, FairseqModel, register_model,
    register_model_architecture,
)

from .lstm import AttentionLayer, Embedding, LSTM, LSTMCell, Linear

import speech_tools.utils as speech_utils


@register_model('speech_lstm')
class SpeechLSTMModel(FairseqModel):
    def __init__(self, encoder, decoder):
        super().__init__(encoder, decoder)

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('--dropout', type=float, metavar='D',
                            help='dropout probability')
        parser.add_argument('--encoder-conv-channels', type=str, metavar='STR',
                            help='list of encoder convolution\'s out channels')
        parser.add_argument('--encoder-conv-kernel-sizes', type=str, metavar='STR',
                            help='list of encoder convolution\'s kernel sizes')
        parser.add_argument('--encoder-conv-strides', type=str, metavar='STR',
                            help='list of encoder convolution\'s strides')
        parser.add_argument('--encoder-rnn-hidden-size', type=int, metavar='N',
                            help='encoder rnn\'s hidden size')
        parser.add_argument('--encoder-rnn-layers', type=int, metavar='N',
                            help='number of rnn encoder layers')
        parser.add_argument('--encoder-rnn-bidirectional', action='store_true',
                            help='make all rnn layers of encoder bidirectional')
        parser.add_argument('--decoder-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension')
        parser.add_argument('--decoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained decoder embedding')
        parser.add_argument('--decoder-freeze-embed', action='store_true',
                            help='freeze decoder embeddings')
        parser.add_argument('--decoder-hidden-size', type=int, metavar='N',
                            help='decoder hidden size')
        parser.add_argument('--decoder-layers', type=int, metavar='N',
                            help='number of decoder layers')
        parser.add_argument('--decoder-out-embed-dim', type=int, metavar='N',
                            help='decoder output embedding dimension')
        parser.add_argument('--attention-type', type=str, metavar='STR',
                            choices=['bahdanau','luong'], default='bahdanau',
                            help='attention type')
        parser.add_argument('--attention-dim', type=int, metavar='N',
                            help='attention dimension')
        parser.add_argument('--adaptive-softmax-cutoff', metavar='EXPR',
                            help='comma separated list of adaptive softmax cutoff points. '
                                 'Must be used with adaptive_loss criterion')
        parser.add_argument('--share-decoder-input-output-embed', default=False,
                            action='store_true',
                            help='share decoder input and output embeddings')

        # Granular dropout settings (if not specified these default to --dropout)
        parser.add_argument('--encoder-rnn-dropout-in', type=float, metavar='D',
                            help='dropout probability for encoder rnn\'s input')
        parser.add_argument('--encoder-rnn-dropout-out', type=float, metavar='D',
                            help='dropout probability for encoder rnn\'s output')
        parser.add_argument('--decoder-dropout-in', type=float, metavar='D',
                            help='dropout probability for decoder input embedding')
        parser.add_argument('--decoder-dropout-out', type=float, metavar='D',
                            help='dropout probability for decoder output')
        # fmt: on

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""
        # make sure that all args are properly defaulted (in case there are any new ones)
        base_architecture(args)

        def load_pretrained_embedding_from_file(embed_path, dictionary, embed_dim):
            num_embeddings = len(dictionary)
            padding_idx = dictionary.pad()
            embed_tokens = Embedding(num_embeddings, embed_dim, padding_idx)
            embed_dict = utils.parse_embedding(embed_path)
            utils.print_embed_overlap(embed_dict, dictionary)
            return utils.load_embedding(embed_dict, dictionary, embed_tokens)

        # separate decoder input embeddings
        pretrained_decoder_embed = None
        if args.decoder_embed_path:
            pretrained_decoder_embed = load_pretrained_embedding_from_file(
                args.decoder_embed_path,
                task.target_dictionary,
                args.decoder_embed_dim
            )
        # one last double check of parameter combinations
        if args.share_decoder_input_output_embed and (
                args.decoder_embed_dim != args.decoder_out_embed_dim):
            raise ValueError(
                '--share-decoder-input-output-embeddings requires '
                '--decoder-embed-dim to match --decoder-out-embed-dim'
            )

        if args.decoder_freeze_embed:
            pretrained_decoder_embed.weight.requires_grad = False

        def eval_str_nested_list_or_tuple(x, type=int):
            if x is None:
                return None
            if isinstance(x, str):
                x = eval(x)
            if isinstance(x, list):
                return list(
                    map(lambda s: eval_str_nested_list_or_tuple(s, type), x))
            elif isinstance(x, tuple):
                return tuple(
                    map(lambda s: eval_str_nested_list_or_tuple(s, type), x))
            else:
                try:
                    return type(x)
                except:
                    raise ValueError

        out_channels = eval_str_nested_list_or_tuple(args.encoder_conv_channels,
            type=int)
        kernel_sizes = eval_str_nested_list_or_tuple(
            args.encoder_conv_kernel_sizes, type=int)
        strides = eval_str_nested_list_or_tuple(args.encoder_conv_strides,
            type=int)
        in_channels = 1 # hard-coded for now
        conv_layers = ConvBNReLU(out_channels, kernel_sizes, strides,
            in_channels=in_channels) if not out_channels is None else None

        assert task.feat_dim % in_channels == 0
        rnn_encoder_input_size = task.feat_dim // in_channels
        if conv_layers is not None:
            for stride in strides:
                if isinstance(stride, (list, tuple)):
                    assert len(stride) > 0
                    s = stride[1] if len(stride) > 1 else stride[0]
                else:
                    assert isinstance(stride, int)
                    s = stride
                rnn_encoder_input_size = (rnn_encoder_input_size + s - 1) // s
            rnn_encoder_input_size *= out_channels[-1]

        encoder = SpeechLSTMEncoder(
            conv_layers_before=conv_layers,
            input_size=rnn_encoder_input_size,
            hidden_size=args.encoder_rnn_hidden_size,
            num_layers=args.encoder_rnn_layers,
            dropout_in=args.encoder_rnn_dropout_in,
            dropout_out=args.encoder_rnn_dropout_out,
            bidirectional=args.encoder_rnn_bidirectional,
        )
        decoder = SpeechLSTMDecoder(
            dictionary=task.target_dictionary,
            embed_dim=args.decoder_embed_dim,
            hidden_size=args.decoder_hidden_size,
            out_embed_dim=args.decoder_out_embed_dim,
            num_layers=args.decoder_layers,
            dropout_in=args.decoder_dropout_in,
            dropout_out=args.decoder_dropout_out,
            encoder_output_units=encoder.output_units,
            attn_type=args.attention_type,
            attn_dim=args.attention_dim,
            pretrained_embed=pretrained_decoder_embed,
            share_input_output_embed=args.share_decoder_input_output_embed,
            adaptive_softmax_cutoff=(
                options.eval_str_list(args.adaptive_softmax_cutoff, type=int)
                if args.criterion == 'adaptive_loss' else None
            ),
        )
        return cls(encoder, decoder)


class ConvBNReLU(nn.Module):
    """Sequence of convolution-BatchNorm-ReLU layers."""
    def __init__(self, out_channels, kernel_sizes, strides, in_channels=1):
        super().__init__()
        self.out_channels = out_channels
        self.kernel_sizes = kernel_sizes
        self.strides = strides
        self.in_channels = in_channels

        num_layers = len(out_channels)
        assert num_layers == len(kernel_sizes) and num_layers == len(strides)

        self.convolutions = nn.ModuleList()
        self.batchnorms = nn.ModuleList()
        for i in range(num_layers):
            self.convolutions.append(
                Convolution2d(
                    self.in_channels if i == 0 else self.out_channels[i-1],
                    self.out_channels[i],
                    self.kernel_sizes[i], self.strides[i]))
            self.batchnorms.append(nn.BatchNorm2d(out_channels[i]))

    def output_lengths(self, in_lengths):
        out_lengths = in_lengths
        for stride in self.strides:
            if isinstance(stride, (list, tuple)):
                assert len(stride) > 0
                s = stride[0]
            else:
                assert isinstance(stride, int)
                s = stride
            out_lengths = (out_lengths + s - 1) // s
        return out_lengths

    def forward(self, src, src_lengths):
        # B X T X C -> B X (input channel num) x T X (C / input channel num)
        x = src.view(src.size(0), src.size(1), self.in_channels,
            src.size(2) // self.in_channels).transpose(1, 2)
        for conv, bn in zip(self.convolutions, self.batchnorms):
            x = F.relu(bn(conv(x)))
        # B X (output channel num) x T X C' -> B X T X (output channel num) X C'
        x = x.transpose(1, 2)
        # B X T X (output channel num) X C' -> B X T X C
        x = x.contiguous().view(x.size(0), x.size(1), x.size(2) * x.size(3))

        x_lengths = self.output_lengths(src_lengths)
        padding_mask = 1 - speech_utils.sequence_mask(x_lengths, x.size(1))
        if padding_mask.any():
            x = x.masked_fill(padding_mask.unsqueeze(-1), 0.0)

        return x, x_lengths, padding_mask


class SpeechLSTMEncoder(FairseqEncoder):
    """LSTM encoder."""
    def __init__(
        self, conv_layers_before=None, input_size=80, hidden_size=512,
        num_layers=1, dropout_in=0.1, dropout_out=0.1, bidirectional=False,
        left_pad=False, pretrained_embed=None, padding_value=0.,
    ):
        super().__init__(None) # no src dictionary
        self.conv_layers_before = conv_layers_before
        self.num_layers = num_layers
        self.dropout_in = dropout_in
        self.dropout_out = dropout_out
        self.bidirectional = bidirectional
        self.hidden_size = hidden_size
        
        self.lstm = LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=self.dropout_out if num_layers > 1 else 0.,
            bidirectional=bidirectional,
        )
        self.left_pad = left_pad
        self.padding_value = padding_value

        self.output_units = hidden_size
        if bidirectional:
            self.output_units *= 2

    def output_lengths(self, in_lengths):
        return in_lengths if self.conv_layers_before is None \
            else self.conv_layers_before.output_lengths(in_lengths)

    def forward(self, src_tokens, src_lengths):
        if self.left_pad:
            # convert left-padding to right-padding
            src_tokens = speech_utils.convert_padding_direction(
                src_tokens,
                src_lengths,
                left_to_right=True,
            )

        if self.conv_layers_before is not None:
            x, src_lengths, padding_mask = self.conv_layers_before(src_tokens,
                src_lengths)
        else:
            x, padding_mask = src_tokens, \
                1 - speech_utils.sequence_mask(src_lengths, src_tokens.size(1))

        bsz, seqlen = x.size(0), x.size(1)

        x = F.dropout(x, p=self.dropout_in, training=self.training)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        # pack embedded source tokens into a PackedSequence
        packed_x = nn.utils.rnn.pack_padded_sequence(x, src_lengths.data.tolist())

        # apply LSTM
        if self.bidirectional:
            state_size = 2 * self.num_layers, bsz, self.hidden_size
        else:
            state_size = self.num_layers, bsz, self.hidden_size
        h0 = x.new_zeros(*state_size)
        c0 = x.new_zeros(*state_size)
        packed_outs, (final_hiddens, final_cells) = self.lstm(packed_x, (h0, c0))

        # unpack outputs and apply dropout
        x, _ = nn.utils.rnn.pad_packed_sequence(packed_outs, padding_value=self.padding_value)
        x = F.dropout(x, p=self.dropout_out, training=self.training)
        assert list(x.size()) == [seqlen, bsz, self.output_units]

        if self.bidirectional:

            def combine_bidir(outs):
                out = outs.view(self.num_layers, 2, bsz, -1).transpose(1, 2).contiguous()
                return out.view(self.num_layers, bsz, -1)

            final_hiddens = combine_bidir(final_hiddens)
            final_cells = combine_bidir(final_cells)

        encoder_padding_mask = padding_mask.t()

        return {
            'encoder_out': (x, final_hiddens, final_cells),
            'encoder_padding_mask': encoder_padding_mask if encoder_padding_mask.any() else None
        }

    def reorder_encoder_out(self, encoder_out, new_order):
        encoder_out['encoder_out'] = tuple(
            eo.index_select(1, new_order)
            for eo in encoder_out['encoder_out']
        )
        if encoder_out['encoder_padding_mask'] is not None:
            encoder_out['encoder_padding_mask'] = \
                encoder_out['encoder_padding_mask'].index_select(1, new_order)
        return encoder_out

    def max_positions(self):
        """Maximum input length supported by the encoder."""
        return int(1e5)  # an arbitrary large number


class SpeechLSTMDecoder(FairseqIncrementalDecoder):
    """LSTM decoder."""
    def __init__(
        self, dictionary, embed_dim=512, hidden_size=512, out_embed_dim=512,
        num_layers=1, dropout_in=0.1, dropout_out=0.1,
        encoder_output_units=512, attn_type='bahdanau', attn_dim=256,
        pretrained_embed=None, share_input_output_embed=False,
        adaptive_softmax_cutoff=None,
    ):
        super().__init__(dictionary)
        self.dropout_in = dropout_in
        self.dropout_out = dropout_out
        self.hidden_size = hidden_size
        self.share_input_output_embed = share_input_output_embed
        self.need_attn = True

        self.adaptive_softmax = None
        num_embeddings = len(dictionary)
        padding_idx = dictionary.pad()
        if pretrained_embed is None:
            self.embed_tokens = Embedding(num_embeddings, embed_dim, padding_idx)
        else:
            self.embed_tokens = pretrained_embed

        self.encoder_output_units = encoder_output_units

        self.layers = nn.ModuleList([
            LSTMCell(
                input_size=encoder_output_units + (embed_dim if layer == 0 else hidden_size),
                hidden_size=hidden_size,
            )
            for layer in range(num_layers)
        ])
        if attn_type == 'bahdanau':
            self.attention = speech_attention.BahdanauAttention(hidden_size,
                encoder_output_units, attn_dim)
        elif attn_type == 'luong':
            self.attention = speech_attention.LuongAttention(hidden_size,
                encoder_output_units)
        else:
            raise ValueError('unrecognized attention type.')
        if hidden_size + encoder_output_units != out_embed_dim:
            self.additional_fc = Linear(hidden_size + encoder_output_units, out_embed_dim)
        if adaptive_softmax_cutoff is not None:
            # setting adaptive_softmax dropout to dropout_out for now but can be redefined
            self.adaptive_softmax = AdaptiveSoftmax(num_embeddings, embed_dim, adaptive_softmax_cutoff,
                                                    dropout=dropout_out)
        elif not self.share_input_output_embed:
            self.fc_out = Linear(out_embed_dim, num_embeddings, dropout=dropout_out)

    def forward(self, prev_output_tokens, encoder_out_dict, incremental_state=None):
        encoder_out = encoder_out_dict['encoder_out']
        encoder_padding_mask = encoder_out_dict['encoder_padding_mask']

        if incremental_state is not None:
            prev_output_tokens = prev_output_tokens[:, -1:]
        bsz, seqlen = prev_output_tokens.size()

        # get outputs from encoder
        encoder_outs, encoder_hiddens, encoder_cells = encoder_out[:3]
        srclen = encoder_outs.size(0)

        # embed tokens
        x = self.embed_tokens(prev_output_tokens)
        x = F.dropout(x, p=self.dropout_in, training=self.training)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        # initialize previous states (or get from cache during incremental generation)
        cached_state = utils.get_incremental_state(self, incremental_state, 'cached_state')
        if cached_state is not None:
            prev_hiddens, prev_cells, input_feed = cached_state
        else:
            num_layers = len(self.layers)
            prev_hiddens = [x.new_zeros(bsz, self.hidden_size) \
                for i in range(num_layers)]
            prev_cells = [x.new_zeros(bsz, self.hidden_size) \
                for i in range(num_layers)]
            input_feed = x.new_zeros(bsz, self.encoder_output_units)

        attn_scores = x.new_zeros(srclen, seqlen, bsz)
        outs = []
        for j in range(seqlen):
            # input feeding: concatenate context vector from previous time step
            input = torch.cat((x[j, :, :], input_feed), dim=1)

            for i, rnn in enumerate(self.layers):
                # recurrent cell
                hidden, cell = rnn(input, (prev_hiddens[i], prev_cells[i]))

                # compute and apply attention using the 1st layer's hidden state
                if i == 0:
                    context, attn_scores[:, j, :], _ = self.attention(hidden,
                        encoder_outs, encoder_padding_mask)

                # hidden state concatenated with context vector becomes the
                # input to the next layer
                input = torch.cat((hidden, context), dim=1) 
                input = F.dropout(input, p=self.dropout_out, training=self.training)

                # save state for next time step
                prev_hiddens[i] = hidden
                prev_cells[i] = cell

            # input feeding
            input_feed = context

            # save final output
            outs.append(input)

        # cache previous states (no-op except during incremental generation)
        utils.set_incremental_state(
            self, incremental_state, 'cached_state',
            (prev_hiddens, prev_cells, input_feed),
        )

        # collect outputs across time steps
        x = torch.cat(outs, dim=0).view(seqlen, bsz, -1)
        assert x.size(2) == self.hidden_size + self.encoder_output_units

        # T x B x C -> B x T x C
        x = x.transpose(1, 0)

        # srclen x tgtlen x bsz -> bsz x tgtlen x srclen
        if not self.training and self.need_attn:
            attn_scores = attn_scores.transpose(0, 2)
        else:
            attn_scores = None

        # project back to size of vocabulary
        if self.adaptive_softmax is None:
            if hasattr(self, 'additional_fc'):
                x = self.additional_fc(x)
                x = F.dropout(x, p=self.dropout_out, training=self.training)
            if self.share_input_output_embed:
                x = F.linear(x, self.embed_tokens.weight)
            else:
                x = self.fc_out(x)
        return x, attn_scores

    def reorder_incremental_state(self, incremental_state, new_order):
        super().reorder_incremental_state(incremental_state, new_order)
        cached_state = utils.get_incremental_state(self, incremental_state, 'cached_state')
        if cached_state is None:
            return

        def reorder_state(state):
            if isinstance(state, list):
                return [reorder_state(state_i) for state_i in state]
            return state.index_select(0, new_order)

        new_state = tuple(map(reorder_state, cached_state))
        utils.set_incremental_state(self, incremental_state, 'cached_state', new_state)

    def max_positions(self):
        """Maximum output length supported by the decoder."""
        return int(1e5)  # an arbitrary large number

    def make_generation_fast_(self, need_attn=False, **kwargs):
        self.need_attn = need_attn


def Convolution2d(in_channels, out_channels, kernel_size, stride):
    if isinstance(kernel_size, (list, tuple)):
        if len(kernel_size) != 2:
            assert len(kernel_size) == 1
            kernel_size = (kernel_size[0], kernel_size[0])
    else:
        assert isinstance(kernel_size, int)
        kernel_size = (kernel_size, kernel_size)
    if isinstance(stride, (list, tuple)):
        if len(stride) != 2:
            assert len(stride) == 1
            stride = (stride[0], stride[0])
    else:
        assert isinstance(stride, int)
        stride = (stride, stride)
    assert kernel_size[0] % 2 == 1 and kernel_size[1] % 2 == 1
    padding = ((kernel_size[0] - 1) // 2, (kernel_size[1] - 1) // 2)
    m = nn.Conv2d(in_channels, out_channels, kernel_size, stride=stride, \
        padding=padding)
    return m


@register_model_architecture('speech_lstm', 'speech_lstm')
def base_architecture(args):
    args.dropout = getattr(args, 'dropout', 0.1)
    args.encoder_conv_channels = getattr(args, 'encoder_conv_channels',
        '[64, 64, 128, 128]')
    args.encoder_conv_kernel_sizes = getattr(args, 'encoder_conv_kernel_sizes',
        '[(3, 3), (3, 3), (3, 3), (3, 3)]')
    args.encoder_conv_strides = getattr(args, 'encoder_conv_strides',
        '[(1, 1), (2, 2), (1, 1), (2, 2)]')
    args.encoder_rnn_hidden_size = getattr(args, 'encoder_rnn_hidden_size', 320)
    args.encoder_rnn_layers = getattr(args, 'encoder_rnn_layers', 3)
    args.encoder_rnn_bidirectional = getattr(args, 'encoder_rnn_bidirectional', True)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 320)
    args.decoder_embed_path = getattr(args, 'decoder_embed_path', None)
    args.decoder_freeze_embed = getattr(args, 'decoder_freeze_embed', False)
    args.decoder_hidden_size = getattr(args, 'decoder_hidden_size', args.decoder_embed_dim)
    args.decoder_layers = getattr(args, 'decoder_layers', 3)
    args.decoder_out_embed_dim = getattr(args, 'decoder_out_embed_dim', 960)
    args.attention_type = getattr(args, 'attention_type', 'bahdanau')
    args.attention_dim = getattr(args, 'attention_dim', 320)
    args.encoder_rnn_dropout_in = getattr(args, 'encoder_rnn_dropout_in', args.dropout)
    args.encoder_rnn_dropout_out = getattr(args, 'encoder_rnn_dropout_out', args.dropout)
    args.decoder_dropout_in = getattr(args, 'decoder_dropout_in', args.dropout)
    args.decoder_dropout_out = getattr(args, 'decoder_dropout_out', args.dropout)
    args.adaptive_softmax_cutoff = getattr(args, 'adaptive_softmax_cutoff', '10000,50000,200000')
    args.share_decoder_input_output_embed = getattr(args, 'share_decoder_input_output_embed', False)

@register_model_architecture('speech_lstm', 'speech_conv_lstm_wsj')
def conv_lstm_wsj(args):
    '''
    args.dropout = getattr(args, 'dropout', 0.1)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 256)
    args.encoder_dropout_in = getattr(args, 'encoder_dropout_in', 0)
    args.encoder_dropout_out = getattr(args, 'encoder_dropout_out', 0)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 256)
    args.decoder_out_embed_dim = getattr(args, 'decoder_out_embed_dim', 256)
    args.decoder_dropout_in = getattr(args, 'decoder_dropout_in', 0)
    args.decoder_dropout_out = getattr(args, 'decoder_dropout_out', args.dropout)
    '''
    base_architecture(args)