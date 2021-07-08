import os

import torch
from torch.utils.data import DataLoader, Dataset

from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.loops import EvaluationLoop, FitLoop, TrainingBatchLoop, TrainingEpochLoop
from pytorch_lightning.trainer.progress import FitLoopProgress


class RandomDataset(Dataset):

    def __init__(self, size, length):
        self.len = length
        self.data = torch.randn(length, size)

    def __getitem__(self, index):
        return self.data[index]

    def __len__(self):
        return self.len


class BoringModel(LightningModule):

    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(32, 2)

    def forward(self, x):
        return self.layer(x)

    def training_step(self, batch, batch_idx):
        loss = self(batch).sum()
        self.log("train_loss", loss)
        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        loss = self(batch).sum()
        self.log("valid_loss", loss)

    def test_step(self, batch, batch_idx):
        loss = self(batch).sum()
        self.log("test_loss", loss)

    def configure_optimizers(self):
        return torch.optim.SGD(self.layer.parameters(), lr=0.1)


def run():
    train_data = DataLoader(RandomDataset(32, 64), batch_size=2)
    val_data = DataLoader(RandomDataset(32, 64), batch_size=2)
    test_data = DataLoader(RandomDataset(32, 64), batch_size=2)

    model = BoringModel()

    trainer = Trainer(
        default_root_dir=os.getcwd(),
        limit_train_batches=1,
        limit_val_batches=1,
        num_sanity_val_steps=0,
        max_epochs=1,
        weights_summary=None,
    )

    # construct loops
    fit_loop = FitLoop()
    fit_loop.any = TrainingEpochLoop()

    train_epoch_loop = TrainingEpochLoop(min_steps=0, max_steps=2)
    train_batch_loop = TrainingBatchLoop()
    val_loop = EvaluationLoop()

    # link loops
    train_epoch_loop.connect(batch_loop=train_batch_loop, val_loop=val_loop)
    fit_loop.connect(epoch_loop=train_epoch_loop)

    # connect fit loop to trainer
    trainer.fit_loop = fit_loop

    fit_loop.connect(epoch_loop=TrainingEpochLoop())

    trainer.fit(model, train_dataloaders=train_data, val_dataloaders=val_data)
    trainer.test(model, dataloaders=test_data)


if __name__ == '__main__':
    run()
