#!/usr/bin/env python3

import bisect
import functools
import itertools
from typing import List, Dict, Tuple

import torch.nn as nn
from reagent.core.utils import lazy_property

from .reagent_lightning_module import ReAgentLightningModule


class MultiStageTrainer(ReAgentLightningModule):
    def __init__(
        self,
        trainers: List[ReAgentLightningModule],
        epochs: List[int],
        assign_reporter_function=None,
        flush_reporter_function=None,
        automatic_optimization=True,
    ):
        super().__init__(automatic_optimization=automatic_optimization)
        # NB: wrapping in a ModuleList so the state can be saved
        self._trainers = nn.ModuleList(trainers)
        self._assign_reporter_function = assign_reporter_function
        self._flush_reporter_function = (
            functools.partial(flush_reporter_function, self)
            if flush_reporter_function
            else self._flush_reporter
        )
        self._in_testing_loop = False
        # Cumulative sum of number of epochs up to the index (of trainers)
        self._trainer_epochs = [0] + epochs
        for i in range(1, len(epochs) + 1):
            self._trainer_epochs[i] += self._trainer_epochs[i - 1]

    def set_reporter(self, reporter):
        super().set_reporter(reporter)
        if self._assign_reporter_function:
            self._assign_reporter_function(self._trainers, reporter)
        else:
            # By default, assume CompoundReporter with the same
            # number of reporters as trainers
            assert len(self._trainers) == len(
                reporter._reporters
            ), f"{len(self._trainers)} != {len(reporter._reporters)}"
            for t, r in zip(self._trainers, reporter._reporters):
                t.set_reporter(r)

    @lazy_property
    def _optimizer_step_to_trainer_idx(self) -> Dict[int, Tuple[int, int]]:
        mapping = {}
        offset = 0

        for i, t in enumerate(self._trainers):
            num_optimizing_steps = t._num_optimizing_steps
            for j in range(num_optimizing_steps):
                mapping[offset + j] = (i, offset)
            offset += num_optimizing_steps

        return mapping

    def _flush_reporter(self, reporter, epoch):
        """
        By default, assume CompoundReporter with the same
        number of reporters as trainers
        """
        if not self._in_testing_loop:
            epoch_trainer_idx = self._get_trainer_idx_from_epoch()
            reporter._reporters[epoch_trainer_idx].flush(epoch)
        else:
            for r in reporter._reporters:
                r.flush(epoch)

    def on_fit_start(self):
        self._starting_epoch = self.trainer.current_epoch
        # Connecting pl.Trainer to stage trainers
        for t in self._trainers:
            t.trainer = self.trainer
            t.on_fit_start()

        self.reporter.set_flush_function(self._flush_reporter_function)

    def on_fit_end(self):
        del self._starting_epoch
        # Disconnecting
        for t in self._trainers:
            t.on_fit_end()
            del t.trainer

        self.reporter.set_flush_function(None)

    def on_test_start(self):
        self._starting_epoch = self.trainer.current_epoch
        self._in_testing_loop = True

        for t in self._trainers:
            t.on_test_start()

    def on_test_end(self):
        del self._starting_epoch
        self._in_testing_loop = False
        for t in self._trainers:
            t.on_test_end()

    def _get_trainer_idx_from_epoch(self):
        # Cycling through the trainers
        epoch = (self.trainer.current_epoch - self._starting_epoch) % (
            self._trainer_epochs[-1]
        )
        trainer_idx = bisect.bisect_right(self._trainer_epochs, epoch) - 1

        return trainer_idx

    def configure_optimizers(self):
        # FIXME: Doesn't support LRScheduler yet
        return list(
            itertools.chain(*[t.configure_optimizers() for t in self._trainers])
        )

    def training_step(self, batch, batch_idx: int, optimizer_idx: int = 0):
        trainer_idx, offset = self._optimizer_step_to_trainer_idx[optimizer_idx]
        epoch_trainer_idx = self._get_trainer_idx_from_epoch()
        assert (
            trainer_idx == epoch_trainer_idx
        ), f"Got {trainer_idx}; expected {epoch_trainer_idx}"
        return self._trainers[trainer_idx].training_step(
            batch, batch_idx, optimizer_idx - offset
        )

    def training_epoch_end(self, outputs):
        epoch_trainer_idx = self._get_trainer_idx_from_epoch()
        self._trainers[epoch_trainer_idx].training_epoch_end(outputs)

    def validation_step(self, *args, **kwargs):
        epoch_trainer_idx = self._get_trainer_idx_from_epoch()
        return self._trainers[epoch_trainer_idx].validation_step(*args, **kwargs)

    def validation_epoch_end(self, outputs):
        epoch_trainer_idx = self._get_trainer_idx_from_epoch()
        self._trainers[epoch_trainer_idx].validation_epoch_end(outputs)

    def test_step(self, *args, **kwargs):
        return {
            str(i): trainer.test_step(*args, **kwargs)
            for i, trainer in enumerate(self._trainers)
        }

    def test_epoch_end(self, outputs):
        for i, trainer in enumerate(self._trainers):
            trainer.test_epoch_end([o[str(i)] for o in outputs])

    def optimizer_step(
        self,
        epoch: int,
        batch_idx: int,
        optimizer,
        optimizer_idx: int,
        optimizer_closure,
        on_tpu: int = False,
        using_native_amp: int = False,
        using_lbfgs: int = False,
    ):
        assert epoch == self.trainer.current_epoch
        epoch_trainer_idx = self._get_trainer_idx_from_epoch()
        optimizer_trainer_idx, offset = self._optimizer_step_to_trainer_idx[
            optimizer_idx
        ]

        if epoch_trainer_idx == optimizer_trainer_idx:
            # FIXME: epoch argument is not really correct
            # Trainer will see the total epochs, including those epochs they
            # are inactive.
            self._trainers[epoch_trainer_idx].optimizer_step(
                epoch,
                batch_idx,
                optimizer,
                optimizer_idx - offset,
                optimizer_closure,
                on_tpu=on_tpu,
                using_native_amp=using_native_amp,
                using_lbfgs=using_lbfgs,
            )
