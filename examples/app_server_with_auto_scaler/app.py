# ! pip install torch torchvision
from typing import List

import torch
import torchvision
from pydantic import BaseModel

import lightning as L


class BatchRequestModel(BaseModel):
    inputs: List[L.app.components.Image]


class BatchResponse(BaseModel):
    outputs: List[L.app.components.Number]


class PyTorchServer(L.app.components.PythonServer):
    def __init__(self, *args, **kwargs):
        super().__init__(
            input_type=BatchRequestModel,
            output_type=BatchResponse,
            *args,
            **kwargs,
        )

    def setup(self):
        self._device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self._model = torchvision.models.resnet18(pretrained=True).to(self._device)

    def predict(self, requests: BatchRequestModel):
        transforms = torchvision.transforms.Compose(
            [
                torchvision.transforms.Resize(224),
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )
        images = []
        for request in requests.inputs:
            image = L.app.components.serve.types.image.Image.deserialize(request.image)
            image = transforms(image).unsqueeze(0)
            images.append(image)
        images = torch.cat(images)
        images = images.to(self._device)
        predictions = self._model(images)
        results = predictions.argmax(1).cpu().numpy().tolist()
        return BatchResponse(outputs=[{"prediction": pred} for pred in results])


class MyAutoScaler(L.app.components.AutoScaler):
    def scale(self, replicas: int, metrics: dict) -> int:
        """The default scaling logic that users can override."""
        # scale out if the number of pending requests exceeds max batch size.
        max_requests_per_work = self.max_batch_size
        pending_requests_per_work = metrics["pending_requests"] / (replicas + metrics["pending_works"])
        if pending_requests_per_work >= max_requests_per_work:
            return replicas + 1

        # scale in if the number of pending requests is below 25% of max_requests_per_work
        min_requests_per_work = max_requests_per_work * 0.25
        pending_requests_per_work = metrics["pending_requests"] / replicas
        if pending_requests_per_work < min_requests_per_work:
            return replicas - 1

        return replicas


app = L.LightningApp(
    MyAutoScaler(
        # work class and args
        PyTorchServer,
        cloud_compute=L.CloudCompute("gpu"),
        # autoscaler specific args
        min_replicas=1,
        max_replicas=4,
        autoscale_interval=10,
        endpoint="predict",
        input_type=L.app.components.Image,
        output_type=L.app.components.Number,
        timeout_batching=1,
        max_batch_size=8,
    )
)
