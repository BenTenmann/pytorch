# [Done] 1 run DDP on Resnet
# [Done] 2 Measure forward and backward -- collect the metrics
# [Done] 3 Pure Python DDP -- copy research code -- measure and compare
# [Done] 3.1 Process Group - Not work -- Bo can refer to Shen's benchmark code. use gloo instead
# 4. Debug why the hook is not invoked correctly
# 4 Modify Pure Python -- re-run
# 5 Send out code review

import legacy_distributed_data_parallel as legacy_ddp
import numpy as np
import os
import python_ddp
import torch
import torch.cuda as cuda
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.optim as optim
import torchvision.models as models

from enum import Enum
from torch.nn.parallel import DistributedDataParallel as DDP

# TODO(bowangbj): Use Toy model to debug. Remove after debugging.
# resnet50 or toy
debug_model = "resnet50"

class ToyModel(nn.Module):
    def __init__(self):
        super(ToyModel, self).__init__()
        self.net1 = nn.Linear(10, 10)
        self.relu = nn.ReLU()
        self.net2 = nn.Linear(10, 5)

    def forward(self, x):
        return self.net2(self.relu(self.net1(x)))

class DDPOption(Enum):
    DDP_CPP_CORE = 1
    LEGACY_DISTRIBUTED_DATA_PARALLEL = 2
    PYTHON_DDP = 3

def _setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'

    # initialize the process group
    dist.init_process_group("gloo", rank=rank, world_size=world_size)

# Helper to create a DDP Model.
def _create_ddp_model(module, rank, pg, ddp_option):
    if ddp_option == DDPOption.DDP_CPP_CORE:
        ddp_model = DDP(module, device_ids=[rank], process_group=pg)
        ddp_model._set_static_graph()
        return ddp_model
    elif ddp_option == DDPOption.LEGACY_DISTRIBUTED_DATA_PARALLEL:
        return legacy_ddp.LegacyDistributedDataParallel(module, pg)
    elif ddp_option == DDPOption.PYTHON_DDP:
        return python_ddp.PythonDDP(module, pg)
    else:
        raise NotImplementedError('only DDP CPP is supported')

def run_ddp(rank, world_size, epochs, ddp_option):
    print(f'bowangbj run_ddp rank {rank}')

    # Setup
    print('setting up ... ')
    _setup(rank, world_size)
    torch.manual_seed(rank)
    torch.cuda.manual_seed(rank)
    device = torch.device('cuda:%d' % rank)
    print('set up DONE')

    # Create ResNet50
    model = models.resnet50().to(device)
    if debug_model == "toy":
        model = ToyModel().to(device)

    # Wrap in DDP Model
    pg = dist.distributed_c10d._get_default_group()
    ddp_model = _create_ddp_model(model, rank, pg, ddp_option)

    loss_fn = nn.MSELoss()
    optimizer = optim.SGD(ddp_model.parameters(),lr=0.001)

    # Container to hold event -> list of events in milliseconds
    MODEL_FORWARD = "forward"
    MODEL_BACKWARD = "backward"
    metrics = {MODEL_FORWARD: [], MODEL_BACKWARD: []}

    for epoch in range(epochs):
        if epoch % 1 == 0:
            print(f'============================== Epoch {epoch} =========================== ........ ')

        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)

        # TODO(bowangbj): Use real training set.
        inputs = torch.rand([32, 3, 224, 224], device=device)
        labels = torch.rand([32, 1000], device=device)
        if debug_model == "toy":
            inputs = torch.rand([20, 10], device=device)
            labels = torch.rand([20, 5], device=device)

        # Forward
        start.record()
        outputs = ddp_model(inputs)
        end.record()
        torch.cuda.synchronize()
        metrics[MODEL_FORWARD].append(start.elapsed_time(end))

        # Backward
        start.record()
        loss_fn(outputs, labels).backward()
        # Reduce all grads in sync.
        if ddp_option != DDPOption.DDP_CPP_CORE:
            ddp_model.all_reduce_grads()
        optimizer.step()
        optimizer.zero_grad()
        end.record()
        torch.cuda.synchronize()
        metrics[MODEL_BACKWARD].append(start.elapsed_time(end))

    if rank == 0:
        print(f'\n\nmetrics for GPU {rank} ddp_option {ddp_option}:')
        for step, elapsed_milliseconds in metrics.items():
            A = np.array(elapsed_milliseconds)
            print(' {N} iterations, {event}, mean={mean} ms, median={median} ms, p90={p90} ms, p99={p99} ms'.format(
                N=len(A), event=step, mean=np.mean(A), median=np.percentile(A, 50), p90=np.percentile(A, 90), p99=np.percentile(A, 99)))

# TODO(bowangbj): Cleanup
# metrics for GPU 0 ddp_option DDPOption.DDP_CPP_CORE:
#  100 iterations, forward, mean=104.12376266479492 ms, median=102.2904167175293 ms, p90=102.72983627319336 ms, p99=107.12981872558682 ms
#  100 iterations, backward, mean=213.57994995117187 ms, median=212.90614318847656 ms, p90=214.0695571899414 ms, p99=215.7702992248539 ms

# for Legacy
# metrics for GPU 0 ddp_option DDPOption.LEGACY_DISTRIBUTED_DATA_PARALLEL:
#  100 iterations, forward, mean=102.34731399536133 ms, median=99.68880081176758 ms, p90=99.8280258178711 ms, p99=102.78610168457168 ms
#  100 iterations, backward, mean=343.8858123779297 ms, median=325.28955078125 ms, p90=329.9462463378906 ms, p99=359.72328094483333 ms

# for PythonDDP.SYNC
# 100 iterations, forward, mean=101.77955757141113 ms, median=99.89172744750977 ms, p90=99.98208389282226 ms, p99=101.96920883178808 ms
# 100 iterations, backward, mean=308.90806060791016 ms, median=288.6599426269531 ms, p90=306.85809631347655 ms, p99=326.2959661865321 ms

def main():
    world_size = 2
    epochs = 100

    # valid options: DDP_CPP_CORE, LEGACY_DISTRIBUTED_DATA_PARALLEL
    ddp_option = DDPOption.PYTHON_DDP
    print('ddp_option=' + str(ddp_option))

    mp.spawn(run_ddp,
        args=(world_size, epochs, ddp_option),
        nprocs=world_size,
        join=True)

if __name__=="__main__":
    main()