


########################## replay part
import torch
import datetime
import io
import pathlib
import uuid

import numpy as np
# import tensorflow as tf

from torch.utils.data import DataLoader





class MyIterableDataset(torch.utils.data.IterableDataset):
  def __init__(self,generator=None):
  	self.generator = generator
  def __iter__(self):
    return self.generator()


class Replay:

  def __init__(self, directory, limit=None):
    directory.mkdir(parents=True, exist_ok=True)
    self._directory = directory
    self._limit = limit
    self._step = sum(int(
        str(n).split('-')[-1][:-4]) - 1 for n in directory.glob('*.npz'))
    self._episodes = load_episodes(directory, limit)

  @property
  def total_steps(self):
    return self._step

  @property
  def num_episodes(self):
    return len(self._episodes)

  @property
  def num_transitions(self):
    return sum(self._length(ep) for ep in self._episodes.values())

  def add(self, episode):
    length = self._length(episode)
    self._step += length
    if self._limit:
      total = 0
      for key, ep in reversed(sorted(
          self._episodes.items(), key=lambda x: x[0])):
        if total <= self._limit - length:
          total += self._length(ep)
        else:
          del self._episodes[key]
    filename = save_episodes(self._directory, [episode])[0]
    self._episodes[str(filename)] = episode

  def dataset(self, batch, length, oversample_ends):
    example = self._episodes[next(iter(self._episodes.keys()))]
    types = {k: v.dtype for k, v in example.items()}
    shapes = {k: (None,) + v.shape[1:] for k, v in example.items()}
    generator = lambda: sample_episodes(
        self._episodes, length, oversample_ends)
    iterable_dataset = MyIterableDataset(generator)
    dataloader = torch.utils.data.DataLoader(iterable_dataset,batch_size=batch)
    return dataloader

  def _length(self, episode):
    return len(episode['reward']) - 1

def save_episodes(directory, episodes):
  directory = pathlib.Path(directory).expanduser()
  directory.mkdir(parents=True, exist_ok=True)
  timestamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
  filenames = []
  for episode in episodes:
    identifier = str(uuid.uuid4().hex)
    length = len(episode['reward']) - 1
    filename = directory / f'{timestamp}-{identifier}-{length}.npz'
    with io.BytesIO() as f1:
      np.savez_compressed(f1, **episode)
      f1.seek(0)
      with filename.open('wb') as f2:
        f2.write(f1.read())
    filenames.append(filename)
  return filenames


def sample_episodes(episodes, length=None, balance=False, seed=0):
  random = np.random.RandomState(seed)
  while True:
    episode = random.choice(list(episodes.values()))
    if length:
      total = len(next(iter(episode.values())))
      available = total - length
      if available < 1:
        print(f'Skipped short episode of length {total}.')
        continue
      if balance:
        index = min(random.randint(0, total), available)
      else:
        index = int(random.randint(0, available + 1))
      episode = {k: v[index: index + length] for k, v in episode.items()}
    yield episode


def load_episodes(directory, limit=None):
  directory = pathlib.Path(directory).expanduser()
  episodes = {}
  total = 0
  for filename in reversed(sorted(directory.glob('*.npz'))):
    try:
      with filename.open('rb') as f:
        episode = np.load(f)
        episode = {k: episode[k] for k in episode.keys()}
    except Exception as e:
      print(f'Could not load episode: {e}')
      continue
    episodes[str(filename)] = episode
    total += len(episode['reward']) - 1
    if limit and total >= limit:
      break
  return episodes



############################### env part 
import os
import threading

import gym
import numpy as np


class DMC:

  def __init__(self, name, action_repeat=1, size=(64, 64), camera=None):
    os.environ['MUJOCO_GL'] = 'egl'
    domain, task = name.split('_', 1)
    if domain == 'cup':  # Only domain with multiple words.
      domain = 'ball_in_cup'
    if isinstance(domain, str):
      from dm_control import suite
      self._env = suite.load(domain, task)
    else:
      assert task is None
      self._env = domain()
    self._action_repeat = action_repeat
    self._size = size
    if camera is None:
      camera = dict(quadruped=2).get(domain, 0)
    self._camera = camera

  @property
  def observation_space(self):
    spaces = {}
    for key, value in self._env.observation_spec().items():
      spaces[key] = gym.spaces.Box(
          -np.inf, np.inf, value.shape, dtype=np.float32)
    spaces['image'] = gym.spaces.Box(
        0, 255, self._size + (3,), dtype=np.uint8)
    return gym.spaces.Dict(spaces)

  @property
  def action_space(self):
    spec = self._env.action_spec()
    action = gym.spaces.Box(spec.minimum, spec.maximum, dtype=np.float32)
    return gym.spaces.Dict({'action': action})

  def step(self, action):
    action = action['action']
    assert np.isfinite(action).all(), action
    reward = 0
    for _ in range(self._action_repeat):
      time_step = self._env.step(action)
      reward += time_step.reward or 0
      if time_step.last():
        break
    obs = dict(time_step.observation)
    obs['image'] = self.render()
    done = time_step.last()
    info = {'discount': np.array(time_step.discount, np.float32)}
    return obs, reward, done, info

  def reset(self):
    time_step = self._env.reset()
    obs = dict(time_step.observation)
    obs['image'] = self.render()
    return obs

  def render(self, *args, **kwargs):
    if kwargs.get('mode', 'rgb_array') != 'rgb_array':
      raise ValueError("Only render mode 'rgb_array' is supported.")
    return self._env.physics.render(*self._size, camera_id=self._camera)


class Atari:

  LOCK = threading.Lock()

  def __init__(
      self, name, action_repeat=4, size=(84, 84), grayscale=True, noops=30,
      life_done=False, sticky_actions=True, all_actions=False):
    assert size[0] == size[1]
    import gym.wrappers
    import gym.envs.atari
    if name == 'james_bond':
      name = 'jamesbond'
    with self.LOCK:
      env = gym.envs.atari.AtariEnv(
          game=name, obs_type='image', frameskip=1,
          repeat_action_probability=0.25 if sticky_actions else 0.0,
          full_action_space=all_actions)
    # Avoid unnecessary rendering in inner env.
    env._get_obs = lambda: None
    # Tell wrapper that the inner env has no action repeat.
    env.spec = gym.envs.registration.EnvSpec('NoFrameskip-v0')
    env = gym.wrappers.AtariPreprocessing(
        env, noops, action_repeat, size[0], life_done, grayscale)
    self._env = env
    self._grayscale = grayscale

  @property
  def observation_space(self):
    return gym.spaces.Dict({
        'image': self._env.observation_space,
        'ram': gym.spaces.Box(0, 255, (128,), np.uint8),
    })

  @property
  def action_space(self):
    return gym.spaces.Dict({'action': self._env.action_space})

  def close(self):
    return self._env.close()

  def reset(self):
    with self.LOCK:
      image = self._env.reset()
    if self._grayscale:
      image = image[..., None]
    obs = {'image': image, 'ram': self._env.env._get_ram()}
    return obs

  def step(self, action):
    action = action['action']
    image, reward, done, info = self._env.step(action)
    if self._grayscale:
      image = image[..., None]
    obs = {'image': image, 'ram': self._env.env._get_ram()}
    return obs, reward, done, info

  def render(self, mode):
    return self._env.render(mode)


