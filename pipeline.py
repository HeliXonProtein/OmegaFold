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
This file contains the utilities that we use for the entire inference pipeline
"""
# =============================================================================
# Imports
# =============================================================================
import argparse
import collections
import logging
import ntpath
import os
import os.path
import pathlib
import typing

from Bio import PDB as PDB
from Bio.PDB import StructureBuilder
import torch
from torch import hub
from torch.backends import cuda, cudnn
from torch.utils.hipify import hipify_python

from omegafold import utils
from omegafold.utils.protein_utils import residue_constants as rc


# =============================================================================
# Constants
# =============================================================================
# =============================================================================
# Functions
# =============================================================================
def _set_precision(allow_tf32: bool) -> None:
    """Set precision (mostly to do with tensorfloat32)

    This allows user to go to fp32

    Args:
        allow_tf32: if allowing

    Returns:

    """
    if int(torch.__version__.split(".")[1]) < 12:
        cuda.matmul.allow_tf32 = allow_tf32
        cudnn.allow_tf32 = allow_tf32
    else:
        precision = "high" if allow_tf32 else "highest"
        torch.set_float32_matmul_precision(precision)


def path_leaf(path: str) -> str:
    """
    Get the filename from the path

    Args:
        path: the absolute or relative path to the file

    Returns:
        the filename

    """
    head, tail = ntpath.split(path)
    return tail or ntpath.basename(head)


def fasta2inputs(
        fasta_path: str,
        output_dir: typing.Optional[str] = None,
        num_pseudo_msa: int = 15,
        device: typing.Optional[torch.device] = torch.device('cpu'),
        mask_rate: float = 0.12,
        num_cycle: int = 10,
        deterministic: bool = True,
        real_msa: bool = False,
) -> typing.Generator[
    typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor, str], None, None]:
    """
    Load a fasta file and

    Args:
        fasta_path: the path to the fasta files
        output_dir: the path to the output directory
        num_pseudo_msa:
        device: the device to move
        mask_rate:
        num_cycle:
        deterministic:

    Returns:

    """
    chain_ids, aastr = list(), list()
    with open(fasta_path, 'r') as file:
        lines = file.readlines()
    name = False
    for line in lines:
        if len(line) == 0:
            continue
        if line.startswith(">") or line .startswith(":"):
            name = True
            chain_ids.append(line.strip(">").strip("\n"))
        else:
            if name:
                aastr.append(line.strip("\n").upper())
                name = False
            else:
                aastr[-1] = aastr[-1] + line.strip("\n").upper()
    if real_msa:
        combined = [chain_ids[0], aastr]
    else:
        combined = sorted(
            list(zip(chain_ids, aastr)), key=lambda x: len(x[1])
        )
    if output_dir is None:
        parent = pathlib.Path(fasta_path).parent
        folder_name = path_leaf(fasta_path).split(".")[0]
        output_dir = os.path.join(parent, folder_name)
        os.makedirs(output_dir, exist_ok=True)
    name_max = os.pathconf(output_dir, 'PC_NAME_MAX') - 4
    
    def chain_break(residue_index, lengths, offset=200):
        '''Minkyung: add big enough number to residue index to indicate chain breaks'''
        L_prev = 0
        for L_i in lengths[:-1]:
            idx_res[L_prev+L_i:] += offset
            L_prev += L_i      
        return residue_index

    for i, (ch, msa) in enumerate(combined):
        
        if not real_msa: msa = [msa]
        
        fas = msa[0]
        lengths = [len(a) for a in fas.split(":")]
        residue_index = torch.arange(sum(lengths))
        residue_index = chain_break(residue_index, lengths)
        
        aatypes = list()
        masks = list()
        for fas in msa:
            fas = fas.replace(":","")
            fas = fas.replace("Z", "E").replace("B", "D").replace("U", "C")
            aatype = torch.LongTensor(
                [rc.restypes_with_x.index(aa) if aa != '-' else 21 for aa in fas]
            )
            assert torch.all(aatype.ge(0)) and torch.all(aatype.le(21)), \
                f"Only take 0-20 amino acids as inputs with unknown amino acid " \
                f"indexed as 20"
            aatypes.append(aatype)
        
        aatype = aatypes[0]
        mask = torch.ones_like(aatype).float()
        
        if len(ch) < name_max:
            out_fname = ch.replace(os.path.sep, "-")
        else:
            out_fname = f"{i}th chain"
        out_fname = os.path.join(output_dir, out_fname + ".pdb")

        num_res = len(aatype)
        data = list()
        g = None
        if deterministic:
            g = torch.Generator()
            g.manual_seed(num_res)
        for _ in range(num_cycle):
            if real_msa:
                p_msa = torch.stack(aatypes)
                num_pseudo_msa = len(aatypes) - 1
            else:
                p_msa = aatype[None, :].repeat(num_pseudo_msa, 1)
                
            p_msa_mask = torch.rand(
                [num_pseudo_msa, num_res], generator=g
            ).gt(mask_rate)
            p_msa_mask = torch.cat((mask[None, :], p_msa_mask), dim=0)
            p_msa = torch.cat((aatype[None, :], p_msa), dim=0)
            p_msa[~p_msa_mask.bool()] = 21
            data.append({"p_msa": p_msa, "p_msa_mask": p_msa_mask, "residue_index":residue_index})

        yield utils.recursive_to(data, device=device), out_fname


def save_pdb(
        pos14: torch.Tensor,
        b_factors: torch.Tensor,
        sequence: torch.Tensor,
        mask: torch.Tensor,
        save_path: str,
        model: int = 0,
        init_chain: str = 'A'
) -> None:
    """
    saves the pos14 as a pdb file

    Args:
        pos14: the atom14 representation of the coordinates
        b_factors: the b_factors of the amino acids
        sequence: the amino acid of the pos14
        mask: the validity of the atoms
        save_path: the path to save the pdb file
        model: the model id of the pdb file
        init_chain

    return:
        the structure saved to ~save_path

    """
    builder = StructureBuilder.StructureBuilder()
    builder.init_structure(0)
    builder.init_model(model)
    builder.init_chain(init_chain)
    builder.init_seg('    ')
    for i, (aa_idx, p_res, b, m_res) in enumerate(
            zip(sequence, pos14, b_factors, mask.bool())
    ):
        if not m_res:
            continue
        aa_idx = aa_idx.item()
        p_res = p_res.clone().detach().cpu()
        if aa_idx == 21:
            continue
        try:
            three = rc.residx_to_3(aa_idx)
        except IndexError:
            continue
        builder.init_residue(three, " ", int(i), icode=" ")
        for j, (atom_name,) in enumerate(
                zip(rc.restype_name_to_atom14_names[three])
        ):
            if len(atom_name) > 0:
                builder.init_atom(
                    atom_name, p_res[j].tolist(), b.item(), 1.0, ' ',
                    atom_name.join([" ", " "]), element=atom_name[0]
                )
    structure = builder.get_structure()
    io = PDB.PDBIO()
    io.set_structure(structure)
    os.makedirs(pathlib.Path(save_path).parent, exist_ok=True)
    io.save(save_path)


def _load_weights(
        weights_url: str, weights_file: str,
) -> collections.OrderedDict:
    """
    Loads the weights from either a url or a local file. If from url,

    Args:
        weights_url: a url for the weights
        weights_file: a local file

    Returns:
        state_dict: the state dict for the model

    """

    use_cache = os.path.exists(weights_file)
    if weights_file and weights_url and not use_cache:
        logging.info(
            f"Downloading weights from {weights_url} to {weights_file}"
        )
        os.makedirs(os.path.dirname(weights_file), exist_ok=True)
        hub.download_url_to_file(weights_url, weights_file)
    else:
        logging.info(f"Loading weights from {weights_file}")

    return torch.load(weights_file, map_location='cpu')


def get_args() -> typing.Tuple[
    argparse.Namespace, collections.OrderedDict, argparse.Namespace]:
    """
    Parse the arguments, which includes loading the weights

    Returns:
        input_file: the path to the FASTA file to load sequences from.
        output_dir: the output folder directory in which the PDBs will reside.
        batch_size: the batch_size of each forward
        weights: the state dict of the model

    """
    parser = argparse.ArgumentParser(
        description=
        """
        Launch OmegaFold and perform inference on the data. 
        Some examples (both the input and output files) are included in the 
        Examples folder, where each folder contains the output of each 
        available model from model1 to model3. All of the results are obtained 
        by issuing the general command with only model number chosen (1-3).
        """
    )
    parser.add_argument(
        'input_file', type=lambda x: os.path.expanduser(str(x)),
        help=
        """
        The input fasta file
        """
    )
    parser.add_argument(
        'output_dir', type=lambda x: os.path.expanduser(str(x)),
        help=
        """
        The output directory to write the output pdb files. 
        If the directory does not exist, we just create it. 
        The output file name follows its unique identifier in the 
        rows of the input fasta file"
        """
    )
    
    parser.add_argument(
        '--num_cycle', default=10, type=int,
        help="The number of cycles for optimization, default to 10"
    )
    parser.add_argument(
        '--subbatch_size', default=None, type=int,
        help=
        """
        The subbatching number, 
        the smaller, the slower, the less GRAM requirements. 
        Default is the entire length of the sequence.
        This one takes priority over the automatically determined one for 
        the sequences
        """
    )
    parser.add_argument(
        '--device', default='cuda', type=str,
        help='The device on which the model will be running, default to cuda'
    )
    parser.add_argument(
        '--weights_file',
        default=os.path.expanduser("~/.cache/omegafold_ckpt/model.pt"),
        type=str,
        help='The model cache to run'
    )
    parser.add_argument(
        '--weights',
        default="https://helixon.s3.amazonaws.com/release1.pt",
        type=str,
        help='The url to the weights of the model'
    )
    parser.add_argument(
        '--pseudo_msa_mask_rate', default=0.12, type=float,
        help='The masking rate for generating pseudo MSAs'
    )
    parser.add_argument(
        '--num_pseudo_msa', default=15, type=int,
        help='The number of pseudo MSAs'
    )
    parser.add_argument(
        '--allow_tf32', default=True, type=hipify_python.str2bool,
        help='if allow tf32 for speed if available, default to True'
    )

    parser.add_argument(
        '--real_msa', default=False, type=hipify_python.str2bool,
        help='treat the input fasta as a real MSA file'
    )

    args = parser.parse_args()
    _set_precision(args.allow_tf32)

    weights_url = args.weights
    weights_file = args.weights_file
    # if the output directory is not provided, we will create one alongside the
    # input fasta file
    if weights_file or weights_url:
        weights = _load_weights(weights_url, weights_file)
        weights = weights.pop('model', weights)
    else:
        weights = None

    forward_config = argparse.Namespace(
        subbatch_size=args.subbatch_size,
        num_recycle=args.num_cycle,
    )

    return args, weights, forward_config


# =============================================================================
# Classes
# =============================================================================
# =============================================================================
# Tests
# =============================================================================
if __name__ == '__main__':
    pass
