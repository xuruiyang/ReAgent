#!/usr/bin/env python3

import logging
from typing import Optional, Dict

import numpy as np
import reagent.core.types as rlt
import torch
from reagent.core.dataclasses import dataclass, field
from reagent.core.parameters import (
    CEMTrainerParameters,
    param_hash,
    NormalizationData,
    NormalizationKey,
)
from reagent.gym.policies.policy import Policy
from reagent.model_managers.model_based.world_model import WorldModel
from reagent.model_managers.world_model_base import WorldModelBase
from reagent.models.cem_planner import CEMPlannerNetwork
from reagent.preprocessing.identify_types import CONTINUOUS_ACTION
from reagent.preprocessing.normalization import get_num_output_features
from reagent.training import ReAgentLightningModule
from reagent.training.cem_trainer import CEMTrainer
from reagent.workflow.types import RewardOptions


logger = logging.getLogger(__name__)


class CEMPolicy(Policy):
    def __init__(self, cem_planner_network: CEMPlannerNetwork, discrete_action: bool):
        self.cem_planner_network = cem_planner_network
        self.discrete_action = discrete_action

    # TODO: consider possible_actions_mask
    def act(
        self, obs: rlt.FeatureData, possible_actions_mask: Optional[np.ndarray] = None
    ) -> rlt.ActorOutput:
        greedy = self.cem_planner_network(obs)
        if self.discrete_action:
            _, onehot = greedy
            return rlt.ActorOutput(
                action=onehot.unsqueeze(0), log_prob=torch.tensor(0.0)
            )
        else:
            return rlt.ActorOutput(
                action=greedy.unsqueeze(0), log_prob=torch.tensor(0.0)
            )


@dataclass
class CrossEntropyMethod(WorldModelBase):
    __hash__ = param_hash

    trainer_param: CEMTrainerParameters = field(default_factory=CEMTrainerParameters)

    def __post_init_post_parse__(self):
        super().__post_init_post_parse__()

    # TODO: should this be in base class?
    def create_policy(
        self,
        trainer_module: ReAgentLightningModule,
        serving: bool = False,
        normalization_data_map: Optional[Dict[str, NormalizationData]] = None,
    ) -> Policy:
        assert isinstance(trainer_module, CEMTrainer)
        # pyre-fixme[16]: `CrossEntropyMethod` has no attribute `discrete_action`.
        return CEMPolicy(trainer_module.cem_planner_network, self.discrete_action)

    def build_trainer(
        self,
        normalization_data_map: Dict[str, NormalizationData],
        use_gpu: bool,
        reward_options: Optional[RewardOptions] = None,
    ) -> CEMTrainer:
        # pyre-fixme[45]: Cannot instantiate abstract class `WorldModel`.
        world_model_manager: WorldModel = WorldModel(
            trainer_param=self.trainer_param.mdnrnn
        )
        world_model_manager.build_trainer(
            use_gpu=use_gpu,
            reward_options=reward_options,
            normalization_data_map=normalization_data_map,
        )
        world_model_trainers = [
            world_model_manager.build_trainer(
                normalization_data_map, reward_options=reward_options, use_gpu=use_gpu
            )
            for _ in range(self.trainer_param.num_world_models)
        ]
        world_model_nets = [trainer.memory_network for trainer in world_model_trainers]
        terminal_effective = self.trainer_param.mdnrnn.not_terminal_loss_weight > 0

        action_normalization_parameters = normalization_data_map[
            NormalizationKey.ACTION
        ].dense_normalization_parameters
        sorted_action_norm_vals = list(action_normalization_parameters.values())
        discrete_action = sorted_action_norm_vals[0].feature_type != CONTINUOUS_ACTION
        action_upper_bounds, action_lower_bounds = None, None
        if not discrete_action:
            action_upper_bounds = np.array(
                [v.max_value for v in sorted_action_norm_vals]
            )
            action_lower_bounds = np.array(
                [v.min_value for v in sorted_action_norm_vals]
            )

        cem_planner_network = CEMPlannerNetwork(
            mem_net_list=world_model_nets,
            cem_num_iterations=self.trainer_param.cem_num_iterations,
            cem_population_size=self.trainer_param.cem_population_size,
            ensemble_population_size=self.trainer_param.ensemble_population_size,
            num_elites=self.trainer_param.num_elites,
            plan_horizon_length=self.trainer_param.plan_horizon_length,
            state_dim=get_num_output_features(
                normalization_data_map[
                    NormalizationKey.STATE
                ].dense_normalization_parameters
            ),
            action_dim=get_num_output_features(
                normalization_data_map[
                    NormalizationKey.ACTION
                ].dense_normalization_parameters
            ),
            discrete_action=discrete_action,
            terminal_effective=terminal_effective,
            gamma=self.trainer_param.rl.gamma,
            alpha=self.trainer_param.alpha,
            epsilon=self.trainer_param.epsilon,
            action_upper_bounds=action_upper_bounds,
            action_lower_bounds=action_lower_bounds,
        )
        # store for building policy
        # pyre-fixme[16]: `CrossEntropyMethod` has no attribute `discrete_action`.
        self.discrete_action = discrete_action
        logger.info(
            f"Built CEM network with discrete action = {discrete_action}, "
            f"action_upper_bound={action_upper_bounds}, "
            f"action_lower_bounds={action_lower_bounds}"
        )
        return CEMTrainer(
            cem_planner_network=cem_planner_network,
            world_model_trainers=world_model_trainers,
            parameters=self.trainer_param,
        )
