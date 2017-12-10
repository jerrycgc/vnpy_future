# -*- coding: utf-8 -*-
"""
@author: Daniel
@contact: 511735184@qq.com
@file: actor_critic.py
@time: 2017/11/17 11:42
"""
import numpy as np
import tensorflow as tf
from sonnet.python.modules.base import AbstractModule
from sonnet.python.modules.basic import Linear as sntLinear
from sonnet.python.modules.gated_rnn import LSTM as sntLSTM
from sonnet.python.modules.basic_rnn import DeepRNN as sntDeepRNN
from sonnet.python.modules.basic import BatchApply as sntBatchApply

from research.reforcement_learning.env import Account


def switch(inputs):
    return inputs * tf.nn.sigmoid(inputs)


def Linear(name, output_size):
    initializers = {"w": tf.truncated_normal_initializer(stddev=0.1),
                    "b": tf.constant_initializer(value=0.1)}
    regularizers = {"w": tf.contrib.layers.l2_regularizer(scale=0.1),
                    "b": tf.contrib.layers.l2_regularizer(scale=0.1)}

    return sntLinear(output_size,
                     initializers=initializers,
                     regularizers=regularizers,
                     name=name)


def build_common_network(inputs):
    batch_size = inputs.get_shape().as_list()[1]
    l1 = sntLSTM(64, name='rnn_first')
    l2 = sntLSTM(32, name='rnn_second')
    rnn = sntDeepRNN([l1, l2])
    initial_state = rnn.initial_state(batch_size)
    output_sequence, final_state = tf.nn.dynamic_rnn(rnn, inputs, initial_state=initial_state, time_major=True)
    return output_sequence


class ActorNet(AbstractModule):
    def __init__(self, name='Actor'):
        super().__init__(name=name)

    def _build(self, output_size, inputs):
        net = build_common_network(inputs)
        net = sntBatchApply(Linear('input_layer', 64))(net)
        net = switch(net)
        net = sntBatchApply(Linear('output_layer', output_size))(net)
        return tf.nn.softmax(net)

    def get_regularization(self):
        return self.get_variables(tf.GraphKeys.REGULARIZATION_LOSSES)


class CriticNet(AbstractModule):
    def __init__(self, name='critic'):
        super().__init__(name=name)

    def _build(self, inputs):
        net = build_common_network(inputs)
        net = sntBatchApply(Linear('input_layer', 64))(net)
        net = switch(net)
        net = sntBatchApply(Linear('output_layer', 1))(net)
        net = tf.squeeze(net, axis=2)
        return tf.reduce_mean(net, axis=1)

    def get_regularization(self):
        return tf.get_variable(tf.GraphKeys.REGULARIZATION_LOSSES)


CLIP_MIN = 0.01
CLIP_MAX = 0.98
ENTROPY_BETA = 0.01
MAX_GRAD_NORM = 50


def batch_choice(a, p):
    action_list = [np.random.choice(a, p=i) for i in p]
    return np.array(action_list)


