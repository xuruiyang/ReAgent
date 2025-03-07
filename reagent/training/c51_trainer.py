#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

from typing import List

import reagent.core.types as rlt
import torch
from reagent.core.configuration import resolve_defaults
from reagent.core.dataclasses import field
from reagent.core.parameters import RLParameters
from reagent.optimizer import Optimizer__Union, SoftUpdate
from reagent.training.reagent_lightning_module import ReAgentLightningModule
from reagent.training.rl_trainer_pytorch import RLTrainerMixin


class C51Trainer(RLTrainerMixin, ReAgentLightningModule):
    """
    Implementation of 51 Categorical DQN (C51)

    See https://arxiv.org/abs/1707.06887 for details
    """

    @resolve_defaults
    def __init__(
        self,
        q_network,
        q_network_target,
        actions: List[str] = field(default_factory=list),  # noqa: B008
        rl: RLParameters = field(default_factory=RLParameters),  # noqa: B008
        double_q_learning: bool = True,
        minibatch_size: int = 1024,
        minibatches_per_step: int = 1,
        num_atoms: int = 51,
        qmin: float = -100,
        qmax: float = 200,
        optimizer: Optimizer__Union = field(  # noqa: B008
            default_factory=Optimizer__Union.default
        ),
    ) -> None:
        """
        Args:
            q_network: states, action -> q-value
            q_network_target: model that provides targets
            actions(optional): list of agent's actions
            rl (optional): an instance of the RLParameter class, which
                defines relevant hyperparameters
            double_q_learning (optional): whether or not double Q learning, enabled by default,
            minibatch_size (optional): the size of the minibatch
            minibatches_per_step (optional): the number of minibatch updates
                per training step
            num_atoms (optional): number of "canonical returns"in the discretized value distributions
            qmin (optional): minimum q-value
            qmax (optional): maximum q-value
            optimizer (optional): the optimizer class and
                optimizer hyperparameters for the q network(s) optimizer
        """
        super().__init__()
        self.double_q_learning = double_q_learning
        self.minibatch_size = minibatch_size
        self.minibatches_per_step = minibatches_per_step
        self._actions = actions
        self.q_network = q_network
        self.q_network_target = q_network_target
        self.q_network_optimizer = optimizer
        self.qmin = qmin
        self.qmax = qmax
        self.num_atoms = num_atoms
        self.rl_parameters = rl
        self.register_buffer("support", None)
        self.support = torch.linspace(self.qmin, self.qmax, self.num_atoms)
        self.scale_support = (self.qmax - self.qmin) / (self.num_atoms - 1.0)

        self.register_buffer("reward_boosts", None)
        self.reward_boosts = torch.zeros([1, len(self._actions)])
        if self.rl_parameters.reward_boost is not None:
            # pyre-fixme[16]: Optional type has no attribute `keys`.
            for k in self.rl_parameters.reward_boost.keys():
                i = self._actions.index(k)
                # pyre-fixme[16]: Optional type has no attribute `__getitem__`.
                self.reward_boosts[0, i] = self.rl_parameters.reward_boost[k]

    def configure_optimizers(self):
        optimizers = [
            self.q_network_optimizer.make_optimizer_scheduler(
                self.q_network.parameters()
            )
        ]
        # soft-update
        target_params = list(self.q_network_target.parameters())
        source_params = list(self.q_network.parameters())
        optimizers.append(
            SoftUpdate.make_optimizer_scheduler(
                target_params, source_params, tau=self.tau
            )
        )
        return optimizers

    def train_step_gen(self, training_batch: rlt.DiscreteDqnInput, batch_idx: int):
        rewards = self.boost_rewards(training_batch.reward, training_batch.action)
        discount_tensor = torch.full_like(rewards, self.gamma)
        possible_next_actions_mask = training_batch.possible_next_actions_mask.float()
        possible_actions_mask = training_batch.possible_actions_mask.float()

        not_terminal = training_batch.not_terminal.float()

        if self.use_seq_num_diff_as_time_diff:
            assert self.multi_steps is None
            discount_tensor = torch.pow(self.gamma, training_batch.time_diff.float())
        if self.multi_steps is not None:
            assert training_batch.step is not None
            # pyre-fixme[16]: Optional type has no attribute `float`.
            discount_tensor = torch.pow(self.gamma, training_batch.step.float())

        next_dist = self.q_network_target.log_dist(training_batch.next_state).exp()

        if self.maxq_learning:
            # Select distribution corresponding to max valued action
            if self.double_q_learning:
                next_q_values = (
                    self.q_network.log_dist(training_batch.next_state).exp()
                    * self.support
                ).sum(2)
            else:
                next_q_values = (next_dist * self.support).sum(2)

            next_action = self.argmax_with_mask(
                next_q_values, possible_next_actions_mask
            )
            next_dist = next_dist[range(rewards.shape[0]), next_action.reshape(-1)]
        else:
            next_dist = (next_dist * training_batch.next_action.unsqueeze(-1)).sum(1)

        # Build target distribution
        target_Q = rewards + discount_tensor * not_terminal * self.support
        target_Q = target_Q.clamp(self.qmin, self.qmax)

        # rescale to indicies [0, 1, ..., N-1]
        b = (target_Q - self.qmin) / self.scale_support
        lo = b.floor().to(torch.int64)
        up = b.ceil().to(torch.int64)

        # handle corner cases of l == b == u
        # without the following, it would give 0 signal, whereas we want
        # m to add p(s_t+n, a*) to index l == b == u.
        # So we artificially adjust l and u.
        # (1) If 0 < l == u < N-1, we make l = l-1, so b-l = 1
        # (2) If 0 == l == u, we make u = 1, so u-b=1
        # (3) If l == u == N-1, we make l = N-2, so b-1 = 1
        # This first line handles (1) and (3).
        lo[(up > 0) * (lo == up)] -= 1
        # Note: l has already changed, so the only way l == u is possible is
        # if u == 0, in which case we let u = 1
        # I don't even think we need the first condition in the next line
        up[(lo < (self.num_atoms - 1)) * (lo == up)] += 1

        # distribute the probabilities
        # m_l = m_l + p(s_t+n, a*)(u - b)
        # m_u = m_u + p(s_t+n, a*)(b - l)
        m = torch.zeros_like(next_dist)
        # pyre-fixme[16]: `Tensor` has no attribute `scatter_add_`.
        m.scatter_add_(dim=1, index=lo, src=next_dist * (up.float() - b))
        m.scatter_add_(dim=1, index=up, src=next_dist * (b - lo.float()))
        log_dist = self.q_network.log_dist(training_batch.state)

        # for reporting only
        all_q_values = (log_dist.exp() * self.support).sum(2).detach()
        model_action_idxs = self.argmax_with_mask(
            all_q_values,
            possible_actions_mask if self.maxq_learning else training_batch.action,
        )

        log_dist = (log_dist * training_batch.action.unsqueeze(-1)).sum(1)

        loss = -(m * log_dist).sum(1).mean()

        if batch_idx % self.trainer.log_every_n_steps == 0:
            self.reporter.log(
                td_loss=loss,
                logged_actions=torch.argmax(training_batch.action, dim=1, keepdim=True),
                logged_propensities=training_batch.extras.action_probability,
                logged_rewards=rewards,
                model_values=all_q_values,
                model_action_idxs=model_action_idxs,
            )
            self.log("td_loss", loss, prog_bar=True)

        yield loss
        result = self.soft_update_result()
        yield result

    # pyre-fixme[56]: Decorator `torch.no_grad(...)` could not be called, because
    #  its type `no_grad` is not callable.
    @torch.no_grad()
    def boost_rewards(
        self, rewards: torch.Tensor, actions: torch.Tensor
    ) -> torch.Tensor:
        # Apply reward boost if specified
        reward_boosts = torch.sum(
            actions.float() * self.reward_boosts, dim=1, keepdim=True
        )
        return rewards + reward_boosts

    def argmax_with_mask(self, q_values, possible_actions_mask):
        # Set q-values of impossible actions to a very large negative number.
        q_values = q_values.reshape(possible_actions_mask.shape)
        q_values = q_values + RLTrainerMixin.ACTION_NOT_POSSIBLE_VAL * (
            1 - possible_actions_mask
        )
        return q_values.argmax(1)
