"""
Implementation of PPO using PyTorch
Works with gym style environments
For an example of how to use see main below
"""

import torch
import torch.nn as nn
from torch.optim import Adam
from torch.distributions.normal import Normal
import numpy as np
import os


def construct_mlp(observation_size, hidden_layers, action_size):
    layers = []
    layer_sizes = [observation_size] + hidden_layers + [action_size]
    for i in range(len(layer_sizes)-1):
        layers.append(nn.Linear(layer_sizes[i], layer_sizes[i+1]))
        layers.append(nn.ReLU())
    layers.append(nn.Linear(layer_sizes[-1], action_size))

    return nn.Sequential(*layers)


class Actor(nn.Module):

    def __init__(self, observation_size, hidden_layers, action_size, lr):
        super().__init__()
        self.pi_net = construct_mlp(observation_size, hidden_layers, action_size)
        self.optimizer = Adam(self.pi_net.parameters(), lr=lr)
        self.log_std = -0.5*torch.ones(action_size)
        # TODO: move to gpu

    def forward(self, observation):
        pi = self.get_distribution(observation)
        action = pi.sample()
        log_prob = pi.log_prob(action).sum(axis=-1)
        return action, log_prob

    def backward(self, loss):
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

    def get_distribution(self, observation):
        mu = self.pi_net(observation)
        std = torch.exp(self.log_std)
        pi = Normal(mu, std)
        return pi


class Critic(nn.Module):

    def __init__(self, observation_size, hidden_layers, lr):
        super().__init__()
        self.v_net = construct_mlp(observation_size, hidden_layers, 1)
        self.optimizer = Adam(self.v_net.parameters(), lr=lr)
        # TODO: move to gpu

    def forward(self, observation):
        return self.v_net(observation)

    def backward(self, loss):
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()


class ActorCritic():

    def __init__(self, observation_size, action_size, actor_hidden, critic_hidden, actor_lr, critic_lr):
        self.pi_actor = Actor(observation_size, actor_hidden, action_size, actor_lr)
        self.v_critic = Critic(observation_size, critic_hidden, critic_lr)

    def forward(self, observation):
        observation = torch.as_tensor(observation, dtype=torch.float32)
        action, log_prob = self.pi_actor.forward(observation)
        value = self.v_critic.forward(observation)
        return action, log_prob, value

    def forward_actor_only(self, observation):
        action, _ = self.pi_actor.forward(torch.as_tensor(observation, dtype=torch.float32))
        return action

    def return_actor(self):
        return self.pi_actor

    def backward(self, pi_loss, v_loss):
        self.pi_actor.backward(pi_loss)
        self.v_critic.backward(v_loss)


class TrajectoryData():

    def __init__(self):
        self.observations = []
        self.actions = []
        self.values = []
        self.rewards = []
        self.log_probs = []

        self.returns = []
        self.advantages = []

    def store(self, observation, action, value, reward, log_prob):
        self.observations.append(observation)
        self.actions.append(action)
        self.values.append(value)
        self.rewards.append(reward)
        self.log_probs.append(log_prob)

    def clear(self):
        self.__init__()


class DataManager():

    def __init__(self, gamma, lambda_):
        self.gamma = gamma
        self.lambda_ = lambda_
        self.observations = []
        self.actions = []
        self.values = []
        self.rewards = []
        self.log_probs = []
        self.returns = []
        self.advantages = []

    def process_and_store(self, trajectory_data, bootstrap_value):
        self._calculate_discounted_return(trajectory_data, bootstrap_value)
        self._calculate_advantage(trajectory_data, bootstrap_value)
        self._store(trajectory_data)

    def _calculate_discounted_return(self, trajectory_data, bootstrap_value):
        rewards = trajectory_data.rewards + [bootstrap_value]
        discounted_returns = self._discounted_sum(rewards, self.gamma)[:-1]
        trajectory_data.returns = discounted_returns

    def _calculate_advantage(self, trajectory_data, bootstrap_value):
        rewards = np.array(trajectory_data.rewards + [bootstrap_value])
        values = np.array(trajectory_data.values + [bootstrap_value])

        deltas = rewards[:-1] + self.gamma*values[1:] - values[:-1]
        advantages = self._discounted_sum(deltas, self.gamma*self.lambda_)
        trajectory_data.advantages = advantages

    def _discounted_sum(self, data, discount_factor):
        discounted_sums = []
        for i in range(len(data)):
            sum = 0
            for j, d in enumerate(data[i:]):
                sum += discount_factor**j * d
            if isinstance(d, float): # the -100 reward due to falling is a float instead of a tensor
                print("DETECTED FLOAT")
                sum = torch.as_tensor(sum, dtype=torch.float64)
            discounted_sums.append(sum)
        return discounted_sums

    def _store(self, trajectory_data):
        self.observations += trajectory_data.observations
        self.actions += trajectory_data.actions
        self.values += trajectory_data.values
        self.rewards += trajectory_data.rewards
        self.log_probs += trajectory_data.log_probs
        self.returns += trajectory_data.returns
        self.advantages += trajectory_data.advantages

    def get_pi_data(self):
        observations = torch.as_tensor(self.observations, dtype=torch.float32)
        actions = torch.stack(self.actions)
        old_log_probs = torch.stack(self.log_probs).detach()
        advantages = torch.as_tensor(self.advantages, dtype=torch.float32)
        return observations, actions, old_log_probs, advantages

    def get_v_data(self):
        observations = torch.as_tensor(self.observations, dtype=torch.float32)
        returns = torch.as_tensor(self.returns, dtype=torch.float32)
        return observations, returns

    def clear(self):
        self.__init__(self.gamma, self.lambda_)


