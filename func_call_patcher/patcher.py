import abc
import functools
import inspect
import sys
from typing import Callable, Optional, Union

from attrs import define, field

from mock import Mock, patch

from . import hints
from .import_tool import ImportTool

PATCHED_BY_FUNC_CALL_PATCHER_TAG = '_patched_by_func_call_patcher'


def get_method_name_from_path(path_to_func: str):
    return path_to_func.split('.')[-1]


class IPatcher(abc.ABC):
    @abc.abstractmethod
    def __init__(
        self,
        path_to_func: str,
        executable_module_name: str,
        line_where_func_executed: int,
        decorator_inner_func: hints.DecoratorInnerFunc,
        relationship_identifier: Optional[hints.RelationshipIdentifier] = None,
    ):
        raise NotImplementedError

    @abc.abstractmethod
    def __enter__(self, *args, **kwargs):
        raise NotImplementedError

    @abc.abstractmethod
    def __exit__(self, *args, **kwargs):
        raise NotImplementedError


class BasePatcher(IPatcher):
    def __init__(
        self,
        path_to_func: str,
        executable_module_name: str,
        line_number_where_func_executed: int,
        decorator_inner_func: hints.DecoratorInnerFunc,
        relationship_identifier: Optional[hints.RelationshipIdentifier] = None,
    ):
        self.path_to_func = path_to_func
        self.decorator_inner_func = decorator_inner_func
        self.line_where_func_executed = line_number_where_func_executed
        self.executable_module_name = executable_module_name
        self.relationship_identifier = relationship_identifier

    def _decorate_func_call(self, func: Callable):
        @functools.wraps(func)
        def inner(*args, **kwargs):
            frame = sys._getframe()
            while frame.f_back:
                full_path_of_module_in_executed_frame = frame.f_code.co_filename
                if (
                    frame.f_lineno == self.line_where_func_executed and    # noqa
                    full_path_of_module_in_executed_frame.endswith(self.executable_module_name)
                ):
                    result = self.decorator_inner_func(func, args, kwargs, frame, self.relationship_identifier)
                    return result
                frame = frame.f_back
            return func(*args, **kwargs)

        # помечаем функцию внутреннюю функцию как функция из func_call_patcher
        # это нужно для MethodPatcher._is_method_aready_patched
        setattr(inner, PATCHED_BY_FUNC_CALL_PATCHER_TAG, True)
        return inner

    @property
    @abc.abstractmethod
    def is_patched(self) -> bool:
        raise NotImplementedError

    def __enter__(self, *args, **kwargs):
        raise NotImplementedError

    def __exit__(self, *args, **kwargs):
        raise NotImplementedError


class FuncPatcher(BasePatcher):
    @define
    class DataContainer:
        does_func_need_a_patch: bool = field(init=False)
        patcher = field(init=False)

    def _is_func_already_patched(self, func) -> bool:
        return isinstance(func, Mock) and hasattr(func, PATCHED_BY_FUNC_CALL_PATCHER_TAG)

    @property
    def is_patched(self) -> bool:
        return hasattr(self, 'data_container') and self.data_container.does_func_need_a_patch

    def __enter__(self, *args, **kwargs):
        func_to_patch = ImportTool.import_func_from_string(path_to_func=self.path_to_func)
        self.data_container = self.DataContainer()

        if self._is_func_already_patched(func=func_to_patch):
            self.data_container.does_func_need_a_patch = False
            return self
        self.data_container.does_func_need_a_patch = True
        mock = Mock()
        setattr(mock, PATCHED_BY_FUNC_CALL_PATCHER_TAG, True)
        mock.side_effect = self._decorate_func_call(func=func_to_patch)

        self.data_container.patcher = patch(self.path_to_func, mock)
        self.data_container.patcher.__enter__()
        return self

    def __exit__(self, *args, **kwargs):
        if not self.data_container.does_func_need_a_patch:
            return
        self.data_container.patcher.__exit__(*args, **kwargs)


class MethodPatcherFacade(BasePatcher):
    @define
    class DataContainer:
        patcher: Union['MethodPatcher', 'PropertyPatcher'] = field(init=False)

    @property
    def is_patched(self) -> bool:
        return hasattr(self, 'data_container') and self.data_container.patcher.data_container.does_need_a_patch

    def _check_is_method(self, method):
        return inspect.isfunction(method) or inspect.ismethod(method)

    def __enter__(self, *args, **kwargs):
        class_obj, method = ImportTool.import_class_and_method_from_string(path_to_func=self.path_to_func)
        method_name = get_method_name_from_path(path_to_func=self.path_to_func)
        # в общем случае method.__name__ != method_name, например если на method навещан декоратор без wraps
        # нам нужен именно "настоящий" method_name из пути до функции
        self.data_container = self.DataContainer()
        if isinstance(method, property):
            property_patcher = PropertyPatcher(
                class_obj=class_obj,
                property_=method,
                property_name=method_name,
                decorate_func_call=self._decorate_func_call,
            )
            property_patcher.patch()
            self.data_container.patcher = property_patcher

        if self._check_is_method(method=method):
            method_patcher = MethodPatcher(
                class_obj=class_obj,
                method=method,
                method_name=method_name,
                decorate_func_call=self._decorate_func_call,
            )
            method_patcher.patch()
            self.data_container.patcher = method_patcher

    def __exit__(self, *args, **kwargs):
        self.data_container.patcher.unpatch()


class MethodPatcher:
    @define
    class DataContainer:
        does_need_a_patch: bool = field(init=False)
        original_method: Callable = field(init=False)

    def __init__(
        self,
        class_obj: type,
        method: Callable,
        method_name: str,
        decorate_func_call: Callable,
    ):
        self.class_obj = class_obj
        self.method = method
        self.method_name = method_name
        self.decorate_func_call = decorate_func_call
        self.data_container = self.DataContainer()

    def _is_method_aready_patched(self) -> bool:
        return hasattr(self.method, PATCHED_BY_FUNC_CALL_PATCHER_TAG)

    def patch(self) -> None:
        if self._is_method_aready_patched():
            self.data_container.does_need_a_patch = False
            return None

        self.data_container.does_need_a_patch = True
        setattr(self.class_obj, self.method_name, self.decorate_func_call(func=self.method))
        self.data_container.original_method = self.method

    def unpatch(self):
        if not self.data_container.does_need_a_patch:
            return

        setattr(
            self.class_obj,
            self.method_name,
            self.data_container.original_method,
        )
        return


class PropertyPatcher:
    @define
    class DataContainer:
        does_need_a_patch: bool = field(init=False)
        original_property: property = field(init=False)

    def __init__(self, class_obj: type, property_: property, property_name: str, decorate_func_call: Callable):
        self.class_obj = class_obj
        self.property_ = property_
        self.property_name = property_name
        self.decorate_func_call = decorate_func_call
        self.data_container = self.DataContainer()

    def _is_property_already_patched(self) -> bool:
        return hasattr(self.property_.fget, PATCHED_BY_FUNC_CALL_PATCHER_TAG)

    def patch(self) -> None:
        if self._is_property_already_patched():
            self.data_container.does_need_a_patch = False
            return None

        self.data_container.does_need_a_patch = True
        patched_property = property(
            fget=self.decorate_func_call(func=self.property_.fget),
            fset=self.property_.fset,
            fdel=self.property_.fdel,
        )
        setattr(self.class_obj, self.property_name, patched_property)
        self.data_container.original_property = self.property_

    def unpatch(self):
        if not self.data_container.does_need_a_patch:
            return

        setattr(
            self.class_obj,
            self.property_name,
            self.data_container.original_property,
        )
        return