class Dummy:

  def __init__(self):
    pass

  @property
  def observation_space(self):
    image = gym.spaces.Box(0, 255, (64, 64, 3), dtype=np.uint8)
    return gym.spaces.Dict({'image': image})

  @property
  def action_space(self):
    action = gym.spaces.Box(-1, 1, (6,), dtype=np.float32)
    return gym.spaces.Dict({'action': action})

  def step(self, action):
    obs = {'image': np.zeros((64, 64, 3))}
    reward = 0.0
    done = False
    info = {}
    return obs, reward, done, info

  def reset(self):
    obs = {'image': np.zeros((64, 64, 3))}
    return obs


class TimeLimit:

  def __init__(self, env, duration):
    self._env = env
    self._duration = duration
    self._step = None

  def __getattr__(self, name):
    return getattr(self._env, name)

  def step(self, action):
    assert self._step is not None, 'Must reset environment.'
    obs, reward, done, info = self._env.step(action)
    self._step += 1
    if self._step >= self._duration:
      done = True
      if 'discount' not in info:
        info['discount'] = np.array(1.0).astype(np.float32)
      self._step = None
    return obs, reward, done, info

  def reset(self):
    self._step = 0
    return self._env.reset()


class NormalizeAction:

  def __init__(self, env, key='action'):
    self._env = env
    self._key = key
    space = env.action_space[key]
    self._mask = np.isfinite(space.low) & np.isfinite(space.high)
    self._low = np.where(self._mask, space.low, -1)
    self._high = np.where(self._mask, space.high, 1)

  def __getattr__(self, name):
    return getattr(self._env, name)

  @property
  def action_space(self):
    low = np.where(self._mask, -np.ones_like(self._low), self._low)
    high = np.where(self._mask, np.ones_like(self._low), self._high)
    space = gym.spaces.Box(low, high, dtype=np.float32)
    return gym.spaces.Dict({**self._env.action_space.spaces, self._key: space})

  def step(self, action):
    orig = (action[self._key] + 1) / 2 * (self._high - self._low) + self._low
    orig = np.where(self._mask, orig, action[self._key])
    return self._env.step({**action, self._key: orig})


class OneHotAction:

  def __init__(self, env, key='action'):
    assert isinstance(env.action_space[key], gym.spaces.Discrete)
    self._env = env
    self._key = key
    self._random = np.random.RandomState()

  def __getattr__(self, name):
    return getattr(self._env, name)

  @property
  def action_space(self):
    shape = (self._env.action_space[self._key].n,)
    space = gym.spaces.Box(low=0, high=1, shape=shape, dtype=np.float32)
    space.sample = self._sample_action
    space.n = shape[0]
    return gym.spaces.Dict({**self._env.action_space.spaces, self._key: space})

  def step(self, action):
    index = np.argmax(action[self._key]).astype(int)
    reference = np.zeros_like(action[self._key])
    reference[index] = 1
    if not np.allclose(reference, action[self._key]):
      raise ValueError(f'Invalid one-hot action:\n{action}')
    return self._env.step({**action, self._key: index})

  def reset(self):
    return self._env.reset()

  def _sample_action(self):
    actions = self._env.action_space.n
    index = self._random.randint(0, actions)
    reference = np.zeros(actions, dtype=np.float32)
    reference[index] = 1.0
    return reference


class RewardObs:

  def __init__(self, env, key='reward'):
    assert key not in env.observation_space.spaces
    self._env = env
    self._key = key

  def __getattr__(self, name):
    return getattr(self._env, name)

  @property
  def observation_space(self):
    space = gym.spaces.Box(-np.inf, np.inf, (), dtype=np.float32)
    return gym.spaces.Dict({
        **self._env.observation_space.spaces, self._key: space})

  def step(self, action):
    obs, reward, done, info = self._env.step(action)
    obs['reward'] = reward
    return obs, reward, done, info

  def reset(self):
    obs = self._env.reset()
    obs['reward'] = 0.0
    return obs


class ResetObs:

  def __init__(self, env, key='reset'):
    assert key not in env.observation_space.spaces
    self._env = env
    self._key = key

  def __getattr__(self, name):
    return getattr(self._env, name)

  @property
  def observation_space(self):
    space = gym.spaces.Box(0, 1, (), dtype=np.bool)
    return gym.spaces.Dict({
        **self._env.observation_space.spaces, self._key: space})

  def step(self, action):
    obs, reward, done, info = self._env.step(action)
    obs['reset'] = np.array(False, np.bool)
    return obs, reward, done, info

  def reset(self):
    obs = self._env.reset()
    obs['reset'] = np.array(True, np.bool)
    return obs


################################## driver part

import numpy as np


class Driver:

  def __init__(self, envs, **kwargs):
    self._envs = envs
    self._kwargs = kwargs
    self._on_steps = []
    self._on_resets = []
    self._on_episodes = []
    self._actspaces = [env.action_space.spaces for env in envs]
    self.reset()

  def on_step(self, callback):
    self._on_steps.append(callback)

  def on_reset(self, callback):
    self._on_resets.append(callback)

  def on_episode(self, callback):
    self._on_episodes.append(callback)

  def reset(self):
    self._obs = [None] * len(self._envs)
    self._dones = [True] * len(self._envs)
    self._eps = [None] * len(self._envs)
    self._state = None

  def __call__(self, policy, steps=0, episodes=0):
    step, episode = 0, 0
    while step < steps or episode < episodes:
      for i, done in enumerate(self._dones):
        if done:
          self._obs[i] = ob = self._envs[i].reset()
          act = {k: np.zeros(v.shape) for k, v in self._actspaces[i].items()}
          tran = {**ob, **act, 'reward': 0.0, 'discount': 1.0, 'done': False}
          [callback(tran, **self._kwargs) for callback in self._on_resets]
          self._eps[i] = [tran]
      obs = {k: np.stack([o[k] for o in self._obs]) for k in self._obs[0]}
      actions, self._state = policy(obs, self._state, **self._kwargs)
      actions = [
          {k: np.array(actions[k][i]) for k in actions}
          for i in range(len(self._envs))]
      assert len(actions) == len(self._envs)
      results = [e.step(a) for e, a in zip(self._envs, actions)]
      for i, (act, (ob, rew, done, info)) in enumerate(zip(actions, results)):
        obs = {k: self._convert(v) for k, v in obs.items()}
        disc = info.get('discount', np.array(1 - float(done)))
        tran = {**ob, **act, 'reward': rew, 'discount': disc, 'done': done}
        [callback(tran, **self._kwargs) for callback in self._on_steps]
        self._eps[i].append(tran)
        if done:
          ep = self._eps[i]
          ep = {k: self._convert([t[k] for t in ep]) for k in ep[0]}
          [callback(ep, **self._kwargs) for callback in self._on_episodes]
      obs, _, dones = zip(*[p[:3] for p in results])
      self._obs = list(obs)
      self._dones = list(dones)
      episode += sum(dones)
      step += len(dones)

  def _convert(self, value):
    value = np.array(value)
    if np.issubdtype(value.dtype, np.floating):
      return value.astype(np.float32)
    elif np.issubdtype(value.dtype, np.signedinteger):
      return value.astype(np.int32)
    elif np.issubdtype(value.dtype, np.uint8):
      return value.astype(np.uint8)
    return value