# local network for advantage actor-critic which are also know as A2C
class Agent(object):
    def __init__(self, name, access, batch_size, state_size, action_size):
        self.Access = access
        self.action_size = action_size
        self.batch_size = batch_size

        with tf.variable_scope(name):
            # placeholder

            # [Time, Batch, N]
            self.inputs = tf.placeholder(
                tf.float32, [None, batch_size, state_size], 'inputs')

            # [T_MAX, Batch]
            self.actions = tf.placeholder(
                tf.int32, [None, batch_size], "actions")

            # [T_MAX]
            self.targets = tf.placeholder(tf.float32, [None], "discounted_rewards")
            self.gathers = tf.placeholder(tf.int32, [None], 'gather_list')

            # build network
            self.actor = ActorNet('actor')
            self.critic = CriticNet('critic')
            policy = self.actor(action_size, self.inputs)  # [Time, Batch, action_size]
            value = self.critic(self.inputs)  # [Time]

            # fix
            policy = tf.clip_by_value(policy, CLIP_MIN, CLIP_MAX, 'constraint')

            # interface
            self.policy = tf.gather(policy, self.gathers)  # [T_MAX, Batch, action_size]
            self.value = tf.gather(value, self.gathers)  # [T_MAX]
            self.policy_step = policy[-1]  # [Batch, action_size]
            self.value_step = value[-1]  # 1

            # build function
            self._build_losses()
            self._build_async()
            self._build_interface()
            print('graph %s' % (str(name)))

    def _build_losses(self):
        # value loss
        self.advantage = self.targets - self.value  # [T_MAX]
        value_loss = 0.5 * tf.square(self.advantage)

        # policy loss
        # [T_MAX, Batch, action_size] -> [T_MAX, Batch]
        policy_action = tf.reduce_sum(
            self.policy * tf.one_hot(self.actions, self.action_size), axis=2)
        # [T_MAX, Batch]
        policy_loss = -tf.log(policy_action) * tf.stop_gradient(
            tf.expand_dims(self.advantage, axis=1))
        # entropy loss [T_MAX, Batch]
        entropy_loss = tf.reduce_sum(self.policy * tf.log(self.policy), axis=2)

        # total loss
        self.critic_loss = tf.reduce_mean(value_loss)
        self.actor_loss = tf.reduce_mean(policy_loss + entropy_loss * ENTROPY_BETA)

        # interface
        self.a_entropy_loss = tf.reduce_mean(entropy_loss)
        self.a_policy_loss = tf.reduce_mean(policy_loss)
        self.a_value_loss = tf.reduce_mean(value_loss)
        self.a_critic_loss = self.critic_loss
        self.a_actor_loss = self.actor_loss
        self.a_advantage = tf.reduce_mean(self.advantage)
        self.a_value_mean = tf.reduce_mean(self.value)
        self.a_policy_mean = tf.reduce_mean(self.policy)

    def _build_async(self):
        global_actor_params, global_critic_params = self.Access.get_trainable()
        local_actor_params, local_critic_params = self.get_trainable()
        actor_grads = tf.gradients(self.actor_loss, list(local_actor_params))
        critic_grads = tf.gradients(self.critic_loss, list(local_critic_params))

        # Set up optimizer with global norm clipping.
        actor_grads, self.a_actor_grad = tf.clip_by_global_norm(actor_grads, MAX_GRAD_NORM)
        critic_grads, self.a_critic_grad = tf.clip_by_global_norm(critic_grads, MAX_GRAD_NORM)

        # update Access
        actor_apply = self.Access.optimizer_actor.apply_gradients(
            zip(list(actor_grads), list(global_actor_params)))
        critic_apply = self.Access.optimizer_critic.apply_gradients(
            zip(list(critic_grads), list(global_critic_params)))
        self.update_global = [actor_apply, critic_apply]

        # update ACNet
        assign_list = []
        for gv, lv in zip(global_actor_params, local_actor_params):
            assign_list.append(tf.assign(lv, gv))
        for gv, lv in zip(global_critic_params, local_critic_params):
            assign_list.append(tf.assign(lv, gv))
        self.update_local = assign_list

    def _build_interface(self):
        self.a_interface = [self.a_actor_loss,
                            self.a_actor_grad,
                            self.a_policy_mean,
                            self.a_policy_loss,
                            self.a_entropy_loss,
                            self.a_critic_loss,
                            self.a_critic_grad,
                            self.a_value_loss,
                            self.a_value_mean,
                            self.a_advantage]

    def get_trainable(self):
        return [self.actor.get_variables(), self.critic.get_variables()]

    def init_or_update_local(self, sess):
        """
        init or update local network
        :param sess:
        :return:
        """
        sess.run(self.update_local)

    def get_step_policy(self, sess, inputs):
        return sess.run(self.policy_step, {self.inputs: inputs})

    def get_step_value(self, sess, inputs):
        return sess.run(self.value_step, {self.inputs: inputs})

    def get_losses(self, sess, inputs, actions, targets, gather_list):
        """
        get all loss functions of network
        :param sess:
        :param inputs:
        :param actions:
        :param targets:
        :return:
        """
        feed_dict = {self.inputs: inputs,
                     self.actions: actions,
                     self.targets: targets,
                     self.gathers: gather_list}
        return sess.run(self.a_interface, feed_dict)

    def train_step(self, sess, inputs, actions, targets, gathers):
        feed_dict = {self.inputs: inputs,
                     self.actions: actions,
                     self.targets: targets,
                     self.gathers: gathers}
        sess.run(self.update_global, feed_dict)

    # get stochastic action for train
    def get_stochastic_action(self, sess, inputs, epsilon=0.9):
        if np.random.uniform() < epsilon:
            policy = sess.run(self.policy_step, {self.inputs: inputs})
            return batch_choice(self.action_size, policy)
        else:
            return np.random.randint(self.action_size, size=self.batch_size)

    # get deterministic action for test
    def get_deterministic_policy_action(self, sess, inputs):
        policy_step = sess.run(self.policy_step, {self.inputs: inputs})
        return np.argmax(policy_step, axis=1)


