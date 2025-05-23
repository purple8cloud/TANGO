# This source code is from Meta Platforms, Inc. and affiliates.
# ETRI modified it for TANGO project.

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import torch
import torch.nn as nn

from tangochat.common.models.model import Model, ModelType
from tangochat.common.models.model_config import resolve_model_config
from tangochat.utils.build_utils import (
    device_sync,
    is_cpu_device,
    is_cuda_or_cpu_device,
    name_to_dtype,
)
from tangochat.utils.measure_time import measure_time

import logging
logger = logging.getLogger(__name__)

@dataclass
class BuilderArgs:
    checkpoint_path: Optional[Union[Path, str]] = None
    checkpoint_dir: Optional[Union[Path, str]] = None
    dcp_dir: Optional[Union[Path, str]] = None
    params_path: Optional[Union[Path, str]] = None
    params_table: Optional[str] = None
    gguf_path: Optional[Union[Path, str]] = None
    gguf_kwargs: Optional[Dict[str, Any]] = None
    dso_path: Optional[Union[Path, str]] = None
    pte_path: Optional[Union[Path, str]] = None
    device: Optional[str] = None
    precision: torch.dtype = torch.float32
    setup_caches: bool = False
    use_distributed: bool = False
    is_chat_model: bool = False
    prefill_possible: bool = False
    dynamic_shapes: bool = False
    max_seq_length: Optional[int] = None

    def __post_init__(self):
        if self.device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        if not (
            (self.checkpoint_path and self.checkpoint_path.is_file())
            or (self.checkpoint_dir and self.checkpoint_dir.is_dir())
            or (self.gguf_path and self.gguf_path.is_file())
            or (self.dso_path and Path(self.dso_path).is_file())
            or (self.pte_path and Path(self.pte_path).is_file())
        ):
            # raise RuntimeError(
            #     "need to specified a valid checkpoint path, checkpoint dir, gguf path, DSO path, or PTE path"
            # )
            logger.error("Error: need to specified a valid checkpoint path, checkpoint dir, gguf path, DSO path, or PTE path")

        if self.dso_path and self.pte_path:
            # raise RuntimeError("specify either DSO path or PTE path, but not both")
            logger.error("Error: specify either DSO path or PTE path, but not both")

        if self.checkpoint_path and (self.dso_path or self.pte_path):
            logger.warning(
                "Warning: checkpoint path ignored because an exported DSO or PTE path specified"
            )
        if self.checkpoint_dir and (self.dso_path or self.pte_path):
            logger.warning(
                "Warning: checkpoint dir ignored because an exported DSO or PTE path specified"
            )
        if self.gguf_path and (self.dso_path or self.pte_path):
            logger.warning(
                "Warning: GGUF path ignored because an exported DSO or PTE path specified"
            )
        if not (self.dso_path) and not (self.pte_path):
            self.prefill_possible = True

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "BuilderArgs":
        # Handle disabled checkpoint_dir option
        checkpoint_dir = None
        if hasattr(args, "checkpoint_dir"):
            checkpoint_dir = args.checkpoint_dir
        if hasattr(args, "dcp_dir"):
            dcp_dir = args.dcp_dir

        checkpoint_path = args.checkpoint_path
        params_table = args.params_table
        if args.model:  # Using a named, well-known model
            logger.info(f"model = {args.model}")
            model_config = resolve_model_config(args.model)

            checkpoint_path = (
                Path(args.model_directory)
                / model_config.name
                / model_config.checkpoint_file
            )
            # The transformers config is keyed on the last section
            # of the name/path.
            params_table = (
                model_config.transformer_params_key or model_config.name.split("/")[-1]
            )

        dso_path = getattr(args, "dso_path", None)
        pte_path = getattr(args, "pte_path", None)

        is_chat_model = False
        if args.is_chat_model:
            is_chat_model = True
        else:
            for path in [
                checkpoint_path,
                checkpoint_dir,
                dso_path,
                pte_path,
                args.gguf_path,
            ]:
                if path is not None:
                    path = str(path)
                    if path.endswith("/"):
                        path = path[:-1]
                    if os.path.isfile(path):
                        path = os.path.dirname(path)

                    path_basename = os.path.basename(path).lower()
                    if "chat" in path_basename or "instruct" in path_basename:
                        is_chat_model = True

        output_pte_path = getattr(args, "output_pte_path", None)
        output_dso_path = getattr(args, "output_dso_path", None)
        if output_pte_path and args.dtype.startswith("fast"):
            if args.dtype == "fast":
                # As per Kimish, float32 should be faster on ET XNNPACK
                # (because fp16 is implemented as upcast to fp32 for several
                # operators, and in particular a8w4dq and ET's sdpa+kv)
                dtype = torch.float32
            else:
                dtype = torch.float16
        else:
            dtype = name_to_dtype(args.dtype, args.device)

        return cls(
            checkpoint_dir=checkpoint_dir,
            checkpoint_path=checkpoint_path,
            dcp_dir=dcp_dir,
            params_path=args.params_path,
            params_table=params_table,
            gguf_path=args.gguf_path,
            gguf_kwargs=None,
            dso_path=dso_path,
            pte_path=pte_path,
            device=args.device,
            precision=dtype,
            setup_caches=(output_dso_path or output_pte_path),
            use_distributed=args.distributed,
            is_chat_model=is_chat_model,
            dynamic_shapes=getattr(args, "dynamic_shapes", False),
            max_seq_length=getattr(args, "max_seq_length", None),
        )

    @classmethod
    def from_speculative_args(cls, args: argparse.Namespace) -> "BuilderArgs":
        speculative_builder_args = BuilderArgs.from_args(args)
        # let's limit multi-checkpoint to checker
        speculative_builder_args.checkpoint_dir = None
        speculative_builder_args.checkpoint_path = args.draft_checkpoint_path
        speculative_builder_args.gguf_path = None
        speculative_builder_args.dso_path = None
        speculative_builder_args.pte_path = None
        return speculative_builder_args