########################################### dist part

# import tensorflow as tf
# import tensorflow_probability as tfp
# from tensorflow_probability import distributions as tfd

import torch 
from torch.distributions.one_hot_categorical import OneHotCategorical



# # Patch to ignore seed to avoid synchronization across GPUs.
# _orig_random_categorical = tf.random.categorical
# def random_categorical(*args, **kwargs):
#   kwargs['seed'] = None
#   return _orig_random_categorical(*args, **kwargs)
# tf.random.categorical = random_categorical

# # Patch to ignore seed to avoid synchronization across GPUs.
# _orig_random_normal = tf.random.normal
# def random_normal(*args, **kwargs):
#   kwargs['seed'] = None
#   return _orig_random_normal(*args, **kwargs)
# tf.random.normal = random_normal


class SampleDist:

  def __init__(self, dist, samples=100):
    self._dist = dist
    self._samples = samples

  @property
  def name(self):
    return 'SampleDist'

  def __getattr__(self, name):
    return getattr(self._dist, name)

  def mean(self):
    samples = self._dist.sample(self._samples)
    return samples.mean(0)

  def mode(self):
    sample = self._dist.sample(self._samples)
    logprob = self._dist.log_prob(sample)
    return sample[torch.argmax(logprob)][0]

  def entropy(self):
    sample = self._dist.sample(self._samples)
    logprob = self.log_prob(sample)
    return -logprob.mean(0)



# sample_np = np.random.randn(2,4,6)
# logprob_np = sample_np.mean(axis=(1,2))
# sample = torch.tensor(sample_np)
# logprob = torch.tensor(logprob_np)


# tf.gather(sample_np, tf.argmax(logprob_np))[0]
# torch.gather(sample, 0, torch.argmax(logprob))[0]

# sample[torch.argmax(logprob)][0]


# sample = torch.tensor([[1,2,3],[10,20,30]])

# torch.gather(sample, 0, [[0,0,0],[1,1,1]])


class OneHotDist(OneHotCategorical):

  def __init__(self, logits=None, probs=None, dtype=None):
    self._sample_dtype = dtype or torch.float32
    super(OneHotDist,self).__init__(logits=logits, probs=probs)

    has_rsample = True

  def rsample(self, sample_shape=torch.Size()):
    samples = self.sample(sample_shape)
    probs = self._categorical.probs  # cached via @lazy_property
    return samples + (probs - probs.detach())
  @property
  def mode(self):
    return torch.argmax(self._categorical.probs)
  




def multivariatenormaldiag(loc, std):
  return Independent()
  


# dist = OneHotDist(logits=torch.tensor([1.0]))
# dist.rsample(torch.Size([2,3]))


# class TruncNormalDist(tfd.TruncatedNormal):

#   def __init__(self, loc, scale, low, high, clip=1e-6, mult=1):
#     super().__init__(loc, scale, low, high)
#     self._clip = clip
#     self._mult = mult

#   def sample(self, *args, **kwargs):
#     event = super().sample(*args, **kwargs)
#     if self._clip:
#       clipped = tf.clip_by_value(
#           event, self.low + self._clip, self.high - self._clip)
#       event = event - tf.stop_gradient(event) + tf.stop_gradient(clipped)
#     if self._mult:
#       event *= self._mult
#     return event


# class TanhBijector(tfp.bijectors.Bijector):

#   def __init__(self, validate_args=False, name='tanh'):
#     super().__init__(
#         forward_min_event_ndims=0,
#         validate_args=validate_args,
#         name=name)

#   def _forward(self, x):
#     return tf.nn.tanh(x)

#   def _inverse(self, y):
#     dtype = y.dtype
#     y = tf.cast(y, tf.float32)
#     y = tf.where(
#         tf.less_equal(tf.abs(y), 1.),
#         tf.clip_by_value(y, -0.99999997, 0.99999997), y)
#     y = tf.atanh(y)
#     y = tf.cast(y, dtype)
#     return y

#   def _forward_log_det_jacobian(self, x):
#     log2 = tf.math.log(tf.constant(2.0, dtype=x.dtype))
#     return 2.0 * (log2 - x - tf.nn.softplus(-2.0 * x))










########################################### other part

import re

# import tensorflow as tf
# from tensorflow_probability import distributions as tfd

# from . import dists

from torch.distributions.uniform import Uniform
from torch.distributions.independent import Independent

class AttrDict(dict):

  __getattr__ = dict.__getitem__
  __setattr__ = dict.__setitem__


class RandomAgent:

  def __init__(self, action_space, logprob=False):
    self._logprob = logprob
    if hasattr(action_space, 'n'):
      self._dist = OneHotDist(torch.zeros(action_space.n))
    else:
      dist = Uniform(torch.tensor(action_space.low), torch.tensor(action_space.high))
      self._dist = Independent(dist, 1)

  def __call__(self, obs, state=None, mode=None):
    action = self._dist.sample(torch.Size([len(obs['reset'])]))
    output = {'action': action}
    if self._logprob:
      output['logprob'] = self._dist.log_prob(action)
    return output, None


# def torch_nest_flatten(x):
#   out = []
#   for _ in x:
#     out += list(_.values())
#   return out


def static_scan(fn, inputs, start, reverse=False):
  last = start
  outputs = [[] for _ in start.values()]
  indices = inputs.shape[0]
  if reverse:
    indices = reversed(indices)
  for index in range(indices):
    inp = inputs[index]
    last = fn(last, inp)
    [o.append(l) for o, l in zip(outputs, last.values())]
  if reverse:
    outputs = [list(reversed(x)) for x in outputs]
  outputs = [torch.stack(x, 0) for x in outputs]
  return pack_sequence_as(last, outputs)

def pack_sequence_as(last,outputs):
  keys = last.keys()
  return dict(zip(keys,outputs))



# torch.flatten(torch.tensor([[1,2,3],[4,5,6]]))
# tf.nest.flatten(np.array([[1,2,3],[4,5,6]]))

# tf.nest.flatten(dict(logit=tf.zeros([2,10,True]),
#   stoch=tf.zeros([2,10,True]),
#   deter=torch.zeros([2,15])))

# list(dict(logit=tf.zeros([2,10,True]),
#   stoch=tf.zeros([2,10,True]),
#   deter=torch.zeros([2,15])).values())

# swap = lambda x: x.permute([1, 0] + list(range(2, len(x.shape))))
# state = dict(logit=torch.zeros([2,10,True]),
#   stoch=torch.zeros([2,10,True]),
#   deter=torch.zeros([2,15]))

# embed, action = swap(state['stoch']), swap(state['stoch'])
# inputs = (action, embed)
# start = (state, state)





