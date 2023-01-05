# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
from os import path

import torch
import torch.nn.functional as F
import torch.optim as optim
import torchvision.transforms as T
from models import Net
from torch.optim.lr_scheduler import StepLR
from torchvision.datasets import MNIST

DATASETS_PATH = path.join(path.dirname(__file__), "..", "..", "Datasets")


# Credit to the PyTorch team
# Taken from https://github.com/pytorch/examples/blob/master/mnist/main.py and slightly adapted.
def run(hparams):
    torch.manual_seed(hparams.seed)

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    transform = T.Compose([T.ToTensor(), T.Normalize((0.1307,), (0.3081,))])
    train_dataset = MNIST(DATASETS_PATH, train=True, download=True, transform=transform)
    test_dataset = MNIST(DATASETS_PATH, train=False, transform=transform)
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=hparams.batch_size,
    )
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=hparams.batch_size)

    model = Net().to(device)
    optimizer = optim.Adadelta(model.parameters(), lr=hparams.lr)

    scheduler = StepLR(optimizer, step_size=1, gamma=hparams.gamma)

    # EPOCH LOOP
    for epoch in range(1, hparams.epochs + 1):

        # TRAINING LOOP
        model.train()
        for batch_idx, (data, target) in enumerate(train_loader):
            data, target = data.to(device), target.to(device)
            optimizer.zero_grad()
            output = model(data)
            loss = F.nll_loss(output, target)
            loss.backward()
            optimizer.step()
            if (batch_idx == 0) or ((batch_idx + 1) % hparams.log_interval == 0):
                print(
                    "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
                        epoch,
                        batch_idx * len(data),
                        len(train_loader.dataset),
                        100.0 * batch_idx / len(train_loader),
                        loss.item(),
                    )
                )
                if hparams.dry_run:
                    break
        scheduler.step()

        # TESTING LOOP
        model.eval()
        test_loss = 0
        correct = 0
        with torch.no_grad():
            for data, target in test_loader:
                data, target = data.to(device), target.to(device)
                output = model(data)
                test_loss += F.nll_loss(output, target, reduction="sum").item()  # sum up batch loss
                pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
                correct += pred.eq(target.view_as(pred)).sum().item()
                if hparams.dry_run:
                    break

        test_loss /= len(test_loader.dataset)

        print(
            "\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
                test_loss, correct, len(test_loader.dataset), 100.0 * correct / len(test_loader.dataset)
            )
        )

        if hparams.dry_run:
            break

    if hparams.save_model:
        torch.save(model.state_dict(), "mnist_cnn.pt")


def main():
    parser = argparse.ArgumentParser(description="PyTorch MNIST Example")
    parser.add_argument(
        "--batch-size", type=int, default=64, metavar="N", help="input batch size for training (default: 64)"
    )
    parser.add_argument("--epochs", type=int, default=14, metavar="N", help="number of epochs to train (default: 14)")
    parser.add_argument("--lr", type=float, default=1.0, metavar="LR", help="learning rate (default: 1.0)")
    parser.add_argument("--gamma", type=float, default=0.7, metavar="M", help="Learning rate step gamma (default: 0.7)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="quickly check a single pass")
    parser.add_argument("--seed", type=int, default=1, metavar="S", help="random seed (default: 1)")
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument("--save-model", action="store_true", default=False, help="For Saving the current Model")
    hparams = parser.parse_args()
    run(hparams)


if __name__ == "__main__":
    main()
