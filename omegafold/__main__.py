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
The main function to run the prediction
"""
# =============================================================================
# Imports
# =============================================================================
import gc
import logging
import os
import sys
import time

import torch

import omegafold as of
from . import pipeline


# =============================================================================
# Functions
# =============================================================================

@torch.no_grad()
def main():
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    args, state_dict, forward_config = pipeline.get_args()
    # create the output directory
    os.makedirs(args.output_dir, exist_ok=True)
    # get the model
    logging.info(f"Constructing OmegaFold")
    model = of.OmegaFold(of.make_config(args.model))
    if state_dict is None:
        logging.warning("Inferencing without loading weight")
    else:
        if "model" in state_dict:
            state_dict = state_dict.pop("model")
        model.load_state_dict(state_dict)
    model.eval()
    model.to(args.device)

    logging.info(f"Reading {args.input_file}")
    for i, (input_data, save_path) in enumerate(
            pipeline.fasta2inputs(
                args.input_file,
                num_pseudo_msa=args.num_pseudo_msa,
                output_dir=args.output_dir,
                device=args.device,
                mask_rate=args.pseudo_msa_mask_rate,
                num_cycle=args.num_cycle,
            )
    ):
        logging.info(f"Predicting {i + 1}th chain in {args.input_file}")
        logging.info(
            f"{len(input_data[0]['p_msa'][0])} residues in this chain."
        )
        ts = time.time()
        try:
            output = model(
                    input_data,
                    predict_with_confidence=True,
                    fwd_cfg=forward_config
                )
        except RuntimeError as e:
            logging.info(f"Failed to generate {save_path} due to {e}")
            logging.info(f"Skipping...")
            continue
        logging.info(f"Finished prediction in {time.time() - ts:.2f} seconds.")

        logging.info(f"Saving prediction to {save_path}")
        pipeline.save_pdb(
            pos14=output["final_atom_positions"],
            b_factors=output["confidence"] * 100,
            sequence=input_data[0]["p_msa"][0],
            mask=input_data[0]["p_msa_mask"][0],
            save_path=save_path,
            model=0
        )
        logging.info(f"Saved")
        del output
        torch.cuda.empty_cache()
        gc.collect()
    logging.info("Done!")


# =============================================================================
# Tests
# =============================================================================
if __name__ == '__main__':
    main()