def schedule(string, step):
  try:
    return float(string)
  except ValueError:
    step = step.to(torch.float32)
    match = re.match(r'linear\((.+),(.+),(.+)\)', string)
    if match:
      initial, final, duration = [float(group) for group in match.groups()]
      mix = torch.clamp(step / duration, 0, 1)
      return (1 - mix) * initial + mix * final
    match = re.match(r'warmup\((.+),(.+)\)', string)
    if match:
      warmup, value = [float(group) for group in match.groups()]
      scale = torch.clamp(step / warmup, 0, 1)
      return scale * value
    match = re.match(r'exp\((.+),(.+),(.+)\)', string)
    if match:
      initial, final, halflife = [float(group) for group in match.groups()]
      return (initial - final) * 0.5 ** (step / halflife) + final
    match = re.match(r'horizon\((.+),(.+),(.+)\)', string)
    if match:
      initial, final, duration = [float(group) for group in match.groups()]
      mix = torch.clamp(step / duration, 0, 1)
      horizon = (1 - mix) * initial + mix * final
      return 1 - 1 / horizon
    raise NotImplementedError(string)


def lambda_return(
    reward, value, pcont, bootstrap, lambda_, axis):
  # Setting lambda=1 gives a discounted Monte Carlo return.
  # Setting lambda=0 gives a fixed 1-step return.
  assert reward.shape.ndims == value.shape.ndims, (reward.shape, value.shape)
  if isinstance(pcont, (int, float)):
    pcont = pcont * torch.ones_like(reward)
  dims = list(range(reward.shape.ndims))
  dims = [axis] + dims[1:axis] + [0] + dims[axis + 1:]
  if axis != 0:
    reward = reward.permuate(dims)
    value = value.permuate(dims)
    pcont = pcont.permuate(dims)
  if bootstrap is None:
    bootstrap = torch.zeros_like(value[-1])
  next_values = torch.cat([value[1:], bootstrap[None]], 0)
  inputs = reward + pcont * next_values * (1 - lambda_)
  returns = static_scan(
      lambda agg, cur: cur[0] + cur[1] * lambda_ * agg,
      (inputs, pcont), bootstrap, reverse=True)
  if axis != 0:
    returns = returns.permuate(dims)
  return returns

from torch.distributions.normal import Normal

def action_noise(action, amount, action_space):
  if amount == 0:
    return action
  amount = amount.to(action.dtype)
  if hasattr(action_space, 'n'):
    probs = amount / action.shape[-1] + (1 - amount) * action
    return dists.OneHotDist(probs=probs).sample()
  else:
    return torch.clamp(Normal(action, amount).sample(), -1, 1)


def pad_dims(tensor, total_dims):
  while len(tensor.shape) < total_dims:
    tensor = tensor[..., None]
  return tensor



################################## other part 

import numpy as np
import torch 
# import tensorflow as tf
# from tensorflow.keras import layers as tfkl
# from tensorflow_probability import distributions as tfd
# from tensorflow.keras.mixed_precision import experimental as prec
from torch.distributions.multivariate_normal import MultivariateNormal

# next(train_dataset)

# image, ram, reward, reset, action, discount, done = next(train_dataset).values()

# _rssm = RSSM()

# _rssm.imagine(action)

# data = next(train_dataset)

# _encoder = ConvEncoder(**config.encoder)

# embed = _encoder(data)



# obs = data

# embed = _encoder(obs)

# post, prior = _rssm.observe(embed,action)

# forward = False

# balance = 0.8

# free = 0.0

# free_avg = True

# _rssm.kl_loss(post, prior, forward, balance, free, free_avg)



def torch_nest_flatten(x):
  if isinstance(x,dict):
    return x.values()
  if isinstance(x,tuple):
    if isinstance(x[0],dict) and len(x)==2:
      return list(x[0].values()) + list(x[1].values())
  else:
    raise NotImplementedError

def static_scan_imagine(fn, inputs, start, reverse=False):
  last = start
  outputs = [[] for _ in torch_nest_flatten(start)]
  indices = inputs.shape[0]
  if reverse:
    indices = reversed(indices)
  for index in range(indices):
    inp = inputs[index]
    last = fn(last, inp)
    [o.append(l) for o, l in zip(outputs, last.values())]
  if reverse:
    outputs = [list(reversed(x)) for x in outputs]
  outputs = [torch.stack(x, 0) for x in outputs]

def static_scan_observe(fn, inputs, start, reverse=False):
  last = start
  outputs1 = [[] for _ in start[0].values()]
  outputs2 = [[] for _ in start[1].values()]
  try:
    indices = inputs.shape[0]
  except:
    indices = inputs[0].shape[0]
  if reverse:
    indices = reversed(indices)
  for index in range(indices):
    inp = (inputs[0][index],inputs[1][index])
    last = fn(last, inp)
    [o.append(l) for o, l in zip(outputs1, last[0].values())]
    [o.append(l) for o, l in zip(outputs2, last[1].values())]

  if reverse:
    outputs1 = [list(reversed(x)) for x in outputs1]
    outputs2 = [list(reversed(x)) for x in outputs2]
  outputs1 = [torch.stack(x, 0) for x in outputs1]
  outputs2 = [torch.stack(x, 0) for x in outputs2]
  return pack_sequence_as(last[0], outputs1),pack_sequence_as(last[1], outputs2)


def pack_sequence_as(last,outputs):
  keys = last.keys()
  return dict(zip(keys,outputs))


def dict_sg(x):
  keys = x.keys()
  y = {}
  for key in keys:
    y[key] = x[key].clone().detach()
  return y

