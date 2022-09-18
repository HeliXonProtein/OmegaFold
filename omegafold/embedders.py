# -*- coding: utf-8 -*-
# =============================================================================
# Copyright 2022 HeliXon Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""

"""
# =============================================================================
# Imports
# =============================================================================
import argparse
import typing

import torch
from torch import nn

from omegafold import modules, utils
from omegafold.utils import residue_constants as rc


# =============================================================================
# Constants
# =============================================================================
# =============================================================================
# Functions
# =============================================================================
def _apply_embed(
        inputs: torch.Tensor,
        sin: torch.Tensor,
        cos: torch.Tensor,
        seq_dim: typing.Tuple[int, ...]
) -> torch.Tensor:
    """Applies RoPE to ~inputs

    Args:
        inputs: the tensor to which RoPE is applied, the dimensions indexed by
            ~seq_dim indicates the spatial dimensions
        sin: the sine tensor that constitutes parts of the RoPE,
            of spatial shape + vector dimension
        cos: the cosine tensor that constitutes parts of the RoPE,
            of spatial shape + vector dimension
        seq_dim: the dimensions indicating the spatial dimensions,
            must be consecutive

    Returns:
        tensor with RoPE applied.

    """
    gaps = [
        (seq_dim[i + 1] - seq_dim[i]) == 1 for i in range(len(seq_dim) - 1)
    ]
    if len(gaps) > 0:
        if not all(gaps):
            raise ValueError(f"seq_dim must be consecutive, but got {seq_dim}")

    # Align dimensions of sine and cosine
    seq_dim = sorted(seq_dim)
    end = seq_dim[-1]
    for _ in range(seq_dim[0]):
        sin = sin.unsqueeze(0)
        cos = cos.unsqueeze(0)
        end += 1

    for _ in range(end, inputs.ndim - 1):
        sin = sin.unsqueeze(_)
        cos = cos.unsqueeze(_)

    # Apply RoPE
    x1, x2 = torch.split(inputs, inputs.shape[-1] // 2, dim=-1)
    return torch.cat([x1 * cos - x2 * sin, x2 * cos + x1 * sin], dim=-1)


# =============================================================================
# Classes
# =============================================================================
class EdgeEmbedder(modules.OFModule):
    """
    Embed the input into node and edge representations

    """

    def __init__(self, cfg: argparse.Namespace) -> None:
        super(EdgeEmbedder, self).__init__(cfg)

        self.proj_i = nn.Embedding(cfg.alphabet_size, cfg.edge_dim)
        self.proj_j = nn.Embedding(cfg.alphabet_size, cfg.edge_dim)
        self.relpos = RelPosEmbedder(cfg.relpos_len * 2 + 1, cfg.edge_dim)

    def forward(
            self,
            fasta_sequence: torch.Tensor,
            residue_index: torch.Tensor,
            out: torch.Tensor
    ) -> torch.Tensor:
        out += self.proj_i(fasta_sequence).unsqueeze(-2)
        out += self.proj_j(fasta_sequence).unsqueeze(-3)
        out += self.relpos(residue_index)

        return out


class RoPE(nn.Module):
    """The RoPE module

    Attributes:
        input_dim: the dimension of the input vectors.

    """

    def __init__(self, input_dim: int) -> None:
        super(RoPE, self).__init__()
        if input_dim % 2 != 0:
            raise ValueError(
                f"Input dimension for RoPE must be a multiple of 2,"
                f" but got {input_dim}"
            )
        self.input_dim = input_dim
        self.half_size = input_dim // 2
        freq_seq = torch.arange(self.half_size, dtype=torch.float32)
        freq_seq = -freq_seq.div(float(self.half_size))

        self.register_buffer(
            "inv_freq", torch.pow(10000., freq_seq), persistent=False
        )

    def forward(
            self,
            tensor: torch.Tensor,
            seq_dim: typing.Union[int, tuple],
            residue_index: torch.Tensor,
            offset_rope=False
    ) -> torch.Tensor:
        """

        Args:
            tensor: the tensor to apply rope onto
            seq_dim: the dimension that represents the sequence dimension

        Returns:

        """
        if isinstance(seq_dim, int):
            seq_dim = [seq_dim, ]

        if offset_rope:
            position = residue_index
        else:
            position = torch.arange(residue_index.shape[0],
                dtype=tensor.dtype,
                device=tensor.device)
        sinusoid = torch.einsum("..., d->...d", position, self.inv_freq)
        sin, cos = torch.sin(sinusoid), torch.cos(sinusoid)

        return _apply_embed(tensor, sin, cos, seq_dim)

class RelPosEmbedder(nn.Embedding):
    """
        Compute the relative positional embedding, this is the same algorithm in
        Jumper et al. (2021) Suppl. Alg. 4 "relpos"
    """

    def forward(self, residue_index: torch.Tensor) -> torch.Tensor:
        """

        Args:
            num_res: number of residues in input sequence.

        Returns:

        """
        idx = residue_index
        one_side = self.num_embeddings // 2
        idx = (idx[None, :] - idx[:, None]).clamp(-one_side, one_side)
        idx = idx + one_side
        return super(RelPosEmbedder, self).forward(idx)  # [num_res, dim]


class RecycleEmbedder(modules.OFModule):
    """
    The recycle embedder from Jumper et al. (2021)

    """

    def __init__(self, cfg: argparse.Namespace):
        super(RecycleEmbedder, self).__init__(cfg)

        self.layernorm_node = nn.LayerNorm(cfg.node_dim)
        self.layernorm_edge = nn.LayerNorm(cfg.edge_dim)
        self.dgram = modules.Val2Bins(cfg.prev_pos)
        self.prev_pos_embed = nn.Embedding(
            cfg.prev_pos.num_bins, cfg.edge_dim,
        )

    def forward(
            self,
            fasta: torch.Tensor,
            prev_node: torch.Tensor,
            prev_edge: torch.Tensor,
            prev_x: torch.Tensor,
            node_repr: torch.Tensor,
            edge_repr: torch.Tensor,
    ) -> typing.Tuple[torch.Tensor, torch.Tensor]:
        """Recycle the last run

        Args:
            fasta:
            prev_node: node representations from the previous cycle
                of shape [num_res, node_repr_dim]
            prev_edge: edge representations from the previous cycle
                of shape [num_res, num_res, edge_repr_dim]
            prev_x: pseudo beta coordinates from the previous cycle.
                of shape [num_res, 3]
            node_repr: the node representation to put stuff in
            edge_repr: the edge representation to put stuff in

        Returns:

        """
        atom_mask = rc.restype2atom_mask[fasta.cpu()].to(self.device)
        prev_beta = utils.create_pseudo_beta(prev_x, atom_mask)
        d = utils.get_norm(prev_beta.unsqueeze(-2) - prev_beta.unsqueeze(-3))
        d = self.dgram(d)
        node_repr[..., 0, :, :] += self.layernorm_node(prev_node)
        edge_repr += self.prev_pos_embed(d)
        edge_repr += self.layernorm_edge(prev_edge)

        return node_repr, edge_repr


# =============================================================================
# Tests
# =============================================================================
if __name__ == '__main__':
    pass
