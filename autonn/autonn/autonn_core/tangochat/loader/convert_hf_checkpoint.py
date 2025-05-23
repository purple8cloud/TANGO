# This source code is from Meta Platforms, Inc. and affiliates.
# ETRI modified it for TANGO project.
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import torch

from common.models.model import TransformerArgs
from common.models.model import ModelArgs

import logging
logger = logging.getLogger(__name__)

import streamlit as st

@torch.inference_mode()
def convert_hf_checkpoint(
    *,
    model_dir: Optional[Path] = None,
    model_name: Optional[str] = None,
    remove_bin_files: bool = False,
) -> None:
    if model_dir is None:
        model_dir = Path("checkpoints/meta-Transformer/Transformer-2-7b-chat-hf")
    if model_name is None:
        model_name = model_dir.name

    config_args = ModelArgs.from_name(model_name).transformer_args['text']
    config = TransformerArgs.from_params(config_args)
    logger.info(f"Model config") # {config.__dict__}")
    # st.write(f"--Transformer Model Configuration--")
    for k, v in config.__dict__.items():
        logger.info(f"   {k} : {v}")
        # st.write(f"\t{k:>20}:{v}")

    # Load the json file containing weight mapping
    model_map_json = model_dir / "pytorch_model.bin.index.json"
    logger.info(f"Weight mapping file: {model_map_json}")
    st.write(f"Weights mapped though {model_map_json}")

    # If there is no weight mapping, check for a consolidated model and
    # tokenizer we can move. Llama 2 and Mistral have weight mappings, while
    # Llama 3 has a consolidated model and tokenizer.
    # Otherwise raise an error.
    if not model_map_json.is_file():
        consolidated_pth = model_dir / "original" / "consolidated.00.pth"
        tokenizer_pth = model_dir / "original" / "tokenizer.model"
        if consolidated_pth.is_file() and tokenizer_pth.is_file():
            # Confirm we can load it
            loaded_result = torch.load(
                str(consolidated_pth), map_location="cpu", 
                # mmap=True, 
                weights_only=True
            )
            del loaded_result  # No longer needed
            logger.info(f"Moving checkpoint to {model_dir / 'model.pth'}.")
            os.rename(consolidated_pth, model_dir / "model.pth")
            os.rename(tokenizer_pth, model_dir / "tokenizer.model")
            logger.info("Done.")
            return
        else:
            raise RuntimeError(
                f"Could not find {model_map_json} or {consolidated_pth} plus {tokenizer_pth}"
            )

    with open(model_map_json) as json_map:
        bin_index = json.load(json_map)

    weight_map = {
        "model.embed_tokens.weight": "tok_embeddings.weight",
        "model.layers.{}.self_attn.q_proj.weight": "layers.{}.attention.wq.weight",
        "model.layers.{}.self_attn.k_proj.weight": "layers.{}.attention.wk.weight",
        "model.layers.{}.self_attn.v_proj.weight": "layers.{}.attention.wv.weight",
        "model.layers.{}.self_attn.o_proj.weight": "layers.{}.attention.wo.weight",
        "model.layers.{}.self_attn.rotary_emb.inv_freq": None,
        "model.layers.{}.mlp.gate_proj.weight": "layers.{}.feed_forward.w1.weight",
        "model.layers.{}.mlp.up_proj.weight": "layers.{}.feed_forward.w3.weight",
        "model.layers.{}.mlp.down_proj.weight": "layers.{}.feed_forward.w2.weight",
        "model.layers.{}.input_layernorm.weight": "layers.{}.attention_norm.weight",
        "model.layers.{}.post_attention_layernorm.weight": "layers.{}.ffn_norm.weight",
        "model.norm.weight": "norm.weight",
        "lm_head.weight": "output.weight",
    }
    bin_files = {model_dir / bin for bin in bin_index["weight_map"].values()}

    def permute(w, n_heads):
        dim = config.dim
        return (
            w.view(n_heads, 2, config.head_dim // 2, dim)
            .transpose(1, 2)
            .reshape(config.head_dim * n_heads, dim)
        )

    merged_result = {}
    for file in sorted(bin_files):
        state_dict = torch.load(
            str(file), 
            map_location="cpu", 
            # mmap=True, 
            weights_only=True
        )
        merged_result.update(state_dict)
    final_result = {}
    for key, value in merged_result.items():
        if "layers" in key:
            abstract_key = re.sub(r"(\d+)", "{}", key)
            layer_num = re.search(r"\d+", key).group(0)
            new_key = weight_map[abstract_key]
            if new_key is None:
                continue
            new_key = new_key.format(layer_num)
        else:
            new_key = weight_map[key]

        final_result[new_key] = value

    for key in tuple(final_result.keys()):
        if "wq" in key:
            q = final_result[key]
            k = final_result[key.replace("wq", "wk")]
            v = final_result[key.replace("wq", "wv")]
            q = permute(q, config.n_heads)
            k = permute(k, config.n_local_heads)
            final_result[key.replace("wq", "wqkv")] = torch.cat([q, k, v])
            del final_result[key]
            del final_result[key.replace("wq", "wk")]
            del final_result[key.replace("wq", "wv")]
    logger.info(f"Saving checkpoint to {model_dir / 'model.pth'}. This may take a while.")
    torch.save(final_result, model_dir / "model.pth")
    logger.info("Done.")

    if remove_bin_files:
        logger.info(f"Delete binary files:")
        st.write("Delete binary files:")
        for file in bin_files:
            logger.info(f"   {str(file)}")
            st.write(f"\t{str(file)}")
            os.remove(file)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Convert Hugging Face checkpoint.")
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=Path("checkpoints/meta-llama/llama-2-7b-chat-hf"),
    )
    parser.add_argument("--model-name", type=str, default=None)

    args = parser.parse_args()
    convert_hf_checkpoint(
        model_dir=args.checkpoint_dir,
        model_name=args.model_name,
    )