class RSSM(torch.nn.Module):

  def __init__(
      self, stoch=30, deter=200, hidden=200, discrete=True, act=torch.nn.functional.elu,
      std_act='softplus', min_std=0.1):
    super(RSSM,self).__init__()
    self._stoch = stoch
    self._deter = deter
    self._hidden = hidden
    self._discrete = discrete
    self._act = getattr(torch.nn.functional, act) if isinstance(act, str) else act
    self._std_act = std_act
    self._min_std = min_std
    self._cell = myGRUCell(self._deter,self._hidden+self._deter, norm=True)
    self._cast = lambda x: x.to(torch.float32)

  def initial(self, batch_size):
    dtype = torch.float32
    if self._discrete:
      state = dict(
          logit=torch.zeros([batch_size, self._stoch, self._discrete], dtype=dtype),
          stoch=torch.zeros([batch_size, self._stoch, self._discrete], dtype=dtype),
          deter=self._cell.get_initial_state(None, batch_size, dtype=dtype))
    else:
      state = dict(
          mean=torch.zeros([batch_size, self._stoch], dtype),
          std=torch.zeros([batch_size, self._stoch], dtype),
          stoch=torch.zeros([batch_size, self._stoch], dtype),
          deter=self._cell.get_initial_state(None, batch_size, dtype))
    return state

  def observe(self, embed, action, state=None):
    swap = lambda x: x.permute([1, 0] + list(range(2, len(x.shape))))
    if state is None:
      state = self.initial(action.shape[0])
    embed, action = swap(embed), swap(action)
    post, prior = static_scan_observe(
        lambda prev, inputs: self.obs_step(prev[0], *inputs),
        (action, embed), (state, state))
    post = {k: swap(v) for k, v in post.items()}
    prior = {k: swap(v) for k, v in prior.items()}
    return post, prior

  def imagine(self, action, state=None):
    swap = lambda x: x.permute([1, 0] + list(range(2, len(x.shape))))
    if state is None:
      state = self.initial(action.shape[0])
    assert isinstance(state, dict), state
    action = swap(action)
    prior = static_scan(self.img_step, action, state)
    prior = {k: swap(v) for k, v in prior.items()}
    return prior

  def get_feat(self, state):
    stoch = self._cast(state['stoch'])
    if self._discrete:
      shape = torch.Size(tuple(stoch.shape[:-2]) + tuple([self._stoch * self._discrete]))
      stoch = torch.reshape(stoch, shape)
    return torch.cat([stoch, state['deter']], -1)

  def get_dist(self, state):
    if self._discrete:
      logit = state['logit']
      logit = logit.to(torch.float32)
      dist = Independent(OneHotDist(logit), 1)
    else:
      mean, std = state['mean'], state['std']
      mean = mean.to(tf.float32)
      std = std.to(tf.float32)
      dist = MultivariateNormal(mean, torch.diag(std))
    return dist

  def obs_step(self, prev_state, prev_action, embed, sample=True):
    prior = self.img_step(prev_state, prev_action, sample)
    x = torch.cat([prior['deter'], embed], -1)
    x = dense(x.shape[-1],self._hidden, self._act)(x)
    stats = self._suff_stats_layer('obs_dist', x)
    dist = self.get_dist(stats)
    stoch = dist.sample() if sample else dist.mode()
    post = {'stoch': stoch, 'deter': prior['deter'], **stats}
    return post, prior

  def img_step(self, prev_state, prev_action, sample=True):
    prev_stoch = self._cast(prev_state['stoch'])
    prev_action = self._cast(prev_action)
    if self._discrete:
      shape = torch.Size(tuple(prev_stoch.shape[:-2]) + tuple([self._stoch * self._discrete]))
      prev_stoch = torch.reshape(prev_stoch, shape)
    x = torch.cat([prev_stoch, prev_action], -1)
    x = dense(x.shape[-1],self._hidden, self._act)(x)
    deter = prev_state['deter']
    x, deter = self._cell(x, [deter])
    deter = deter[0]  # Keras wraps the state in a list.
    x = dense(x.shape[-1],self._hidden, self._act)(x)
    stats = self._suff_stats_layer('img_dist', x)
    dist = self.get_dist(stats)
    stoch = dist.rsample(torch.Size([1])).squeeze(0) if sample else dist.mean
    prior = {'stoch': stoch, 'deter': deter, **stats}
    return prior

  def _suff_stats_layer(self, name, x):
    if self._discrete:
      x = dense(x.shape[-1],self._stoch * self._discrete)(x)
      logit = torch.reshape(x, torch.Size(tuple(x.shape[:-1]) + tuple([self._stoch, int(self._discrete)])))
      return {'logit': logit}
    else:
      x = dense(x.shape[-1],2 * self._stoch)(x)
      mean, std = torch.chunk(x, 2, -1)
      std = {
          'softplus': lambda: torch.nn.functional.softplus(std),
          'sigmoid': lambda: torch.sigmoid(std),
          'sigmoid2': lambda: 2 * torch.sigmoid(std / 2),
      }[self._std_act]()
      std = std + self._min_std
      return {'mean': mean, 'std': std}

  def kl_loss(self, post, prior, forward, balance, free, free_avg):
    kld = torch.distributions.kl.kl_divergence
    sg = dict_sg
    lhs, rhs = (prior, post) if forward else (post, prior)
    mix = balance if forward else (1 - balance)
    if balance == 0.5:
      value = kld(self.get_dist(lhs), self.get_dist(rhs))
      loss = torch.maximum(value, torch.tensor([free])).mean()
    else:
      value_lhs = value = kld(self.get_dist(lhs), self.get_dist(sg(rhs)))
      value_rhs = kld(self.get_dist(sg(lhs)), self.get_dist(rhs))
      if free_avg:
        loss_lhs = torch.maximum(value_lhs.mean(), torch.tensor([free]))
        loss_rhs = torch.maximum(value_rhs.mean(), torch.tensor([free]))
      else:
        loss_lhs = torch.maximum(value_lhs, torch.tensor([free])).mean()
        loss_rhs = torch.maximum(value_rhs, torch.tensor([free])).mean()
      loss = mix * loss_lhs + (1 - mix) * loss_rhs
    return loss, value

def get_conv2d(in_channel,depth, kernel, stride):
  return torch.nn.Conv2d(in_channel, depth, kernel, stride)

class ConvEncoder(torch.nn.Module):
  def __init__(self, depth=32, act=torch.nn.functional.elu, kernels=[4,4,4,4], keys=['image']):
    super(ConvEncoder,self).__init__()
    self._act = getattr(torch.nn.functional, act) if isinstance(act,str) else act
    self._depth = depth
    self._kernels = kernels
    self._keys = keys

  def forward(self, obs):
    if tuple(self._keys) == ('image',):
      x = torch.reshape(obs['image'],(-1,) + tuple(obs['image'].shape[-3:])).permute((0,3,1,2))
      for i, kernel in enumerate(self._kernels):
        depth = 2 ** i * self._depth
        x = self._act(get_conv2d(x.shape[1],depth, kernel,2)(x))
      x = torch.reshape(x, [x.shape[0], np.prod(x.shape[1:])])
      shape = torch.Size(tuple(obs['image'].shape[:-3]) + tuple([x.shape[-1]]))
      return torch.reshape(x, shape)
    else:
      dtype = torch.float32
      features = []
      for key in self._keys:
        value = torch.tensor(obs[key])
        if isinstance(value.dtype,int):
          value = value.to(dtype)
          semilog = torch.sign(value) * torch.log(1 + torch.abs(value))
          features.append(semilog[..., None])
        elif len(obs[key].shape) >= 4:
          x = torch.reshape(obs['image'], (-1,) + tuple(obs['image'].shape[-3:])).permute((0,3,1,2))
          for i, kernel in enumerate(self._kernels):
            depth = 2 ** i * self._depth
            x = self._act(get_conv2d(x.shape[1],depth, kernel,2)(x))
          x = torch.reshape(x, torch.Size([x.shape[0], np.prod(x.shape[1:])]))
          shape = tf.concat(tuple(obs['image'].shape[:-3]), tuple([x.shape[-1]]))
          features.append(torch.reshape(x, shape))
        else:
          raise NotImplementedError((key, value.dtype, value.shape))
      return torch.cat(features, -1)


# obs = {'image' : torch.zeros((3,64,64))}
# _layer = ConvEncoder(32)
# features = _layer(obs)

def get_convT2d(in_channel,depth, kernel, stride):
  return torch.nn.ConvTranspose2d(in_channel, depth, kernel, stride)

class ConvDecoder(torch.nn.Module):
  def __init__(self,shape=(64,64,3),depth=32,act=torch.nn.functional.elu, kernels=(5,5,6,6)):
    super(ConvDecoder, self).__init__()
    self._shape = shape
    self._depth = depth
    self._act = getattr(torch.nn.functional, act) if isinstance(act, str) else act
    self._kernels = kernels

  def forward(self, features):
    x = dense(features.shape[-1],32*self._depth)(features)
    x = torch.reshape(x, [-1, 1, 1, 32 * self._depth])
    for i, kernel in enumerate(self._kernels):
      depth = 2 ** (len(self._kernels) - i - 2) * self._depth
      act = self._act
      if i == len(self._kernels) - 1:
        depth = self._shape[-1]
        act = torch.nn.Identity()
      x = act(get_convT2d(x.shape[1],depth,kernel,2)(x))   
    mean = torch.reshape(x, tuple(features.shape[:-1])+self._shape)
    return Independent(Normal(mean, 1), len(self._shape))


