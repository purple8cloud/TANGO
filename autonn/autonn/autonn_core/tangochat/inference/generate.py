# This source code is from Meta Platforms, Inc. and affiliates.
# ETRI modified it for TANGO project.

import argparse
import itertools
import logging
import os
import textwrap
import time

from abc import ABC, abstractmethod
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import torch
# import torch._dynamo.config
# import torch._inductor.config

import logging
logger = logging.getLogger(__name__)

try:
    from _torchchat_test_script import flamingo_transform, padded_collate
except ImportError:
    pass

from PIL import Image

from .builder import (
    _initialize_model,
    _initialize_tokenizer,
    BuilderArgs,
    TokenizerArgs,
)
from common.models.model import Model, ModelType
from utils.build_utils import device_sync, set_precision
from utils.device_info import get_device_info

# torchtune model definition dependencies
# from torchtune.data import Message
# from torchtune.generation._generation import sample as tune_sample
# from torchtune.models.llama3 import llama3_tokenizer
# from torchtune.training import set_default_dtype

import streamlit as st

class _ChatFormatter(ABC):
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    @abstractmethod
    def encode_dialog_prompt(self, dialog) -> List[int]:
        raise NotImplementedError()


class Llama3ChatFormatter(_ChatFormatter):
    """Format a chat prompt using special tokens to demarcate roles and messages.

    Refer to the LLaMA3 documentation for more details https://llama.meta.com/docs/model-cards-and-prompt-formats/meta-llama-3

    """

    def encode_header(self, role) -> List[int]:
        tokens = []
        tokens.append(self.tokenizer.special_tokens["<|start_header_id|>"])
        tokens.extend(self.tokenizer.encode(role, bos=False, eos=False))
        tokens.append(self.tokenizer.special_tokens["<|end_header_id|>"])
        tokens.extend(self.tokenizer.encode("\n\n", bos=False, eos=False))
        return tokens

    def encode_message(self, message) -> List[int]:
        tokens = self.encode_header(message["role"])
        if type(message["content"]) is str:
            tokens.extend(
                self.tokenizer.encode(message["content"], bos=False, eos=False)
            )
        elif type(message["content"]) is list:
            for content in message["content"]:
                if content["type"] == "text":
                    tokens.extend(
                        self.tokenizer.encode(content["text"], bos=False, eos=False)
                    )

        tokens.append(self.tokenizer.special_tokens["<|eot_id|>"])
        return tokens

    def encode_dialog_prompt(self, dialog) -> List[int]:
        tokens = []
        tokens.append(self.tokenizer.special_tokens["<|begin_of_text|>"])
        for message in dialog:
            tokens.extend(self.encode_message(message))
        # Add the start of an assistant message for the model to complete.
        tokens.extend(self.encode_header("assistant"))  # Pass role directly as a string
        return tokens


B_INST, E_INST = "[INST]", "[/INST]"
B_SYS, E_SYS = "<<SYS>>", "<</SYS>>"


class Llama2ChatFormatter(_ChatFormatter):
    def encode_dialog_prompt(self, dialog) -> List[int]:
        tokens = self.tokenizer.encode(f"{B_INST} ")
        first_message = True  # Bool to handle placing the B_INST token. Behavior is weird - the system prompt should have the B_INST, but not the first user message. All following user messages *should* have it. Also, if there is no system prompt, then the user message should have it.
        for message in dialog:
            content = message["content"].strip()
            if message["role"] == "system":
                encoded = self.tokenizer.encode(f"{B_SYS}\n{content}\n{E_SYS}")
                first_message = False
            elif message["role"] == "user":
                encoded = [self.tokenizer.bos_id()] + self.tokenizer.encode(
                    f"{B_INST if first_message else ''} {content} {E_INST} "
                )
                first_message = True
            elif message["role"] == "assistant":
                encoded = self.tokenizer.encode(f"{content}\n\n") + [
                    self.tokenizer.eos_id()
                ]
            tokens += encoded
        return tokens


