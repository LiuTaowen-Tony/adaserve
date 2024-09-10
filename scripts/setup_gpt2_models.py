import argparse
from transformers.models.gpt2 import GPT2Config, GPT2LMHeadModel
from transformers import AutoTokenizer
from accelerate import init_empty_weights

def setup_models(args):
    HEAD_SIZE = 64
    save_path = args.save_path
    tokenizer = AutoTokenizer.from_pretrained(f"openai-community/gpt2-xl")

    checkpoints = [
        "nice-gpt2-1.5b",
        "nice-gpt2-4b",
        "nice-gpt2-5.7b",
        "nice-gpt2-11.2b",
        "nice-gpt2-30.1b",
        "nice-gpt2-68.8b",
    ]
    configs = []

    num_head_list = [24, 40, 48]
    for num_heads in num_head_list:
        config = GPT2Config(
            n_layer=48,
            n_head=num_heads,
            n_embd=num_heads * HEAD_SIZE,
            n_positions = 16384,
        )
        config.activation_function = "gelu"
        configs.append(config)

    num_head_list = [48, 80, 120]
    for num_heads in num_head_list:
        config = GPT2Config(
            n_layer=96,
            n_head=num_heads,
            n_embd=num_heads * HEAD_SIZE,
            n_positions = 16384,
        )
        config.activation_function = "gelu"
        configs.append(config)


    for idx, config in enumerate(configs):
        print(f"Checkpoint: {checkpoints[idx]}")
        print(
            f"num heads: {config.n_head}, num layers: {config.n_layer}, emb size: {config.n_embd}"
        )

        # Uncomment below to initialize quickly
        # with init_empty_weights():
        model = GPT2LMHeadModel(config)

        # # Get parameter count
        params = sum(p.numel() for p in model.parameters())
        print(f"params: {params:,}")

        model.save_pretrained(f"{save_path}/{checkpoints[idx]}")
        tokenizer.save_pretrained(f"{save_path}/{checkpoints[idx]}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Action (running mode)
    # --------------------------------------------
    action_group = parser.add_argument_group("Action (running mode)")
    action_group.add_argument(
        "--save_path",
        default="/mnt/data",
        type=str,
        help="Run autosharding on a single design point, defined by batch size and sequence length.",
    )

    args = parser.parse_args()

    setup_models(args)