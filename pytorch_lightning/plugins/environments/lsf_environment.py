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

import os
import socket

from pytorch_lightning import _logger as log
from pytorch_lightning.plugins.environments import ClusterEnvironment
from pytorch_lightning.utilities import rank_zero_deprecation


class LSFEnvironment(ClusterEnvironment):
    """An environment for running on clusters managed by the LSF resource manager.

    It is expected that any execution using this ClusterEnvironment was executed
    using the Job Step Manager i.e. jsrun.

    This plugin expects the following environment variables:

    LSB_JOBID
      The LSF assigned job ID

    LSB_DJOB_RANKFILE
      The OpenMPI compatibile rank file for the LSF job

    JSM_NAMESPACE_LOCAL_RANK
      The node local rank for the task. This environment variable is set by jsrun

    JSM_NAMESPACE_SIZE
      The world size for the task. This environment variable is set by jsrun

    JSM_NAMESPACE_RANK
      The global rank for the task. This environment variable is set by jsrun
    """

    def __init__(self):
        super().__init__()
        # TODO: remove in 1.7
        if hasattr(self, "is_using_lsf") and callable(self.is_using_lsf):
            rank_zero_deprecation(
                f"`{self.__class__.__name__}.is_using_lsf` has been deprecated in v1.6 and will be removed in v1.7."
                " Implement the static method `detect()` instead (do not forget to add the `@staticmethod` decorator)."
            )
        self._main_address = self._get_main_address()
        self._main_port = self._get_main_port()
        self._local_rank = self._get_local_rank()
        self._global_rank = self._get_global_rank()
        self._world_size = self._get_world_size()
        self._node_rank = self._get_node_rank()

        # set environment variables needed for initializing torch distributed process group
        os.environ["MASTER_ADDR"] = str(self._main_address)
        log.debug(f"MASTER_ADDR: {os.environ['MASTER_ADDR']}")
        os.environ["MASTER_PORT"] = str(self._main_port)
        log.debug(f"MASTER_PORT: {os.environ['MASTER_PORT']}")

        tmp = ("main_address", "main_port", "world_size", "local_rank", "node_rank", "global_rank")
        self._rep = ",".join("{}={}".format(s, getattr(self, "_" + s)) for s in tmp)

    def _read_hosts(self):
        var = "LSB_DJOB_RANKFILE"
        try:
            rankfile = os.environ[var]
        except KeyError:
            raise ValueError("Could not find environment variable LSB_DJOB_RANKFILE")
        if not rankfile:
            raise ValueError("Environment variable LSB_DJOB_RANKFILE is empty")
        with open(rankfile) as f:
            ret = [line.strip() for line in f]
        return ret

    def _get_main_address(self):
        """A helper for getting the master address."""
        hosts = self._read_hosts()
        return hosts[1]

    def _get_main_port(self):
        """A helper for getting the master port.

        Use the LSF job ID so all ranks can compute the master port
        """
        # check for user-specified master port
        port = os.environ.get("MASTER_PORT")
        if not port:
            var = "LSB_JOBID"
            jobid = os.environ.get(var)
            if not jobid:
                raise ValueError("Could not find job id -- expected in environment variable %s" % var)
            else:
                port = int(jobid)
                # all ports should be in the 10k+ range
                port = int(port) % 1000 + 10000
            log.debug("calculated master port")
        else:
            log.debug("using externally specified master port")
        return port

    def _get_global_rank(self):
        """A helper function for getting the global rank.

        Read this from the environment variable JSM_NAMESPACE_LOCAL_RANK
        """
        var = "JSM_NAMESPACE_RANK"
        global_rank = os.environ.get(var)
        if global_rank is None:
            raise ValueError(
                "Cannot determine global rank -- expected in %s "
                "-- make sure you run your executable with jsrun" % var
            )
        return int(global_rank)

    def _get_local_rank(self):
        """A helper function for getting the local rank.

        Read this from the environment variable JSM_NAMESPACE_LOCAL_RANK
        """
        var = "JSM_NAMESPACE_LOCAL_RANK"
        local_rank = os.environ.get(var)
        if local_rank is None:
            raise ValueError(
                "Cannot determine local rank -- expected in %s " "-- make sure you run your executable with jsrun" % var
            )
        return int(local_rank)

    def _get_world_size(self):
        """A helper function for getting the world size.

        Read this from the environment variable JSM_NAMESPACE_SIZE
        """
        var = "JSM_NAMESPACE_SIZE"
        world_size = os.environ.get(var)
        if world_size is None:
            raise ValueError(
                "Cannot determine local rank -- expected in %s " "-- make sure you run your executable with jsrun" % var
            )
        return int(world_size)

    def _get_node_rank(self):
        """A helper function for getting the node rank.
        
        Node rank is determined by the position of the current node in the hosts
        used in the job. This is calculated by reading all hosts from LSB_DJOB_RANKFILE
        and finding this nodes hostname in the list.
        """
        hosts = self._read_hosts()
        count = dict()
        for host in hosts:
            if "batch" in host or "login" in host:
                continue
            if host not in count:
                count[host] = len(count)
        return count[socket.gethostname()]

    def __str__(self):
        return self._rep

    @staticmethod
    def detect():
        """Detect if running in an LSF environment."""
        env_vars = ["LSB_JOBID", "LSB_DJOB_RANKFILE", "JSM_NAMESPACE_LOCAL_RANK", "JSM_NAMESPACE_SIZE"]
        flags = [v in os.environ for v in env_vars]
        return any(flags)

    def creates_processes_externally(self):
        """LSF creates subprocesses -- i.e. PyTorch Lightning does not need to spawn them."""
        return True

    @property
    def main_address(self):
        """Master address is read from an OpenMPI host rank file in the environment variable *LSB_DJOB_RANKFILE*"""
        return self._main_address

    @property
    def main_port(self):
        """Master port is calculated from the LSF job ID."""
        return self._main_port

    def world_size(self):
        """World size is read from the environment variable JSM_NAMESPACE_SIZE."""
        return self._world_size

    def local_rank(self):
        """World size is read from the environment variable JSM_NAMESPACE_LOCAL_RANK."""
        return self._local_rank

    def node_rank(self):
        """Node rank is determined by the position of the current hostname in the OpenMPI host rank file stored in
        LSB_DJOB_RANKFILE."""
        return self._node_rank

    def global_rank(self):
        """World size is read from the environment variable JSM_NAMESPACE_RANK."""
        return self._global_rank

    def set_world_size(self, size: int) -> None:
        log.debug("SLURMEnvironment.set_world_size was called, but setting " "world size is not allowed. Ignored.")

    def set_global_rank(self, rank: int) -> None:
        log.debug("SLURMEnvironment.set_global_rank was called, but setting " "global rank is not allowed. Ignored.")
