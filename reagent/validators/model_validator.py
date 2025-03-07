#!/usr/bin/env python3

import abc
import inspect
import logging
from typing import List, Optional

from reagent.core.registry_meta import RegistryMeta
from reagent.core.result_registries import ValidationResult
from reagent.workflow.types import RLTrainingOutput


logger = logging.getLogger(__name__)


class ModelValidator(metaclass=RegistryMeta):
    """
    Base class for model validator. All validator should subclass from this so that
    they can be registered in the workflows.
    """

    def validate(
        self,
        training_output: RLTrainingOutput,
        result_history: Optional[List[RLTrainingOutput]] = None,
    ):
        """
        This method takes RLTrainingOutput so that it can extract anything it
        might need from it.
        """
        result = self.do_validate(training_output, result_history)
        # Avoid circular dependency at import time
        from reagent.workflow.types import ValidationResult__Union

        # We need to use inspection because the result can be a future when running on
        # FBL
        result_type = inspect.signature(self.do_validate).return_annotation
        assert result_type != inspect.Signature.empty
        # pyre-fixme[16]: `ValidationResult__Union` has no attribute
        #  `make_union_instance`.
        # pyre-fixme[16]: `ValidationResult__Union` has no attribute
        #  `make_union_instance`.
        return ValidationResult__Union.make_union_instance(result, result_type)

    @abc.abstractmethod
    def do_validate(
        self,
        training_output: RLTrainingOutput,
        result_history: Optional[List[RLTrainingOutput]],
    ) -> ValidationResult:
        """
        This method takes RLTrainingOutput so that it can extract anything it
        might need from it.
        """
        pass