# features = torch.zeros(1,64*64*3)
# _layer = ConvDecoder()
# _layer(features)


def dense(input_size=10,output_size=5,use_bias=True):
  model = torch.nn.Sequential(
    torch.nn.Linear(input_size, output_size ,use_bias),
    torch.nn.ReLU())
  return model


class myGRUCell(torch.nn.Module):
  def __init__(self, size, input_size, norm=False, act=torch.tanh, update_bias=-1, **kwargs):
    super(myGRUCell, self).__init__()
    self._size = size 
    self._input_size  = input_size
    self._act = getattr(torch.nn.functional, act) if isinstance(act, str) else act
    self._norm = norm
    self._update_bias = update_bias
    self._layer = dense(input_size,3*size, norm is not None)
    if norm:
      self._norm = torch.nn.LayerNorm(3*size)

  @property
  def state_size(self):
    return self._size

  def get_initial_state(self, inputs=None, batch_size=None, dtype=None):
    if inputs is not None:
      batch_size = inputs.shape[0]
      dtype = inputs.dtype

    if batch_size is None or dtype is None:
      raise ValueError('batch size and dtype should not be None')

    flat_dims = (self._size,)
    init_state_size = torch.Size((batch_size,) + flat_dims)
    return torch.zeros(init_state_size, dtype=dtype)

  # @torch.function
  def forward(self, inputs, state):
    state = state[0]
    parts = self._layer(torch.cat([inputs, state], -1))
    if self._norm:
      dtype = parts.dtype 
      parts = parts.to(torch.float32)
      parts = self._norm(parts)
      parts = parts.to(dtype)
    reset, cand, update = torch.chunk(parts,3, -1)
    reset = torch.sigmoid(reset)
    cand = self._act(reset*cand)
    update = torch.sigmoid(update+self._update_bias)
    output = update*cand + (1- update)*state 
    return output, [output]

# _layer = myGRUCell(5)
# _layer(torch.zeros(10),[torch.ones(5)])

from torch.distributions.bernoulli import Bernoulli

class Distlayer(torch.nn.Module):
  def __init__(self, shape, dist='mse', min_std=0.1, init_std=0.0):
    super(Distlayer, self).__init__()
    self._shape = shape
    self._dist = dist
    self._min_std = min_std
    self._init_std = init_std

  def forward(self, inputs):
    out = dense(inputs.shape[-1],np.prod(self._shape))(inputs)
    out = torch.reshape(out,torch.Size(tuple(inputs.shape[:-1]) + self._shape))
    out = out.to(torch.float32)
    if self._dist in ('normal', 'tanh_normal', 'trunc_normal'):
      std = dense(inputs.shape[-1],np.prod(self._shape))(inputs)
      std = torch.reshape(std, torch.Size(tuple(inputs.shape[:-1]) + self._shape))
      std = std.to(torch.float32)

    if self._dist == 'mse':
      dist = Normal(out, 1.0)
      return Independent(dist, len(self._shape))
    if self._dist == 'binary':
      dist = Bernoulli(out)
      return Independent(dist, len(self._shape))
    if self._dist == 'onehot':
      return OneHotDist(out)
    NotImplementedError(self._dist)

# _layer = Distlayer((10,))
# _layer(inputs)

# torch.stack([inputs.shape[:-1], torch.Size([10])],1)
# list(inputs.shape[:-1] + self._shape)


def get_distlayer(shape):
  layer = Distlayer(shape)
  return layer




class MLP(torch.nn.Module):
  def __init__(self, shape, layers, units, act=torch.nn.functional.elu, **out):
    super(MLP,self).__init__()
    self._shape = (shape,) if isinstance(shape, int) else shape
    self._layers = layers
    self._units = units
    self._act = getattr(torch.nn.functional, act) if isinstance(act, str) else act
    self._out = out

  def forward(self, features):
    x = features.to(torch.float32)
    for index in range(self._layers):
      x = dense(x.shape[-1],self._units, self._act)(x)
    return get_distlayer(self._shape)(x)


# _layer = MLP(4,3,10)
# _layer(torch.zeros(10))






################################## explr part

class Random():

  def __init__(self, action_space):
    self._action_space = action_space

  def actor(self, feat):
    shape = torch.Size(tuple(feat.shape[:-1]) + tuple([self._action_space.shape[-1]]))
    if hasattr(self._action_space, 'n'):
      return OneHotDist(torch.zeros(shape))
    else:
      dist = tfd.Uniform(-torch.ones(shape), torch.ones(shape))
      return tfd.Independent(dist, 1)

  def train(self, start, context, data):
    return None, {}





################################## agent part

# import tensorflow as tf
# from tensorflow.keras import mixed_precision as prec

import elements
# import common

# image, ram, reward, reset, action, discount, done = next(train_dataset).values()











class Agent(object):

  def __init__(self, config, logger, actspce, step, dataset):
    self.config = config
    self._logger = logger
    self._action_space = actspce
    self._num_act = actspce.n if hasattr(actspce, 'n') else actspce.shape[0]
    self._should_expl = elements.Until(int(
        config.expl_until / config.action_repeat))
    self._counter = step
    self.step = torch.tensor([int(self._counter)], dtype=torch.int64)
    self._dataset = dataset
    self.wm = WorldModel(self.step, config)
    self._task_behavior = ActorCritic(config, self.step, self._num_act)
    reward = lambda f, s, a: self.wm.heads['reward'](f).mode()
    self._expl_behavior = dict(
        greedy=lambda: self._task_behavior,
        random=lambda: Random(actspce),
        plan2explore=lambda: Plan2Explore(
            config, self.wm, self._num_act, self.step, reward),
        model_loss=lambda: ModelLoss(
            config, self.wm, self._num_act, self.step, reward),
    )[config.expl_behavior]()
    # Train step to initialize variables including optimizer statistics.
    self.train(next(self._dataset))

  def policy(self, obs, state=None, mode='train'):
    tf.py_function(lambda: self.step.assign(
        int(self._counter), read_value=False), [], [])
    if state is None:
      latent = self.wm.rssm.initial(len(obs['image']))
      action = torch.zeros((len(obs['image']), self._num_act))
      state = latent, action
    elif obs['reset'].any():
      state = list(map(lambda x: x * pad_dims(
          1.0 - tf.cast(obs['reset'], x.dtype), len(x.shape)), state))
    latent, action = state
    embed = self.wm.encoder(self.wm.preprocess(obs))
    sample = (mode == 'train') or not self.config.eval_state_mean
    latent, _ = self.wm.rssm.obs_step(latent, action, embed, sample)
    feat = self.wm.rssm.get_feat(latent)
    if mode == 'eval':
      actor = self._task_behavior.actor(feat)
      action = actor.mode()
    elif self._should_expl(self.step):
      actor = self._expl_behavior.actor(feat)
      action = actor.sample()
    else:
      actor = self._task_behavior.actor(feat)
      action = actor.sample()
    noise = {'train': self.config.expl_noise, 'eval': self.config.eval_noise}
    action = action_noise(action, noise[mode], self._action_space)
    outputs = {'action': action}
    state = (latent, action)
    return outputs, state

  def train(self, data, state=None):
    metrics = {}
    state, outputs, mets = self.wm.train(data, state)
    metrics.update(mets)
    start = outputs['post']
    if self.config.pred_discount:  # Last step could be terminal.
      start = list(map(lambda x: x[:, :-1], start))
    reward = lambda f, s, a: self.wm.heads['reward'](f).mode()
    metrics.update(self._task_behavior.train(self.wm, start, reward))
    if self.config.expl_behavior != 'greedy':
      if self.config.pred_discount:
        data = list(map(lambda x: x[:, :-1], data))
        outputs = list(map(lambda x: x[:, :-1], outputs))
      mets = self._expl_behavior.train(start, outputs, data)[-1]
      metrics.update({'expl_' + key: value for key, value in mets.items()})
    return state, metrics

  def report(self, data):
    return {'openl': self.wm.video_pred(data)}