@dataclass
class GeneratorArgs:
    prompt: Optional[str] = (
        None  # When passed into the Generator, this will be used as the system prompt
    )
    encoded_prompt: Optional[torch.Tensor] = None
    image_prompts: Optional[Sequence[Union[str, PathLike, bytes]]] = (
        None  # string or Path to an image file or the raw base64 bytes of an image
    )
    chat_mode: bool = False
    gui_mode: bool = False
    num_samples: int = 1
    max_new_tokens: int = 200
    top_k: int = 200
    temperature: float = 0.0  # deterministic argmax if 0.0
    compile: bool = False
    compile_prefill: bool = False
    speculate_k: int = 5
    sequential_prefill: bool = False
    max_autotune: bool = False
    is_tune_model: bool = False

    def __post_init__(self):
        if self.compile_prefill and self.sequential_prefill:
            raise RuntimeError("prefill compilation requires parallel prefill")

    def validate_build(
        self, builder_args: BuilderArgs, model_description: str = "model"
    ):
        reason = ""
        model_type = ""
        if not self.sequential_prefill:
            reason = "parallel prefill"
        if self.compile_prefill:
            reason = "model compilation for prefill"
        if self.compile:
            reason = "model compilation"
        if builder_args.dso_path:
            model_type = "DSO"
        if builder_args.pte_path:
            model_type = "PTE"
        if model_type and reason:
            raise RuntimeError(
                f"cannot perform {reason} because a {model_type} {model_description} is used"
            )

    @classmethod
    def from_args(cls, args):
        dso_path = getattr(args, "dso_path", None)
        pte_path = getattr(args, "pte_path", None)
        sequential_prefill = args.sequential_prefill or bool(dso_path) or bool(pte_path)

        # Validate that all image prompts exist before expensive model load
        if image_prompts := getattr(args, "image_prompts", None):
            if not all(os.path.exists(image_prompt) for image_prompt in image_prompts):
                raise RuntimeError(f"Image prompt {image_prompt} does not exist")

        return cls(
            prompt=getattr(args, "prompt", ""),
            encoded_prompt=None,
            image_prompts=image_prompts,
            chat_mode=args.chat,
            gui_mode=args.gui,
            num_samples=getattr(args, "num_samples", 1),
            max_new_tokens=args.max_new_tokens,
            top_k=args.top_k,
            temperature=args.temperature,
            compile=args.compile,
            compile_prefill=args.compile_prefill,
            speculate_k=args.speculate_k,
            sequential_prefill=sequential_prefill,
            max_autotune=args.max_autotune,
            is_tune_model=args.model and args.model.endswith("tune"),
        )


