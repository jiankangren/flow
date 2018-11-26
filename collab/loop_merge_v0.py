import json

import ray
import ray.rllib.agents.es as es
from ray.tune import run_experiments, grid_search
from ray.tune.registry import register_env
from ray.rllib.models import ModelCatalog, Model

from flow.controllers import RLController, IDMController, ContinuousRouter, \
    SumoLaneChangeController
from flow.core.params import SumoCarFollowingParams, SumoLaneChangeParams, \
    SumoParams, EnvParams, InitialConfig, NetParams
from flow.utils.registry import make_create_env
from flow.utils.rllib import FlowParamsEncoder
from flow.core.vehicles import Vehicles

import tensorflow as tf
import tensorflow.contrib.slim as slim

import sys

augmentation = sys.argv[1]

class PixelFlowNetwork(Model):
    def _build_layers(self, inputs, num_outputs, options):
        print(inputs)
        # Convolutional layer 1
        conv1 = tf.layers.conv2d(
          inputs=inputs,
          filters=8,
          kernel_size=[4, 4],
          padding="same",
          activation=tf.nn.relu)
        # Pooling layer 1
        pool1 = tf.layers.max_pooling2d(
          inputs=conv1,
          pool_size=[2, 2],
          strides=2)
        # Convolutional layer 2
        conv2 = tf.layers.conv2d(
          inputs=pool1,
          filters=16,
          kernel_size=[4, 4],
          padding="same",
          activation=tf.nn.relu)
        # Pooling layer 2
        pool2 = tf.layers.max_pooling2d(
          inputs=conv2,
          pool_size=[2, 2],
          strides=2)
        # Fully connected layer 1
        flat = tf.contrib.layers.flatten(pool2)
        fc1 = tf.layers.dense(
          inputs=flat,
          units=32,
          activation=tf.nn.sigmoid)
        # Fully connected layer 2
        fc2 = tf.layers.dense(
          inputs=fc1,
          units=num_outputs,
          activation=None)
        return fc2, fc1


ModelCatalog.register_custom_model("pixel_flow_network", PixelFlowNetwork)


# time horizon of a single rollout
HORIZON = 3000
# number of rollouts per training iteration
N_ROLLOUTS = 1
# number of parallel workers
N_CPUS = 2

RING_RADIUS = 50
NUM_MERGE_HUMANS = 8
NUM_MERGE_RL = 1

# note that the vehicles are added sequentially by the generator,
# so place the merging vehicles after the vehicles in the ring
vehicles = Vehicles()
# Inner ring vehicles
vehicles.add(
    veh_id="human",
    acceleration_controller=(IDMController, {
        "noise": 0.2
    }),
    lane_change_controller=(SumoLaneChangeController, {}),
    routing_controller=(ContinuousRouter, {}),
    speed_mode="no_collide",
    num_vehicles=6,
    sumo_car_following_params=SumoCarFollowingParams(minGap=0.0, tau=0.5),
    sumo_lc_params=SumoLaneChangeParams())
# A single learning agent in the inner ring
vehicles.add(
    veh_id="rl",
    acceleration_controller=(RLController, {}),
    lane_change_controller=(SumoLaneChangeController, {}),
    routing_controller=(ContinuousRouter, {}),
    speed_mode="no_collide",
    num_vehicles=NUM_MERGE_RL,
    sumo_car_following_params=SumoCarFollowingParams(minGap=0.01, tau=0.5),
    sumo_lc_params=SumoLaneChangeParams())
# Outer ring vehicles
vehicles.add(
    veh_id="merge-human",
    acceleration_controller=(IDMController, {
        "noise": 0.2
    }),
    lane_change_controller=(SumoLaneChangeController, {}),
    routing_controller=(ContinuousRouter, {}),
    speed_mode="no_collide",
    num_vehicles=NUM_MERGE_HUMANS,
    sumo_car_following_params=SumoCarFollowingParams(minGap=0.0, tau=0.5),
    sumo_lc_params=SumoLaneChangeParams())

flow_params = dict(
    # name of the experiment
    exp_tag="loop_merge_v0_%s" % augmentation,

    # name of the flow environment the experiment is running on
    env_name="TwoLoopsMergeCNNIDMEnv",

    # name of the scenario class the experiment is running on
    scenario="TwoLoopsOneMergingScenario",

    # name of the generator used to create/modify network configuration files
    generator="TwoLoopOneMergingGenerator",

    # sumo-related parameters (see flow.core.params.SumoParams)
    sumo=SumoParams(
        sim_step=0.1,
        render='gray',
    ),

    # environment related parameters (see flow.core.params.EnvParams)
    env=EnvParams(
        horizon=HORIZON,
        additional_params={
            "max_accel": 3,
            "max_decel": 5,
            "target_velocity": 10,
            "n_preceding": 2,
            "n_following": 2,
            "n_merging_in": 2,
            "augmentation": float(augmentation),
        },
    ),

    # network-related parameters (see flow.core.params.NetParams and the
    # scenario's documentation or ADDITIONAL_NET_PARAMS component)
    net=NetParams(
        no_internal_links=False,
        additional_params={
            "ring_radius": RING_RADIUS,
            "lane_length": 75,
            "inner_lanes": 1,
            "outer_lanes": 1,
            "speed_limit": 30,
            "resolution": 40,
        },
    ),

    # vehicles to be placed in the network at the start of a rollout (see
    # flow.core.vehicles.Vehicles)
    veh=vehicles,

    # parameters specifying the positioning of vehicles upon initialization/
    # reset (see flow.core.params.InitialConfig)
    initial=InitialConfig(
        x0=50,
        spacing="uniform",
        additional_params={
            "merge_bunching": 0,
        },
    ),
)

if __name__ == "__main__":
    ray.init(num_cpus=N_CPUS, num_gpus=0, redirect_output=False)

    config = es.DEFAULT_CONFIG.copy()
    config["episodes_per_batch"] = N_ROLLOUTS
    config["num_workers"] = N_ROLLOUTS
    config["eval_prob"] = 0.05
    config["noise_stdev"] = 0.01
    config["stepsize"] = 0.01
    config["observation_filter"] = "NoFilter"
    config["model"] = {"custom_model": "pixel_flow_network",
                       "custom_options": {},}

    # save the flow params for replay
    flow_json = json.dumps(
        flow_params, cls=FlowParamsEncoder, sort_keys=True, indent=4)
    config['env_config']['flow_params'] = flow_json

    create_env, env_name = make_create_env(params=flow_params, version=0)

    # Register as rllib env
    register_env(env_name, create_env)

    trials = run_experiments({
        flow_params["exp_tag"]: {
            "run": "ES",
            "env": env_name,
            "config": {
                **config
            },
            "checkpoint_freq": 10,
            "max_failures": 999,
            "stop": {
                "training_iteration": 100,
            },
            "num_samples": 6,
        },
    })