# image, ram, reward, reset, action, discount, done = next(train_dataset).values()

# data = next(train_dataset)

# _wm = WorldModel(step, config)

# _ac = Random(action_space)

# _policy = _ac.actor


# start = post

# _wm.imagine(policy,start,horizon)

# features = _wm.rssm.get_feat(states)


#   def loss(self, data, state=None):
#     data = _wm.preprocess(data)
#     embed = _wm.encoder(data)
#     post, prior = _wm.rssm.observe(embed, data['action'], state)
#     kl_loss, kl_value = _wm.rssm.kl_loss(post, prior, **_wm.config.kl)
#     assert len(kl_loss.shape) == 0
#     likes = {}
#     losses = {'kl': kl_loss}
#     feat = _wm.rssm.get_feat(post)
#     for name, head in _wm.heads.items():
#       grad_head = (name in _wm.config.grad_heads)
#       inp = feat if grad_head else feat.detach()
#       like = (head(inp).log_prob(data[name])).to(roch.float32)
#       likes[name] = like
#       losses[name] = -like.mean()
#     model_loss = sum(
#         _wm.config.loss_scales.get(k, 1.0) * v for k, v in losses.items())
#     outs = dict(
#         embed=embed, feat=feat, post=post,
#         prior=prior, likes=likes, kl=kl_value)
#     metrics = {f'{name}_loss': value for name, value in losses.items()}
#     metrics['model_kl'] = kl_value.mean()
#     metrics['prior_ent'] = _wm.rssm.get_dist(prior).entropy().mean()
#     metrics['post_ent'] = _wm.rssm.get_dist(post).entropy().mean()
#     return model_loss, post, outs, metrics





def static_scan_wm_imagine(fn, inputs, start, reverse=False):
  last = start
  outputs = [[] for _ in range(len(start))]
  indices = inputs.shape[0]
  if reverse:
    indices = reversed(indices)
  for index in range(indices):
    inp = inputs[index]
    last = fn(last, inp)
    [o.append(l) for o, l in zip(outputs, last)]
  if reverse:
    outputs = [list(reversed(x)) for x in outputs]

  outputs = [stack_dict(outputs[0])] + [torch.stack(x, 0) for x in outputs[1:3]]

  return outputs

def stack_dict(x):
  keys = x[0].keys()
  o = {}
  for key in keys:
    o[key] = torch.stack(list(map(lambda x: x[key],x)),0)
  return o




class WorldModel(object):

  def __init__(self, step, config):
    self.step = step
    self.config = config
    self.rssm = RSSM(**config.rssm)
    self.heads = {}
    shape = config.image_size + (1 if config.grayscale else 3,)
    self.encoder = ConvEncoder(**config.encoder)
    self.heads['image'] = ConvDecoder(shape, **config.decoder)
    self.heads['reward'] = MLP([], **config.reward_head)
    for name in config.grad_heads:
      assert name in self.heads, name
  #   self.model_opt = torch.optim.Adam([
  #     {'params':self.encoder.parameters()},
  #     {'params':self.heads['image'].parameters()},
  #     {'params':self.heads['reward'].parameters()},
  #     {'params':self.rssm.parameters()}], lr=config.model_opt['lr'])

  # def train(self, data, state=None):
  #   self.model_opt.zero_grad()
  #   model_loss, state, outputs, metrics = self.loss(data, state)
  #   model_loss.backward()
  #   model_opt.step()
  #   metris['model_loss'] = model_loss.detach()
  #   return state, outputs, metrics

  def loss(self, data, state=None):
    data = self.preprocess(data)
    embed = self.encoder(data)
    post, prior = self.rssm.observe(embed, data['action'], state)
    kl_loss, kl_value = self.rssm.kl_loss(post, prior, **self.config.kl)
    # assert len(kl_loss.shape) == 0
    likes = {}
    losses = {'kl': kl_loss}
    feat = self.rssm.get_feat(post)
    for name, head in self.heads.items():
      grad_head = (name in self.config.grad_heads)
      inp = feat if grad_head else feat.detach()
      like = (head(inp).log_prob(data[name])).to(roch.float32)
      likes[name] = like
      losses[name] = -like.mean()
    model_loss = sum(
        self.config.loss_scales.get(k, 1.0) * v for k, v in losses.items())
    outs = dict(
        embed=embed, feat=feat, post=post,
        prior=prior, likes=likes, kl=kl_value)
    metrics = {f'{name}_loss': value for name, value in losses.items()}
    metrics['model_kl'] = kl_value.mean()
    metrics['prior_ent'] = self.rssm.get_dist(prior).entropy().mean()
    metrics['post_ent'] = self.rssm.get_dist(post).entropy().mean()
    return model_loss, post, outs, metrics

  def imagine(self, policy, start, horizon):
    flatten = lambda x: x.reshape([-1] + list(x.shape[2:]))
    start = {k: flatten(v) for k, v in start.items()}
    def step(prev, _):
      state, _, _ = prev
      feat = self.rssm.get_feat(state)
      action = policy(feat.clone().detach()).sample()
      succ = self.rssm.img_step(state, action)
      return succ, feat, action
    feat = 0 * self.rssm.get_feat(start)
    action = policy(feat.clone().detach()).sample()
    succs, feats, actions = static_scan_wm_imagine(
        step, torch.arange(horizon), (start, feat, action))
    states = {k: torch.cat([
        start[k][None], v[:-1]], 0) for k, v in succs.items()}
    if 'discount' in self.heads:
      discount = self.heads['discount'](feats).mean()
    else:
      discount = self.config.discount * torch.ones_like(feats[..., 0])
    return feats, states, actions, discount

  def preprocess(self, obs):
    dtype = torch.float32
    obs = obs.copy()
    obs['image'] = obs['image'].to(dtype) / 255.0 - 0.5
    obs['reward'] = getattr(torch.nn.functional, self.config.clip_rewards)(obs['reward'])
    if 'discount' in obs:
      obs['discount'] *= self.config.discount
    return obs

  def video_pred(self, data):
    data = self.preprocess(data)
    truth = data['image'][:6] + 0.5
    embed = self.encoder(data)
    states, _ = self.rssm.observe(embed[:6, :5], data['action'][:6, :5])
    recon = self.heads['image'](
        self.rssm.get_feat(states)).mode()[:6]
    init = {k: v[:, -1] for k, v in states.items()}
    prior = self.rssm.imagine(data['action'][:6, 5:], init)
    openl = self.heads['image'](self.rssm.get_feat(prior)).mode()
    model = torch.cat([recon[:, :5] + 0.5, openl + 0.5], 1)
    error = (model - truth + 1) / 2
    video = torch.cat([truth, model, error], 2)
    B, T, H, W, C = video.shape
    return video.permute((1, 2, 0, 3, 4)).reshape((T, H, B * W, C))


