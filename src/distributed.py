import os
import torch
from functools import partial

import torch.distributed as dist

from torch.distributed._tensor import DeviceMesh

from chop.distributed.tensor import distribute_module, distribute_tensor, DTensor
from chop.distributed.utils import (
    rlog,
    distributed_timing,
    distributed_average_timing,
    dist_model_fn,
)
import chop.passes as passes
from chop.passes.graph.analysis.utils import fetch_attr, load_arg
from chop.tools import get_logger
from chop.nn.functional.dtensor import redistribute_dtensor

from auto import autosharding_runner

logger = get_logger(__name__)
logger.setLevel("INFO")


def node_interpreter(rank, mg, inputs):
    """
    Execute the graph node-by-node following the interpreter pattern
    https://pytorch.org/docs/stable/fx.html#the-interpreter-pattern

    Args:
        rank (int): Rank of the current process.
        mg (MaseGraph): input MaseGraph
        inputs (DTensor): input distributed tensor
    """
    env = {}
    for node in mg.fx_graph.nodes:
        args, kwargs = None, None
        if node.op == "placeholder":
            result = inputs
            rlog(
                logger,
                rank,
                f"Placeholder {node.name} has type {type(inputs)}, shape {inputs.shape}",
                level="debug",
            )

        elif node.op == "get_attr":
            result = fetch_attr(mg.model, node.target)
            rlog(
                logger,
                rank,
                f"get_attr node {node.name} has type {type(inputs)}, shape {inputs.shape}",
                level="debug",
            )
            if isinstance(result, DTensor):
                rlog(
                    logger,
                    rank,
                    f"Local shape: {result._local_tensor.shape}, sharding: {result._spec}",
                    level="debug",
                )

        elif node.op == "call_function":
            args = load_arg(node.args, env)
            kwargs = load_arg(node.kwargs, env)
            rlog(logger, rank, f"Running function {node.name}", level="info")

            for arg_idx, arg in enumerate(args):
                if isinstance(arg, DTensor):
                    rlog(
                        logger,
                        rank,
                        f"Arg {arg_idx} (DTensor) shape: {arg.shape}, local_tensor.shape: {arg._local_tensor.shape}, spec: {arg._spec}",
                        level="debug",
                    )
                elif isinstance(arg, torch.Tensor):
                    rlog(
                        logger,
                        rank,
                        f"Arg {arg_idx} (Tensor) shape: {arg.shape}",
                        level="debug",
                    )
                else:
                    rlog(
                        logger,
                        rank,
                        f"Arg {arg_idx}: {arg}",
                        level="debug",
                    )

            result = node.target(*args, **kwargs)

            rlist = (result,) if not isinstance(result, (tuple, list)) else result
            for ridx, r in enumerate(rlist):
                if isinstance(r, DTensor):
                    rlog(
                        logger,
                        rank,
                        f"Result {ridx} (DTensor) shape: {r.shape}, local_shape: {r._local_tensor.shape}, spec: {r._spec}",
                        level="debug",
                    )

                    if node.meta["mase"]["common"].get("results", None) is not None:
                        node_spec = node.meta["mase"]["common"]["results"][
                            f"data_out_{ridx}"
                        ]["dtensor_spec"]
                        rlog(
                            logger,
                            rank,
                            f"expected spec: {node_spec}",
                            level="debug",
                        )
                elif isinstance(r, torch.Tensor):
                    rlog(
                        logger,
                        rank,
                        f"Result {ridx} (Tensor) shape: {r.shape}",
                        level="debug",
                    )
                else:
                    rlog(
                        logger,
                        rank,
                        f"Result {ridx}: {r}",
                        level="debug",
                    )

        # Register in environment dictionary
        env[node.name] = result


def device_fn(
    rank,
    world_size,
    device_mesh=None,
    model_class=None,
    model_config=None,
    cli_args=None,
):
    """
    This function gets called on each GPU device to set up the distributed environment and distribute the model,
    following the SPMD model.
    """
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"
    os.environ["RANK"] = str(rank)

    # Initialize
    dist.init_process_group("nccl", rank=rank, world_size=world_size)
    device = torch.device("cuda", rank)
    torch.cuda.set_device(device)

    # Run the autosharding pass etc
    inputs = torch.randn(
        (cli_args.batch_size, cli_args.sequence_length, model_config.hidden_size)
    )
    mg, pass_outputs = autosharding_runner(
        model_class=model_class,
        model_config=model_config,
        args=cli_args,
        inputs=inputs,
    )

    # Distribute model parameters according to sharding configuration
    mesh = DeviceMesh("cuda", mesh=device_mesh)
    rlog(logger, rank, f"Distributing module parameters...", level="info")
    mg.model, dist_time = distributed_timing(
        distribute_module,
        mg.model,
        mesh,
        partial(
            dist_model_fn,
            rank=rank,
            tensor_sharding_map=pass_outputs["autosharding_analysis_pass"][
                "tensor_sharding_map"
            ],
        ),
        input_fn=None,
        output_fn=None,
    )
    rlog(logger, rank, f"Module distribution done. Time taken: {dist_time} seconds.")

    if rank == 0:
        print(mg.model.code)

    # Run forward pass
    rlog(logger, rank, f"Starting forward pass.", level="info")

    # Get the sharding spec for the input tensor
    for node in mg.fx_graph.nodes:
        if node.op == "placeholder":
            input_sharding = node.meta["mase"]["common"]["results"]["data_out_0"][
                "dtensor_spec"
            ].placements

    rlog(logger, rank, f"Sharding input to: {input_sharding}", level="debug")
    inputs = distribute_tensor(
        inputs,
        mesh,
        input_sharding,
    )

    _, time_taken = distributed_average_timing(
        fn=mg.model,
        args=[inputs],
        repeat=4,
        warmup_iters=2,
    )
    rlog(logger, rank, f"Forward pass finished. Time taken: {time_taken}", level="info")
    # node_interpreter(rank, mg, inputs)

    dist.destroy_process_group()
