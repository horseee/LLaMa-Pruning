# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the GNU General Public License version 3.

from typing import Tuple
import os
import sys
import torch
import fire
import time
import json

import torch_pruning as tp 
from llama_pruner import RMSNormPrunner, AttentionPrunner

from pathlib import Path

from fairscale.nn.model_parallel.initialize import initialize_model_parallel

from llama import ModelArgs, Transformer, Tokenizer, LLaMA
from llama.model import RMSNorm, Attention, precompute_freqs_cis

from fairscale.nn.model_parallel.layers import (
    ParallelEmbedding,
    RowParallelLinear,
    ColumnParallelLinear,
)


def setup_model_parallel() -> Tuple[int, int]:
    local_rank = int(os.environ.get("LOCAL_RANK", -1))
    world_size = int(os.environ.get("WORLD_SIZE", -1))

    torch.distributed.init_process_group("nccl")
    initialize_model_parallel(world_size)
    torch.cuda.set_device(local_rank)

    # seed must be the same in all processes
    torch.manual_seed(1)
    return local_rank, world_size


def load(
    ckpt_dir: str,
    tokenizer_path: str,
    local_rank: int,
    world_size: int,
    max_seq_len: int,
    max_batch_size: int,
) -> LLaMA:
    start_time = time.time()
    checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
    assert world_size == len(
        checkpoints
    ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {world_size}"
    ckpt_path = checkpoints[local_rank]
    print("Loading")
    #checkpoint = torch.load(ckpt_path, map_location="cpu")
    with open(Path(ckpt_dir) / "params.json", "r") as f:
        params = json.loads(f.read())

    model_args: ModelArgs = ModelArgs(
        max_seq_len=max_seq_len, max_batch_size=max_batch_size, **params
    )
    tokenizer = Tokenizer(model_path=tokenizer_path)
    model_args.vocab_size = tokenizer.n_words
    torch.set_default_tensor_type(torch.cuda.HalfTensor)
    model = Transformer(model_args)
    torch.set_default_tensor_type(torch.FloatTensor)
    #model.load_state_dict(checkpoint, strict=False)

    generator = LLaMA(model, tokenizer)
    print(f"Loaded in {time.time() - start_time:.2f} seconds")
    return generator


def add_customized_pruner(DG):
    pruners = [
        (Attention, AttentionPrunner()),
        (RMSNorm, RMSNormPrunner()),
        (ColumnParallelLinear, tp.pruner.function.LinearPruner()),
        (RowParallelLinear, tp.pruner.function.LinearPruner()),
        (ParallelEmbedding, tp.pruner.function.EmbeddingPruner())
    ]
    
    for pruner in pruners:
        DG.register_customized_layer(
            *pruner
        )
    return DG, pruners


def main(
    ckpt_dir: str,
    tokenizer_path: str,
    temperature: float = 0.8,
    top_p: float = 0.95,
    max_seq_len: int = 512,
    max_batch_size: int = 32,
    local_rank: int = -1,
):
    local_rank, world_size = setup_model_parallel()
    #local_rank, world_size = 0, 1
    if local_rank > 0:
        sys.stdout = open(os.devnull, "w")
    
    generator = load(
        ckpt_dir, tokenizer_path, local_rank, world_size, max_seq_len, max_batch_size
    )
    for param in generator.model.parameters():
        param.requires_grad_(True)
    before_pruning_parameters = sum(p.numel() for p in generator.model.parameters() if p.requires_grad)
    
    example_prompts = torch.tensor([
        [    1,   306,  4658,   278,  6593,   310,  2834,   338],
        [    1,  3439, 17632,  1925, 29892,   278,  6368,   310],
        [    1, 17166,   263,  4700,   508,   367,  2309,   297],
        [    1,   323, 16668, 29901,   376, 29902, 26277,   372],
        [    1,  4103,  9632,  4223,   304,  5176, 29901,    13]],
    ).cuda()


    #DG = tp.DependencyGraph()
    #DG, pruners = add_customized_pruner(DG)
    imp = tp.importance.RandomImportance()
    
    iterative_steps = 1 # progressive pruning
    pruner = tp.pruner.MagnitudePruner(
        generator.model,
        example_prompts,
        importance=imp,
        iterative_steps=iterative_steps,
        ch_sparsity=0.5, # remove 50% channels, ResNet18 = {64, 128, 256, 512} => ResNet18_Half = {32, 64, 128, 256}
        ignored_layers=[],
        customized_pruners = {
            Attention: AttentionPrunner(),
            RMSNorm: RMSNormPrunner(),
            ColumnParallelLinear: tp.pruner.function.LinearPruner(),
            RowParallelLinear: tp.pruner.function.LinearPruner(),
            ParallelEmbedding: tp.pruner.function.EmbeddingPruner()
        },
        root_module_types = [ParallelEmbedding, RMSNorm, RowParallelLinear, ColumnParallelLinear, Attention]
    )

    for i in range(iterative_steps):
        pruner.step()
        after_pruning_parameters = sum(p.numel() for p in generator.model.parameters() if p.requires_grad)
        print("#Param before: {}, #Param after: {}".format(before_pruning_parameters, after_pruning_parameters))
        #macs, nparams = tp.utils.count_ops_and_params(model, example_inputs)

    # modify inferece-related attributes
    generator.model.params.dim = int(0.5 * generator.model.params.dim)
    generator.model.freqs_cis = precompute_freqs_cis(
            generator.model.params.dim // generator.model.params.n_heads, generator.model.params.max_seq_len * 2
    )

    # 
    del pruner, example_prompts
    torch.cuda.empty_cache()
    generator.model.to('cuda')
    torch.save(generator.model, 'pruned_llama.ckpt')
    #print(generator.model)

    #DG.build_dependency(generator.model, example_inputs=example_prompts)
    #groups = DG.get_all_groups(root_module_types=[ParallelEmbedding, RMSNorm, RowParallelLinear, ColumnParallelLinear])
    #group = DG.get_pruning_group( generator.model.tok_embeddings, pruners[-1][1].prune_out_channels, idxs=[2, 6, 9] )
    #if DG.check_pruning_group(group): # avoid full pruning, i.e., channels=0.
    #    group.prune()

    #print("Pruning Group:")
    #for group in groups:
    #    print(group)
    #    idxs = [2,4,6]
    #    group.prune(idxs=idxs)
    #    print(group)
    
    prompts = [
        # For these prompts, the expected answer is the natural continuation of the prompt
        "I believe the meaning of life is",
        "Simply put, the theory of relativity states that ",
        "Building a website can be done in 10 simple steps:\n",
        # Few shot prompts: https://huggingface.co/blog/few-shot-learning-gpt-neo-and-inference-api
        """Tweet: "I hate it when my phone battery dies."
Sentiment: Negative
###
Tweet: "My day has been 👍"
Sentiment: Positive
###
Tweet: "This is the link to the article"
Sentiment: Neutral
###
Tweet: "This new music video was incredibile"
Sentiment:""",
        """Translate English to French:

sea otter => loutre de mer

peppermint => menthe poivrée

plush girafe => girafe peluche

cheese =>""",
    ]

    with torch.no_grad():
        results = generator.generate(
            prompts, max_gen_len=256, temperature=temperature, top_p=top_p, device="cuda"
        )
    
    for result in results:
        print(result)
        print("\n==================Finish================\n")
    


if __name__ == "__main__":
    fire.Fire(main)
