from typing import Any, Literal, Optional, Type, Union

from lightning_app import structures
from lightning_app.components.multi_node.executors import LiteRunExecutor, PyTorchSpawnRunExecutor
from lightning_app.components.multi_node.protocols import (
    DistributedProtocol,
    DistributedPyTorchSpawnProtocol,
    LiteProtocol,
)
from lightning_app.core.flow import LightningFlow
from lightning_app.core.work import LightningWork
from lightning_app.utilities.enum import WorkStageStatus
from lightning_app.utilities.packaging.cloud_compute import CloudCompute
from lightning_app.utilities.proxies import WorkRunExecutor


class MultiNode(LightningFlow):
    def __init__(
        self,
        work_cls: Type["LightningWork"],
        num_nodes: int,
        cloud_compute: "CloudCompute",
        executor: Optional[Union[WorkRunExecutor, Literal["lite", "pytorch_spawn"]]] = None,
        *work_args: Any,
        **work_kwargs: Any,
    ) -> None:
        """This component enables performing distributed multi-node multi-device training.

        Example::

            import torch

            import lightning as L
            from lightning.components import MultiNode

            class AnyDistributedComponent(L.LightningWork):
                def run(
                    self,
                    main_address: str,
                    main_port: int,
                    node_rank: int,
                ):
                    print(f"ADD YOUR DISTRIBUTED CODE: {main_address} {main_port} {node_rank}")


            compute = L.CloudCompute("gpu")
            app = L.LightningApp(
                MultiNode(
                    AnyDistributedComponent,
                    num_nodes=8,
                    cloud_compute=compute,
                )
            )

        Arguments:
            work_cls: The work to be executed
            num_nodes: Number of nodes.
            cloud_compute: The cloud compute object used in the cloud.
            executor: Choose the work run executor. Currently supported are [None, `lite`, `pytorch`].
            work_args: Arguments to be provided to the work on instantiation.
            work_kwargs: Keywords arguments to be provided to the work on instantiation.
        """
        super().__init__()
        self.ws = structures.List()
        self._work_cls = work_cls
        self.num_nodes = num_nodes
        self._cloud_compute = cloud_compute
        self._work_args = work_args
        self._work_kwargs = work_kwargs
        self._protocol = DistributedProtocol

        if executor == "lite":
            self._work_kwargs["run_executor_cls"] = LiteRunExecutor
            self._protocol = LiteProtocol

        elif executor == "pytorch_spawn":
            self._work_kwargs["run_executor_cls"] = PyTorchSpawnRunExecutor
            self._protocol = DistributedPyTorchSpawnProtocol

        self.has_started = False

    def run(self) -> None:
        if not self.has_started:

            # 1. Create & start the works
            if not self.ws:
                for node_rank in range(self.num_nodes):
                    work = self._work_cls(
                        *self._work_args,
                        cloud_compute=self._cloud_compute,
                        **self._work_kwargs,
                        parallel=True,
                    )

                    assert isinstance(work, self._protocol)

                    # Starting node `node_rank`` ...
                    work.start()

                    self.ws.append(work)

            # 2. Wait for all machines to be started !
            if not all(w.status.stage == WorkStageStatus.STARTED for w in self.ws):
                return

            self.has_started = True

        # Loop over all node machines
        for node_rank in range(self.num_nodes):

            # 3. Run the user code in a distributed way !
            self.ws[node_rank].run(
                main_address=self.ws[0].internal_ip,
                main_port=self.ws[0].port,
                num_nodes=self.num_nodes,
                node_rank=node_rank,
            )

            # 4. Stop the machine when finished.
            if self.ws[node_rank].has_succeeded:
                self.ws[node_rank].stop()