@dataclass
class TokenizerArgs:
    tokenizer_path: Optional[Union[Path, str]] = None
    is_sentencepiece: bool = False
    is_tiktoken: bool = False
    t: Optional[Any] = None

    def __post_init__(self):
        try:
            from tokenizer.tiktoken import Tokenizer as TiktokenTokenizer
            logger.info(f"try to use tiktoken as tokenizer...")
            self.t = TiktokenTokenizer(model_path=str(self.tokenizer_path))
            self.is_tiktoken = True
            self.is_sentencepiece = False
            return
        except Exception as e:
            logger.warning(f"tiktoken tokenizer: exception: {e}")
            pass

        try:
            from sentencepiece import SentencePieceProcessor
            logger.info(f"try to use sentencepiece as tokenizer...")
            self.t = SentencePieceProcessor(model_file=str(self.tokenizer_path))
            self.is_tiktoken = False
            self.is_sentencepiece = True
            return
        except Exception as e:
            logger.warning(f"sentence piece processor: exception: {e}")
            pass

        self.is_tiktoken = False
        self.is_sentencepiece = False
        self.t = None
        return

    def validate_model(
        self,
        model: Optional[Model],
        model_description: str = "model",
    ) -> None:
        if model is None:
            return

        logger.info(f"is tiktoken? {self.is_tiktoken}")
        logger.info(f"is sentence piece? {self.is_sentencepiece}")

        if self.is_tiktoken == self.is_sentencepiece:
            raise RuntimeError(f"no tokenizer was found at {self.tokenizer_path}")

        is_tiktoken = self.is_tiktoken
        is_sentencepiece = self.is_sentencepiece
        use_tiktoken = model.config.use_tiktoken

        if not (is_tiktoken == use_tiktoken) or not (is_sentencepiece != use_tiktoken):
            raise RuntimeError(
                f"model-specified tokenizer ({tokenizer_setting_to_name(use_tiktoken)}) does not match provided tokenizer ({tokenizer_setting_to_name(is_tiktoken)}) for {model_description}"
            )

        return

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "TokenizerArgs":
        """
        Create a TokenizerArgs object from command line arguments.
        Specifically, `tokenizer_path` is resolved with precedence:
          * From Explicitly provided tokenizer_path
          * Resolve via model_config identified by args.model
          * Look in the directory of args.checkpoint_path for tokenizer.model
          * Look in the directory of args.checkpoint_dir for tokenizer.model

        Args:
            args (argparse.Namespace): The command line arguments.

        Returns:
            TokenizerArgs: A TokenizerArgs object.
        """
        if args.tokenizer_path:
            tokenizer_path = args.tokenizer_path
        elif args.model:  # Using a named, well-known model
            model_config = resolve_model_config(args.model)
            tokenizer_path = (
                Path(args.model_directory)
                / model_config.name
                / model_config.tokenizer_file
            )
        elif args.checkpoint_path:
            tokenizer_path = args.checkpoint_path.parent / "tokenizer.model"
        elif hasattr(args, "checkpoint_dir") and args.checkpoint_dir:
            tokenizer_path = args.checkpoint_dir / "tokenizer.model"
        else:
            # raise RuntimeError("cannot find tokenizer model")
            logger.error("Error: cannot find tokenizer model")

        logger.info(f"Tokenizer path = {tokenizer_path}")

        if not tokenizer_path.is_file():
            raise RuntimeError(
                f"did not find tokenizer at {os.path.abspath(tokenizer_path)}"
            )

        return cls(tokenizer_path=tokenizer_path)