class PPO():
    """
    The main class that should be constructed externally
    """
    # # TODO: implement logging
    def __init__(self, env, actor_hidden=[32,32], critic_hidden=[32,32], actor_lr=0.0003, critic_lr=0.001, gamma=0.99,
                 lambda_=0.97, clip_epsilon=0.2, env_steps_per_epoch=4000, iterations_per_epoch=20, save_frequency=10,
                 save_dir="model"):
        self.env = env
        self.env_steps_per_epoch = env_steps_per_epoch
        self.iterations_per_epoch = iterations_per_epoch
        self.clip_epsilon = clip_epsilon
        self.save_frequency = save_frequency
        self.save_dir = save_dir
        self.actor_critic = ActorCritic(env.observation_space.shape[0], env.action_space.shape[0], actor_hidden, critic_hidden, actor_lr, critic_lr)
        self.data_manager = DataManager(gamma, lambda_)

    def train(self, epochs = 5):
        for i in range(epochs):
            print(f"epoch {i}")
            self._train_one_epoch()
            if i % self.save_frequency == 0:
                self.save_model(i)
        self.save_model(i)

    def _train_one_epoch(self):
        self._collect_trajectories()
        for _ in range(self.iterations_per_epoch):
            pi_loss = self._compute_pi_loss()
            v_loss = self._compute_v_loss()
            self.actor_critic.backward(pi_loss, v_loss)

    def _collect_trajectories(self):
        self.data_manager.clear()
        trajectory_data = TrajectoryData()
        observation = self.env.reset()
        for i in range(self.env_steps_per_epoch):
            action, log_prob, value = self.actor_critic.forward(observation)
            new_observation, reward, done, _ = self.env.step(action.detach())
            trajectory_data.store(observation, action, value, reward, log_prob)

            observation = new_observation
            if done is True:
                print("episode return: ", sum(trajectory_data.rewards).item())
                self.data_manager.process_and_store(trajectory_data, 0)
                trajectory_data.clear()
                observation = self.env.reset()
                done = False

        bootstrap_value = self.actor_critic.v_critic(torch.as_tensor(observation, dtype=torch.float32)).squeeze()
        self.data_manager.process_and_store(trajectory_data, bootstrap_value)

    def _compute_pi_loss(self): # TODO implement KL early stopping
        observations, actions, old_log_probs, advantages = self.data_manager.get_pi_data()

        pi = self.actor_critic.pi_actor.get_distribution(observations)
        log_probs = pi.log_prob(actions).sum(axis=-1)
        ratios = torch.exp(log_probs - old_log_probs)
        clipped_advantages = advantages * torch.clamp(ratios, 1-self.clip_epsilon, 1+self.clip_epsilon)

        loss = -torch.min(ratios * advantages, clipped_advantages).mean()
        return loss

    def _compute_v_loss(self):
        observations, returns = self.data_manager.get_v_data()
        values = self.actor_critic.v_critic(observations)

        loss = ((values - returns)**2).mean()
        return loss

    def run_and_render(self, runs=3, reward_floor=-7):
        for _ in range(runs):
            observation = env.reset()
            env.render()
            done = False
            cumulative_reward = 0
            while not done:
                action = self.actor_critic.forward_actor_only(observation)
                observation, reward, done, _ = env.step(action.detach())
                cumulative_reward += reward
                env.render()
                if cumulative_reward < reward_floor:
                    break
            env.close()

    def save_model(self, i):
        save_path = os.path.join(os.getcwd(), self.save_dir)
        if not os.path.isdir(save_path):
            os.mkdir(save_path)
        torch.save(self.actor_critic.pi_actor.state_dict(), os.path.join(save_path, f"epoch_{i}_actor"))
        torch.save(self.actor_critic.v_critic.state_dict(), os.path.join(save_path, f"epoch_{i}_critic"))

    def load_model(self, folder_name, epoch_number):
        actor_path = os.path.join(os.getcwd(), folder_name, f"epoch_{epoch_number}_actor")
        self.actor_critic.pi_actor.load_state_dict(torch.load(actor_path))

        critic_path = os.path.join(os.getcwd(), folder_name, f"epoch_{epoch_number}_critic")
        self.actor_critic.v_critic.load_state_dict(torch.load(critic_path))


"""
Example showing how to use this PPO module below
"""
if __name__ == "__main__":
    import gym

    env = gym.make("BipedalWalker-v3")

    ppo = PPO(env)
    ppo.train(20)
    ppo.run_and_render()

    # ppo = PPO(env)
    # ppo.load_model("model", 19)
    # ppo.run_and_render()

    # policy = PPO.return_actor()