from torch.optim import Adam

class ActorCritic(object):

  def __init__(self, config, step, num_actions):
    self.config = config
    self.step = step
    self.num_actions = num_actions
    self.actor = MLP(num_actions, **config.actor)
    self.critic = MLP([], **config.critic)
    if config.slow_target:
      self._target_critic = MLP([], **config.critic)
      self._updates = torch.tensor(0, tf.int64)
    else:
      self._target_critic = self.critic
    self.actor_opt = Adam(self.actor.parameters(), lr=config.actor_opt['lr'])
    self.critic_opt = Adam(self.critic.parameters(), lr=config.critic_opt['lr'])

  def train(self, world_model, start, reward_fn):
    metrics = {}
    hor = self.config.imag_horizon
    feat, state, action, disc = world_model.imagine(self.actor, start, hor)
    reward = reward_fn(feat, state, action)
    target, weight, mets1 = self.target(feat, action, reward, disc)

    actor_opt.zero_grad()
    actor_loss, mets2 = self.actor_loss(feat, action, target, weight)
    actor_loss.backward()
    actor_opt.step()

    critic_opt.zero_grad()
    critic_loss, mets3 = self.critic_loss(feat, action, target, weight)
    critic_loss.backward()
    critic_opt.step()

    metrics['actor_loss'] = actor_loss.detach()
    metrics['critic_loss'] = critic_loss.detach()

    metrics.update(**mets1, **mets2, **mets3)
    self.update_slow_target()  # Variables exist after first forward pass.
    return metrics

  def actor_loss(self, feat, action, target, weight):
    metrics = {}
    policy = self.actor(feat.detach())
    if self.config.actor_grad == 'dynamics':
      objective = target
    elif self.config.actor_grad == 'reinforce':
      baseline = self.critic(feat[:-1]).mode()
      advantage = (target - baseline).detach()
      objective = policy.log_prob(action)[:-1] * advantage
    elif self.config.actor_grad == 'both':
      baseline = self.critic(feat[:-1]).mode()
      advantage = (target - baseline).detach()
      objective = policy.log_prob(action)[:-1] * advantage
      mix = schedule(self.config.actor_grad_mix, self.step)
      objective = mix * target + (1 - mix) * objective
      metrics['actor_grad_mix'] = mix
    else:
      raise NotImplementedError(self.config.actor_grad)
    ent = policy.entropy()
    ent_scale = schedule(self.config.actor_ent, self.step)
    objective += ent_scale * ent[:-1]
    actor_loss = -(weight[:-1] * objective).mean()
    metrics['actor_ent'] = ent.mean()
    metrics['actor_ent_scale'] = ent_scale
    return actor_loss, metrics

  def critic_loss(self, feat, action, target, weight):
    dist = self.critic(feat)[:-1]
    target = target.detach()
    critic_loss = -(dist.log_prob(target) * weight[:-1]).mean()
    metrics = {'critic': dist.mode().mean()}
    return critic_loss, metrics

  def target(self, feat, action, reward, disc):
    reward = reward.to(tf.float32)
    disc = disc.to(tf.float32)
    value = self._target_critic(feat).mode()
    target = lambda_return(
        reward[:-1], value[:-1], disc[:-1],
        bootstrap=value[-1], lambda_=self.config.discount_lambda, axis=0)
    weight = (torch.cumprod(
        torch.cat(
        [torch.ones_like(disc[:1]), disc[:-1]], 0), 0)).detach()
    metrics = {}
    metrics['reward_mean'] = reward.mean()
    metrics['reward_std'] = reward.std()
    metrics['critic_slow'] = value.mean()
    metrics['critic_target'] = target.mean()
    return target, weight, metrics

  def update_slow_target(self):
    if self.config.slow_target:
      if self._updates % self.config.slow_target_update == 0:
        mix = 1.0 if self._updates == 0 else float(
            self.config.slow_target_fraction)
        for s, d in zip(self.critic.parameters, self._target_critic.parameters):
          d.assign(mix * s + (1 - mix) * d)
      self._updates += 1



########################################## torch utils part

class myOptimizer(torch.nn.Module):

  def __init__(
      self, parameters, name, lr, eps=1e-4, clip=None, wd=None,
      opt='adam', wd_pattern=r'.*'):
    assert 0 <= wd < 1
    assert not clip or 1 <= clip
    self._parameters = []
    for param in parameters:
    	self._parameters.append(param)
    self._name = name
    self._clip = clip
    self._wd = wd
    self._wd_pattern = wd_pattern
    self._opt = {
        'adam': lambda: torch.nn.optim.Adam(lr, epsilon=eps),
        'nadam': lambda: torch.nn.optim.Nadam(lr, epsilon=eps),
        'adamax': lambda: torch.nn.optim.Adamax(lr, epsilon=eps),
        'sgd': lambda: torch.nn.optim.SGD(lr),
        'momentum': lambda: torch.nn.optim.SGD(lr, 0.9),
    }[opt]()
    self._once = True

  @property
  def variables(self):
    return self._parameters

  def __call__(self, loss):
    assert loss.dtype is torch.float32, (self._name, loss.dtype)
    assert len(loss.shape) == 0, (self._name, loss.shape)
    varibs = self._parameters
    count = sum(torch.numel(x.shape) for x in varibs)
    if self._once:
      print(f'Found {count} {self._name} parameters.')
      self._once = False
    
    metrics = {}
    metrics[f'{self._name}_loss'] = loss

    grads = torch.autograd.(loss, varibs)
    norm = tf.linalg.global_norm(grads)
    if not self._mixed:
      tf.debugging.check_numerics(norm, self._name + '_norm')
    if self._clip:
      grads, _ = tf.clip_by_global_norm(grads, self._clip, norm)
    if self._wd:
      self._apply_weight_decay(varibs)
    self._opt.apply_gradients(zip(grads, varibs))
    metrics[f'{self._name}_grad_norm'] = norm
    if self._mixed:
      metrics[f'{self._name}_loss_scale'] = self._opt.loss_scale
    return metrics

  def _apply_weight_decay(self, varibs):
    nontrivial = (self._wd_pattern != r'.*')
    if nontrivial:
      print('Applied weight decay to variables:')
    for var in varibs:
      if re.search(self._wd_pattern, self._name + '/' + var.name):
        if nontrivial:
          print('- ' + self._name + '/' + var.name)
        var.assign((1 - self._wd) * var)




_layer = dense(4,4)
loss = _layer(torch.zeros(4)).mean()

_opt = torch.optim.Adam(_layer.parameters(),lr=0.001)




















