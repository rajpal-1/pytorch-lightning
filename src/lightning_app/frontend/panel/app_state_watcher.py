"""The `AppStateWatcher` enables a Frontend to.

- subscribe to App state changes
- to access and change the App state.

This is particularly useful for the `PanelFrontend` but can be used by other frontends too.
"""
from __future__ import annotations

import logging
import os

from lightning_app.frontend.panel.app_state_comm import watch_app_state
from lightning_app.frontend.utils import _get_flow_state
from lightning_app.utilities.imports import _is_param_available, requires
from lightning_app.utilities.state import AppState

_logger = logging.getLogger(__name__)


if _is_param_available():
    from param import ClassSelector, edit_constant, Parameterized
else:
    Parameterized = object
    ClassSelector = dict


class AppStateWatcher(Parameterized):
    """The `AppStateWatcher` enables a Frontend to:

    - Subscribe to any App state changes.
    - To access and change the App state from the UI.

    This is particularly useful for the `PanelFrontend , but can be used by
    other frontends too.

    Example
    -------

    .. code-block:: python

        import param

        app = AppStateWatcher()

        app.state.counter = 1


        @param.depends(app.param.state, watch=True)
        def update(state):
            print(f"The counter was updated to {state.counter}")


        app.state.counter += 1

    This would print ``The counter was updated to 2``.

    The `AppStateWatcher  is built on top of Param which is a framework like dataclass, attrs and
    Pydantic which additionally provides powerful and unique features for building reactive apps.

    Please note the `AppStateWatcher` is a singleton, i.e. only one instance is instantiated
    """

    state: AppState = ClassSelector(
        class_=AppState,
        constant=True,
        doc="The AppState holds the state of the app reduced to the scope of the Flow",
    )

    def __new__(cls):
        # This makes the AppStateWatcher a *singleton*.
        # The AppStateWatcher is a singleton to minimize the number of requests etc..
        if not hasattr(cls, "_instance"):
            cls._instance = super().__new__(cls)
        return cls._instance

    @requires("param")
    def __init__(self):
        # It is critical to initialize only once
        # See https://github.com/holoviz/param/issues/643
        if not hasattr(self, "_initialized"):
            super().__init__(name="singleton")
            self._start_watching()
            self.param.state.allow_None = False
            self._initialized = True

        # The below was observed when using mocks during testing
        if not self.state:
            raise Exception(".state has not been set.")
        if not self.state._state:
            raise Exception(".state._state has not been set.")

    def _start_watching(self):
        # Create a thread listening to state changes.
        watch_app_state(self._update_flow_state)
        self._update_flow_state()

    def _get_flow_state(self) -> AppState:
        flow = os.environ["LIGHTNING_FLOW_NAME"]
        return _get_flow_state(flow)

    def _update_flow_state(self):
        # Todo: Consider whether to only update if ._state changed
        # This might be much more performant.
        with edit_constant(self):
            self.state = self._get_flow_state()
        _logger.debug("Requested App State.")
