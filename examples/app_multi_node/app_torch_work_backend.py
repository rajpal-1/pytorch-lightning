import torch
from torch.nn.parallel.distributed import DistributedDataParallel

import lightning as L
from lightning.app.components import MultiNode


class PyTorchDistributed(L.LightningWork):
    @staticmethod
    def run(
        world_size: int,
        node_rank: int,
        global_rank: str,
        local_rank: int,
    ):
        # 1. Prepare distributed model
        model = torch.nn.Linear(32, 2)
        device = torch.device(f"cuda:{local_rank}") if torch.cuda.is_available() else torch.device("cpu")
        device_ids = device if torch.cuda.is_available() else None
        model = DistributedDataParallel(model, device_ids=device_ids).to(device)

        # 2. Prepare loss and optimizer
        criterion = torch.nn.MSELoss()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)

        # 3. Train the model for 50 steps.
        for step in range(50):
            model.zero_grad()
            x = torch.randn(64, 32).to(device)
            output = model(x)
            loss = criterion(output, torch.ones_like(output))
            print(f"global_rank: {global_rank} step: {step} loss: {loss}")
            loss.backward()
            optimizer.step()


compute = L.CloudCompute("gpu-fast-multi")  # 4xV100
app = L.LightningApp(
    MultiNode(
        PyTorchDistributed,
        num_nodes=2,
        cloud_compute=compute,
        backend="pytorch",
    )
)
