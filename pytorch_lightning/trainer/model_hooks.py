import inspect
from abc import ABC, abstractmethod

from pytorch_lightning.core.lightning import LightningModule


class TrainerModelHooksMixin(ABC):

    def is_function_implemented(self, f_name):
        model = self.get_model()
        f_op = getattr(model, f_name, None)
        return callable(f_op)

    def is_overriden(self, method_name: str, model: LightningModule = None) -> bool:
        if model is None:
            model = self.get_model()
        super_object = LightningModule

        if not hasattr(model, method_name):
            # in case of calling deprecated method
            return False

        instance_attr = getattr(model, method_name)
        super_attr = getattr(super_object, method_name)

        # when code pointers are different, it was overriden
        is_overriden = str(instance_attr.__code__).hash() is not str(super_attr.__code__).hash()
        return is_overriden

    def has_arg(self, f_name, arg_name):
        model = self.get_model()
        f_op = getattr(model, f_name, None)
        return arg_name in inspect.signature(f_op).parameters

    @abstractmethod
    def get_model(self):
        """Warning: this is just empty shell for code implemented in other class."""