def _initialize_tokenizer(tokenizer_args: TokenizerArgs):
    return tokenizer_args.t


def _set_gguf_kwargs(builder_args: BuilderArgs, is_et: bool, context: str) -> None:
    assert context in ["export", "generate"]
    assert builder_args.gguf_kwargs is None

    if builder_args.gguf_path is None:
        print("No gguf_path provided, so ignoring set_gguf_kwargs.")
        return

    builder_args.gguf_kwargs = {}
    if is_et:
        builder_args.gguf_kwargs["load_as_quantized"] = False


def _unset_gguf_kwargs(builder_args: BuilderArgs) -> None:
    builder_args.gguf_kwargs = None


def _init_model_on_meta_device(builder_args: BuilderArgs) -> Model:
    with torch.device("meta"):
        if builder_args.params_path:
            return Model.from_params(builder_args.params_path)
        elif builder_args.params_table:
            return Model.from_table(builder_args.params_table)
        else:
            return Model.from_name(builder_args.checkpoint_path.parent.name)


def _init_model(builder_args: BuilderArgs) -> Model:
    if builder_args.params_path:
        return Model.from_params(builder_args.params_path)
    elif builder_args.params_table:
        return Model.from_table(builder_args.params_table)
    else:
        return Model.from_name(builder_args.checkpoint_path.parent.name)    


def _load_model_gguf(builder_args: BuilderArgs) -> Model:
    assert builder_args.gguf_path
    if builder_args.gguf_kwargs is None:
        kwargs = {}
    else:
        kwargs = builder_args.gguf_kwargs
    model = Model.from_gguf(builder_args.gguf_path, **kwargs)
    return model


def _load_model_default(builder_args: BuilderArgs) -> Model:
    assert not builder_args.gguf_path

    # model: Model = _init_model_on_meta_device(builder_args)
    model: Model = _init_model(builder_args)

    if builder_args.params_table and builder_args.params_table.endswith("Tune"):
        logger.info("Loading Tune checkpoint")
        meta_checkpoint = torch.load(
            str(builder_args.checkpoint_path), mmap=True, weights_only=True
        )
        checkpoint = meta_to_tune(meta_checkpoint)
    elif builder_args.checkpoint_dir is not None:
        # Load multiple checkpoint; ignore the single path.
        builder_args.checkpoint_path = None
        cps = []
        for i in range(4):
            cp_name = f"consolidated.{i}.pth"
            logger.info(f"Loading {cp_name}")
            cps.append(
                torch.load(
                    os.path.join(builder_args.checkpoint_dir, cp_name),
                    map_location=builder_args.device,
                    mmap=True,
                )
            )
        checkpoint = {}
        for key in cps[0].keys():
            if not torch.allclose(cps[0][key], cps[1][key]):
                values = (cps[0][key], cps[1][key], cps[2][key], cps[3][key])
                if key.endswith("wo.weight") or key.endswith("w2.weight"):
                    checkpoint[key] = torch.cat(values, dim=1)
                else:
                    checkpoint[key] = torch.cat(values, dim=0)
            else:
                checkpoint[key] = cps[0][key]
    else:
        logger.info(f"checkpoint path = {builder_args.checkpoint_path}")
        checkpoint = torch.load(
            str(builder_args.checkpoint_path),
            map_location=builder_args.device,
            # mmap=True, # [tenace] mmap is not supported in torch 1.13
            weights_only=True,
        )

    logger.info(f"====== checkpoint is loaded ======")
    if "model" in checkpoint and "stories" in str(builder_args.checkpoint_path):
        checkpoint = checkpoint["model"]

    for k, v in checkpoint.items():
        logger.info(f"\t{k}")

    if model.config.model_type == ModelType.Flamingo:
        # TODO: Refactor this. For now, overwrite the model with model loaded from params_path
        with set_default_dtype(builder_args.precision), torch.device(
            builder_args.device
        ):
            model = Model.from_params(builder_args.params_path)
        state_dict = flamingo_meta_to_tune(checkpoint)
        model.model.load_state_dict(state_dict)
    else:
        checkpoint = {"model." + k: v for k, v in checkpoint.items()}
        model.load_state_dict(checkpoint, 
                            #   assign=True, [tenace] assign is not supported in torch 1.13 
                              strict=True)

    return model