class Generator:
    """
    Generates text samples based on a pre-trained Transformer model and tokenizer.
    Args:
        builder_args: Defines the model configuration
        speculative_builder_args: Defines the speculative model configuration for speculative decode
        tokenizer_args: Defines the tokenizer configuration for both the model and speculative model
        generator_args: Controls the generation parameters
        profile: A Path to a directory where the profiling results will be stored, if enabled.
        quantize: If True, quantize the model. Please refer to docs/quantization.md for details.
        draft_quantize: If True, quantize the draft model.
    """

    def __init__(
        self,
        builder_args: BuilderArgs,
        speculative_builder_args: BuilderArgs,
        tokenizer_args: TokenizerArgs,
        generator_args: GeneratorArgs,
        profile: Optional[Path],
        quantize: bool,
        draft_quantize: bool,
    ):
        ''' [tenace] torch._inductor reuires PyTorch 2.0 or later
        torch._inductor.config.coordinate_descent_tuning = (
            False if builder_args.device == "cpu" else True
        )
        torch._inductor.config.triton.unique_kernel_names = True
        torch._inductor.config.fx_graph_cache = True  # Experimental feature to reduce compilation times, will be on by default in future
        '''

        ''' [tenace] print arguements for debugging'''
        from dataclasses import asdict
        builder_args_dict = asdict(builder_args)
        logger.info(f"builder args:")
        for k, v in builder_args_dict.items():
            logger.info(f"\t{k}: {v}")

        logger.info(f"speculative builder args:")
        speculative_builder_args_dict = asdict(speculative_builder_args)
        for k, v in speculative_builder_args_dict.items():
            logger.info(f"\t{k}: {v}")

        logger.info(f"tokenizer args:")
        tokenizer_args_dict = asdict(tokenizer_args)
        for k, v in tokenizer_args_dict.items():
            logger.info(f"\t{k}: {v}")

        logger.info(f"generator args:")
        generator_args_dict = asdict(generator_args)
        for k, v in generator_args_dict.items():
            logger.info(f"\t{k}: {v}")
        ''''''

        self.builder_args = builder_args
        self.speculative_builder_args = speculative_builder_args
        self.tokenizer_args = tokenizer_args
        self.profile = profile
        self.quantize = quantize
        self.draft_quantize = draft_quantize
        self.is_tune_model = generator_args.is_tune_model
        self.dtype = builder_args.precision

        # global print
        #    from tp import maybe_init_dist
        #    rank = maybe_init_dist()
        # use_distributed = False
        self.rank: Optional[int] = None
        #    if use_distributed:
        #        if rank != 0:
        #            # only print on rank 0
        #            print = lambda *args, **kwargs: None

        logger.info(
            f"Using device={self.builder_args.device} {get_device_info(self.builder_args.device)}"
        )
        set_precision(self.builder_args.precision)
        if builder_args.use_distributed:
            device = torch.device(f"cuda:{int(os.environ['LOCAL_RANK'])}")
            torch.cuda.set_device(device)
        self.is_speculative = self.speculative_builder_args.checkpoint_path is not None

        if generator_args.chat_mode and not self.builder_args.is_chat_model:
            # fmt: off
            logger.warning(textwrap.dedent(
                """
                *******************************************************
                This model is not known to support the chat function
                and may produce nonsensical or false output.
                *******************************************************
                """
            ))
            # fmt: on
            # raise RuntimeError("You need to use --is-chat-model to indicate model has chat support.")
        self.system_prompt = generator_args.prompt
        self.tokenizer = _initialize_tokenizer(self.tokenizer_args)

        # Right now the assumption is only llama3 uses tiktokenizer and it
        # must use tiktokenizer.
        # Piggy backing off of this flag then for now to identify llama3
        # without prompting user.
        self.is_llama3_model = self.tokenizer_args.is_tiktoken
        if self.is_llama3_model:
            self.chat_formatter = Llama3ChatFormatter(self.tokenizer)
            if generator_args.chat_mode:
                logging.debug(
                    "Llama3 model detected in chat mode. Using updated sentence schemas"
                )
        else:
            self.chat_formatter = Llama2ChatFormatter(self.tokenizer)

        self.builder_args.setup_caches = False
        self.model = _initialize_model(self.builder_args, self.quantize, self.tokenizer)

        if self.is_speculative:
            self.draft_model = _initialize_model(
                self.speculative_builder_args,
                (
                    self.quantize
                    if self.draft_quantize == "quantize"
                    else self.draft_quantize
                ),
                self.tokenizer,
            )
        else:
            self.draft_model = None

        # torchtune model does not contain essential info for validation
        # TODO: refactor model config to be more generic
        if not self.is_torchtune_model:
            self.tokenizer_args.validate_model(self.model)
        self.tokenizer_args.validate_model(self.draft_model, "draft model")
        generator_args.validate_build(self.builder_args)
        generator_args.validate_build(self.speculative_builder_args, "draft model")

    def multinomial_sample_one_no_sync(
        self,
        probs_sort,
    ):  # Does multinomial sampling without a cuda synchronization
        q = torch.empty_like(probs_sort).exponential_(1)
        return torch.argmax(probs_sort / q, dim=-1, keepdim=True).to(dtype=torch.int)

    def logits_to_probs(
        self, logits, temperature: float = 1.0, top_k: Optional[int] = None
    ):
        logits = logits / max(
            temperature, 1e-5 if logits.dtype != torch.float16 else 1e-3
        )

        if top_k is not None:
            v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
            pivot = v.select(-1, -1).unsqueeze(-1)
            logits = torch.where(logits < pivot, -float("Inf"), logits)
        probs = torch.nn.functional.softmax(logits, dim=-1)
        return probs

    def sample(
        self,
        logits,
        need_probs: bool,
        temperature: float = 0,
        top_k: Optional[int] = None,
    ):
        if temperature == 0 and not need_probs:
            _, idx_next = torch.topk(logits[0, -1], k=1, dim=-1)
            return (idx_next, None)
        probs = self.logits_to_probs(logits[0, -1], temperature, top_k)
        idx_next = self.multinomial_sample_one_no_sync(probs)
        return idx_next, probs

    def prefill(
        self,
        model: Model,
        x: torch.Tensor,
        input_pos: torch.Tensor,
        batch: Optional[Dict[str, Any]] = None,  # Inputs for multimodal models
        *,
        sequential_prefill=True,
        **sampling_kwargs,
    ) -> torch.Tensor:
        # logging.debug(f"x: {x}, input_pos: {input_pos}")
        width = x.size(1)
        assert input_pos.size(0) == width
        if batch is not None:
            # TODO: Verify sequential prefill works with multimodal models
            logits = model(**batch)[:, -1]
            return tune_sample(logits, 0, 500)
        elif sequential_prefill:
            for i in range(width):
                x_sliced, ip_sliced = x[:, i].view(-1, 1), input_pos[i].view(-1)
                # logging.debug(f"<sliced> x: {x_sliced}, input_pos: {ip_sliced}")
                logits = model(x_sliced, ip_sliced)  # (x[:, i], input_pos[i])
        elif self.model.config.model_type == ModelType.Flamingo:
            logits = model(x)
        else:
            # input_pos: [B, S]
            logits = model(x, input_pos)
            # logger.info(f"logits {logits.shape}")

        # print(f"x: {x},\n  input_pos: {input_pos}\n")
        return self.sample(logits, need_probs=False, **sampling_kwargs)[0]

    def decode_one_token(
        self,
        model: Model,
        x: torch.Tensor,
        input_pos: torch.Tensor,
        need_probs: bool,
        batch: Optional[Dict[str, Any]] = None,  # Inputs for multimodal models
        **sampling_kwargs,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        # input_pos: [B, 1]
        assert input_pos.shape[-1] == 1
        x = x.view(1, -1)
        if model.config.model_type == ModelType.Flamingo:
            if batch is not None:
                logits = model(x, encoder_mask=batch["encoder_mask"][:, -1:])
            else:
                logits = model(x)
        else:
            logits = model(x, input_pos)
        # print(f"x: {x},\n  input_pos: {input_pos}\n")
        return self.sample(logits, need_probs=need_probs, **sampling_kwargs)

    """
    Decode the next n tokens.

    Yields a tuple of (token, prob) for each token.
    """

    def decode_n_tokens(
        self,
        model: Model,
        cur_token: torch.Tensor,
        input_pos: torch.Tensor,
        num_new_tokens: int,
        need_probs: bool,
        batch=Optional[Dict[str, Any]],  # Inputs for multimodal models
        callback=lambda _: _,
        eos_token_id: int = 2,
        eot_id: Optional[int] = None,
        **sampling_kwargs,
    ):
        new_tokens, new_probs = [], []
        encountered_eos = False
        for _i in range(
            num_new_tokens - 1
        ):  # -1 to save space to run an EoS if dont generate it naturally
            # Actually better for Inductor to codegen attention here
            # with torch.nn.attention.sdpa_kernel([torch.nn.attention.SDPBackend.MATH]): # [tenace] PyTorch 2.0 or later

            out_token = cur_token.clone()
            next_token, next_prob = self.decode_one_token(
                model,
                out_token,
                input_pos,
                batch=batch,
                need_probs=need_probs,
                **sampling_kwargs,
            )
            input_pos += 1
            new_tokens.append(next_token.clone())
            callback(new_tokens[-1], done_generating=_i == num_new_tokens - 2)
            if need_probs or next_prob is None:
                yield out_token, None
            else:
                new_probs.append(next_prob.clone())
                yield out_token, next_prob.clone()
            cur_token = next_token

            # encountered eos
            if next_token.item() == eos_token_id or (
                eot_id is not None and next_token.item() == eot_id
            ):
                encountered_eos = True
                final_token, next_prob = self.decode_one_token(
                    model,
                    cur_token,
                    input_pos,
                    need_probs,
                    batch=batch,
                    **sampling_kwargs,
                )
                input_pos += 1
                break

        if not encountered_eos:
            eos_token = torch.tensor(
                [eos_token_id if eot_id is None else eot_id],
                dtype=cur_token.dtype,
                device=cur_token.device,
            )
            new_tokens.append(eos_token.clone())
            eos_token, next_prob = self.decode_one_token(
                model,
                eos_token.view(1, -1),
                input_pos,
                need_probs,
                batch=batch,
                **sampling_kwargs,
            )
            input_pos += 1
            yield eos_token.clone(), (
                next_prob.clone() if next_prob is not None else None
            )

        # return new_tokens, new_probs

    def model_forward(self, model, x, input_pos):
        return model(x, input_pos)

    def speculative_decode(
        self,
        model: Model,
        draft_model: Model,
        cur_token: torch.Tensor,
        input_pos: int,
        speculate_k: int,
        batch: Optional[Dict[str, Any]] = None,  # Inputs for multimodal models
        **sampling_kwargs,
    ) -> torch.Tensor:
        # draft model inference sequentially
        device = cur_token.device
        orig_input_pos = torch.tensor(
            [input_pos], dtype=torch.int64, device=cur_token.device
        )
        draft_tokens, draft_probs = self.decode_n_tokens(
            draft_model,
            cur_token,
            orig_input_pos.clone(),
            speculate_k,
            batch=batch,
            need_probs=True,
            **sampling_kwargs,
        )

        draft_tokens = torch.cat(draft_tokens)
        # parallel inference on target model using draft tokens
        target_logits = self.model_forward(
            model,
            torch.cat([cur_token.view(1), draft_tokens]).view(1, -1),
            torch.arange(
                input_pos, input_pos + speculate_k + 1, device=cur_token.device
            ),
        )
        target_probs = self.logits_to_probs(target_logits[0], **sampling_kwargs)
        draft_probs = torch.stack(draft_probs)
        # q: target prob, p: draft prob
        # q >= p: always accept draft token
        # q < p: q/p prob to accept draft token
        p = draft_probs[torch.arange(0, speculate_k, device=device), draft_tokens]
        q = target_probs[torch.arange(0, speculate_k, device=device), draft_tokens]
        accept_draft_prob = torch.minimum(torch.ones(()), q[:speculate_k] / p)
        rejected_locations = (
            torch.rand_like(accept_draft_prob) > accept_draft_prob
        ).nonzero()

        if rejected_locations.shape[0] == 0:  # All draft tokens have been accepted
            accept_length = speculate_k + 1
            last_token = self.multinomial_sample_one_no_sync(target_probs[-1])
            # fill last token into draft model
            self.model_forward(
                draft_model,
                draft_tokens[-1].view(1, -1),
                orig_input_pos + speculate_k,
            )
            return torch.cat([draft_tokens, last_token])
        else:
            accept_length = rejected_locations[0].item()
            p = draft_probs[accept_length]
            q = target_probs[accept_length]
            new = q - p
            new = torch.where(new > 0, new, 0.0)
            new = new / new.sum()
            next_token = self.multinomial_sample_one_no_sync(new)
            return torch.cat([draft_tokens[:accept_length], next_token])

    @torch.no_grad()
    def generate(
        self,
        model: Model,
        prompt: torch.Tensor,
        max_new_tokens: int,
        *,
        chat_mode: bool,
        batch: Optional[
            Dict[str, Any]
        ] = None,  # List of Image prompt tensors for multimodal models
        start_pos: int = 0,
        draft_model: Model,
        speculate_k: Optional[int] = 8,
        sequential_prefill=True,
        callback=lambda x: x,
        max_seq_length: int,
        seed: Optional[int] = None,
        **sampling_kwargs,
    ) -> torch.Tensor:
        """
        Takes a conditioning sequence (prompt) as input and continues to generate as many tokens as requested.
        """
        if seed:
            torch.manual_seed(seed)

        is_speculative = draft_model is not None
        device, dtype = prompt.device, prompt.dtype

        # create an empty tensor of the expected final shape and
        # fill in the current tokens
        if len(prompt.shape) > 1:
            prompt = prompt.squeeze(0)
        T = prompt.size(0)
        max_new_tokens = min(max_new_tokens, max_seq_length - start_pos - T)
        T_new = T + max_new_tokens
        # set up caches only if first inference
        if start_pos == 0:
            model = model.to(device=device)
            # with torch.device(device): #[tenace] since pytorch>=2.0, torch.device can be used as context manager
            #     if (
            #         self.is_torchtune_model
            #         or self.model.config.model_type == ModelType.Flamingo
            #     ):
            #         model.setup_caches(max_batch_size=1, dtype=self.dtype)
            #     else:
            #         model.setup_caches(max_batch_size=1, max_seq_length=max_seq_length)
            #     if is_speculative and draft_model is not model:
            #         draft_model.setup_caches(
            #             max_batch_size=1,
            #             max_seq_length=max_seq_length,
            #         )
            if (
                self.is_torchtune_model
                or self.model.config.model_type == ModelType.Flamingo
            ):
                model.setup_caches(max_batch_size=1, dtype=self.dtype)
            else:
                model.setup_caches(max_batch_size=1, max_seq_length=max_seq_length)
            if is_speculative and draft_model is not model:
                draft_model.setup_caches(
                    max_batch_size=1,
                    max_seq_length=max_seq_length,
                )
            if model.config.model_type == ModelType.Flamingo:
                model.reset_caches()

        # create an empty tensor of the expected final shape and
        # fill in the current tokens
        empty = torch.empty(T_new, dtype=dtype, device=device)
        empty[:T] = prompt

        input_pos = torch.arange(
            start_pos, T + start_pos, device=device, dtype=torch.int64 # [tenace] index tensor should be long(int64)
        )

        prefill_t0 = time.perf_counter()
        next_token = self.prefill(
            model.to(device=device),
            prompt.view(1, -1),
            input_pos,
            batch=batch,
            sequential_prefill=sequential_prefill,
            **sampling_kwargs,
        )
        if is_speculative:
            self.prefill(
                draft_model,
                prompt.view(1, -1),
                input_pos,
                sequential_prefill=sequential_prefill,
                **sampling_kwargs,
            )

        time_to_first_token = time.perf_counter() - prefill_t0
        yield None, {"time_to_first_token": time_to_first_token}
        # max_new_tokens <= 2 means we are effectively not calling decode_n_tokens().
        callback(next_token.clone().view(-1), done_generating=max_new_tokens <= 2)

        input_pos = torch.tensor([start_pos + T], device=device, dtype=torch.int)
        accept_counts = [0] * (
            speculate_k + 1
        )  # creates array of [0, 0, 0, ...] that is speculate_k + 1 long

        if is_speculative:
            input_pos = (
                input_pos.item()
            )  # for speculative decoding easier to keep on host
            while input_pos < max_new_tokens - 1:
                cur_token = next_token.view(())

                next_tokens = self.speculative_decode(
                    model,
                    draft_model,
                    cur_token,
                    input_pos,
                    speculate_k,
                    batch=batch,
                    **sampling_kwargs,
                )

                accept_counts[len(next_tokens) - 1] += 1
                num_added = min(T_new - input_pos - 1, len(next_tokens))
                for token in next_tokens[:num_added,]:
                    callback(token)
                    yield token, None
                input_pos = input_pos + num_added
                next_token = next_tokens[-1]
        else:
            generated_tokens = []
            for generated_token, _ in self.decode_n_tokens(
                model,
                next_token,
                input_pos,
                max_new_tokens - 1,
                batch=batch,
                callback=callback,
                need_probs=False,
                eos_token_id=self.tokenizer.eos_id() if self.tokenizer else 2,
                eot_id=(
                    self.tokenizer.special_tokens["<|eot_id|>"]
                    if self.is_llama3_model
                    else None
                ),
                **sampling_kwargs,
            ):
                generated_tokens.append(generated_token.view(-1))
                yield generated_token, None

        generate_stats = {
            "accept_counts": accept_counts,
        }
        yield None, generate_stats

    def encode_tokens(self, string, bos=True, device="cpu"):
        tokens = self.tokenizer.encode(string)
        if bos:
            tokens = [self.tokenizer.bos_id()] + tokens
        logger.debug(f"Size after encode_tokens: {len(tokens)}")
        return torch.tensor(tokens, dtype=torch.int, device=device)

    def _callback(self, x, *, buffer, done_generating):
        # TODO: Refactor this callback to only include basic functionality & remove print statements
        period_id = self.tokenizer.encode(".")[0]
        buffer.append(
            self.tokenizer.decode([period_id] + x.tolist())[1:]
        )  # I think this results in the first output token being dropped from the display which is wrong.
        if x.item() == self.tokenizer.eos_id():
            done_generating = True
        if (
            self.is_llama3_model
            and x.item() == self.tokenizer.special_tokens["<|eot_id|>"]
        ):
            done_generating = True
            buffer = buffer[:-1]  # drop the eot_id from the output buffer
        if len(buffer) == 4 or done_generating:
            print("".join(buffer), end="", flush=True)
            buffer.clear()
        # print(, end='', flush=True)

    def chat(
        self,
        generator_args: GeneratorArgs,
    ):
        if generator_args.chat_mode:
            logger.info("Starting Interactive Chat")

        if generator_args.image_prompts is not None:
            logger.info("Image prompts", generator_args.image_prompts)

            # Support for just the first image prompt for now
            images = [Image.open(generator_args.image_prompts[0])]
            messages = [
                Message(
                    role="user",
                    content=[
                        {"type": "image", "content": images[0]},
                        {"type": "text", "content": generator_args.prompt},
                    ],
                    eot=True,
                ),
                Message(role="assistant", content=""),
            ]

            transform = flamingo_transform(str(self.tokenizer_args.tokenizer_path))
            data = transform({"messages": messages}, inference=True)
            batch = padded_collate([data], self.builder_args.device)
            batch.pop("mask")
            encoded = batch["tokens"]

        else:
            logger.info(f"user msg: {generator_args.prompt}")
            encoded = self.encode_tokens(
                generator_args.prompt, bos=True, device=self.builder_args.device
            )
            # logger.info(f"encoded tensor = {encoded}")
            batch = None
        # model_size = sum(
        #     [
        #         p.numel() * p.dtype.itemsize
        #         for p in itertools.chain(self.model.parameters(), self.model.buffers())
        #     ]
        # )
        if generator_args.compile:
            if (
                self.is_speculative and self.builder_args.use_distributed
            ):  # and ("cuda" in builder_args.device):
                torch._inductor.config.triton.cudagraph_trees = (
                    False  # Bug with cudagraph trees in this case
                )

            if self.builder_args.device == "cpu":
                if generator_args.max_autotune:
                    kwargs = {"mode": "max-autotune"}
                else:
                    kwargs = {}
            else:
                kwargs = {"mode": "reduce-overhead"}

            if self.is_speculative:
                self.model_forward = torch.compile(
                    self.model_forward, fullgraph=True, **kwargs
                )

            logger.info("do torch.compile()")
            self.decode_one_token = torch.compile(
                self.decode_one_token, fullgraph=True, **kwargs
            )

            if generator_args.compile_prefill:
                self.prefill = torch.compile(
                    self.prefill, fullgraph=True, dynamic=True, **kwargs
                )

        self.system_prompt = None
        # Set up our max_seq_length

        # This is a hack to get around the fact that different models have different ways to record their max_seq_length and might be wrong
        # TODO: unify the max_seq_length config representation.
        text_transformer_args = self.model.text_transformer_args
        max_seq_length = (
            text_transformer_args.max_seq_length if text_transformer_args else 2048
        )
        if generator_args.chat_mode:
            logger.info(
                f"Entering Chat Mode. Will continue chatting back and forth with the language model until the models max context length of {max_seq_length} tokens is hit or until the user says /bye"
            )
            get_system_prompt = input(
                "Do you want to enter a system prompt? Enter y for yes and anything else for no. \n"
            )
            if get_system_prompt == "y" or get_system_prompt == "Y":
                self.system_prompt = input("What is your system prompt? \n")

        elif not generator_args.is_torchtune_model:
            max_seq_length = min(
                encoded.size(0) + generator_args.max_new_tokens,
                (
                    text_transformer_args.block_size
                    if text_transformer_args is not None
                    else 2048
                ),
            )
        max_seq_length = (
            max_seq_length + self.speculative_builder_args.speculate_k + 1
            if self.draft_model is not None
            else max_seq_length
        )
        aggregate_metrics = {
            "tokens_per_sec": [],
            "first_token_per_sec": [],
            "next_tokens_per_sec": [],
            "accept_counts": [],
        }
        start_pos = 0
        # arbitrarily large number as chat mode goes until max_seq length
        # or user exits
        num_samples = (
            generator_args.num_samples if not generator_args.chat_mode else 100000
        )
        for i in range(num_samples):
            device_sync(device=self.builder_args.device)
            if generator_args.chat_mode:
                prompt = input("User: ")
                if prompt == "/bye":
                    print("Exiting Chat.\n")
                    break
                if not self.is_llama3_model:
                    if self.system_prompt:
                        prompt = f"{B_INST} {B_SYS}\n{self.system_prompt.strip()}\n{E_SYS}\n\n{prompt.strip()} {E_INST}"
                        self.system_prompt = (
                            None  # can only provide system prompt on first interaction
                        )
                    else:
                        prompt = f"{B_INST} {prompt.strip()} {E_INST}"
                    encoded = self.encode_tokens(
                        prompt, bos=True, device=self.builder_args.device
                    )
                else:
                    if self.system_prompt:
                        encoded = self.chat_formatter.encode_dialog_prompt(
                            [
                                {"role": "system", "content": self.system_prompt},
                                {"role": "user", "content": prompt},
                            ]
                        )
                        self.system_prompt = None
                    elif i == 0:
                        encoded = self.chat_formatter.encode_dialog_prompt(
                            [{"role": "user", "content": prompt}]
                        )
                    else:
                        encoded = self.chat_formatter.encode_message(
                            {"role": "user", "content": prompt}
                        )
                        encoded.extend(self.chat_formatter.encode_header("assistant"))
                    encoded = torch.tensor(
                        encoded, dtype=torch.int, device=self.builder_args.device
                    )
                if encoded.size(0) + start_pos > max_seq_length:
                    print(
                        "This prompt would take us past the max_seq_length. Ending Conversation."
                    )
                    break

                print("Model: ", end="")

                buffer = []

                def callback(x, *, done_generating=False):
                    return self._callback(
                        x,
                        buffer=buffer,
                        done_generating=done_generating,
                    )

            else:
                assert not generator_args.chat_mode

                buffer = [generator_args.prompt]

                def callback(x, *, done_generating=False):
                    return self._callback(
                        x,
                        buffer=buffer,
                        done_generating=done_generating,
                    )

            if self.profile:
                from torch._inductor import config as inductor_config

                torch._inductor.config.profiler_mark_wrapper_call = True
                torch._inductor.config.cpp.enable_kernel_profile = True
            if (i != generator_args.num_samples - 1 or not self.profile) or (
                self.builder_args.use_distributed and self.rank != 0
            ):
                import contextlib
                prof = contextlib.nullcontext()
            else:
                torch.profiler._utils._init_for_cuda_graphs()
                prof = torch.profiler.profile()
            t0 = time.perf_counter()
            num_tokens_generated = 0
            with prof:
                generator_func = self.generate(
                    self.model,
                    encoded,
                    generator_args.max_new_tokens,
                    draft_model=self.draft_model,
                    speculate_k=generator_args.speculate_k,
                    chat_mode=generator_args.chat_mode,
                    batch=batch,
                    callback=callback,
                    temperature=generator_args.temperature,
                    top_k=generator_args.top_k,
                    sequential_prefill=generator_args.sequential_prefill,
                    start_pos=start_pos,
                    max_seq_length=max_seq_length,
                )
                for token_tensor, metrics in generator_func:
                    logger.info('-------------------------')
                    logger.info(f"token_tensor={token_tensor}")
                    logger.info(f"metrics={metrics}")
                    if token_tensor is not None:
                        start_pos += token_tensor.size(0)
                        num_tokens_generated += token_tensor.size(0)
                    if metrics is not None:
                        aggregate_metrics.update(metrics)
                    logger.info('________________________')
                    logger.info(f"token_tensor.type={type(token_tensor)}")
                    logger.info(f"metric.type = {type(metrics)}")
                    yield token_tensor, metrics
            logger.info('===============')
            jit_compile = (i == 0) and (
                generator_args.compile or generator_args.compile_prefill
            )
            compilation_time = time.perf_counter() - t0
            device_sync(device=self.builder_args.device)
            t = time.perf_counter() - t0
            if hasattr(prof, "export_chrome_trace"):
                if self.builder_args.device == "cpu":
                    print(prof.key_averages().table(sort_by="self_cpu_time_total"))
                else:
                    print(prof.key_averages().table(sort_by="self_cuda_time_total"))
                if self.builder_args.use_distributed:
                    prof.export_chrome_trace(f"{self.profile}_rank_{self.rank}.json")
                else:
                    prof.export_chrome_trace(f"{self.profile}.json")
            logger.info('***************')

            if start_pos >= max_seq_length:
                print(
                    f"[Max Sequence Length {max_seq_length} Reached. Ending Conversation.]"
                )
                print("---------------------------------------------------")

            tokens_sec = (num_tokens_generated + 1) / t
            first_token_sec = 1 / aggregate_metrics.get("time_to_first_token", 0)
            next_tokens_sec = num_tokens_generated / (
                t - aggregate_metrics.get("time_to_first_token", 0)
            )
            logger.info('^^^^^^^^^^^^^^^^')

            if jit_compile:
                print(
                    f"just-in-time compilation time (incl run time): {compilation_time:.2} seconds"
                )
                aggregate_metrics["tokens_per_sec_jit_compile"] = tokens_sec
                # Don't continue here.... because we need to report and reset
                # continue
            else:
                aggregate_metrics["tokens_per_sec"].append(tokens_sec)
                aggregate_metrics["first_token_per_sec"].append(first_token_sec)
                aggregate_metrics["next_tokens_per_sec"].append(next_tokens_sec)

            logger.info(
                f"\n~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\
                \nGenerated {num_tokens_generated} tokens \
                \nTime for inference {i + 1}: {t:.04f} sec total \
                \nTime to first token: {aggregate_metrics.get('time_to_first_token', 0):.04f} sec \
with {'sequential' if generator_args.sequential_prefill else 'parallel'} prefill.\
                \n\n      Total throughput: {tokens_sec:.04f} tokens/sec, {1 / tokens_sec:.04f} s/token \
                \nFirst token throughput: {first_token_sec:.04f} tokens/sec, {1 / first_token_sec:.04f} s/token \
                \n Next token throughput: {next_tokens_sec:.04f} tokens/sec, {1 / next_tokens_sec:.04f} s/token \
                    "
            )
            logger.info(
                f"\nBandwidth achieved: {model_size * tokens_sec / 1e9:.02f} GB/s"
            )
            if i == 0:
                logger.info(
                    f"*** This first iteration will include cold start effects for dynamic import, hardware caches{', JIT compilation' if jit_compile else ''}. ***"
                )
            logger.info("\n========================================\n")
            if start_pos >= max_seq_length:
                if generator_args.chat_mode:
                    break

            if not generator_args.chat_mode:
                start_pos = 0

        if self.is_speculative:
            counts_aggregated = [
                sum(i) for i in zip(*aggregate_metrics["accept_counts"])
            ]
            acceptance_probs = [i / sum(counts_aggregated) for i in counts_aggregated]
            print(f"Acceptance probs: {acceptance_probs}")
            print(
                f"Mean Accepted: {sum([idx * i for idx, i in enumerate(counts_aggregated)])/sum(counts_aggregated)}"
            )

        logger.info(
            f"\n      Average tokens/sec (total): {torch.mean(torch.tensor(aggregate_metrics['tokens_per_sec'])).item():.2f} \
                \nAverage tokens/sec (first token): {torch.mean(torch.tensor(aggregate_metrics['first_token_per_sec'])).item():.2f} \
                \nAverage tokens/sec (next tokens): {torch.mean(torch.tensor(aggregate_metrics['next_tokens_per_sec'])).item():.2f} \n\
                "
        )
        if torch.cuda.is_available():
            logger.info(f"Memory used: {torch.cuda.max_memory_reserved() / 1e9:.02f} GB")


COMMON_ROOT = Path("/shared/common")
DATASET_ROOT = Path("/shared/datasets")
MODEL_ROOT = Path("/shared/models")
CORE_DIR = Path(__file__).resolve().parent.parent # /source/autonn_core
CFG_DIR = CORE_DIR / 'tangochat' / 'common' / 'cfg'
HF_HOME = os.environ.get('HF_HOME', '/root/.cache/huggingface')

def main(args):

    '''[tenace] for direct llm inference'''
    if not args:
        import json
        args = argparse.Namespace()
        args_path = CFG_DIR / 'args_chat.json'
        if os.path.isfile(args_path):
            with open(args_path, 'r') as f:
                args_dict = json.load(f)
            args = argparse.Namespace(**args_dict)
    
    for k, v in vars(args).items():
        logger.info(f"{k}:{v}")

    builder_args = BuilderArgs.from_args(args)
    speculative_builder_args = BuilderArgs.from_speculative_args(args)
    tokenizer_args = TokenizerArgs.from_args(args)
    generator_args = GeneratorArgs.from_args(args)
    gen = Generator(
        builder_args,
        speculative_builder_args,
        tokenizer_args,
        generator_args,
        args.profile,
        args.quantize,
        args.draft_quantize,
    )
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    for _ in gen.chat(generator_args):
        pass