MAX_EPISODE_LENGTH = 200
GAMMA = 0.9


def batch_stack(inputs):
    # gather index
    gather_list = 63 + 16 * np.arange(len(inputs))
    # stack
    a = [inputs[0][:-16]]
    b = [i[-16:] for i in inputs]
    return np.vstack(a + b), gather_list


class Framework(object):
    def __init__(self, name, access, batch_size, state_size, action_size):
        self.Access = access
        self.AC = Agent(name, self.Access, batch_size, state_size, action_size)
        self.env = Account()
        self.name = name

    def run(self, sess, max_episodes, t_max=8):
        buffer_score = []
        buffer_loss = []
        episode = 0
        while episode < max_episodes:
            episode += 1
            episode_score, outputs = self.run_episode(sess, t_max)
            buffer_score.append(episode_score)
            buffer_loss.append(outputs)
        return buffer_score, buffer_loss

    def run_episode(self, sess, t_max=8):
        t_start = t = 0
        episode_score = 1
        buffer_state = []
        buffer_action = []
        buffer_reward = []

        self.AC.init_or_update_local(sess)
        state = self.env.reset()
        while True:
            t += 1
            action = self.AC.get_stochastic_action(sess, state)
            next_state, reward, done = self.env.step(action)

            # buffer for loop
            episode_score *= (1 + reward / 100)
            buffer_state.append(state)
            buffer_action.append(action)
            buffer_reward.append(reward)
            state = next_state

            if t - t_start == t_max or done:
                t_start = t
                terminal = self.get_bootstrap(sess, next_state, done)

                buffer_target = []
                for r in buffer_reward[::-1]:
                    terminal = r + GAMMA * terminal
                    buffer_target.append(terminal)
                buffer_target.reverse()

                # stack
                inputs, gather_list = batch_stack(buffer_state)
                actions = np.vstack(buffer_action)
                targets = np.squeeze(np.vstack(buffer_target), axis=1)

                # empty buffer
                buffer_state = []
                buffer_action = []
                buffer_reward = []

                # update Access gradients
                self.AC.train_step(sess, inputs, actions, targets, gather_list)

                # update local network
                self.AC.init_or_update_local(sess)

            if done or t > MAX_EPISODE_LENGTH:
                outputs = self.get_losses(sess, inputs, actions, targets, gather_list)
                outputs = tuple(outputs)
                if self.name == 'W0':
                    print('actor: %f, actor_grad: %f, policy mean: %f, policy: %f, entropy: %f, '
                          'critic: %f, critic_grad: %f, value: %f, value_mean: %f, advantage: %f'
                          % outputs)
                return episode_score, outputs

    def get_bootstrap(self, sess, next_state, done):
        if done:
            terminal = 0
        else:
            terminal = self.AC.get_step_value(sess, next_state)
        return terminal

    def get_losses(self, sess, inputs, actions, targets, gather_list):
        return self.AC.get_losses(sess, inputs, actions, targets, gather_list)