def _load_model(builder_args: BuilderArgs) -> Model:
    # world_mesh, parallel_dims = _maybe_init_distributed(builder_args)
    if builder_args.gguf_path:
        model = _load_model_gguf(builder_args)
    elif builder_args.use_distributed:
        model = _init_model_on_meta_device(builder_args)
    else:
        model = _load_model_default(builder_args)
    # model = _maybe_parellelize_model(model, builder_args, world_mesh, parallel_dims)

    model = model.to(device=builder_args.device, dtype=builder_args.precision)
    return model.eval()


def _initialize_model(
    builder_args: BuilderArgs,
    quantize,
    tokenizer=None,
    max_seq_length=None,
    support_tensor_subclass: bool = True,
) -> Model:
    logging.info("Loading model...")

    if builder_args.gguf_path and (builder_args.dso_path or builder_args.pte_path):
        logger.info("Setting gguf_kwargs for generate.")
        is_dso = builder_args.dso_path is not None
        is_pte = builder_args.pte_path is not None
        assert not (is_dso and is_pte)
        assert builder_args.gguf_kwargs is None
        # TODO: make GGUF load independent of backend
        # currently not working because AVX int_mm broken
        #   (no unpack available)
        _set_gguf_kwargs(builder_args, is_et=is_pte, context="generate")

    if builder_args.dso_path:
        if not is_cuda_or_cpu_device(builder_args.device):
            logger.warning(
                f"Cannot load specified DSO to {builder_args.device}. Attempting to load model to CPU instead"
            )
            builder_args.device = "cpu"

        # assert (
        #     quantize is None or quantize == "{ }"
        # ), "quantize not valid for exported DSO model. Specify quantization during export."

        with measure_time("Time to load model: {time:.02f} seconds"):
            model = _load_model(builder_args)
            device_sync(device=builder_args.device)

        try:
            # Replace model forward with the AOT-compiled forward
            # This is a hacky way to quickly demo AOTI's capability.
            # model is still a Python object, and any mutation to its
            # attributes will NOT be seen on by AOTI-compiled forward
            # function, e.g. calling model.setup_cache will NOT touch
            # AOTI compiled and maintained model buffers such as kv_cache.
            model.forward = torch._export.aot_load(
                str(builder_args.dso_path.absolute()), builder_args.device
            )
        except:
            raise RuntimeError(f"Failed to load AOTI compiled {builder_args.dso_path}")
    elif builder_args.pte_path:
        if not is_cpu_device(builder_args.device):
            logger.warning(
                f"Cannot load specified PTE to {builder_args.device}. Attempting to load model to CPU instead"
            )
            builder_args.device = "cpu"

        # assert (
        #     quantize is None or quantize == "{ }"
        # ), "quantize not valid for exported PTE model. Specify quantization during export."

        with measure_time("Time to load model: {time:.02f} seconds"):
            model = _load_model(builder_args)
            device_sync(device=builder_args.device)

        try:
            from common.models.model import PTEModel
            model = PTEModel(model.config, builder_args.pte_path)
        except Exception:
            logger.error(f"Error: Failed to load ET compiled {builder_args.pte_path}")
            # raise RuntimeError(f"Failed to load ET compiled {builder_args.pte_path}")
    else:
        with measure_time("Time to load model: {time:.02f} seconds"):
            model = _load_model(builder_args)
            device_sync(device=builder_args.device)

        # if quantize:
        #     logger.info(f"Quantizing the model with: {quantize}")
        #     with measure_time("Time to quantize model: {time:.02f} seconds"):
        #         quantize_model(
        #             model,
        #             builder_args.device,
        #             quantize,
        #             tokenizer,
        #             support_tensor_subclass,
        #         )
        #         device_sync(device=builder_args.device)

        if builder_args.setup_caches:
            with torch.device(builder_args.device):
                model.setup_caches(
                    max_batch_size=1,
                    max_seq_length=max_seq_length
                    or model.text_transformer_args.max_seq_length,
                )

        model.to(dtype=builder_args.precision)

    logger.info("-----------------------------------------------------------")
    return model


def tokenizer_setting_to_name(tiktoken: bool = False) -> str:
    return "TikToken" if tiktoken else "SentencePiece"
