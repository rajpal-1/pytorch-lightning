import functools
import importlib

TORCHXLA_AVAILABLE = importlib.util.find_spec("torch_xla") is not None
if TORCHXLA_AVAILABLE:
    import torch_xla.core.xla_model as xm
else:
    xm = None


def pl_multi_process(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):

        from multiprocessing import Process, Queue

        queue = Queue()

        def inner_f():
            try:
                queue.put(func(**kwargs))
            except Exception:
                import traceback

                traceback.print_exc()
                queue.put(None)

        proc = Process(target=inner_f, kwargs=kwargs)
        proc.start()
        proc.join()

        return queue.get()

    return wrapper


def fetch_xla_device_type(device):
    if xm is not None:
        return xm.xla_device_hw(device)
    else:
        return None


@pl_multi_process
def tpu_device_exists():
    if xm is not None:
        device = xm.xla_device()
        device_type = fetch_xla_device_type(device)
        return device_type == "TPU"
    else:
        return False


TPU_AVAILABLE = tpu_device_exists()
