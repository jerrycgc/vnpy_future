# -*- coding: utf-8 -*-
"""
@author: Daniel
@contact: 511735184@qq.com
@file: train.py
@time: 2017/11/17 11:41
"""
import os
import multiprocessing
import threading

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import tensorflow as tf

from research.reforcement_learning.access import Access
from research.reforcement_learning.actor_critic import Framework, Agent
from research.reforcement_learning.env import Account

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # Hide messy TensorFlow warnings
sns.set_style('whitegrid')

NUMS_CPU = multiprocessing.cpu_count()
state_size = 58
batch_size = 50
action_size = 3
max_episodes = 1
GD = {}


class Worker(Framework):
    def __init__(self, name, access, batch_size, state_size, action_size):
        super().__init__(name, access, batch_size, state_size, action_size)

    def run(self, sess, max_episodes, t_max=8):
        episode_score_list = []
        episode = 0
        while episode < max_episodes:
            episode += 1
            episode_socre, _ = self.run_episode(sess, t_max)
            episode_score_list.append(episode_socre)
            GD[str(self.name)] = episode_score_list
            if self.name == 'W0':
                print('Episode: %f, score: %f' % (episode, episode_socre))
                print('\n')


def train():
    with tf.Session() as sess:
        with tf.device("/cpu:0"):
            A = Access(batch_size, state_size, action_size)
            F_list = []
            for i in range(NUMS_CPU):
                F_list.append(Worker('W%i' % i, A, batch_size, state_size, action_size))
            COORD = tf.train.Coordinator()
            sess.run(tf.global_variables_initializer())
            sess.graph.finalize()

            threads_list = []
            for ac in F_list:
                job = lambda: ac.run(sess, max_episodes)
                t = threading.Thread(target=job)
                t.start()
                threads_list.append(t)
            COORD.join(threads_list)
            A.save(sess, 'model/saver_1.ckpt')


def predict():
    state_size = 58
    batch_size = 50
    action_size = 3

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        with tf.device("/cpu:0"):
            A = Access(batch_size, state_size, action_size)
            W = Agent('W0', A, batch_size, state_size, action_size)
            A.restore(sess, 'model/saver_1.ckpt')
            W.init_or_update_local(sess)
            env = Account()
            state = env.reset()
            for _ in range(200):
                action = W.get_deterministic_policy_action(sess, state)
                state, reward, done = env.step(action)

    value, reward = env.plot_data()

    pd.Series(value).plot(figsize=(16, 6))
    pd.Series(reward).plot(figsize=(16, 6))
    pd.Series(np.zeros_like(reward)).plot(figsize=(16, 6), color='r')
    plt.show()


if __name__ == '__main__':
    predict()
